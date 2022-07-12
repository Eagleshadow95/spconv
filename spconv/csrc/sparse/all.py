# Copyright 2021 Yan Yan
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from cumm.common import TensorView, TensorViewCPU, TensorViewKernel, ThrustLib, GemmBasicHost
from cumm.conv.bases import ConvOpType, NHWC
from cumm.conv.params import ConvProblem
from cumm import dtypes
from cumm.constants import CUMM_CPU_ONLY_BUILD
import pccm
from ccimport import compat
from .pointops import Point2Voxel, Point2VoxelCPU
from .indices import SparseConvIndicesKernel, CudaCommonKernel, SparseConvIndicesCPU
from .maxpool import IndiceMaxPool, IndiceMaxPoolCPU
from .gather import GatherCPU
from .alloc import ExternalAllocator, ThrustAllocator
from spconv.constants import SpconvAllocatorKeys

class CustomThrustLib(pccm.Class):
    def __init__(self):
        super().__init__()
        self.add_dependency(ThrustLib)
        # https://github.com/NVIDIA/thrust/issues/1401#issuecomment-806403746
        if compat.InLinux:
            self.build_meta.add_public_cflags("nvcc", "-Xcompiler", "-fno-gnu-unique")


class ThrustCustomAllocatorV2(pccm.Class, pccm.pybind.PybindClassMixin):
    def __init__(self):
        super().__init__()
        self.add_dependency(TensorView)
        self.add_include("functional", "memory")
        self.add_pybind_member("alloc_func",
                               "std::function<std::uintptr_t(std::size_t)>",
                               pyanno="Callable[[int], int]")
        self.add_typedef("value_type", "char")

    @pccm.member_function
    def allocate(self):
        code = pccm.FunctionCode()
        code.arg("num_bytes", "std::ptrdiff_t")
        code.ret("char*")
        code.raw(f"""
        if (alloc_func){{
            char* result = reinterpret_cast<char*>(alloc_func(num_bytes));
            return result;
        }}
        else{{
            TV_THROW_RT_ERR("set alloc function first.");
        }}
        """)
        return code

    @pccm.member_function
    def deallocate(self):
        code = pccm.FunctionCode()
        code.arg("ptr", "char *")
        code.arg("num_bytes", "size_t")
        return code        


class SpconvOps(pccm.Class):
    def __init__(self):
        super().__init__()
        self.add_dependency(ThrustCustomAllocatorV2, ExternalAllocator, GemmBasicHost, ThrustAllocator)
        self.ndims = [1, 2, 3, 4]
        for ndim in self.ndims:
            p2v = Point2Voxel(dtypes.float32, ndim)
            p2v_cpu = Point2VoxelCPU(dtypes.float32, ndim)
            self.add_param_class(f"ops_cpu{ndim}d", p2v_cpu,
                                 f"Point2Voxel{ndim}DCPU")

            problem = ConvProblem(ndim, ConvOpType.kForward, NHWC, NHWC, NHWC)
            indices = SparseConvIndicesKernel(problem, dtypes.int32)
            indices_cpu = SparseConvIndicesCPU(problem, dtypes.int32)
            self.add_param_class(f"ops_cpu{ndim}d", indices_cpu,
                                 f"SpconvIndicesCPU{ndim}D")
            # self.add_param_class("ops", indices, "SpconvIndices")
            if not CUMM_CPU_ONLY_BUILD:
                self.add_param_class(f"ops{ndim}d", p2v, f"Point2Voxel{ndim}D")
                cuda_funcs = [
                    self.generate_subm_conv_inds,
                    self.generate_conv_inds_stage1,
                    self.generate_conv_inds_stage1_5,
                    self.generate_conv_inds_stage2, self.sort_1d_by_key,
                    self.generate_conv_inds_mask_stage1,
                    self.generate_conv_inds_mask_stage2
                ]
                self.add_impl_only_param_class(cuda_funcs, f"ops{ndim}d",
                                               indices,
                                               f"SpconvIndices{ndim}D")

    @pccm.pybind.mark
    @pccm.cuda.static_function
    def generate_conv_inds_stage1(self):
        code = pccm.FunctionCode()
        code.arg("indices", "tv::Tensor")
        code.arg("indice_pairs, indice_pairs_uniq, indice_num_per_loc",
                 "tv::Tensor")
        code.arg("batch_size", "int")
        code.arg("output_dims, input_dims", f"std::vector<int>")
        code.arg("ksize, stride, padding, dilation", f"std::vector<int>")
        code.arg("transposed", f"bool", "false")

        code.arg("stream_int", f"std::uintptr_t", "0", pyanno="int")
        if CUMM_CPU_ONLY_BUILD:
            return code.make_invalid()
        code.raw(f"""
        int ndim = indices.dim(1) - 1;
        TV_ASSERT_RT_ERR(output_dims.size() == ndim && input_dims.size() == ndim &&
            ksize.size() == ndim && stride.size() == ndim && dilation.size() == ndim &&
            padding.size() == ndim, "your params size not equal to ndim", ndim);
        """)

        for ndim in self.ndims:
            code.raw(f"""
            if (ndim == {ndim}){{
                tv::array<int, {ndim}> output_dims_, input_dims_;
                tv::array<int, {ndim}> ksize_, stride_, padding_, dilation_;
                for (int i = 0; i < {ndim}; ++i){{
                    output_dims_[i] = output_dims[i];
                    input_dims_[i] = input_dims[i];
                    ksize_[i] = ksize[i];
                    stride_[i] = stride[i];
                    padding_[i] = padding[i];
                    dilation_[i] = dilation[i];
                }}
                return SpconvIndices{ndim}D::generate_conv_inds_stage1(indices,
                    indice_pairs, indice_pairs_uniq, indice_num_per_loc,
                    batch_size, output_dims_, input_dims_, 
                    ksize_, stride_, padding_, dilation_, transposed, stream_int);
            }}
            """)
        code.raw(f"""TV_THROW_RT_ERR("unknown ndim", ndim);""")

        return code  # .ret("int")

    @pccm.pybind.mark
    @pccm.cuda.static_function
    def generate_conv_inds_stage1_5(self):
        code = pccm.FunctionCode()
        code.arg("indice_pairs_uniq", "tv::Tensor")
        code.arg("ndim", "int")
        code.arg("uniq_size", "int64_t")
        code.arg("stream_int", f"std::uintptr_t", "0", pyanno="int")
        if CUMM_CPU_ONLY_BUILD:
            return code.make_invalid()
        for ndim in self.ndims:
            code.raw(f"""
            if (ndim == {ndim}){{
                return SpconvIndices{ndim}D::generate_conv_inds_stage1_5(indice_pairs_uniq, uniq_size, stream_int);
            }}
            """)
        code.raw(f"""TV_THROW_RT_ERR("unknown ndim", ndim);""")
        return code.ret("int")

    @pccm.pybind.mark
    @pccm.cuda.static_function
    def generate_conv_inds_stage2(self):
        code = pccm.FunctionCode()
        code.arg("indices, hashdata_k, hashdata_v", "tv::Tensor")
        code.arg("indice_pairs, indice_pairs_uniq, indice_pairs_uniq_before_sort, out_inds", "tv::Tensor")
        code.arg("num_out_act", "int")
        code.arg("batch_size", "int")
        code.arg("output_dims, input_dims", f"std::vector<int>")
        code.arg("ksize, stride, padding, dilation", f"std::vector<int>")
        code.arg("transposed", f"bool", "false")
        code.arg("stream_int", f"std::uintptr_t", "0", pyanno="int")
        if CUMM_CPU_ONLY_BUILD:
            return code.make_invalid()

        code.raw(f"""
        int ndim = indices.dim(1) - 1;
        TV_ASSERT_RT_ERR(output_dims.size() == ndim && input_dims.size() == ndim &&
            ksize.size() == ndim && stride.size() == ndim && dilation.size() == ndim &&
            padding.size() == ndim, "your params size not equal to ndim", ndim);
        """)

        for ndim in self.ndims:
            code.raw(f"""
            if (ndim == {ndim}){{
                tv::array<int, {ndim}> output_dims_, input_dims_;
                tv::array<int, {ndim}> ksize_, stride_, padding_, dilation_;
                for (int i = 0; i < {ndim}; ++i){{
                    output_dims_[i] = output_dims[i];
                    input_dims_[i] = input_dims[i];
                    ksize_[i] = ksize[i];
                    stride_[i] = stride[i];
                    padding_[i] = padding[i];
                    dilation_[i] = dilation[i];
                }}
                return SpconvIndices{ndim}D::generate_conv_inds_stage2(indices, 
                    hashdata_k, hashdata_v, indice_pairs,
                    indice_pairs_uniq, indice_pairs_uniq_before_sort, out_inds, num_out_act,
                    batch_size, output_dims_, input_dims_, 
                    ksize_, stride_, padding_, dilation_, transposed, stream_int);
            }}
            """)
        code.raw(f"""TV_THROW_RT_ERR("unknown ndim", ndim);""")

        return code.ret("int")

    @pccm.pybind.mark
    @pccm.cuda.static_function
    def generate_conv_inds_mask_stage1(self):
        code = pccm.FunctionCode()
        if CUMM_CPU_ONLY_BUILD:
            return code.make_invalid()

        code.arg("indices", "tv::Tensor")
        code.arg("indice_pairs_bwd, indice_pairs_uniq, indice_num_per_loc",
                 "tv::Tensor")
        code.arg("batch_size", "int")
        code.arg("output_dims, input_dims", f"std::vector<int>")
        code.arg("ksize, stride, padding, dilation", f"std::vector<int>")
        code.arg("transposed", f"bool", "false")

        code.arg("stream_int", f"std::uintptr_t", "0", pyanno="int")
        code.raw(f"""
        int ndim = indices.dim(1) - 1;
        TV_ASSERT_RT_ERR(output_dims.size() == ndim && input_dims.size() == ndim &&
            ksize.size() == ndim && stride.size() == ndim && dilation.size() == ndim &&
            padding.size() == ndim, "your params size not equal to ndim", ndim);
        """)

        for ndim in self.ndims:
            code.raw(f"""
            if (ndim == {ndim}){{
                tv::array<int, {ndim}> output_dims_, input_dims_;
                tv::array<int, {ndim}> ksize_, stride_, padding_, dilation_;
                for (int i = 0; i < {ndim}; ++i){{
                    output_dims_[i] = output_dims[i];
                    input_dims_[i] = input_dims[i];
                    ksize_[i] = ksize[i];
                    stride_[i] = stride[i];
                    padding_[i] = padding[i];
                    dilation_[i] = dilation[i];
                }}
                return SpconvIndices{ndim}D::generate_conv_inds_mask_stage1(indices,
                    indice_pairs_bwd, indice_pairs_uniq, indice_num_per_loc,
                    batch_size, output_dims_, input_dims_, 
                    ksize_, stride_, padding_, dilation_, transposed, stream_int);
            }}
            """)
        code.raw(f"""TV_THROW_RT_ERR("unknown ndim", ndim);""")

        return code  # .ret("int")

    @pccm.pybind.mark
    @pccm.cuda.static_function
    def generate_conv_inds_mask_stage2(self):
        code = pccm.FunctionCode()
        if CUMM_CPU_ONLY_BUILD:
            return code.make_invalid()
        code.arg("indices, hashdata_k, hashdata_v", "tv::Tensor")
        code.arg(
            "indice_pairs_fwd, indice_pairs_bwd, indice_pairs_uniq, indice_pairs_uniq_before_sort, out_inds",
            "tv::Tensor")
        code.arg("mask_fwd, mask_bwd", "tv::Tensor")
        code.arg("num_out_act", "int")
        code.arg("batch_size", "int")
        code.arg("output_dims, input_dims", f"std::vector<int>")
        code.arg("ksize, stride, padding, dilation", f"std::vector<int>")
        code.arg("transposed", f"bool", "false")
        code.arg("stream_int", f"std::uintptr_t", "0", pyanno="int")
        code.raw(f"""
        int ndim = indices.dim(1) - 1;
        TV_ASSERT_RT_ERR(output_dims.size() == ndim && input_dims.size() == ndim &&
            ksize.size() == ndim && stride.size() == ndim && dilation.size() == ndim &&
            padding.size() == ndim, "your params size not equal to ndim", ndim);
        """)

        for ndim in self.ndims:
            code.raw(f"""
            if (ndim == {ndim}){{
                tv::array<int, {ndim}> output_dims_, input_dims_;
                tv::array<int, {ndim}> ksize_, stride_, padding_, dilation_;
                for (int i = 0; i < {ndim}; ++i){{
                    output_dims_[i] = output_dims[i];
                    input_dims_[i] = input_dims[i];
                    ksize_[i] = ksize[i];
                    stride_[i] = stride[i];
                    padding_[i] = padding[i];
                    dilation_[i] = dilation[i];
                }}
                return SpconvIndices{ndim}D::generate_conv_inds_stage2_mask(
                    indices, hashdata_k, hashdata_v,
                    indice_pairs_fwd, indice_pairs_bwd, 
                    indice_pairs_uniq, indice_pairs_uniq_before_sort,
                    out_inds, mask_fwd, mask_bwd,
                    num_out_act, batch_size, output_dims_, input_dims_, 
                    ksize_, stride_, padding_, dilation_, transposed, stream_int);
            }}
            """)
        code.raw(f"""TV_THROW_RT_ERR("unknown ndim", ndim);""")

        return code.ret("int")

    @pccm.pybind.mark
    @pccm.cuda.static_function
    def generate_subm_conv_inds(self):
        code = pccm.FunctionCode()
        if CUMM_CPU_ONLY_BUILD:
            return code.make_invalid()
        code.arg("indices, hashdata_k, hashdata_v", "tv::Tensor")
        code.arg("indice_pairs, out_inds, indice_num_per_loc", "tv::Tensor")
        code.arg("batch_size", "int")
        code.arg("input_dims", f"std::vector<int>")
        code.arg("ksize, dilation", f"std::vector<int>")
        code.arg("indice_pair_mask", "tv::Tensor", "tv::Tensor()",
                 "cumm.tensorview.Tensor = Tensor()")
        code.arg("backward", "bool", "false")
        code.arg("stream_int", f"std::uintptr_t", "0", pyanno="int = 0")
        code.raw(f"""
        int ndim = indices.dim(1) - 1;
        TV_ASSERT_RT_ERR(input_dims.size() == ndim &&
            ksize.size() == ndim && dilation.size() == ndim, "your params size not equal to ndim", ndim);
        """)
        for ndim in self.ndims:
            code.raw(f"""
            if (ndim == {ndim}){{
                tv::array<int, {ndim}> input_dims_;
                tv::array<int, {ndim}> ksize_, dilation_;
                for (int i = 0; i < {ndim}; ++i){{
                    input_dims_[i] = input_dims[i];
                    ksize_[i] = ksize[i];
                    dilation_[i] = dilation[i];
                }}
                return SpconvIndices{ndim}D::generate_subm_conv_inds(indices, 
                    hashdata_k, hashdata_v,
                    indice_pairs, out_inds, indice_num_per_loc,
                    batch_size, input_dims_, 
                    ksize_, dilation_, indice_pair_mask, backward,
                    stream_int);
            }}
            """)
        code.raw(f"""TV_THROW_RT_ERR("unknown ndim", ndim);""")
        return code.ret("int")

    @pccm.pybind.mark
    @pccm.static_function
    def generate_conv_inds_cpu(self):
        code = pccm.FunctionCode()
        code.arg("indices", "tv::Tensor")
        code.arg("indice_pairs, out_inds, indice_num_per_loc", "tv::Tensor")
        code.arg("batch_size", "int")
        code.arg("output_dims, input_dims", f"std::vector<int>")
        code.arg("ksize, stride, padding, dilation", f"std::vector<int>")
        code.arg("transposed", f"bool", "false")
        code.raw(f"""
        int ndim = indices.dim(1) - 1;
        TV_ASSERT_RT_ERR(output_dims.size() == ndim && input_dims.size() == ndim &&
            ksize.size() == ndim && stride.size() == ndim && dilation.size() == ndim &&
            padding.size() == ndim, "your params size not equal to ndim", ndim);
        """)

        for ndim in self.ndims:
            code.raw(f"""
            if (ndim == {ndim}){{
                tv::array<int, {ndim}> output_dims_, input_dims_;
                tv::array<int, {ndim}> ksize_, stride_, padding_, dilation_;
                for (int i = 0; i < {ndim}; ++i){{
                    output_dims_[i] = output_dims[i];
                    input_dims_[i] = input_dims[i];
                    ksize_[i] = ksize[i];
                    stride_[i] = stride[i];
                    padding_[i] = padding[i];
                    dilation_[i] = dilation[i];
                }}
                return SpconvIndicesCPU{ndim}D::generate_conv_inds(indices,
                    indice_pairs, out_inds, indice_num_per_loc,
                    batch_size, output_dims_, input_dims_, 
                    ksize_, stride_, padding_, dilation_, transposed);
            }}
            """)
        code.raw(f"""TV_THROW_RT_ERR("unknown ndim", ndim);""")

        return code.ret("int")

    @pccm.pybind.mark
    @pccm.static_function
    def generate_subm_conv_inds_cpu(self):
        code = pccm.FunctionCode()
        code.arg("indices", "tv::Tensor")
        code.arg("indice_pairs, out_inds, indice_num_per_loc", "tv::Tensor")
        code.arg("batch_size", "int")
        code.arg("input_dims", f"std::vector<int>")
        code.arg("ksize, dilation", f"std::vector<int>")

        code.raw(f"""
        int ndim = indices.dim(1) - 1;
        TV_ASSERT_RT_ERR(input_dims.size() == ndim &&
            ksize.size() == ndim && dilation.size() == ndim, "your params size not equal to ndim", ndim);
        """)
        for ndim in self.ndims:
            code.raw(f"""
            if (ndim == {ndim}){{
                tv::array<int, {ndim}> input_dims_;
                tv::array<int, {ndim}> ksize_, dilation_;
                for (int i = 0; i < {ndim}; ++i){{
                    input_dims_[i] = input_dims[i];
                    ksize_[i] = ksize[i];
                    dilation_[i] = dilation[i];
                }}
                return SpconvIndicesCPU{ndim}D::generate_subm_conv_inds(indices,
                    indice_pairs, out_inds, indice_num_per_loc,
                    batch_size, input_dims_, 
                    ksize_, dilation_);
            }}
            """)
        code.raw(f"""TV_THROW_RT_ERR("unknown ndim", ndim);""")
        return code.ret("int")

    @pccm.pybind.mark
    @pccm.cuda.static_function
    def maxpool_forward(self):
        code = pccm.FunctionCode()
        if CUMM_CPU_ONLY_BUILD:
            return code.make_invalid()
        code.arg("out", "tv::Tensor")
        code.arg("inp", "tv::Tensor")
        code.arg("out_inds", "tv::Tensor")
        code.arg("in_inds", "tv::Tensor")
        code.arg("stream", "std::uintptr_t", "0", pyanno="int")
        code.add_dependency(IndiceMaxPool)
        code.raw(f"""
        return IndiceMaxPool::forward(out, inp, out_inds, in_inds, stream);
        """)
        return code

    @pccm.pybind.mark
    @pccm.cuda.static_function
    def maxpool_backward(self):
        code = pccm.FunctionCode()
        if CUMM_CPU_ONLY_BUILD:
            return code.make_invalid()
        code.arg("out", "tv::Tensor")
        code.arg("inp", "tv::Tensor")
        code.arg("dout", "tv::Tensor")
        code.arg("dinp", "tv::Tensor")
        code.arg("out_inds", "tv::Tensor")
        code.arg("in_inds", "tv::Tensor")
        code.arg("stream", "std::uintptr_t", "0", pyanno="int")
        code.add_dependency(IndiceMaxPool)
        code.raw(f"""
        return IndiceMaxPool::backward(out, inp, dout, dinp, out_inds, in_inds, stream);
        """)
        return code

    @pccm.pybind.mark
    @pccm.cuda.static_function
    def maxpool_implicit_gemm_forward(self):
        code = pccm.FunctionCode()
        if CUMM_CPU_ONLY_BUILD:
            return code.make_invalid()
        code.arg("out", "tv::Tensor")
        code.arg("inp", "tv::Tensor")
        code.arg("inds", "tv::Tensor")
        code.arg("stream", "std::uintptr_t", "0", pyanno="int")
        code.add_dependency(IndiceMaxPool)
        code.raw(f"""
        return IndiceMaxPool::forward_implicit_gemm(out, inp, inds, stream);
        """)
        return code

    @pccm.pybind.mark
    @pccm.cuda.static_function
    def maxpool_implicit_gemm_backward(self):
        code = pccm.FunctionCode()
        if CUMM_CPU_ONLY_BUILD:
            return code.make_invalid()
        code.arg("out", "tv::Tensor")
        code.arg("inp", "tv::Tensor")
        code.arg("dout", "tv::Tensor")
        code.arg("dinp", "tv::Tensor")
        code.arg("inds", "tv::Tensor")
        code.arg("stream", "std::uintptr_t", "0", pyanno="int")
        code.add_dependency(IndiceMaxPool)
        code.raw(f"""
        return IndiceMaxPool::backward_implicit_gemm(out, inp, dout, dinp, inds, stream);
        """)
        return code

    @pccm.pybind.mark
    @pccm.static_function
    def maxpool_forward_cpu(self):
        code = pccm.FunctionCode()
        code.arg("out", "tv::Tensor")
        code.arg("inp", "tv::Tensor")
        code.arg("out_inds", "tv::Tensor")
        code.arg("in_inds", "tv::Tensor")
        code.add_dependency(IndiceMaxPoolCPU)
        code.raw(f"""
        return IndiceMaxPoolCPU::forward(out, inp, out_inds, in_inds);
        """)
        return code

    @pccm.pybind.mark
    @pccm.static_function
    def maxpool_backward_cpu(self):
        code = pccm.FunctionCode()
        code.arg("out", "tv::Tensor")
        code.arg("inp", "tv::Tensor")
        code.arg("dout", "tv::Tensor")
        code.arg("dinp", "tv::Tensor")
        code.arg("out_inds", "tv::Tensor")
        code.arg("in_inds", "tv::Tensor")
        code.add_dependency(IndiceMaxPoolCPU)
        code.raw(f"""
        return IndiceMaxPoolCPU::backward(out, inp, dout, dinp, out_inds, in_inds);
        """)
        return code

    @pccm.pybind.mark
    @pccm.static_function
    def gather_cpu(self):
        code = pccm.FunctionCode()
        code.arg("out", "tv::Tensor")
        code.arg("inp", "tv::Tensor")
        code.arg("inds", "tv::Tensor")
        code.add_dependency(GatherCPU)
        code.raw(f"""
        return GatherCPU::gather(out, inp, inds);
        """)
        return code

    @pccm.pybind.mark
    @pccm.static_function
    def scatter_add_cpu(self):
        code = pccm.FunctionCode()
        code.arg("out", "tv::Tensor")
        code.arg("inp", "tv::Tensor")
        code.arg("inds", "tv::Tensor")
        code.add_dependency(GatherCPU)
        code.raw(f"""
        return GatherCPU::scatter_add(out, inp, inds);
        """)
        return code

    @pccm.pybind.mark
    @pccm.cuda.static_function
    def sort_1d_by_key(self):
        code = pccm.FunctionCode()
        if CUMM_CPU_ONLY_BUILD:
            return code.make_invalid()
        code.arg("data", "tv::Tensor")
        code.arg("indices",
                 "tv::Tensor",
                 "tv::Tensor()",
                 pyanno="cumm.tensorview.Tensor = Tensor()")
        code.arg("stream", "std::uintptr_t", "0", pyanno="int")
        code.code_after_include = f"""
        template <typename T> struct SmallOrEqualTo {{
            TV_HOST_DEVICE_INLINE T operator()(const T &x, const T &y) const {{
                return x < y;
            }}
        }};
        template <typename T> __global__ void mask_input(T* inp, T mask, int size){{
            for (int i : tv::KernelLoopX<int>(size)){{
                inp[i] &= mask;
            }}
        }}

        """
        code.add_dependency(CustomThrustLib, TensorViewKernel)
        code.add_param_class("cudakers", CudaCommonKernel())
        code.raw(f"""
        cudaStream_t stream_cu = reinterpret_cast<cudaStream_t>(stream);
        if (indices.empty()){{
            indices = tv::empty({{data.dim(0)}}, tv::int32, 0);
        }}
        tv::cuda::Launch launcher(data.dim(0), stream_cu);
        launcher(cudakers::arange_kernel<int32_t>, indices.data_ptr<int32_t>(), indices.dim(0));
        auto timer = tv::CUDATimer();
        tv::dispatch<int32_t, uint32_t, int64_t, uint64_t>(data.dtype(), [&](auto I){{
            using T = TV_DECLTYPE(I);
            thrust::device_ptr<T> ptr_tr(data.data_ptr<T>());
            thrust::device_ptr<int32_t> ptr_k(indices.data_ptr<int32_t>());
            auto thrust_ctx = thrust::cuda::par.on(stream_cu);
            thrust::stable_sort_by_key(thrust_ctx, ptr_tr, ptr_tr + data.dim(0), ptr_k, SmallOrEqualTo<uint32_t>());
        }});
        tv::ssprint("SORT BY KEY TIME", data.dim(0), timer.report() / 1000.0);
        return indices;
        """)
        return code.ret("tv::Tensor")

    def sort_1d_by_key_allocator_template(self, use_allocator: bool):
        code = pccm.FunctionCode()
        if CUMM_CPU_ONLY_BUILD:
            return code.make_invalid()
        code.arg("data", "tv::Tensor")
        if not use_allocator:
            code.arg("alloc_func", "std::function<std::uintptr_t(std::size_t)>")
        else:
            code.arg("allocator", "ThrustAllocator&")

        code.arg("indices",
                 "tv::Tensor",
                 "tv::Tensor()",
                 pyanno="cumm.tensorview.Tensor = Tensor()")
        code.arg("stream", "std::uintptr_t", "0", pyanno="int")
        code.code_after_include = f"""
        template <typename T> struct SmallOrEqualTo {{
            TV_HOST_DEVICE_INLINE T operator()(const T &x, const T &y) const {{
                return x < y;
            }}
        }};
        template <typename T> __global__ void mask_input(T* inp, T mask, int size){{
            for (int i : tv::KernelLoopX<int>(size)){{
                inp[i] &= mask;
            }}
        }}
        """
        code.add_dependency(CustomThrustLib, TensorViewKernel)
        code.add_param_class("cudakers", CudaCommonKernel())
        if not use_allocator:
            code.raw(f"""
            ThrustCustomAllocatorV2 allocator{{alloc_func}};
            """)
        code.raw(f"""
        cudaStream_t stream_cu = reinterpret_cast<cudaStream_t>(stream);
        if (indices.empty()){{
            indices = tv::empty({{data.dim(0)}}, tv::int32, 0);
        }}
        tv::cuda::Launch launcher(data.dim(0), stream_cu);
        launcher(cudakers::arange_kernel<int32_t>, indices.data_ptr<int32_t>(), indices.dim(0));
        // auto timer = tv::CUDATimer();
        tv::dispatch<int32_t, uint32_t, int64_t, uint64_t>(data.dtype(), [&](auto I){{
            using T = TV_DECLTYPE(I);
            thrust::device_ptr<T> ptr_tr(data.data_ptr<T>());
            thrust::device_ptr<int32_t> ptr_k(indices.data_ptr<int32_t>());
            auto thrust_ctx = thrust::cuda::par.on(stream_cu);
            auto ctx2 = thrust::cuda::par(allocator).on(stream_cu);
            thrust::sort_by_key(ctx2, ptr_tr, ptr_tr + data.dim(0), ptr_k);
        }});
        // tv::ssprint("SORT BY KEY TIME", data.dim(0), timer.report() / 1000.0);
        return indices;
        """)
        return code.ret("tv::Tensor")


    @pccm.pybind.mark
    @pccm.cuda.static_function
    def sort_1d_by_key_allocator(self):
        # for python
        return self.sort_1d_by_key_allocator_template(False)

    @pccm.cuda.static_function
    def sort_1d_by_key_allocator_v2(self):
        # for cpp only
        return self.sort_1d_by_key_allocator_template(True)


    @pccm.pybind.mark
    @pccm.cuda.static_function
    def sort_1d_by_key_split(self):
        code = pccm.FunctionCode()
        if CUMM_CPU_ONLY_BUILD:
            return code.make_invalid()
        code.arg("data", "tv::Tensor")
        code.arg("mask", "tv::Tensor")

        code.arg("indices",
                 "tv::Tensor",
                 "tv::Tensor()",
                 pyanno="cumm.tensorview.Tensor = Tensor()")
        code.arg("stream", "std::uintptr_t", "0", pyanno="int")
        code.arg("mask_output", "bool", "false")

        code.code_after_include = f"""
        template <typename T> struct MaskedElementComp {{
            T mask_;
            TV_HOST_DEVICE_INLINE T operator()(const T &x, const T &y) const {{
                return (x & mask_) < (y & mask_);
            }}
        }};
        template <typename T> __global__ void mask_input(T* inp, T mask, int size){{
            for (int i : tv::KernelLoopX<int>(size)){{
                inp[i] &= mask;
            }}
        }}
        """
        code.add_dependency(CustomThrustLib, TensorViewKernel)
        code.add_param_class("cudakers", CudaCommonKernel())
        code.raw(f"""
        cudaStream_t stream_cu = reinterpret_cast<cudaStream_t>(stream);
        // auto timer = tv::CudaContextTimer<>();
        if (indices.empty()){{
            indices = tv::empty({{data.dim(0)}}, tv::int32, 0);
        }}
        tv::cuda::Launch launcher(data.dim(0), stream_cu);
        launcher(cudakers::arange_kernel<int32_t>, indices.data_ptr<int32_t>(), indices.dim(0));
        tv::dispatch<int32_t, uint32_t, int64_t, uint64_t>(data.dtype(), [&](auto I){{
            using T = TV_DECLTYPE(I);
            auto masks_ptr = mask.data_ptr<T>();
            MaskedElementComp<T> op_comp{{masks_ptr[0]}};
            thrust::device_ptr<T> ptr_tr(data.data_ptr<T>());
            thrust::device_ptr<int32_t> ptr_k(indices.data_ptr<int32_t>());
            auto thrust_ctx = thrust::cuda::par.on(stream_cu);
            thrust::sort_by_key(thrust_ctx, ptr_tr, ptr_tr + data.dim(0), ptr_k, op_comp);
            if (mask_output){{
                launcher(mask_input<T>, data.data_ptr<T>(), masks_ptr[0], data.dim(0));
            }}
        }});
        // tv::ssprint("SORT BY KEY MASKED TIME", timer.report() / 1000.0);
        return indices;
        """)
        return code.ret("tv::Tensor")

    def sort_1d_by_key_split_allocator_template(self, use_allocator: bool):
        code = pccm.FunctionCode()
        if CUMM_CPU_ONLY_BUILD:
            return code.make_invalid()
        code.arg("data", "tv::Tensor")
        if not use_allocator:
            code.arg("alloc_func", "std::function<std::uintptr_t(std::size_t)>")
        else:
            code.arg("allocator", "ThrustAllocator&")

        code.arg("mask", "tv::Tensor")

        code.arg("indices",
                 "tv::Tensor",
                 "tv::Tensor()",
                 pyanno="cumm.tensorview.Tensor = Tensor()")
        code.arg("stream", "std::uintptr_t", "0", pyanno="int")
        code.arg("mask_output", "bool", "false")

        code.code_after_include = f"""
        template <typename T> struct MaskedElementComp {{
            T mask_;
            TV_HOST_DEVICE_INLINE T operator()(const T &x, const T &y) const {{
                return (x & mask_) < (y & mask_);
            }}
        }};
        template <typename T> __global__ void mask_input(T* inp, T mask, int size){{
            for (int i : tv::KernelLoopX<int>(size)){{
                inp[i] &= mask;
            }}
        }}
        """
        code.add_dependency(CustomThrustLib, TensorViewKernel)
        code.add_param_class("cudakers", CudaCommonKernel())
        if not use_allocator:
            code.raw(f"""
            ThrustCustomAllocatorV2 allocator{{alloc_func}};
            """)
        code.raw(f"""
        cudaStream_t stream_cu = reinterpret_cast<cudaStream_t>(stream);
        // auto timer = tv::CudaContextTimer<>();
        if (indices.empty()){{
            indices = tv::empty({{data.dim(0)}}, tv::int32, 0);
        }}
        tv::cuda::Launch launcher(data.dim(0), stream_cu);
        launcher(cudakers::arange_kernel<int32_t>, indices.data_ptr<int32_t>(), indices.dim(0));
        tv::dispatch<int32_t, uint32_t, int64_t, uint64_t>(data.dtype(), [&](auto I){{
            using T = TV_DECLTYPE(I);
            auto masks_ptr = mask.data_ptr<T>();
            MaskedElementComp<T> op_comp{{masks_ptr[0]}};
            thrust::device_ptr<T> ptr_tr(data.data_ptr<T>());
            thrust::device_ptr<int32_t> ptr_k(indices.data_ptr<int32_t>());
            // auto thrust_ctx = thrust::cuda::par.on(stream_cu);
            auto ctx2 = thrust::cuda::par(allocator).on(stream_cu);
            thrust::sort_by_key(ctx2, ptr_tr, ptr_tr + data.dim(0), ptr_k, op_comp);
            if (mask_output){{
                launcher(mask_input<T>, data.data_ptr<T>(), masks_ptr[0], data.dim(0));
            }}
        }});
        // tv::ssprint("SORT_BY_KEY_MASKED", timer.report() / 1000.0);
        return indices;
        """)
        return code.ret("tv::Tensor")



    @pccm.pybind.mark
    @pccm.cuda.static_function
    def sort_1d_by_key_split_allocator(self):
        return self.sort_1d_by_key_split_allocator_template(False)

    @pccm.pybind.mark
    @pccm.cuda.static_function
    def sort_1d_by_key_split_allocator_v2(self):
        return self.sort_1d_by_key_split_allocator_template(True)

    @pccm.pybind.mark
    @pccm.cuda.static_function
    def count_bits(self):
        code = pccm.FunctionCode()
        if CUMM_CPU_ONLY_BUILD:
            return code.make_invalid()

        code.add_dependency(TensorViewKernel)
        code.arg("a", "tv::Tensor")
        code.code_after_include = f"""
        __global__ void count_bits_kernel_64(const uint64_t* data, int32_t* out, int size){{
            for (int i : tv::KernelLoopX<int>(size)){{
                out[i] = __popcll(reinterpret_cast<const unsigned long long*>(data)[i]);
            }}
        }}
        __global__ void count_bits_kernel(const uint32_t* data, int32_t* out, int size){{
            for (int i : tv::KernelLoopX<int>(size)){{
                out[i] = __popc(data[i]);
            }}
        }}

        int numberOfSetBits(uint32_t i)
        {{
            // https://stackoverflow.com/questions/109023/how-to-count-the-number-of-set-bits-in-a-32-bit-integer
            // Java: use int, and use >>> instead of >>. Or use Integer.bitCount()
            // C or C++: use uint32_t
            i = i - ((i >> 1) & 0x55555555);        // add pairs of bits
            i = (i & 0x33333333) + ((i >> 2) & 0x33333333);  // quads
            i = (i + (i >> 4)) & 0x0F0F0F0F;        // groups of 8
            return (i * 0x01010101) >> 24;          // horizontal sum of bytes
        }}

        int numberOfSetBits(uint64_t i)
        {{
            return numberOfSetBits(uint32_t(i)) + numberOfSetBits(uint32_t(i >> 32));
        }}
        """
        code.raw(f"""
        tv::Tensor res(a.shape(), tv::int32, a.device());
        tv::dispatch<uint32_t, uint64_t>(a.dtype(), [&](auto I){{
            auto res_ptr = res.data_ptr<int>();
            using T = TV_DECLTYPE(I);
            auto a_ptr = a.data_ptr<const T>();
            if (a.device() == -1){{
                for (int i = 0; i < a.size(); ++i){{
                    res_ptr[i] = numberOfSetBits(a_ptr[i]);
                }}
            }}else{{
                tv::cuda::Launch launcher(a.size());
                tv::if_constexpr<std::is_same<T, uint64_t>::value>([=](auto _)mutable{{
                    launcher(_(count_bits_kernel_64), a_ptr, res_ptr, int(a.size()));
                }}, [=](auto _)mutable{{
                    launcher(_(count_bits_kernel), a_ptr, res_ptr, int(a.size()));
                }});
            }}
        }});
        return res;
        """)
        return code.ret("tv::Tensor")

    @pccm.pybind.mark
    @pccm.static_function
    def calc_point2voxel_meta_data(self):
        code = pccm.FunctionCode()
        code.arg("vsize_xyz", f"std::vector<float>")
        code.arg("coors_range_xyz", f"std::vector<float>")
        code.raw(f"""
        int ndim = vsize_xyz.size();
        TV_ASSERT_RT_ERR(vsize_xyz.size() == ndim &&
            coors_range_xyz.size() == ndim * 2, "your params size not equal to ndim", ndim);
        """)
        for ndim in self.ndims:
            code.raw(f"""
            if (ndim == {ndim}){{
                std::array<float, {ndim}> vsize_xyz_;
                std::array<float, {ndim * 2}> coors_range_xyz_;
                for (int i = 0; i < {ndim}; ++i){{
                    vsize_xyz_[i] = vsize_xyz[i];
                    coors_range_xyz_[i] = coors_range_xyz[i];
                    coors_range_xyz_[i + {ndim}] = coors_range_xyz[i + {ndim}];
                }}
                auto res = Point2Voxel{ndim}DCPU::calc_meta_data(vsize_xyz_, coors_range_xyz_);
                std::vector<float> vsize({ndim}), coors_range({ndim * 2});
                std::vector<int> grid_size({ndim}), grid_stride({ndim});

                for (int i = 0; i < {ndim}; ++i){{
                    vsize[i] = std::get<0>(res)[i];
                    grid_size[i] = std::get<1>(res)[i];
                    grid_stride[i] = std::get<2>(res)[i];
                    coors_range[i] = std::get<3>(res)[i];
                    coors_range[i + {ndim}] = std::get<3>(res)[i + {ndim}];
                }}
                return std::make_tuple(vsize, grid_size, grid_stride, coors_range);
            }}
            """)
        code.raw(f"""TV_THROW_RT_ERR("unknown ndim", ndim);""")
        return code.ret(
            "std::tuple<std::vector<float>, std::vector<int>, std::vector<int>, std::vector<float>>"
        )

    @pccm.pybind.mark
    @pccm.static_function
    def point2voxel_cpu(self):
        code = pccm.FunctionCode()
        code.arg("points", "tv::Tensor")
        code.arg("voxels, indices, num_per_voxel, densehashdata, pc_voxel_id", "tv::Tensor")
        code.arg("vsize", f"std::vector<float>")
        code.arg("grid_size, grid_stride", f"std::vector<int>")
        code.arg("coors_range", f"std::vector<float>")

        code.arg("empty_mean", "bool", "false")
        code.arg("clear_voxels", "bool", "true")

        code.raw(f"""
        int ndim = vsize.size();
        TV_ASSERT_RT_ERR(vsize.size() == ndim && grid_stride.size() == ndim && 
            coors_range.size() == ndim * 2 && grid_size.size() == ndim, 
            "your params size not equal to ndim", ndim);
        // voxels: []
        """)
        for ndim in self.ndims:
            code.raw(f"""
            if (ndim == {ndim}){{
                std::array<float, {ndim}> vsize_;
                std::array<int, {ndim}> grid_size_, grid_stride_;
                std::array<float, {ndim * 2}> coors_range_;
                for (int i = 0; i < {ndim}; ++i){{
                    vsize_[i] = vsize[i];
                    grid_size_[i] = grid_size[i];
                    grid_stride_[i] = grid_stride[i];
                    coors_range_[i] = coors_range[i];
                    coors_range_[i + {ndim}] = coors_range[i + {ndim}];
                }}
                if (empty_mean){{
                    return Point2Voxel{ndim}DCPU::point_to_voxel_empty_mean_static(points, voxels, indices, 
                        num_per_voxel, densehashdata, pc_voxel_id,
                        vsize_, grid_size_, grid_stride_, coors_range_, clear_voxels);
                }} else{{
                    return Point2Voxel{ndim}DCPU::point_to_voxel_static(points, voxels, indices, 
                        num_per_voxel, densehashdata, pc_voxel_id,
                        vsize_, grid_size_, grid_stride_, coors_range_, clear_voxels);
                }}
            }}
            """)
        code.raw(f"""TV_THROW_RT_ERR("unknown ndim", ndim);""")
        return code.ret("std::tuple<tv::Tensor, tv::Tensor, tv::Tensor>")

    @pccm.pybind.mark
    @pccm.static_function
    def point2voxel_cuda(self):
        code = pccm.FunctionCode()
        code.arg("points", "tv::Tensor")
        code.arg("voxels, indices, num_per_voxel, hashdata, point_indice_data, pc_voxel_id",
                 "tv::Tensor")
        code.arg("vsize", f"std::vector<float>")
        code.arg("grid_size, grid_stride", f"std::vector<int>")
        code.arg("coors_range", f"std::vector<float>")

        code.arg("empty_mean", "bool", "false")
        code.arg("clear_voxels", "bool", "true")
        code.arg("stream_int", f"std::uintptr_t", "0")
        if CUMM_CPU_ONLY_BUILD:
            return code.make_invalid()

        code.raw(f"""
        int ndim = vsize.size();
        TV_ASSERT_RT_ERR(vsize.size() == ndim && grid_stride.size() == ndim && 
            coors_range.size() == ndim * 2 && grid_size.size() == ndim, 
            "your params size not equal to ndim", ndim);
        // voxels: []
        """)
        for ndim in self.ndims:
            code.raw(f"""
            if (ndim == {ndim}){{
                std::array<float, {ndim}> vsize_;
                std::array<int, {ndim}> grid_size_, grid_stride_;
                std::array<float, {ndim * 2}> coors_range_;
                for (int i = 0; i < {ndim}; ++i){{
                    vsize_[i] = vsize[i];
                    grid_size_[i] = grid_size[i];
                    grid_stride_[i] = grid_stride[i];
                    coors_range_[i] = coors_range[i];
                    coors_range_[i + {ndim}] = coors_range[i + {ndim}];
                }}
                return Point2Voxel{ndim}D::point_to_voxel_hash_static(points, voxels, indices, 
                    num_per_voxel, hashdata, point_indice_data, pc_voxel_id,
                    vsize_, grid_size_, grid_stride_, coors_range_, clear_voxels, 
                    empty_mean, stream_int);
            }}
            """)
        code.raw(f"""TV_THROW_RT_ERR("unknown ndim", ndim);""")
        return code.ret("std::tuple<tv::Tensor, tv::Tensor, tv::Tensor>")

    @pccm.pybind.mark
    @pccm.static_function
    def get_int32_max(self):
        code = pccm.FunctionCode()
        code.raw(f"return std::numeric_limits<int>::max();")
        return code.ret("int")

    @pccm.static_function
    def get_conv_output_size(self):
        code = pccm.FunctionCode()
        code.arg("input_dims", f"std::vector<int>")
        code.arg("ksize, stride, padding, dilation", f"std::vector<int>")
        code.raw(f"""
        int ndim = input_dims.size();
        std::vector<int> out_dims;
        for (int i = 0; i < ndim; ++i){{
            if (ksize[i] == -1){{
                out_dims.push_back(1);
            }}else{{
                auto size = (input_dims[i] + 2 * padding[i] - dilation[i] *
                    (ksize[i] - 1) - 1) / stride[i] + 1;
                out_dims.push_back(size);
            }}
        }}
        return out_dims;
        """)
        return code.ret("std::vector<int>")

    @pccm.static_function
    def get_deconv_output_size(self):
        code = pccm.FunctionCode()
        code.arg("input_dims", f"std::vector<int>")
        code.arg("ksize, stride, padding, dilation, output_padding", f"std::vector<int>")
        code.raw(f"""
        int ndim = input_dims.size();
        std::vector<int> out_dims;
        for (int i = 0; i < ndim; ++i){{
            if (ksize[i] == -1){{
                TV_THROW_INVALID_ARG("kernel size can't be -1");
            }}else{{
                auto size = (input_dims[i] - 1) * stride[i] - 2 * padding[i] + ksize[
                    i] + output_padding[i];
                out_dims.push_back(size);
            }}
        }}
        return out_dims;
        """)
        return code.ret("std::vector<int>")

    @pccm.cuda.static_function
    def apply_thrust_unique_to_indice_pairs_uniq(self):
        code = pccm.code()
        code.add_dependency(CustomThrustLib)
        code.arg("data", "tv::Tensor")
        code.arg("allocator", "ThrustAllocator&")
        code.arg("stream_int", f"std::uintptr_t", "0")
        code.raw(f"""
        int num_out_act = 0;
        int uniq_size = data.dim(0);
        tv::dispatch<int32_t, int64_t>(data.dtype(), [&](auto I){{
            using T = TV_DECLTYPE(I);
            thrust::device_ptr<T> ptr_tr(data.data_ptr<T>());
            auto thrust_ctx = thrust::cuda::par(allocator).on(reinterpret_cast<cudaStream_t>(stream_int));
            thrust::sort(thrust_ctx, ptr_tr, ptr_tr + uniq_size);
            auto new_end = thrust::unique(thrust_ctx, ptr_tr, ptr_tr + uniq_size);
            num_out_act = new_end - ptr_tr - 1;
        }});
        return num_out_act;
        """)
        return code.ret("int")

    @pccm.pybind.mark 
    @pccm.static_function
    def get_indice_pairs_implicit_gemm(self):
        code = pccm.code()
        code.arg("allocator", "ExternalAllocator&")
        code.arg("indices", "tv::Tensor")
        code.arg("batch_size", "int")
        code.arg("input_dims", f"std::vector<int>")
        code.arg("algo", "int")
        code.arg("ksize, stride, padding, dilation, out_padding", f"std::vector<int>")
        code.arg("subm, transposed, is_train", f"bool")

        code.arg("stream_int", f"std::uintptr_t", "0", pyanno="int")
        if CUMM_CPU_ONLY_BUILD:
            code.raw(f"""
            throw std::runtime_error("this function can only be used with CUDA.")
            """)
            return code.ret("tv::Tensor")
        code.raw(f"""
        auto tvctx = tv::Context();
        tvctx.set_cuda_stream(reinterpret_cast<cudaStream_t>(stream_int));

        auto conv_algo = static_cast<tv::gemm::SparseConvAlgo>(algo);
        int kv = std::accumulate(ksize.begin(), ksize.end(), 1, std::multiplies<int>());
        TV_ASSERT_RT_ERR(kv <= 32, "currently only support ksize < 32");
        std::vector<int64_t> input_dims_i64(input_dims.begin(), input_dims.end());
        int64_t spatial_volume = std::accumulate(input_dims_i64.begin(),
          input_dims_i64.end(), int64_t(1), std::multiplies<int64_t>());
        bool use_int64_hash_k = spatial_volume >= int64_t(std::numeric_limits<int>::max());
        tv::DType indice_uniq_dtype = use_int64_hash_k ? tv::int64 : tv::int32;
        std::vector<int> out_shape;
        if (!subm){{
            if (transposed){{
                out_shape = get_deconv_output_size(input_dims, ksize, stride, padding, dilation, out_padding);
            }}else{{
                out_shape = get_conv_output_size(input_dims, ksize, stride, padding, dilation);
            }}
        }}else{{
            out_shape = input_dims;
        }}
        for (auto& v : out_shape){{
            if (v <= 0){{
                TV_THROW_RT_ERR("your out spatial shape", out_shape, "ratch zero!, input shape:", input_dims);
            }}
        }}
        TV_ASSERT_RT_ERR(conv_algo == tv::gemm::SparseConvAlgo::kMaskImplicitGemm || 
            conv_algo == tv::gemm::SparseConvAlgo::kMaskSplitImplicitGemm, "only support implicit gemm");
        bool is_mask_split = conv_algo == tv::gemm::SparseConvAlgo::kMaskSplitImplicitGemm;
        int mask_split_count = is_mask_split ? 2 : 1;
        tv::Tensor pair;
        if (subm){{
            pair = allocator.full_int({pccm.literal(SpconvAllocatorKeys.Pair)}, 
                {{2, kv, indices.dim(0)}}, -1, indices.dtype(), indices.device());
        }}else{{
            pair = allocator.full_int({pccm.literal(SpconvAllocatorKeys.Pair)}, 
                {{kv, indices.dim(0)}}, -1, indices.dtype(), indices.device());
        }}
        auto indice_num_per_loc = allocator.zeros({pccm.literal(SpconvAllocatorKeys.IndiceNumPerLoc)}, 
            {{kv}}, indices.dtype(), indices.device());
        tv::Tensor mask_tensor = tv::zeros({{mask_split_count}}, tv::uint32, -1);
        auto mask_tensor_ptr = mask_tensor.data_ptr<uint32_t>();

        if (is_mask_split){{
            auto kv_div_2 = kv / 2;
            auto remain = kv - kv_div_2;
            uint64_t mask_np_1 = 1;
            uint64_t first = ((mask_np_1 << remain) - 1);
            uint64_t second = ((mask_np_1 << kv_div_2) - 1) << remain;
            mask_tensor_ptr[0] = uint32_t(first);
            mask_tensor_ptr[1] = uint32_t(second);
        }}
        else{{
            mask_tensor_ptr[1] = 0xffffffff;
        }}
        tv::Tensor out_inds;
        ThrustAllocator thrustalloc(allocator);

        if (subm){{
            ExternalAllocator::guard_t hash_k_guard, hash_v_gurad, hash_kv_gurad;
            out_inds = indices;
            int num_points = out_inds.dim(0);
            tv::Tensor hash_k, hash_v;
            if (use_int64_hash_k){{
                hash_k_guard = allocator.empty_guard({{num_points * 2}}, tv::int64, 0);
                hash_v_gurad = allocator.empty_guard({{num_points * 2}}, tv::int32, 0);
                hash_k = hash_k_guard->tensor;
                hash_v = hash_v_gurad->tensor;
            }}else{{
                hash_kv_gurad = allocator.empty_guard({{2, num_points * 2}}, tv::int32, 0);
                hash_k = hash_kv_gurad->tensor[0];
                hash_v = hash_kv_gurad->tensor[1];
            }}
            auto pair_mask = allocator.empty({pccm.literal(SpconvAllocatorKeys.PairMask)}, 
                {{mask_split_count, indices.dim(0)}}, tv::uint32, 0);
            generate_subm_conv_inds(indices, hash_k, hash_v, pair, out_inds, indice_num_per_loc,
                batch_size, input_dims, ksize, dilation, pair_mask, false, stream_int);
            auto mask_argsort = allocator.empty({pccm.literal(SpconvAllocatorKeys.MaskArgSort)}, 
                {{mask_split_count, out_inds.dim(0)}}, tv::uint32, 0);
            for (int j = 0; j < mask_split_count; ++j){{
                sort_1d_by_key_allocator_v2(pair_mask[j], thrustalloc, mask_argsort[j], stream_int);
            }}
        }}else{{
            auto pair_bwd = pair;
            auto indice_pairs_uniq_guard = allocator.empty_guard({{int64_t(pair.numel() + 1)}}, indice_uniq_dtype, 0);
            auto indice_pairs_uniq_bkp_guard = allocator.empty_guard({{int64_t(pair.numel() + 1)}}, indice_uniq_dtype, 0);

            auto indice_pairs_uniq = indice_pairs_uniq_guard->tensor;
            generate_conv_inds_mask_stage1(indices, pair_bwd, indice_pairs_uniq,
                indice_num_per_loc, batch_size, out_shape, input_dims, ksize,
                stride, padding, dilation, transposed, stream_int);
            indice_pairs_uniq_bkp_guard->tensor.copy_(indice_pairs_uniq, tvctx);
            // TODO pytorch unique may be faster?
            int num_act_out = apply_thrust_unique_to_indice_pairs_uniq(indice_pairs_uniq, thrustalloc, stream_int) - 1;
            indice_pairs_uniq = indice_pairs_uniq.slice_first_axis(0, num_act_out);
            out_inds = allocator.empty({pccm.literal(SpconvAllocatorKeys.OutIndices)}, 
                {{num_act_out, indices.dim(1)}}, indices.dtype(), 0);
            auto pair_fwd = allocator.full_int({pccm.literal(SpconvAllocatorKeys.PairFwd)}, 
                {{kv, num_act_out}}, -1, indices.dtype(), indices.device());
            auto pair_mask_fwd = allocator.zeros({pccm.literal(SpconvAllocatorKeys.PairMask)}, 
                {{mask_split_count, num_act_out}}, tv::uint32, 0);
            auto pair_mask_bwd = tv::Tensor();
            if (is_train){{
                pair_mask_bwd = allocator.zeros({pccm.literal(SpconvAllocatorKeys.PairMaskBwd)}, 
                    {{mask_split_count, indices.dim(0)}}, tv::uint32, 0);
            }}
            ExternalAllocator::guard_t hash_k_guard, hash_v_gurad, hash_kv_gurad;
            tv::Tensor hash_k, hash_v;
            if (use_int64_hash_k){{
                hash_k_guard = allocator.empty_guard({{num_act_out * 2}}, tv::int64, 0);
                hash_v_gurad = allocator.empty_guard({{num_act_out * 2}}, tv::int32, 0);
                hash_k = hash_k_guard->tensor;
                hash_v = hash_v_gurad->tensor;
            }}else{{
                hash_kv_gurad = allocator.empty_guard({{2, num_act_out * 2}}, tv::int32, 0);
                hash_k = hash_kv_gurad->tensor[0];
                hash_v = hash_kv_gurad->tensor[1];
            }}
            generate_conv_inds_mask_stage2(indices, hash_k, hash_v, pair_fwd, pair_bwd,
                indice_pairs_uniq, indice_pairs_uniq_bkp_guard->tensor, 
                out_inds, pair_mask_fwd, pair_mask_bwd, num_act_out,
                batch_size, out_shape, input_dims, ksize, stride, padding, dilation,
                transposed, stream_int);
            auto mask_argsort_fwd = allocator.empty({pccm.literal(SpconvAllocatorKeys.MaskArgSort)}, 
                {{mask_split_count, out_inds.dim(0)}}, tv::uint32, 0);
            tv::Tensor mask_argsort_bwd = tv::Tensor();
            if (is_train){{
                mask_argsort_bwd = allocator.zeros({pccm.literal(SpconvAllocatorKeys.MaskArgSortBwd)}, 
                    {{mask_split_count, indices.dim(0)}}, tv::uint32, 0);
            }}
            if (is_mask_split){{
                for (int j = 0; j < mask_split_count; ++j){{
                    if (!is_train){{
                        sort_1d_by_key_split_allocator_v2(pair_mask_fwd[j], thrustalloc, 
                            mask_tensor[j], mask_argsort_fwd[j], stream_int);
                    }}else{{
                        sort_1d_by_key_split_allocator_v2(pair_mask_fwd[j], thrustalloc, 
                            mask_tensor[j], mask_argsort_fwd[j], stream_int);
                        sort_1d_by_key_split_allocator_v2(pair_mask_bwd[j], thrustalloc, 
                            mask_tensor[j], mask_argsort_bwd[j], stream_int);
                    }}
                }}
            }}else{{
                if (!is_train){{
                    sort_1d_by_key_allocator_v2(pair_mask_fwd[0], thrustalloc, 
                        mask_argsort_fwd[0], stream_int);
                }}else{{
                    sort_1d_by_key_allocator_v2(pair_mask_fwd[0], thrustalloc, 
                        mask_argsort_fwd[0], stream_int);
                    sort_1d_by_key_allocator_v2(pair_mask_bwd[0], thrustalloc, 
                        mask_argsort_bwd[0], stream_int);
                }}
            }}
        }}
        return mask_tensor;
        """)
        return code.ret("tv::Tensor")

    @pccm.pybind.mark 
    @pccm.static_function
    def get_indice_pairs(self):
        code = pccm.code()
        code.arg("allocator", "ExternalAllocator&")
        code.arg("indices", "tv::Tensor")
        code.arg("batch_size", "int")
        code.arg("input_dims", f"std::vector<int>")
        code.arg("algo", "int")
        code.arg("ksize, stride, padding, dilation, out_padding", f"std::vector<int>")
        code.arg("subm, transposed", f"bool")
        code.arg("stream_int", f"std::uintptr_t", "0", pyanno="int")
        if CUMM_CPU_ONLY_BUILD:
            code.raw(f"""
            throw std::runtime_error("this function can only be used with CUDA.")
            """)
            return code
        code.raw(f"""
        int kv = std::accumulate(ksize.begin(), ksize.end(), 1, std::multiplies<int>());
        TV_ASSERT_RT_ERR(kv <= 32, "currently only support ksize < 32");
        auto conv_algo = static_cast<tv::gemm::SparseConvAlgo>(algo);
        TV_ASSERT_RT_ERR(conv_algo == tv::gemm::SparseConvAlgo::kNative, "only support kNative");

        std::vector<int64_t> input_dims_i64(input_dims.begin(), input_dims.end());
        int64_t spatial_volume = std::accumulate(input_dims_i64.begin(),
          input_dims_i64.end(), int64_t(1), std::multiplies<int64_t>());
        bool use_int64_hash_k = spatial_volume >= int64_t(std::numeric_limits<int>::max());
        tv::DType indice_uniq_dtype = use_int64_hash_k ? tv::int64 : tv::int32;
        std::vector<int> out_shape;
        if (!subm){{
            if (transposed){{
                out_shape = get_deconv_output_size(input_dims, ksize, stride, padding, dilation, out_padding);
            }}else{{
                out_shape = get_conv_output_size(input_dims, ksize, stride, padding, dilation);
            }}
        }}else{{
            out_shape = input_dims;
        }}
        for (auto& v : out_shape){{
            if (v <= 0){{
                TV_THROW_RT_ERR("your out spatial shape", out_shape, "ratch zero!, input shape:", input_dims);
            }}
        }}
        tv::Tensor pair;
        pair = allocator.full_int({pccm.literal(SpconvAllocatorKeys.Pair)}, 
            {{2, kv, indices.dim(0)}}, -1, indices.dtype(), indices.device());
        
        auto indice_num_per_loc = allocator.zeros({pccm.literal(SpconvAllocatorKeys.IndiceNumPerLoc)}, 
            {{kv}}, indices.dtype(), indices.device());
        tv::Tensor out_inds;
        """)
        with code.if_("subm"):
            code.raw(f"""
            if (indices.is_cpu()){{
                generate_subm_conv_inds_cpu(indices, pair, out_inds, indice_num_per_loc,
                    batch_size, input_dims, ksize, dilation);
            }}
            """)
            if not CUMM_CPU_ONLY_BUILD:
                code.raw(f"""
                else {{
                    ExternalAllocator::guard_t hash_k_guard, hash_v_gurad, hash_kv_gurad;
                    out_inds = indices;
                    int num_points = out_inds.dim(0);
                    tv::Tensor hash_k, hash_v;
                    if (use_int64_hash_k){{
                        hash_k_guard = allocator.empty_guard({{num_points * 2}}, tv::int64, 0);
                        hash_v_gurad = allocator.empty_guard({{num_points * 2}}, tv::int32, 0);
                        hash_k = hash_k_guard->tensor;
                        hash_v = hash_v_gurad->tensor;
                    }}else{{
                        hash_kv_gurad = allocator.empty_guard({{2, num_points * 2}}, tv::int32, 0);
                        hash_k = hash_kv_gurad->tensor[0];
                        hash_v = hash_kv_gurad->tensor[1];
                    }}
                    generate_subm_conv_inds(indices, hash_k, hash_v, pair, out_inds, indice_num_per_loc,
                        batch_size, input_dims, ksize, dilation, tv::Tensor(), false, stream_int);
                }}
                """)
            else:
                code.raw(f"""
                else {{
                    TV_THROW_RT_ERR("not implemented for CPU ONLY build.")
                }}
                """)
        with code.else_():
            code.raw(f"""
            if (indices.is_cpu()){{

                out_inds = allocator.empty({pccm.literal(SpconvAllocatorKeys.OutIndices)}, 
                    {{kv * indices.dim(0), indices.dim(1)}}, indices.dtype(), -1);
                generate_conv_inds_cpu(indices, pair, out_inds, indice_num_per_loc,
                    batch_size, out_shape, input_dims, ksize, 
                    stride, padding, dilation, transposed);
            }}
            """)
            if not CUMM_CPU_ONLY_BUILD:
                code.raw(f"""
                else {{
                    ThrustAllocator thrustalloc(allocator);

                    auto tvctx = tv::Context();
                    tvctx.set_cuda_stream(reinterpret_cast<cudaStream_t>(stream_int));

                    auto indice_pairs_uniq_guard = allocator.empty_guard({{int64_t(pair.numel() + 1)}}, indice_uniq_dtype, 0);
                    auto indice_pairs_uniq = indice_pairs_uniq_guard->tensor;
                    auto indice_pairs_uniq_bkp_guard = allocator.empty_guard({{int64_t(pair.numel() + 1)}}, indice_uniq_dtype, 0);

                    generate_conv_inds_stage1(indices, pair, indice_pairs_uniq,
                        indice_num_per_loc, batch_size, out_shape, input_dims, ksize,
                        stride, padding, dilation, transposed, stream_int);
                    indice_pairs_uniq_bkp_guard->tensor.copy_(indice_pairs_uniq, tvctx);

                    // TODO pytorch unique may be faster?
                    int num_act_out = apply_thrust_unique_to_indice_pairs_uniq(indice_pairs_uniq, thrustalloc, stream_int) - 1;
                    indice_pairs_uniq = indice_pairs_uniq.slice_first_axis(0, num_act_out);
                    out_inds = allocator.empty({pccm.literal(SpconvAllocatorKeys.OutIndices)}, 
                        {{num_act_out, indices.dim(1)}}, indices.dtype(), 0);
                    ExternalAllocator::guard_t hash_k_guard, hash_v_gurad, hash_kv_gurad;
                    tv::Tensor hash_k, hash_v;
                    if (use_int64_hash_k){{
                        hash_k_guard = allocator.empty_guard({{num_act_out * 2}}, tv::int64, 0);
                        hash_v_gurad = allocator.empty_guard({{num_act_out * 2}}, tv::int32, 0);
                        hash_k = hash_k_guard->tensor;
                        hash_v = hash_v_gurad->tensor;
                    }}else{{
                        hash_kv_gurad = allocator.empty_guard({{2, num_act_out * 2}}, tv::int32, 0);
                        hash_k = hash_kv_gurad->tensor[0];
                        hash_v = hash_kv_gurad->tensor[1];
                    }}
                    generate_conv_inds_stage2(indices, hash_k, hash_v, pair,
                        indice_pairs_uniq, indice_pairs_uniq_bkp_guard->tensor, 
                        out_inds, num_act_out,
                        batch_size, out_shape, input_dims, ksize, stride, padding, dilation,
                        transposed, stream_int);
                }}
                """)
            else:
                code.raw(f"""
                else {{
                    TV_THROW_RT_ERR("not implemented for CPU ONLY build.")
                }}
                """)
        code.raw(f"""
        return;
        """)
        return code


    @pccm.pybind.mark 
    @pccm.static_function
    def test_allocator(self):
        code = pccm.code()
        code.arg("allocator", "ExternalAllocator&")
        code.raw(f"""
        auto guard = allocator.zeros_guard({{1, 2, 3}}, tv::int32, 0);
        tv::ssprint("????");
        """)
        return code 