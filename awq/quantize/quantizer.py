import torch
import inspect
import logging
import functools
import torch.nn as nn
from tqdm import tqdm
from typing import Dict, List, Optional
from collections import defaultdict
from awq.utils.calib_data import get_calib_dataset
from awq.quantize.scale import apply_scale, apply_clip
from awq.utils.utils import clear_memory, get_best_device
from awq.modules.linear import (
    WQLinear_GEMM,
    WQLinear_GEMV,
    WQLinear_Marlin,
    WQLinear_GEMVFast,
)
from awq.utils.module import (
    append_str_prefix,
    get_op_name,
    get_named_linears,
    set_op_by_name,
    exclude_layers_to_not_quantize,
)


class AwqQuantizer:
    def __init__(
        self,
        awq_model,
        model,
        tokenizer,
        w_bit,
        group_size,
        zero_point,
        version,
        calib_data,
        split,
        text_column,
        duo_scaling,
        modules_to_not_convert=None,
        export_compatible=False,
        apply_clip=True,
        n_parallel_calib_samples=None,
        max_calib_samples=128,
        max_calib_seq_len=512,
        max_chunk_memory=1024 * 1024 * 1024,
        init_only=False
    ) -> None:
        self.awq_model = awq_model
        self.model = model
        self.tokenizer = tokenizer
        self.w_bit = w_bit
        self.group_size = group_size
        self.zero_point = zero_point
        self.version = version
        self.calib_data = calib_data
        self.split = split
        self.text_column = text_column
        self.duo_scaling = duo_scaling
        self.export_compatible = export_compatible
        self.apply_clip = apply_clip
        self.n_parallel_calib_samples = n_parallel_calib_samples
        self.max_calib_samples = max_calib_samples
        self.max_calib_seq_len = max_calib_seq_len
        self.max_chunk_memory = max_chunk_memory
        self.modules_to_not_convert = (
            modules_to_not_convert if modules_to_not_convert is not None else []
        )
        if not init_only:
            self.modules, self.module_kwargs, self.inps = self.init_quant(
                n_samples=self.max_calib_samples, max_seq_len=self.max_calib_seq_len
            )
        else:
            self.modules, self.module_kwargs = [], {}

    def pseudo_quantize_tensor(self, w: torch.Tensor):
        org_w_shape = w.shape
        if self.group_size > 0:
            assert org_w_shape[-1] % self.group_size == 0
            w = w.reshape(-1, self.group_size)
        assert w.dim() == 2
        assert torch.isnan(w).sum() == 0

        # zero point quantization
        if self.zero_point:
            max_val = w.amax(dim=1, keepdim=True)
            min_val = w.amin(dim=1, keepdim=True)
            max_int = 2**self.w_bit - 1
            min_int = 0
            scales = (max_val - min_val).clamp(min=1e-5) / max_int
            zeros = (-torch.round(min_val / scales)).clamp_(min_int, max_int)
            w = (
                torch.clamp(torch.round(w / scales) + zeros, min_int, max_int) - zeros
            ) * scales
            zeros = zeros.view(org_w_shape[0], -1)
        else:
            max_val = w.abs().amax(dim=1, keepdim=True)
            max_val = max_val.clamp(min=1e-5)
            max_int = 2 ** (self.w_bit - 1) - 1
            min_int = -(2 ** (self.w_bit - 1))
            scales = max_val / max_int
            zeros = None
            w = torch.clamp(torch.round(w / scales), min_int, max_int) * scales

        assert torch.isnan(scales).sum() == 0
        assert torch.isnan(w).sum() == 0

        scales = scales.view(org_w_shape[0], -1)
        w = w.reshape(org_w_shape)

        return w, scales, zeros

    def pseudo_dequantize_tensor(
        self, w: nn.Linear, scales: torch.Tensor, zeros: Optional[torch.Tensor] = None
    ):
        # get repeated count
        repeat_count = w.weight.data.shape[-1] // scales.shape[-1]
        scales = scales.repeat(1, repeat_count).reshape(w.weight.data.shape)

        # dequantize
        if self.zero_point:
            zeros = zeros.repeat(1, repeat_count).reshape(w.weight.data.shape)
            w = (w.weight.data - zeros) * scales
        else:
            w = w.weight.data * scales

        return w

    def quantize(self):
        for i in tqdm(range(len(self.modules)), desc="AWQ"):
            # Move module and inputs to correct device
            common_device = next(self.modules[i].parameters()).device
            if common_device is None or str(common_device) == "cpu":
                if torch.cuda.is_available():
                    best_device = "cuda:" + str(i % torch.cuda.device_count())
                else:
                    best_device = get_best_device()

                self.modules[i] = self.modules[i].to(best_device)
                common_device = next(self.modules[i].parameters()).device

            if self.module_kwargs.get("position_ids") is not None:
                self.module_kwargs["position_ids"] = self.module_kwargs[
                    "position_ids"
                ].to(common_device)

            if self.module_kwargs.get("attention_mask") is not None:
                self.module_kwargs["attention_mask"] = self.module_kwargs[
                    "attention_mask"
                ].to(common_device)

            self.inps = self.inps.to(common_device)

            # [STEP 1]: Get layer, extract linear modules, extract input features
            named_linears = get_named_linears(self.modules[i])

            # Filter out the linear layers we don't want to exclude
            named_linears = exclude_layers_to_not_quantize(
                named_linears, self.modules_to_not_convert
            )

            input_feat = self._get_input_feat(self.modules[i], named_linears)
            clear_memory()

            # [STEP 2]: Compute and apply scale list
            module_config: List[Dict] = self.awq_model.get_layers_for_scaling(
                self.modules[i], input_feat, self.module_kwargs
            )
            scales_list = [
                self._search_best_scale(idx, self.modules[i], **layer)
                for idx, layer in enumerate(module_config)
            ]

            # for down_proj layer (idx=3):
            # p module_config[3]['prev_op']
            # Linear(in_features=4096, out_features=11008, bias=False)
            # p module_config[3]['inp'].shape
            # torch.Size([65, 512, 11008])
            # p scales_list[3]
            # mlp.up_proj, [mlp.down_proj], Tensor of torch.Size([11008])
            apply_scale(self.modules[i], scales_list, input_feat_dict=input_feat)
            scales_list = append_str_prefix(
                scales_list, get_op_name(self.model, self.modules[i]) + "."
            )

            # [STEP 3]: Compute and apply clipping list
            if self.apply_clip:
                clip_list = self._search_best_clip(
                    self.modules[i], named_linears, input_feat
                )
                apply_clip(self.modules[i], clip_list)
                clip_list = append_str_prefix(
                    clip_list, get_op_name(self.model, self.modules[i]) + "."
                )

            # [STEP 4]: Quantize weights
            if not self.export_compatible:
                self._apply_quant(self.modules[i], named_linears)

            clear_memory()

    def pack(self):
        for i in tqdm(range(len(self.modules)), desc="Packing"):
            named_linears = get_named_linears(self.modules[i])
            named_linears = exclude_layers_to_not_quantize(
                named_linears, self.modules_to_not_convert
            )
            self._apply_quant(self.modules[i], named_linears)
            clear_memory()

    def _apply_quant(self, module, named_linears: Dict[str, nn.Linear]):
        for name, linear_layer in named_linears.items():
            # NOTE: small regression in perplexity if linear layer uses .cpu().float()
            linear_layer = linear_layer.to(get_best_device()).half()

            linear_layer.weight.data, scales, zeros = self.pseudo_quantize_tensor(
                linear_layer.weight.data
            )

            if self.version == "gemm":
                scales = scales.t().contiguous()
                if zeros is not None:
                    zeros = zeros.t().contiguous()
                q_linear_module = WQLinear_GEMM

            elif self.version == "gemv":
                q_linear_module = WQLinear_GEMV

            elif self.version == "marlin":
                q_linear_module = WQLinear_Marlin

            elif self.version == "gemv_fast":
                q_linear_module = WQLinear_GEMVFast

            else:
                raise ValueError(f"Unknown version {self.version}")

            q_linear = q_linear_module.from_linear(
                linear=linear_layer,
                w_bit=self.w_bit,
                group_size=self.group_size,
                init_only=False,
                scales=scales,
                zeros=zeros,
            )

            linear_layer.cpu()
            q_linear.to(next(module.parameters()).device)
            set_op_by_name(module, name, q_linear)
            clear_memory()

    @torch.no_grad()
    def _module_forward(
        self, x: torch.Tensor, module: torch.nn.Module, module_kwargs: Dict
    ) -> torch.Tensor:
        #print('_module_forward', module.idx if hasattr(module, 'idx') else -1, module)
        if self.n_parallel_calib_samples is None:
            # runs through all samples at once
            module_output = module(x, **module_kwargs)
            if isinstance(module_output, tuple):
                module_output = module_output[0]
        else:
            # memory efficiently runs through all calibration samples
            # but only n_parallel_calib_samples at a time
            module_output = []
            partitioned_inputs = torch.split(x, self.n_parallel_calib_samples)
            for x_partial in partitioned_inputs:
                partial_output = module(x_partial, **module_kwargs)

                if isinstance(partial_output, tuple):
                    partial_output = partial_output[0]

                module_output.append(partial_output.cpu())

            module_output = torch.cat(module_output, dim=0)

        return module_output

    @torch.no_grad()
    def _search_best_scale(
        self,
        idx,
        module,
        prev_op,
        layers: List[nn.Linear],
        inp: torch.Tensor,
        module2inspect=None,
        kwargs={},
    ):
        if module2inspect is None:
            assert len(layers) == 1
            module2inspect = layers[0]

        if "use_cache" in kwargs:
            kwargs.pop("use_cache")

        print('search best scales...', module2inspect)

        # Put x on the right device
        inp = inp.to(next(module2inspect.parameters()).device)

        # [STEP 1]: Compute per-channel mean of normalised weights
        # All layer weights are concatted together
        weight = torch.cat([_m.weight for _m in layers], dim=0)
        org_shape = weight.shape # e.g., down_proj (out_features=4096, in_features=11008)
        # The weights are reshaped to be organised by quantization group
        weight = weight.view(-1, self.group_size)
        # Calculates the relative magnitude of the weights within each of the quantization groups,
        # and rescales each group individually so that each group has weights on a 0-1 scale.
        w_scale = weight.abs() / (weight.abs().amax(dim=1, keepdim=True) + 1e-6)
        # Resizes the rescaled weight matrix back up to its original dimensions
        w_scale = w_scale.view(org_shape)
        # Gets the average rescaled magnitude for each output channel
        w_mean = w_scale.mean(0) # torch.Size([-1, 11008]) --> torch.Size([11008])
        clear_memory(weight)

        # [STEP 2]: Compute per-channel mean of the input activation with chunking
        # move inp to cpu to avoid memory leak
        inp_flat = inp.cpu().abs().view(-1, inp.shape[-1]) # abs [-1, 11008]
        num_elements = inp_flat.size(0)
        num_channels = inp_flat.size(1)
        element_size_bytes = inp_flat.element_size() * 2 # multiplied by 2 for FP32

        # Calculate chunk size dynamically based on max_chunk_memory
        chunk_size = int(self.max_chunk_memory // (element_size_bytes * num_channels))
        chunk_size = min(chunk_size, num_elements)

        # Use float32 for sum calculation
        x_sum = torch.zeros(num_channels, dtype=torch.float32, device=inp.device)

        for i in range(0, num_elements, chunk_size):
            end = min(i + chunk_size, num_elements)
            chunk_sum = inp_flat[i:end].to(torch.float32).sum(dim=0) # torch.Size([11008])
            x_sum += chunk_sum.to(inp.device)

        x_mean = (x_sum / num_elements).to(inp.dtype) # torch.Size([11008])
        # x_mean is the same as inp_flat.mean(dim=0)
        clear_memory(x_sum)

        # [STEP 3]: Compute output of module
        with torch.no_grad():
            module_kwargs = self._sanitize_kwargs(kwargs, module2inspect)
            fp16_output = self._module_forward(inp, module2inspect, module_kwargs)

        # [STEP 4]: Compute loss
        best_scales = self._compute_best_scale(
            idx, inp, w_mean, x_mean, module2inspect, layers, fp16_output, module_kwargs
            # inp: torch.Size([65, 512, 11008])
            # w_mean: torch.Size([11008])
            # x_mean: torch.Size([11008])
            # fp16_output: torch.Size([65, 512, 4096])
        )

        return (
            None if prev_op is None else get_op_name(module, prev_op), # up_proj
            tuple([get_op_name(module, m) for m in layers]), # [down_proj]
            best_scales,
        )

    def _compute_best_scale(
        self, idx,
        x: torch.Tensor,
        w_mean: torch.Tensor,
        x_mean: torch.Tensor,
        module2inspect: torch.nn.Module,
        linears2scale: List[nn.Linear],
        fp16_output: torch.Tensor,
        kwargs: Dict={},
    ):
        """
        Compute loss and select best scales

        L(s) = || Q(W * s) (s^-1 * X) - W * X ||
        Q: weight quantization function | pseudo_quantize_tensor(W * s)
        X: inputs from calib dataset    | X
        W: original weights in FP16     | layer
        s: per channel scaling factor   | s^-1 * X
        """
        n_grid = 20
        history = []
        best_ratio = -1
        best_scales = None
        best_error = float("inf")

        org_sd = {k: v.cpu() for k, v in module2inspect.state_dict().items()}

        device = x.device
        x_mean = x_mean.view(-1).to(device)
        w_mean = w_mean.view(-1).to(device)

        for ratio in range(n_grid):
            #print(ratio, n_grid)

            # create new scales
            ratio = ratio / n_grid

            # NOTE: s^-1 * x is fused here, according to paper
            if self.duo_scaling:
                scales = (x_mean.pow(ratio) / (w_mean.pow(1 - ratio) + 1e-4)).clamp(min=1e-4)
            else:
                scales = x_mean.pow(ratio).clamp(min=1e-4).view(-1)
            scales = scales / (scales.max() * scales.min()).sqrt()
            scales_view = scales.view(1, -1).to(device)

            # avoid scaling values that overflow
            scales[torch.isinf(scales)] = 1
            scales[torch.isnan(scales)] = 1

            # Q(W * s)
            for fc in linears2scale:
                fc.weight.mul_(scales_view)
                fc.weight.data = (
                    self.pseudo_quantize_tensor(fc.weight.data)[0] / scales_view
                )

            # W * X
            int_w_output = self._module_forward(x, module2inspect, kwargs)

            # compute mean squared error (L2 norm)
            loss = self._compute_loss(fp16_output, int_w_output, device)

            history.append(loss)
            if loss < best_error:
                best_error = loss
                best_ratio = ratio
                best_scales = scales.clone()

            module2inspect.load_state_dict(org_sd) # restore after modify fc.weight

        print(f'best ratio={best_ratio}, loss={best_error}')
        if best_ratio == -1:
            logging.debug(history)
            raise Exception

        assert torch.isnan(best_scales).sum() == 0, best_scales
        return best_scales.detach().cpu()

    @torch.no_grad()
    def _compute_loss(
        self,
        fp16_output: torch.Tensor,
        int_w_output: torch.Tensor,
        device: torch.device,
    ):
        loss = 0.0
        fp16_output_flat = fp16_output.view(-1)
        int_w_output_flat = int_w_output.view(-1)
        num_elements = fp16_output_flat.size(0)
        element_size_bytes = fp16_output.element_size()

        # Calculate chunk size dynamically based on max_chunk_memory
        # Divide the max_chunk_memory by twice the element size
        chunk_size = self.max_chunk_memory // (element_size_bytes * 2)
        chunk_size = min(chunk_size, num_elements)

        # Split the computation into chunks
        fp16_chunks = torch.split(fp16_output_flat, chunk_size)
        int_w_chunks = torch.split(int_w_output_flat, chunk_size)

        # Compute the loss for each chunk
        for fp16_chunk, int_w_chunk in zip(fp16_chunks, int_w_chunks):
            chunk_loss = (fp16_chunk.to(device) - int_w_chunk.to(device)).float().pow(2).sum().item()
            loss += chunk_loss

        # Normalize the loss by the total number of elements
        loss /= num_elements

        return loss

    @torch.no_grad()
    def _search_best_clip(self, layer, named_linears, input_feat):
        clip_list = []
        avoid_clipping = ["q_", "k_", "query", "key", "Wqkv"]
        # left over:
        #
        # self_attn.v_proj
        # self_attn.o_proj
        # mlp.gate_proj
        # mlp.up_proj
        # mlp.down_proj
        # E

        for name in named_linears:
            # due to qk bmm, it is hard to clip precisely
            if any([_ in name for _ in avoid_clipping]):
                continue
            best_dev = get_best_device()
            print('search clip for', name,
                named_linears[name].weight.shape, f'(dev={best_dev})')
            named_linears[name].to(best_dev)
            max_val = self._compute_best_clip(
                named_linears[name].weight, input_feat[name]
            ).cpu()
            # for self_attn.v_proj of torch.Size([4096, 4096]), max_val.shape
            # is torch.Size([4096, 32, 1]) where 32 = 4096 / 128.
            clip_list.append((name, max_val))
            named_linears[name].cpu()
            clear_memory()

        return clip_list

    @torch.no_grad()
    def _compute_best_clip(
        self,
        w: torch.Tensor,
        input_feat: torch.Tensor,
        n_grid=20,
        max_shrink=0.5,
        n_sample_token=512,
    ):
        assert w.dim() == 2
        org_w_shape = w.shape
        # w           [co, ci]      -> [co, 1, n_group, group size]
        # input_feat  [n_token, ci] -> [1, n_token, n_group, group size]
        group_size = self.group_size if self.group_size > 0 else org_w_shape[1]
        input_feat = input_feat.view(-1, input_feat.shape[-1])
        input_feat = input_feat.reshape(1, input_feat.shape[0], -1, group_size)

        # Compute input feature step size (minimum 1)
        step_size = max(1, input_feat.shape[1] // n_sample_token)
        input_feat = input_feat[:, ::step_size]
        
        w = w.reshape(org_w_shape[0], 1, -1, group_size)

        #oc_batch_size = 256
        oc_batch_size = 64 # prevent OOM
        assert org_w_shape[0] % oc_batch_size == 0
        w_all = w
        best_max_val_all = []

        for i_b in range(org_w_shape[0] // oc_batch_size):
            w = w_all[i_b * oc_batch_size : (i_b + 1) * oc_batch_size]

            org_max_val = w.abs().amax(dim=-1, keepdim=True)  # co, 1, n_group, 1

            best_max_val = org_max_val.clone()
            min_errs = torch.ones_like(org_max_val) * 1e9
            input_feat = input_feat.to(w.device)
            org_out = (input_feat * w).sum(dim=-1)  # co, n_token, n_group

            for i_s in range(int(max_shrink * n_grid)):
                max_val = org_max_val * (1 - i_s / n_grid)
                min_val = -max_val
                cur_w = torch.clamp(w, min_val, max_val)
                q_w = self.pseudo_quantize_tensor(cur_w)[0]
                cur_out = (input_feat * q_w).sum(dim=-1)

                # co, 1, n_group, 1
                err = (cur_out - org_out).pow(2).mean(dim=1).view(min_errs.shape)
                del cur_w
                del cur_out
                cur_best_idx = err < min_errs
                min_errs[cur_best_idx] = err[cur_best_idx]
                best_max_val[cur_best_idx] = max_val[cur_best_idx]
            best_max_val_all.append(best_max_val)

        best_max_val = torch.cat(best_max_val_all, dim=0)

        clear_memory(input_feat)
        clear_memory(org_out)

        return best_max_val.squeeze(1)

    def init_quant(self, n_samples=128, max_seq_len=512):
        modules = self.awq_model.get_model_layers(self.model)
        samples = get_calib_dataset(
            data=self.calib_data,
            tokenizer=self.tokenizer,
            n_samples=n_samples,
            max_seq_len=max_seq_len,
            split=self.split,
            text_column=self.text_column,
        )
        samples = torch.cat(samples, dim=0)

        inps = []
        layer_kwargs = {}

        best_device = get_best_device()
        modules[0] = modules[0].to(best_device)
        self.awq_model.move_embed(self.model, best_device)

        def forward_catch(*args, **kwargs):
            # assume first input to forward is hidden states
            if len(args) > 0:
                hidden_states = args[0]
                del args
            else:
                first_key = list(kwargs.keys())[0]
                hidden_states = kwargs.pop(first_key)

            inps.append(hidden_states)
            layer_kwargs.update(kwargs)
            raise ValueError  # early exit to break later inference

        # patch layer 0 to catch input and kwargs
        save = modules[0].forward
        modules[0].forward = forward_catch
        try:
            self.model(samples.to(next(self.model.parameters()).device))
        except ValueError:  # work with early exit
            pass
        modules[0].forward = save  # restore

        if hasattr(self.model, 'prepare_inputs_for_generation'):
            # Update the layer kwargs with `prepare_inputs_for_generation` method
            # that takes care of everything to avoid unexpected errors.
            layer_kwargs = self.model.prepare_inputs_for_generation(samples, **layer_kwargs)
            # Pop the input_ids as they are not needed at all.
            layer_kwargs.pop("input_ids")

        #print(samples.shape) # torch.Size([65, 512])
        del samples
        inps = inps[0]
        #print(inps.shape) # torch.Size([65, 512, 4096])

        modules[0] = modules[0].cpu()
        self.awq_model.move_embed(self.model, "cpu")

        clear_memory()

        if layer_kwargs.get("attention_mask") is not None:
            layer_kwargs["attention_mask"] = layer_kwargs["attention_mask"].to(
                best_device
            )

        return modules, layer_kwargs, inps

    def _get_input_feat(self, layer, named_linears):
        # firstly, get input features of all linear layers
        def cache_input_hook(m, x, y, name, feat_dict):
            x = x[0]
            x = x.detach().cpu()
            feat_dict[name].append(x)

        input_feat = defaultdict(list)
        handles = []

        # FIXME: Workaround for Mixtral to use block_sparse_moe input features
        if self.awq_model.model_type == "mixtral":
            named_linears = {
                **named_linears,
                "block_sparse_moe": layer.block_sparse_moe,
            }

        if self.awq_model.model_type == "deepseek_v2":
            named_linears = {
                **named_linears,
                "mlp": layer.mlp,
            }

        for name in named_linears:
            handles.append(
                named_linears[name].register_forward_hook(
                    functools.partial(cache_input_hook, name=name, feat_dict=input_feat)
                )
            )
        self.inps = self.inps.to(next(layer.parameters()).device)  # in case multi-gpu
        # get output as next layer's input

        # Sanitize the kwargs in case we use transformers version that contains
        # kwargs that are not handled by the module.
        # Useful for trust_remote_code models.
        module_kwargs = self._sanitize_kwargs(self.module_kwargs, layer)

        self.inps = self._module_forward(self.inps, layer, module_kwargs)
        #print('updated self.inps')
        for h in handles:
            h.remove()
        # now solve for scaling and clipping
        input_feat = {k: torch.cat(v, dim=0) for k, v in input_feat.items()}

        return input_feat

    def _sanitize_kwargs(self, inputs_kwargs, module):
        """
        Remove the arguments that are not supported in the module's
        forward pass to avoid breaking behaviour between different versions
        of transformers.

        Args:
            inputs_kwargs (`dict`):
                The input dictionary to pass to the model layer
            module (`torch.nn.Module`):
                Target module to quantize.
        """
        module_signature = inspect.signature(module.forward).parameters
        sanitized_kwargs = {}
        for k, v in inputs_kwargs.items():
            if k in module_signature:
                sanitized_kwargs[k] = v
        return sanitized_kwargs
