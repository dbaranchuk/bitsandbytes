import operator
import torch
import bitsandbytes.functional as F

from dataclasses import dataclass
from functools import reduce  # Required in Python 3

# math.prod not compatible with python < 3.8
def prod(iterable):
    return reduce(operator.mul, iterable, 1)

tensor = torch.Tensor

"""
    This class pools outlier dimensions across layers.
    This is particularly important for small models where outlier features 
    are less systematic and occur with low frequency.
"""
class GlobalOutlierPooler(object):
    _instance = None

    def __init__(self):
        raise RuntimeError("Call get_instance() instead")

    def initialize(self):
        self.outliers = set()
        self.model_dim = None

    @classmethod
    def get_instance(cls):
        if cls._instance is None:
            cls._instance = cls.__new__(cls)
            cls._instance.initialize()
        return cls._instance

    def add_outliers(self, outlier_idx, feature_dim):
        if self.model_dim is None:
            self.model_dim = feature_dim
        if feature_dim != self.model_dim:
            return  # we do not encode outliers for the 2nd FFN layer

        self.outliers.update(outlier_idx.tolist())

    def get_current_outlier_idx(self):
        return torch.Tensor(list(self.outliers)).to(torch.int64)


class MatMul8bit(torch.autograd.Function):
    @staticmethod
    def forward(ctx, A, B, out=None, quant_type="vector", precision=[8, 8, 8]):

        if precision[0] != 8:
            with torch.no_grad():
                output = torch.matmul(A, B)
        else:
            if len(B.shape) == 2:
                dim = 0
            else:
                dim = 1
            qA, SA = F.vectorwise_quant(A, dim=-1, quant_type=quant_type)
            qB, SB = F.vectorwise_quant(B, dim=dim, quant_type=quant_type)
            iout = F.igemm(qA, qB)
            output = F.vectorwise_mm_dequant(iout, SA, SB, A.dtype, quant_type)

        if A.requires_grad or B.requires_grad:
            ctx.save_for_backward(A, B)

        ctx.quant_type = quant_type
        ctx.precision = precision

        return output

    @staticmethod
    def backward(ctx, grad_output):
        A, B = ctx.saved_tensors
        quant_type = ctx.quant_type
        precision = ctx.precision
        grad_A = grad_B = None

        if B.requires_grad:
            if len(A.shape) == 3:
                dims = [0, 1]
                # bsi -> ibs
                permute_dim = [0, 2, 1]
            else:
                dims = [0]
                # bs -> sb
                permute_dim = [1, 0]

            if precision[1] != 8:
                with torch.no_grad():
                    grad_B = torch.matmul(A.permute(permute_dim), grad_output)
            else:
                if len(B.shape) == 2 and len(A.shape) == 3:
                    grad_output = grad_output.contiguous()
                    if not grad_output.is_contiguous():
                        grad_output.contiguous()
                    qgrad_output, S1 = F.vectorwise_quant(
                        grad_output.view(-1, grad_output.shape[2]),
                        dim=0,
                        quant_type=quant_type,
                    )
                    if not A.is_contiguous():
                        A = A.contiguous()
                    qA, S2 = F.vectorwise_quant(
                        A.view(-1, A.shape[2]), dim=0, quant_type=quant_type
                    )
                    igrad_B = F.igemm(qA.t(), qgrad_output)
                    grad_B = F.vectorwise_mm_dequant(
                        igrad_B, S2.t(), S1, grad_output.dtype, quant_type
                    )
                else:
                    qgrad_output, S1 = F.vectorwise_quant(
                        grad_output, dim=dims, quant_type=quant_type
                    )
                    qA, S2 = F.vectorwise_quant(
                        A, dim=dims, quant_type=quant_type
                    )
                    igrad_B = F.igemm(qA.permute(permute_dim), qgrad_output)
                    grad_B = F.vectorwise_mm_dequant(
                        igrad_B,
                        S2.permute(permute_dim),
                        S1,
                        grad_output.dtype,
                        quant_type,
                    )

        if A.requires_grad:
            if len(grad_output.shape) == 3:
                dims = [2]
            else:
                dims = [1]

            if len(B.shape) == 3:
                # bio -> boi
                permute_dim = [0, 2, 1]
                dim_B = dims
            else:
                # io -> oi
                permute_dim = [1, 0]
                dim_B = [1]

            if precision[2] != 8:
                with torch.no_grad():
                    grad_A = torch.matmul(grad_output, B.permute(permute_dim))
            else:
                qgrad_output, S1 = F.vectorwise_quant(
                    grad_output, dim=dims, quant_type=quant_type
                )
                qB, S3 = F.vectorwise_quant(B, dim=dim_B, quant_type=quant_type)
                igrad_A = F.igemm(qgrad_output, qB.permute(permute_dim))
                grad_A = F.vectorwise_mm_dequant(
                    igrad_A,
                    S1,
                    S3.permute(permute_dim),
                    grad_output.dtype,
                    quant_type,
                )

        return grad_A, grad_B, None, None, None


mm_cublas = MatMul8bit.apply
bmm_cublas = MatMul8bit.apply
matmul_cublas = MatMul8bit.apply


@dataclass
class MatmulLtState:
    CB = None
    CxB = None
    SB = None
    SCB = None

    CxBt = None
    SBt = None
    CBt = None

    subB = None

    outlier_pool = None
    has_accumulated_gradients = False
    threshold = 0.0
    idx = None
    is_training = True
    has_fp16_weights = True
    use_pool = False
    formatB = F.get_special_format_str()

    def reset_grads(self):
        self.CB = None
        self.CxB = None
        self.SB = None
        self.SCB = None

        self.CxBt = None
        self.SBt = None
        self.CBt = None


class MatMul8bitLt(torch.autograd.Function):
    @staticmethod
    def forward(ctx, A, B, out=None, bias=None, state=MatmulLtState()):
        # default to pytorch behavior if inputs are empty
        ctx.is_empty = False
        if prod(A.shape) == 0:
            ctx.is_empty = True
            ctx.A = A
            ctx.B = B
            ctx.bias = bias
            if A.shape[-1] == B.shape[0]:
                return torch.empty(A.shape[:-1]+B.shape[1:], dtype=torch.float16, device=A.device)
            else:
                return torch.empty(A.shape[:-1]+B.shape[:1], dtype=torch.float16, device=A.device)

        # 1. Quantize A
        # 2. Quantize B
        # 3. Matmul
        # 4. Mixed-precision decomposition matmul
        # 5. Save state
        requires_gradA = A.requires_grad
        requires_gradB = B.requires_grad
        requires_gradBias = bias is not None and bias.requires_grad
        formatB = state.formatB
        input_shape = A.shape
        if state.outlier_pool is None:
            state.outlier_pool = GlobalOutlierPooler.get_instance()
        assert (
            A.dtype == torch.float16
        ), f"The input data type needs to be fp16 but {A.dtype} was found!"

        # 1. Quantize A
        if len(A.shape) == 3:
            A = A.view(-1, A.shape[-1]).contiguous()
        CA, CAt, SCA, SCAt, coo_tensorA = F.double_quant(
            A, threshold=state.threshold
        )

        if state.threshold > 0.0 and coo_tensorA is not None:
            if state.has_fp16_weights:
                idx = torch.unique(coo_tensorA.colidx).long()
                CA[:, idx] = 0
                CAt[:, idx] = 0
                subA = A[:, idx]
                state.subB = B[:, idx].t().contiguous()
                state.idx = idx
            else:
                if state.CxB is None:
                    # B in in 8-bit row-major, we can transform it back to 16-bit to extract outlier dimensions
                    # we also need to convert it to the turing/ampere format
                    state.CxB, state.SB = F.transform(state.CB, to_order=formatB)
        else:
            if not state.has_fp16_weights and state.CxB is None:
                state.CxB, state.SB = F.transform(state.CB, to_order=formatB)
            subA = None

        # 2. Quantize B
        if state.has_fp16_weights:
            has_grad = True if (getattr(B, "grad", None) is not None) else False
            is_transposed = not B.is_contiguous() and B.shape[0] == B.stride(1)
            if is_transposed:
                B = B.contiguous()

            if (state.is_training and not has_grad) or state.CxB is None:
                state.reset_grads()
                (
                    CB,
                    state.CBt,
                    state.SCB,
                    state.SCBt,
                    coo_tensorB,
                ) = F.double_quant(B)
                state.CxB, state.SB = F.transform(CB, to_order=formatB)
        else:
            has_grad = False

        if coo_tensorA is not None and not state.has_fp16_weights:
            # extract outliers

            outlier_idx = torch.unique(coo_tensorA.colidx)
            state.idx = outlier_idx
            outliers = F.extract_outliers(state.CxB, state.SB, state.idx.int())
            state.subB = (
                (outliers * state.SCB.view(-1, 1) / 127.0)
                .t()
                .contiguous()
                .half()
            )
            CA[:, state.idx.long()] = 0
            CAt[:, state.idx.long()] = 0
            subA = A[:, state.idx.long()]

        shapeB = state.SB[0]

        if len(input_shape) == 3:
            output_shape = (input_shape[0], input_shape[1], shapeB[0])
        else:
            output_shape = (input_shape[0], shapeB[0])

        # 3. Matmul
        C32A, SA = F.transform(CA, "col32")
        out32, Sout32 = F.igemmlt(C32A, state.CxB, SA, state.SB)
        # we apply the fused bias here
        output = F.mm_dequant(out32, Sout32, SCA, state.SCB, bias=bias)

        # 4. Mixed-precision decomposition matmul
        if coo_tensorA is not None and subA is not None:
            output += torch.matmul(subA, state.subB)

        # 5. Save state
        ctx.state = state

        ctx.formatB = formatB
        ctx.grad_shape = input_shape
        ctx.req_grads = [requires_gradA, requires_gradB, requires_gradBias]

        if requires_gradA or requires_gradB:
            ctx.tensors = (CAt, subA)
            ctx.tensor_states = (SCAt, state.idx)
        else:
            ctx.tensors = [None, None]
            ctx.tensor_states = (None, None)
            ctx.save_for_backward(None, None)

        clone_func = torch.clone if len(output_shape) == 3 else lambda x : x
        #clone_func = torch.clone
        return clone_func(output.view(output_shape))

    @staticmethod
    def backward(ctx, grad_output):
        if ctx.is_empty:
            bias_grad = (None if ctx.bias is None else torch.zeros_like(ctx.bias))
            return torch.zeros_like(ctx.A), torch.zeros_like(ctx.B), None, bias_grad, None
        req_gradA, req_gradB, req_gradBias = ctx.req_grads
        CAt, subA = ctx.tensors
        SCAt, idx = ctx.tensor_states
        formatB = ctx.formatB
        state = ctx.state

        if len(grad_output.shape) == 3:
            grad_output = grad_output.reshape(
                -1, grad_output.shape[-1]
            ).contiguous()

        grad_A = grad_B = grad_bias = None

        Cgrad, Cgradt, SCgrad, SCgradt, coo_tensor = F.double_quant(grad_output)
        if req_gradB:
            CxAt, SAt = F.transform(CAt, formatB, transpose=True)
            C32grad, Sgrad = F.transform(Cgradt, "col32", transpose=True)
            gradB32, SgradB32 = F.igemmlt(C32grad, CxAt, Sgrad, SAt)
            grad_B = F.mm_dequant(gradB32, SgradB32, SCgradt, SCAt)
            if state.threshold > 0.0 and subA is not None:
                grad_B[:, idx] += torch.matmul(grad_output.t(), subA)

        if req_gradA:
            C32grad, Sgrad = F.transform(Cgrad, "col32")
            if state.CxBt is None:
                if state.has_fp16_weights:
                    CBt = state.CBt
                else:
                    # Restore CBt from CB
                    assert state.CBt is None, "CBt should not be stored in state"
                    CB = state.CB.half()
                    SCB = state.SCB.unsqueeze(1).half()
                    SCBt = state.SCBt.unsqueeze(1).half()
                    Bt = (CB * SCB).t().contiguous()
                    CBt = (Bt / SCBt).t().to(torch.int8)

                # intentionally, do not store CxBt in state  
                CxBt, SBt = F.transform(
                    CBt, to_order=formatB, transpose=True
                )
            else:
                CxBt = state.CxBt
            gradA32, SgradA32 = F.igemmlt(C32grad, CxBt, Sgrad, SBt)
            grad_A = F.mm_dequant(gradA32, SgradA32, SCgrad, state.SCBt).view(ctx.grad_shape)

        if req_gradBias:
            grad_bias = grad_output.sum(0)

        return grad_A, grad_B, None, grad_bias, None


def matmul(
    A: tensor,
    B: tensor,
    out: tensor = None,
    state: MatmulLtState = None,
    threshold=0.0,
    bias=None
):
    state = state or MatmulLtState()
    if threshold > 0.0:
        state.threshold = threshold
    return MatMul8bitLt.apply(A, B, out, bias, state)
