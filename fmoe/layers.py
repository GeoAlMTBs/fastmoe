r"""
FMoE core layer
"""
import tree
import os
import torch
import torch.nn as nn

import sys

basedir = os.getenv('basedir')
sys.path.append(basedir + 'fastmoe/fmoe')

from functions import prepare_forward, ensure_comm
from functions import MOEScatter, MOEGather
from functions import AllGather, Slice
from gates import NaiveGate, NoisyGate

from fastermoe.config import switch_from_env


def mark_module_parallel_comm(module, comm):
    r"""
    Mark all parameters in `module` as doing data parallel in `comm`, where
    `comm` may be one of `'world', 'dp', 'none'`.
    """
    for p in module.parameters():
        setattr(p, "dp_comm", comm)


def _fmoe_general_global_forward(inp, gate, expert_fn, num_expert, world_size, **kwargs):
    r"""
    A private function that performs the following steps to complete the MoE
    computation.
    * Count the number of tokens from each worker to each expert.
    * Send the features to their target position so that input features to each
    expert are contiguous in memory.
    * Perform the forward computation of the experts using `expert_fn`
    * Gather the output features of experts back, and reorder them as sentences.
    Intermediate results like expert counts are hidden from users by this
    function.
    """
    (
        pos,
        local_expert_count,
        global_expert_count,
        fwd_expert_count,
        fwd_batch_size,
    ) = prepare_forward(gate, num_expert, world_size)
    topk = 1
    if len(gate.shape) == 2:
        topk = gate.shape[1]
    
    #debug
    # print('gate', gate.shape)
    # print('pos', pos)

    def scatter_func(tensor):
        return MOEScatter.apply(
            tensor,
            torch.div(pos, topk, rounding_mode='floor'),
            local_expert_count,
            global_expert_count,
            fwd_batch_size,
            world_size,
        )

    x = tree.map_structure(scatter_func, inp)
    #debug
    # print('x, inp', x.shape, inp.shape)

    x = expert_fn(x, fwd_expert_count)
    #debug
    # print('x', x.shape)

    original_shape = x.shape

    out_batch_size = tree.flatten(inp)[0].shape[0]
    #debug
    # print('tree.flatten(inp).shape', tree.flatten(inp))
    # print('out_batch_size', out_batch_size)

    if len(gate.shape) == 2:
        out_batch_size *= gate.shape[1]

    def gather_func(tensor):
        return MOEGather.apply(
            tensor,
            pos,
            local_expert_count,
            global_expert_count,
            out_batch_size,
            world_size,
        )

    outp = tree.map_structure(gather_func, x)
    return outp


fmoe_faster_schedule = False
if switch_from_env('FMOE_FASTER_SCHEDULE_ENABLE', False):
    fmoe_faster_schedule = True
    from .fastermoe.schedule import _fmoe_general_global_forward


class FMoE(nn.Module):
    r"""
    A general moe implementation that supports an arbitrary module as the
    expert.
    * `num_expert` stands for the number of experts on **each** worker.
    * `world_size` stands for the total number of workers that contains
    different experts.
    * `slice_group` can be a torch's communication group, indicating that
    specific model parallel is applied across the group, and workers in the
    group hold the same copy of input feature, and requires the same copy of
    the output. For each worker, FMoE only computes the output of a certain
    slice of the input batch, and will all-gather the outputs after
    computation.
    * `top_k` stands for the number of experts each token is going to.
    * `gate` is a gate class which can found in `fmoe.gates`.
    * `expert` can be specified as a module class, it is used to generate
    `num_expert` expert modules.
    """

    def __init__(
        self,
        num_expert=32,
        d_model=1024,
        world_size=1,
        mp_group=None,  # being deprecated
        slice_group=None,
        moe_group=None,
        top_k=2,
        gate=NaiveGate,
        expert=None,
        gate_hook=None,
        mask=None,
        mask_dict=None,
        #? What's mask used for?
    ):
        super().__init__()
        self.num_expert = num_expert
        self.d_model = d_model
        self.world_size = world_size

        self.selected_experts_log = None

        self.slice_group = slice_group
        if mp_group is not None:
            print("[Warning] mp_group is being deprecated")
            self.slice_group = mp_group
        if self.slice_group is None:
            self.slice_size = 1
            self.slice_rank = 0
        else:
            self.slice_size = self.slice_group.size()
            self.slice_rank = self.slice_group.rank()

        self.top_k = top_k
        if type(expert) is list:
            # debug
            # print('d_model: ', d_model)
            # debug
            # print('type(expert[0]): ', type(expert[0]))
            # !

            # if type(expert) is list:
            #     self.experts = nn.ModuleList([e(d_model) for e in expert])
            #     self.experts_fused = False
            #     self.num_expert = num_expert = len(expert)
            if (True):
                self.experts = nn.ModuleList([e for e in expert])
            else:
                self.experts = nn.ModuleList([e(d_model) for e in expert])
                #? Why initialize the model's dimension here?
                #? What's e(d_model)? To check if the dimensions match.
            self.experts_fused = False
            self.num_expert = num_expert = len(expert)
        elif expert is not None:
            # self.experts = nn.ModuleList([expert(d_model) for _ in range(num_expert)])
            self.experts = nn.ModuleList([expert for _ in range(num_expert)])
            self.experts_fused = False
        else:
            self.experts_fused = True
            #? What's expert_fused?

        self.gate = gate(d_model, num_expert, world_size, top_k)
        self.gate_hook = gate_hook
        #? What's gate_hook?
        self.mask = mask
        self.mask_dict = mask_dict
        self.moe_group = moe_group

    def expert_fn(self, inp, fwd_expert_count):
        r"""
        The default expert function which either calls the experts as a whole
        or as separate experts.
        """
        if self.experts_fused:
            return self.experts(inp, fwd_expert_count)
        if isinstance(fwd_expert_count, torch.Tensor):
            fwd_expert_count = fwd_expert_count.cpu().numpy()
            #?
        outputs = []
        base_idx = 0
        for i in range(self.num_expert):
            batch_size = fwd_expert_count[i]
            inp_slice = inp[base_idx : base_idx + batch_size]
            outputs.append(self.experts[i](inp_slice))
            base_idx += batch_size
        return torch.cat(outputs, dim=0)

    def mark_parallel_comm(self, expert_dp_comm="none"):
        r"""
        Automatically mark the data parallel comms of the parameters within the
        module. This can be typically called at the end of the __init__ function
        in child classes.
        """
        if self.experts is not None:
            comm = expert_dp_comm
            if isinstance(self.experts, list):
                for e in self.experts:
                    mark_module_parallel_comm(e, comm)
            else:
                mark_module_parallel_comm(self.experts, comm)
        mark_module_parallel_comm(self.gate, "gate")

    def enable_logging_experts(self):
        self.selected_experts_log = []
    
    def disable_logging_experts(self):
        self.selected_experts_log = None

    # def forward(self, moe_inp, selected_experts_log):
    def forward(self, moe_inp):
        r"""
        The FMoE module first computes gate output, and then conduct MoE forward
        according to the gate.  The score of the selected gate given by the
        expert is multiplied to the experts' output tensors as a weight.
        """

        moe_inp_batch_size = tree.flatten(
            tree.map_structure(lambda tensor: tensor.shape[0], moe_inp)
        )
        #debug
        # print("moe_inp_shape: ", moe_inp.shape)
        #?
        # debug
        # print('moe_inp_batch_size: ', moe_inp_batch_size)
        assert all(
            [batch_size == moe_inp_batch_size[0] for batch_size in moe_inp_batch_size]
        ), "MoE inputs must have the same batch size"
        #? Lazy generation?
        #? Why does the batch size need to be the same?

        if self.world_size > 1:

            def ensure_comm_func(tensor):
                ensure_comm(tensor, self.moe_group)

            tree.map_structure(ensure_comm_func, moe_inp)
        if self.slice_size > 1:

            def slice_func(tensor):
                return Slice.apply(
                    tensor, self.slice_rank, self.slice_size, self.slice_group
                )
            print('moe_inp.shape: ', moe_inp.shape)
            moe_inp = tree.map_structure(slice_func, moe_inp)

        # original_shape = moe_inp.shape
        # moe_inp = moe_inp.reshape(-1, self.d_model)

        #debug
        # print('d_model', self.d_model)
        # gate_top_k_idx, gate_score = self.gate(moe_inp.reshape(-1, self.d_model))
        gate_top_k_idx, gate_score = self.gate(moe_inp)
        #debug
        # print('gate_top_k_idx, gate_score', gate_top_k_idx.shape, gate_score.shape)

        if self.gate_hook is not None:
            self.gate_hook(gate_top_k_idx, gate_score, None)

        # delete masked tensors
        if self.mask is not None and self.mask_dict is not None:
            # TODO: to fix
            def delete_mask_func(tensor):
                # to: (BxL') x d_model
                tensor = tensor[mask == 0, :]
                return tensor

            mask = self.mask.view(-1)
            moe_inp = tree.map_structure(delete_mask_func, moe_inp)
            gate_top_k_idx = gate_top_k_idx[mask == 0, :]
            #? Wrong? The shape of gate_top_k_idx is not feasible to perform this.

        if self.selected_experts_log is not None:
            self.selected_experts_log.append(torch.stack([gate_top_k_idx, gate_score], dim=2))
        #! Shape of gate_top_k_idx and gate_score are (BS, top_k) and (BS, top_k)
        #! After stacking, the shape is (BS, top_k, 2)

        #debug
        # print('moe_inp.shape: ', moe_inp.shape)
        fwd = _fmoe_general_global_forward(
            moe_inp, gate_top_k_idx, self.expert_fn,
            self.num_expert, self.world_size,
            experts=self.experts
        )
        #debug
        # print('fwd.shape: ', fwd.shape)

        original_shape = list(fwd.shape[1:])

        # recover deleted tensors
        if self.mask is not None and self.mask_dict is not None:

            def recover_func(tensor):
                # to: (BxL') x top_k x dim
                dim = tensor.shape[-1]
                tensor = tensor.view(-1, self.top_k, dim)
                # to: (BxL) x top_k x d_model
                x = torch.zeros(
                    mask.shape[0],
                    self.top_k,
                    dim,
                    device=tensor.device,
                    dtype=tensor.dtype,
                )
                # recover
                x[mask == 0] = tensor
                for k, v in self.mask_dict.items():
                    x[mask == k] = v
                return x

            moe_outp = tree.map_structure(recover_func, fwd)
        else:

            def view_func(tensor):
                dim = torch.prod(torch.Tensor(list(tensor.shape[1:])), dtype=torch.int64)
                tensor = tensor.view(-1, self.top_k, dim)
                return tensor

        
            moe_outp = tree.map_structure(view_func, fwd)

        #? View.
        gate_score = gate_score.view(-1, 1, self.top_k)

        def bmm_func(tensor):
            dim = tensor.shape[-1]
            tensor = torch.bmm(gate_score, tensor).reshape(-1, dim)
            return tensor

        #debug
        # print(moe_outp.shape)
        moe_outp = tree.map_structure(bmm_func, moe_outp)
        moe_outp = moe_outp.reshape([moe_outp.shape[0]] + original_shape)

        if self.slice_size > 1:

            def all_gather_func(tensor):
                return AllGather.apply(
                    tensor, self.slice_rank, self.slice_size, self.slice_group
                )

            moe_outp = tree.map_structure(all_gather_func, moe_outp)

        moe_outp_batch_size = tree.flatten(
            tree.map_structure(lambda tensor: tensor.shape[0], moe_outp)
        )
        assert all(
            [batch_size == moe_outp_batch_size[0] for batch_size in moe_outp_batch_size]
        ), "MoE outputs must have the same batch size"
        # return (moe_outp, selected_experts_log)
        return moe_outp
        #! Shape (BS, 1, dim).
