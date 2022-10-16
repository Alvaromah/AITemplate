#  Copyright (c) Meta Platforms, Inc. and affiliates.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#
import logging
import unittest

import numpy as np
import torch

from aitemplate.compiler import compile_model, ops
from aitemplate.compiler.ops.common.epilogue import FuncEnum
from aitemplate.frontend import IntImm, Tensor
from aitemplate.testing import detect_target
from aitemplate.utils import graph_utils, shape_utils

logger = logging.getLogger(__name__)


class StridedScatterTestCase(unittest.TestCase):
    def __init__(self, *args, **kwargs):
        super(StridedScatterTestCase, self).__init__(*args, **kwargs)
        self.test_count = 0

    def _make_tensor(
        self,
        input_shape,
        input_name,
        input_type="float16",
    ):
        x_shape = [
            shape_utils.gen_int_var_min_max(d) if isinstance(d, list) else IntImm(d)
            for d in input_shape
        ]
        X = Tensor(shape=x_shape, dtype=input_type, name=input_name, is_input=True)
        return X

    def _make_add(
        self, input_shape, input_0_name, input_1_name, output_name, input_type="float16"
    ):
        input_add_shape = [
            shape_utils.gen_int_var_min_max(d) if isinstance(d, list) else IntImm(d)
            for d in input_shape
        ]
        input_Add_0 = Tensor(
            shape=input_add_shape,
            dtype=input_type,
            name=input_0_name,
            is_input=True,
        )
        input_Add_1 = Tensor(
            shape=input_add_shape,
            dtype=input_type,
            name=input_1_name,
            is_input=True,
        )
        add_output = ops.elementwise(FuncEnum.ADD)(input_Add_0, input_Add_1)
        add_output._attrs["name"] = output_name
        return add_output

    def _make_slice_ops(
        self,
        input_shapes,
        input_tensors,
        start_indices,
        end_indices,
        input_type="float16",
    ):
        Ys = []
        for idx, (input_shape, input_tensor, s_indices, e_indices) in enumerate(
            zip(input_shapes, input_tensors, start_indices, end_indices)
        ):
            slice_op = ops.dynamic_slice()
            if input_tensor is not None:
                X = input_tensor
            else:
                X_name = f"input_{idx}"
                X = self._make_tensor(input_shape, X_name)
            Y = slice_op(X, start_indices=s_indices, end_indices=e_indices)
            Ys.append(Y)
        return Ys

    # all slice ops take input tensors
    def _test_strided_scatter_basic(
        self,
        input_shapes,
        start_indices,
        end_indices,
        scatter_dim,
        test_name,
    ):
        logger.info(
            f"test_strided_scatter_basic with {input_shapes}, "
            f"{start_indices}, {end_indices}"
        )

        input_tensors = [None] * len(input_shapes)
        slice_outputs = self._make_slice_ops(
            input_shapes,
            input_tensors,
            start_indices,
            end_indices,
        )
        concat_op = ops.concatenate()
        Y = concat_op(slice_outputs, scatter_dim)
        Y._attrs["name"] = "output_0"
        Y._attrs["is_output"] = True

        target = detect_target()
        dll_name = f"test_{self.test_count}.so"
        module = compile_model(Y, target, "./tmp", test_name, dll_name=dll_name)

        # Verify the generated graph.
        sorted_graph = module.debug_sorted_graph
        # len(inputs) + 1 output
        self.assertEqual(len(sorted_graph), len(input_shapes) + 1)
        sorted_ops = graph_utils.get_sorted_ops(sorted_graph)
        self.assertEqual(len(sorted_ops), 1)
        fused_op = sorted_ops[0]
        self.assertEqual(fused_op._attrs["op"], "concatenate")

        # Run PyTorch
        slice_outputs_pt = []
        xs_pt = []
        for input_shape, s_indices, e_indices in zip(
            input_shapes, start_indices, end_indices
        ):
            x_pt = torch.randn(input_shape).cuda().half()
            xs_pt.append(x_pt)
            slice_indices = [slice(i, j) for i, j in zip(s_indices, e_indices)]
            slice_output_pt = x_pt[slice_indices]
            slice_outputs_pt.append(slice_output_pt)
        y_pt = torch.cat(slice_outputs_pt, scatter_dim)

        # run ait
        input_name_to_index = module.get_input_name_to_index_map()
        inputs = [0 for i in range(len(xs_pt))]
        for i, x_pt in enumerate(xs_pt):
            inputs[input_name_to_index[f"input_{i}"]] = x_pt
        y = torch.empty(y_pt.size()).cuda().half()
        module.run_with_tensors(inputs, [y])
        self.assertTrue(torch.allclose(y_pt, y, atol=1e-2, rtol=1e-2))
        self.test_count += 1

    def test_strided_scatter_basic(self):
        self._test_strided_scatter_basic(
            input_shapes=([2], [3]),
            start_indices=([1], [2]),
            end_indices=([2], [-1]),
            scatter_dim=0,
            test_name="strided_scatter_basic",
        )
        self._test_strided_scatter_basic(
            input_shapes=([3, 10], [3, 10]),
            start_indices=([0, 4], [0, 2]),
            end_indices=([None, 6], [None, 4]),
            scatter_dim=1,
            test_name="strided_scatter_basic",
        )
        self._test_strided_scatter_basic(
            input_shapes=([10, 8], [20, 8]),
            start_indices=([1, 0], [4, 0]),
            end_indices=([2, None], [8, None]),
            scatter_dim=0,
            test_name="strided_scatter_basic",
        )
        self._test_strided_scatter_basic(
            input_shapes=([10, 30, 20], [10, 8, 20], [10, 10, 20]),
            start_indices=([0, 5, 0], [0, 6, 0], [0, 1, 0]),
            end_indices=([None, 6, None], [None, 8, None], [None, 4, None]),
            scatter_dim=1,
            test_name="strided_scatter_basic",
        )

    def _test_strided_scatter_dynamic(
        self,
        input_shapes,
        start_indices,
        end_indices,
        scatter_dim,
        test_name,
        make_slices=None,
    ):
        logger.info(
            f"test_strided_scatter_dynamic with {input_shapes}, "
            f"{start_indices}, {end_indices}"
        )

        input_tensors = [None] * len(input_shapes)
        if make_slices is not None:
            assert len(input_shapes) == len(make_slices), (
                "Expected input_shapes and make_slices to have the smae length"
                f" but got {len(input_shapes)} and {len(make_slices)}"
            )
            for idx, (input_shape, make_slice) in enumerate(
                zip(input_shapes, make_slices)
            ):
                if not make_slice:
                    input_name = f"input_{idx}"
                    input_tensors[idx] = self._make_tensor(input_shape, input_name)
        slice_outputs = self._make_slice_ops(
            input_shapes,
            input_tensors,
            start_indices,
            end_indices,
        )
        concat_op = ops.concatenate()
        Y = concat_op(slice_outputs, scatter_dim)
        Y._attrs["name"] = "output_0"
        Y._attrs["is_output"] = True

        target = detect_target()
        dll_name = f"test_{self.test_count}.so"
        module = compile_model(Y, target, "./tmp", test_name, dll_name=dll_name)

        # Verify the generated graph.
        sorted_graph = module.debug_sorted_graph
        # len(inputs) + 1 output
        self.assertEqual(len(sorted_graph), len(input_shapes) + 1)
        sorted_ops = graph_utils.get_sorted_ops(sorted_graph)
        self.assertEqual(len(sorted_ops), 1)
        fused_op = sorted_ops[0]
        self.assertEqual(fused_op._attrs["op"], "concatenate")

        for d in input_shapes[0]:
            if isinstance(d, list):
                Ms = d
                break
        assert Ms is not None, "expected to have at least one dynamic dim"
        for idx in range(len(Ms)):
            # Run PyTorch
            slice_outputs_pt = []
            xs_pt = []
            for input_shape, s_indices, e_indices in zip(
                input_shapes, start_indices, end_indices
            ):
                input_shape_pt = [
                    d[idx] if isinstance(d, list) else d for d in input_shape
                ]
                x_pt = torch.randn(*input_shape_pt).cuda().half()
                xs_pt.append(x_pt)
                slice_indices = [slice(i, j) for i, j in zip(s_indices, e_indices)]
                slice_output_pt = x_pt[slice_indices]
                slice_outputs_pt.append(slice_output_pt)
            y_pt = torch.cat(slice_outputs_pt, scatter_dim)

            # run ait
            input_name_to_index = module.get_input_name_to_index_map()
            inputs = [0 for i in range(len(xs_pt))]
            for i, x_pt in enumerate(xs_pt):
                inputs[input_name_to_index[f"input_{i}"]] = x_pt
            y = torch.empty(y_pt.size()).cuda().half()
            module.run_with_tensors(inputs, [y])
            self.assertTrue(torch.allclose(y_pt, y, atol=1e-2, rtol=1e-2))
            self.test_count += 1

    def test_strided_scatter_dynamic(self):
        self._test_strided_scatter_dynamic(
            input_shapes=([[5, 16], 5], [[5, 16], 10]),
            start_indices=([0, 1], [0, 2]),
            end_indices=([None, 3], [None, 10]),
            scatter_dim=1,
            test_name="strided_scatter_dynamic",
        )
        self._test_strided_scatter_dynamic(
            input_shapes=([[5, 16], [10, 20], 4], [[5, 16], [10, 20], 10]),
            start_indices=([0, 0, 2], [0, 0, 2]),
            end_indices=([None, None, 4], [None, None, 10]),
            scatter_dim=2,
            test_name="strided_scatter_dynamic",
        )

    def test_strided_scatter_partial(self):
        self._test_strided_scatter_dynamic(
            input_shapes=([[5, 16], 5], [[5, 16], 10]),
            start_indices=([0, 1], [0, 2]),
            end_indices=([None, 3], [None, 10]),
            scatter_dim=1,
            test_name="strided_scatter_partial",
            make_slices=[True, False],
        )
        self._test_strided_scatter_dynamic(
            input_shapes=(
                [[5, 7], [1, 10], 4],
                [[5, 7], [1, 10], 6],
                [[5, 7], [1, 10], 8],
            ),
            start_indices=([0, 0, 2], [0, 0, 4], [0, 0, 6]),
            end_indices=([None, None, 4], [None, None, 6], [None, None, 8]),
            scatter_dim=2,
            test_name="strided_scatter_partial",
            make_slices=[True, False, True],
        )
        self._test_strided_scatter_dynamic(
            input_shapes=(
                [[5, 7], [1, 10], 4],
                [[5, 7], [1, 10], 6],
                [[5, 7], [1, 10], 8],
            ),
            start_indices=([0, 0, 2], [0, 0, 4], [0, 0, 6]),
            end_indices=([None, None, 4], [None, None, 6], [None, None, 8]),
            scatter_dim=2,
            test_name="strided_scatter_partial",
            make_slices=[False, False, True],
        )

    def _make_test_graph_multi_dsts_2(
        self,
        input_shapes,
        input_tensors,
        start_indices,
        end_indices,
        scatter_dim,
    ):
        """Make a graph where (1) a tensor is sliced twice and both slices are
        fed into the same concat op, and (2) another sliced output (i.e not
        the one from (1)) is fed into the same concat op twice.
        """

        Ys = self._make_slice_ops(
            input_shapes,
            input_tensors,
            start_indices,
            end_indices,
            scatter_dim,
        )
        slice_op_0 = list(Ys[0].src_ops())[0]
        X0 = slice_op_0._attrs["inputs"][0]
        # make one more slice op that takes the tensor input of the first slice op
        slice_op = ops.dynamic_slice()
        Y0 = slice_op(X0, start_indices=start_indices[0], end_indices=end_indices[0])
        Ys.append(Y0)

        # The last sliced output is fed into concat twice
        Y_1 = Ys[-1]
        Ys.append(Y_1)

        concat_op = ops.concatenate()
        Y = concat_op(Ys, scatter_dim)
        Y._attrs["name"] = "output_0"
        Y._attrs["is_output"] = True

        return Y

    def _test_strided_scatter_multi_dsts_2(
        self,
        input_shapes,
        start_indices,
        end_indices,
        scatter_dim,
        test_name,
    ):
        logger.info(
            f"strided_scatter_multi_dsts_2 with input_shapes: {input_shapes}, "
            f"start_indices: {start_indices}, end_indices: {end_indices}"
        )
        target = detect_target()

        Ys_pt = []
        Xs_pt = []
        for input_shape, s_indices, e_indices in zip(
            input_shapes, start_indices, end_indices
        ):
            X_pt = torch.randn(input_shape).cuda().half()
            Xs_pt.append(X_pt)
            slice_indices = [slice(i, j) for i, j in zip(s_indices, e_indices)]
            Y_pt = X_pt[slice_indices]
            Ys_pt.append(Y_pt)
        X0_pt = Xs_pt[0]
        slice0_indices = [slice(i, j) for i, j in zip(start_indices[0], end_indices[0])]
        Y0_pt = X0_pt[slice0_indices]
        Ys_pt.append(Y0_pt)

        Y1_pt = Ys_pt[-1]
        Ys_pt.append(Y1_pt)

        Y_pt = torch.cat(Ys_pt, scatter_dim)

        input_tensors = [None] * len(input_shapes)
        Y = self._make_test_graph_multi_dsts_2(
            input_shapes, input_tensors, start_indices, end_indices, scatter_dim
        )

        test_name = "strided_scatter_multi_dsts_2"
        dll_name = f"test_{self.test_count}.so"
        module = compile_model(Y, target, "./tmp", test_name, dll_name=dll_name)
        sorted_graph = module.debug_sorted_graph
        # len(inputs) + 1 output
        self.assertEqual(len(sorted_graph), len(input_shapes) + 1)
        sorted_ops = graph_utils.get_sorted_ops(sorted_graph)
        self.assertEqual(len(sorted_ops), 1)
        fused_op = sorted_ops[0]
        self.assertEqual(fused_op._attrs["op"], "concatenate")

        input_name_to_index = module.get_input_name_to_index_map()
        inputs = [0 for i in range(len(Xs_pt))]
        for i, X_pt in enumerate(Xs_pt):
            inputs[input_name_to_index[f"input_{i}"]] = X_pt
        y = torch.empty(Y_pt.size()).cuda().half()
        module.run_with_tensors(inputs, [y])
        self.assertTrue(torch.allclose(Y_pt, y, atol=1e-2, rtol=1e-2))
        self.test_count += 1

    def test_strided_scatter_multi_dsts_2(self):
        self._test_strided_scatter_multi_dsts_2(
            input_shapes=[[3, 3, 10], [3, 7, 10]],
            start_indices=[[0, 1, 0], [0, 1, 0]],
            end_indices=[[None, 2, None], [None, 7, None]],
            scatter_dim=1,
            test_name="strided_scatter_partial",
        )

    def _test_strided_scatter_input_masks(
        self,
        Ms,
        N,
        K,
        input_shapes,
        start_indices,
        end_indices,
        scatter_dim,
        test_name,
        make_slices,
    ):
        # make a graph with 1 gemm_rcr_bias + 1 elemwise + multiple slices -> cat
        logger.info(
            f"test_strided_scatter_input_masks with {input_shapes}, "
            f"{start_indices}, {end_indices}"
        )

        input_A_name = "input_a"
        input_A = self._make_tensor([list(Ms), K], input_A_name)
        input_B_name = "input_b"
        input_B = self._make_tensor([N, K], input_B_name)
        input_Bias_name = "input_bias"
        input_Bias = self._make_tensor([N], input_Bias_name)
        gemm_output = ops.gemm_rcr_bias()(input_A, input_B, input_Bias)
        gemm_output._attrs["name"] = "gemm_output"

        input_Add_0_name = "input_add_0"
        input_Add_1_name = "input_add_1"
        add_output = self._make_add(
            [list(Ms), N], input_Add_0_name, input_Add_1_name, "add_output"
        )
        # A, B, bias, add_0 and add_1
        num_extra_inputs = 5

        input_tensors = [None] * len(input_shapes)
        if make_slices is not None:
            assert len(input_shapes) == len(make_slices), (
                "Expected input_shapes and make_slices to have the smae length"
                f" but got {len(input_shapes)} and {len(make_slices)}"
            )
            for idx, (input_shape, make_slice) in enumerate(
                zip(input_shapes, make_slices)
            ):
                if not make_slice:
                    input_name = f"input_{idx}"
                    input_tensors[idx] = self._make_tensor(input_shape, input_name)
        slice_outputs = self._make_slice_ops(
            input_shapes,
            input_tensors,
            start_indices,
            end_indices,
        )
        concat_inputs = [gemm_output] + slice_outputs + [add_output]
        concat_op = ops.concatenate()
        Y = concat_op(concat_inputs, scatter_dim)
        Y._attrs["name"] = "output_0"
        Y._attrs["is_output"] = True

        target = detect_target()
        dll_name = f"test_{self.test_count}.so"
        module = compile_model(Y, target, "./tmp", test_name, dll_name=dll_name)

        # Verify the generated graph.
        sorted_graph = module.debug_sorted_graph
        # len(inputs) + (A, B, bias, add_0 and add_1) + 1 output
        self.assertEqual(len(sorted_graph), len(input_shapes) + num_extra_inputs + 1)
        sorted_ops = graph_utils.get_sorted_ops(sorted_graph)
        # gemm, add, concat
        self.assertEqual(len(sorted_ops), 3)
        result_concat_op = None
        for op in sorted_ops:
            if op._attrs["op"] == "concatenate":
                result_concat_op = op
                break
        np.testing.assert_equal(result_concat_op is not None, True)
        input_masks = [False] + [True] * len(input_shapes) + [False]
        np.testing.assert_equal(concat_op._attrs["input_masks"], input_masks)

        for idx, M in enumerate(Ms):
            # Run PyTorch
            a_pt = torch.randn(M, K).cuda().half()
            b_pt = torch.randn(N, K).cuda().half()
            bias_pt = torch.randn(N).cuda().half()
            gemm_output_pt = torch.nn.functional.linear(a_pt, b_pt, bias=bias_pt)

            add_0_pt = torch.randn(M, N).cuda().half()
            add_1_pt = torch.randn(M, N).cuda().half()
            add_output_pt = add_0_pt + add_1_pt

            slice_outputs_pt = []
            xs_pt = []
            for input_shape, s_indices, e_indices in zip(
                input_shapes, start_indices, end_indices
            ):
                input_shape_pt = [
                    d[idx] if isinstance(d, list) else d for d in input_shape
                ]
                x_pt = torch.randn(*input_shape_pt).cuda().half()
                xs_pt.append(x_pt)
                slice_indices = [slice(i, j) for i, j in zip(s_indices, e_indices)]
                slice_output_pt = x_pt[slice_indices]
                slice_outputs_pt.append(slice_output_pt)
            cat_inputs_pt = [gemm_output_pt] + slice_outputs_pt + [add_output_pt]
            y_pt = torch.cat(cat_inputs_pt, scatter_dim)

            # run ait
            input_name_to_index = module.get_input_name_to_index_map()
            inputs = [0 for i in range(len(xs_pt) + num_extra_inputs)]
            for i, x_pt in enumerate(xs_pt):
                inputs[input_name_to_index[f"input_{i}"]] = x_pt
            inputs[input_name_to_index["input_a"]] = a_pt
            inputs[input_name_to_index["input_b"]] = b_pt
            inputs[input_name_to_index["input_bias"]] = bias_pt
            inputs[input_name_to_index[input_Add_0_name]] = add_0_pt
            inputs[input_name_to_index[input_Add_1_name]] = add_1_pt
            y = torch.empty(y_pt.size()).cuda().half()
            module.run_with_tensors(inputs, [y])
            self.assertTrue(torch.allclose(y_pt, y, atol=1e-2, rtol=1e-2))
            self.test_count += 1

    def test_strided_scatter_input_masks(self):
        # gemm_output[Ms, N]
        self._test_strided_scatter_input_masks(
            Ms=(5, 16),
            N=4,
            K=10,
            input_shapes=([[5, 16], 5], [[5, 16], 10]),
            start_indices=([0, 1], [0, 2]),
            end_indices=([None, 3], [None, 10]),
            scatter_dim=1,
            test_name="strided_scatter_input_masks",
            make_slices=[True, False],
        )
        self._test_strided_scatter_input_masks(
            Ms=(5, 16),
            N=4,
            K=10,
            input_shapes=([[5, 16], 5], [[5, 16], 10]),
            start_indices=([0, 1], [0, 2]),
            end_indices=([None, 2], [None, 10]),
            scatter_dim=1,
            test_name="strided_scatter_input_masks",
            make_slices=[True, True],
        )

    # one tensor is sliced twice
    def _test_strided_scatter_basic_2(
        self,
        input_shape_0,
        input_shape_2,
        start_indices,
        end_indices,
        scatter_dim,
        test_name,
    ):
        logger.info(f"test_strided_scatter_basic with {start_indices}, {end_indices}")

        input_name_0 = "input_0"
        input_0 = self._make_tensor(input_shape_0, input_name_0)
        input_name_2 = "input_2"
        input_2 = self._make_tensor(input_shape_2, input_name_2)

        input_tensors = [input_2, input_0, input_2]
        input_shapes = [None] * len(input_tensors)
        slice_outputs = self._make_slice_ops(
            input_shapes,
            input_tensors,
            start_indices,
            end_indices,
        )
        concat_op = ops.concatenate()
        Y = concat_op(slice_outputs, scatter_dim)
        Y._attrs["name"] = "output_0"
        Y._attrs["is_output"] = True

        target = detect_target()
        dll_name = f"test_{self.test_count}.so"
        module = compile_model(Y, target, "./tmp", test_name, dll_name=dll_name)

        # Verify the generated graph.
        sorted_graph = module.debug_sorted_graph
        # len(inputs) + 1 output
        self.assertEqual(len(sorted_graph), 2 + 1)
        sorted_ops = graph_utils.get_sorted_ops(sorted_graph)
        self.assertEqual(len(sorted_ops), 1)
        fused_op = sorted_ops[0]
        self.assertEqual(fused_op._attrs["op"], "concatenate")

        # Run PyTorch
        slice_outputs_pt = []
        x0_pt = torch.randn(input_shape_0).cuda().half()
        x2_pt = torch.randn(input_shape_2).cuda().half()
        xs_pt = [x2_pt, x0_pt, x2_pt]
        for x_pt, s_indices, e_indices in zip(xs_pt, start_indices, end_indices):
            slice_indices = [slice(i, j) for i, j in zip(s_indices, e_indices)]
            slice_output_pt = x_pt[slice_indices]
            slice_outputs_pt.append(slice_output_pt)
        y_pt = torch.cat(slice_outputs_pt, scatter_dim)

        # run ait
        inputs = {"input_0": x0_pt, "input_2": x2_pt}
        y = torch.empty(y_pt.size()).cuda().half()
        module.run_with_tensors(inputs, [y])
        self.assertTrue(torch.allclose(y_pt, y, atol=1e-2, rtol=1e-2))
        self.test_count += 1

    def test_strided_scatter_basic_2(self):
        self._test_strided_scatter_basic_2(
            input_shape_0=(1, 10),
            input_shape_2=(1, 8),
            start_indices=(
                [0, 0],
                [0, 0],
                [0, 0],
            ),
            end_indices=(
                [None, 2],  # input_2
                [None, 8],  # input_0
                [None, 4],  # input_2
            ),
            scatter_dim=1,
            test_name="strided_scatter_basic_2",
        )

    def _test_strided_scatter_input_masks_2(
        self,
        Ms0,
        N0,
        Ms1,
        N1,
        start_indices,
        end_indices,
        scatter_dim,
        test_name,
    ):
        # make a graph with 2 elemwise + 3 slices where 1 elemwise is sliced twice
        logger.info(
            f"test_strided_scatter_input_masks {start_indices}, {end_indices}"
        )

        add_0_input_name_0 = "add_0_input_0"
        add_0_input_name_1 = "add_0_input_1"
        add_output0 = self._make_add(
            [list(Ms0), N0], add_0_input_name_0, add_0_input_name_1, "add_0_output"
        )
        add_1_input_name_0 = "add_1_input_0"
        add_1_input_name_1 = "add_1_input_1"
        add_output1 = self._make_add(
            [list(Ms1), N1], add_1_input_name_0, add_1_input_name_1, "add_1_output"
        )

        input_tensors = [add_output0, add_output1, add_output0]
        input_shapes = [None] * len(input_tensors)
        slice_outputs = self._make_slice_ops(
            input_shapes,
            input_tensors,
            start_indices,
            end_indices,
        )
        concat_op = ops.concatenate()
        Y = concat_op(slice_outputs, scatter_dim)
        Y._attrs["name"] = "output_0"
        Y._attrs["is_output"] = True

        target = detect_target()
        dll_name = f"test_{self.test_count}.so"
        module = compile_model(Y, target, "./tmp", test_name, dll_name=dll_name)

        # Verify the generated graph.
        sorted_graph = module.debug_sorted_graph
        # 4 adds' inputs + 1 add0 output + 1 add1 output + 1 concat output
        self.assertEqual(len(sorted_graph), 6 + 1)
        sorted_ops = graph_utils.get_sorted_ops(sorted_graph)
        # 2 adds + concat
        self.assertEqual(len(sorted_ops), 3)
        result_concat_op = None
        for op in sorted_ops:
            if op._attrs["op"] == "concatenate":
                result_concat_op = op
                break
        np.testing.assert_equal(result_concat_op is not None, True)
        input_masks = [True, True, True]
        np.testing.assert_equal(concat_op._attrs["input_masks"], input_masks)

        for M0, M1 in zip(Ms0, Ms1):
            # Run PyTorch
            add_0_0_pt = torch.randn(M0, N0).cuda().half()
            add_0_1_pt = torch.randn(M0, N0).cuda().half()
            add_0_output_pt = add_0_0_pt + add_0_1_pt
            add_1_0_pt = torch.randn(M1, N1).cuda().half()
            add_1_1_pt = torch.randn(M1, N1).cuda().half()
            add_1_output_pt = add_1_0_pt + add_1_1_pt

            slice_outputs_pt = []
            xs_pt = [add_0_output_pt, add_1_output_pt, add_0_output_pt]
            for x_pt, s_indices, e_indices in zip(xs_pt, start_indices, end_indices):
                slice_indices = [slice(i, j) for i, j in zip(s_indices, e_indices)]
                slice_output_pt = x_pt[slice_indices]
                slice_outputs_pt.append(slice_output_pt)
            y_pt = torch.cat(slice_outputs_pt, scatter_dim)

            # run ait
            inputs = {
                add_0_input_name_0: add_0_0_pt,
                add_0_input_name_1: add_0_1_pt,
                add_1_input_name_0: add_1_0_pt,
                add_1_input_name_1: add_1_1_pt,
            }
            y = torch.empty(y_pt.size()).cuda().half()
            module.run_with_tensors(inputs, [y])
            self.assertTrue(torch.allclose(y_pt, y, atol=1e-2, rtol=1e-2))
            self.test_count += 1

    def test_strided_scatter_input_masks_2(self):
        self._test_strided_scatter_input_masks_2(
            Ms0=(4, 10),
            N0=6,
            Ms1=(4, 10),
            N1=7,
            start_indices=(
                [0, 0],
                [0, 0],
                [0, 0],
            ),
            end_indices=(
                [None, 2],  # input0
                [None, 5],  # input1
                [None, 4],  # input0
            ),
            scatter_dim=1,
            test_name="strided_scatter_input_masks_2",
        )

    # concatenating a slice op, a split op, and an elementwise op
    def _test_strided_scatter_with_split(
        self,
        add_input_shape,
        split_input_shape,
        slice_input_shapes,
        start_indices,
        end_indices,
        scatter_dim,
        test_name,
    ):
        logger.info(
            f"test_strided_scatter_with_split with {start_indices}, {end_indices}"
        )

        # make add
        input_Add_0_name = "input_add_0"
        input_Add_1_name = "input_add_1"
        add_output = self._make_add(
            add_input_shape, input_Add_0_name, input_Add_1_name, "add_output"
        )

        # make split
        split_input_name = "split_input"
        split_input = self._make_tensor(split_input_shape, split_input_name)
        split_dim_size = split_input_shape[scatter_dim]
        split_outputs = ops.split()(
            split_input, int(split_dim_size / 2), dim=scatter_dim
        )

        slice_input_tensors = [None] * len(slice_input_shapes)
        slice_outputs = self._make_slice_ops(
            slice_input_shapes,
            slice_input_tensors,
            start_indices,
            end_indices,
        )
        concat_inputs = [add_output] + slice_outputs + list(split_outputs)
        concat_op = ops.concatenate()
        Y = concat_op(concat_inputs, scatter_dim)
        Y._attrs["name"] = "output_0"
        Y._attrs["is_output"] = True

        target = detect_target()
        dll_name = f"test_{self.test_count}.so"
        module = compile_model(Y, target, "./tmp", test_name, dll_name=dll_name)

        # Verify the generated graph.
        sorted_graph = module.debug_sorted_graph
        # 2 add inputs + 1 split input + len(slice_inputs) + 1 output
        self.assertEqual(len(sorted_graph), 3 + len(slice_input_shapes) + 1)
        sorted_ops = graph_utils.get_sorted_ops(sorted_graph)
        # add and concat
        self.assertEqual(len(sorted_ops), 2)
        result_concat_op = None
        for op in sorted_ops:
            if op._attrs["op"] == "concatenate":
                result_concat_op = op
                break
        np.testing.assert_equal(result_concat_op is not None, True)
        input_masks = [False, True, True, True]
        np.testing.assert_equal(concat_op._attrs["input_masks"], input_masks)

        # Run PyTorch
        input_add_0_pt = torch.randn(add_input_shape).cuda().half()
        input_add_1_pt = torch.randn(add_input_shape).cuda().half()
        add_output_pt = input_add_0_pt + input_add_1_pt

        split_input_pt = torch.randn(split_input_shape).cuda().half()
        split_outputs_pt = torch.split(
            split_input_pt, int(split_dim_size / 2), dim=scatter_dim
        )

        slice_outputs_pt = []
        xs_pt = []
        for input_shape, s_indices, e_indices in zip(
            slice_input_shapes, start_indices, end_indices
        ):
            x_pt = torch.randn(input_shape).cuda().half()
            xs_pt.append(x_pt)
            slice_indices = [slice(i, j) for i, j in zip(s_indices, e_indices)]
            slice_output_pt = x_pt[slice_indices]
            slice_outputs_pt.append(slice_output_pt)
        cat_inputs_pt = [add_output_pt] + slice_outputs_pt + list(split_outputs_pt)
        y_pt = torch.cat(cat_inputs_pt, scatter_dim)

        inputs = {
            input_Add_0_name: input_add_0_pt,
            input_Add_1_name: input_add_1_pt,
            split_input_name: split_input_pt,
        }
        for i, x_pt in enumerate(xs_pt):
            inputs[f"input_{i}"] = x_pt

        # run ait
        y = torch.empty(y_pt.size()).cuda().half()
        module.run_with_tensors(inputs, [y])
        self.assertTrue(torch.allclose(y_pt, y, atol=1e-2, rtol=1e-2))
        self.test_count += 1

    def test_strided_scatter_with_split(self):
        self._test_strided_scatter_with_split(
            add_input_shape=(4, 10),
            split_input_shape=(4, 9),
            slice_input_shapes=([4, 6], [4, 12]),
            start_indices=([0, 2], [0, 8]),
            end_indices=([None, 4], [None, 12]),
            scatter_dim=1,
            test_name="strided_scatter_with_split",
        )


if __name__ == "__main__":
    unittest.main()
