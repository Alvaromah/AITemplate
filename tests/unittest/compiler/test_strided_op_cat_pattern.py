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
import itertools
import logging
import os
import unittest

from typing import List, Optional

import numpy as np
import torch

from aitemplate.compiler import compile_model, ops
from aitemplate.compiler.base import IntVar
from aitemplate.compiler.ops.common.epilogue import FuncEnum
from aitemplate.frontend import IntImm, Tensor
from aitemplate.testing import detect_target
from aitemplate.testing.test_utils import get_random_torch_tensor
from aitemplate.utils import graph_utils, shape_utils


class StridedOpCatPatternTestCase(unittest.TestCase):
    def __init__(self, *args, **kwargs):
        super(StridedOpCatPatternTestCase, self).__init__(*args, **kwargs)
        self._test_id = 0

    def _fused_elementwise_e2e_helper(
        self,
        batch0_sizes: List[int],
        batch1_sizes: List[int],
        m1: int,
        m2: int,
        m3: int,
        k: int,
    ):
        # Construct one graph with 2 fused_elementwises + 1 cat.
        batch0_dim = shape_utils.gen_int_var_min_max(batch0_sizes, "batch_0")
        batch1_dim = shape_utils.gen_int_var_min_max(batch1_sizes, "batch_1")

        X1 = Tensor(
            shape=[batch0_dim, batch1_dim, IntImm(m1), IntImm(k)],
            dtype="float16",
            name="input0",
            is_input=True,
        )
        X2 = Tensor(
            shape=[],
            dtype="float16",
            name="X2",
            value=3.0,
        )
        X3 = Tensor(
            shape=[batch0_dim, batch1_dim, IntImm(m2), IntImm(k)],
            dtype="float16",
            name="input1",
            is_input=True,
        )
        X9 = Tensor(
            shape=[batch0_dim, batch1_dim, IntImm(m3), IntImm(k)],
            dtype="float16",
            name="input2",
            is_input=True,
        )

        X4 = ops.elementwise(FuncEnum.ADD)(X1, X2)
        X5 = ops.elementwise(FuncEnum.TANH)(X4)
        X6 = ops.elementwise(FuncEnum.TANH)(X3)
        X7 = ops.concatenate()([X5, X6, X9], dim=2)
        X8 = ops.reshape()(X7, [-1, (m1 + m2 + m3) * k])
        X8._attrs["name"] = "output0"
        X8._attrs["is_output"] = True

        # Gen module.
        target = detect_target()
        with compile_model(
            [X8],
            target,
            "./tmp",
            "fused_elementwise_cat_m1_{}_m2_{}_m3_{}_k_{}".format(m1, m2, m3, k),
        ) as module:
            # Verify the generated graph.
            sorted_graph = module.debug_sorted_graph
            self.assertEqual(len(sorted_graph), 5)
            sorted_ops = graph_utils.get_sorted_ops(sorted_graph)
            self.assertEqual(len(sorted_ops), 4)

            # Run PyTorch baseline.
            for sizes in itertools.product(batch0_sizes, batch1_sizes):
                x1_pt = torch.randn(sizes[0], sizes[1], m1, k).cuda().half()
                x3_pt = torch.randn(sizes[0], sizes[1], m2, k).cuda().half()
                x9_pt = torch.randn(sizes[0], sizes[1], m3, k).cuda().half()
                x5_pt = torch.tanh(x1_pt + 3.0)
                x6_pt = torch.tanh(x3_pt)
                x7_pt = torch.cat([x5_pt, x6_pt, x9_pt], dim=2)
                x8_pt = torch.reshape(x7_pt, [-1, (m1 + m2 + m3) * k])

                # Run AITemplate module.
                inputs = [x1_pt, x3_pt, x9_pt]
                x8 = (
                    torch.empty([sizes[0] * sizes[1], (m1 + m2 + m3) * k]).cuda().half()
                )
                module.run_with_tensors(inputs, [x8])

                # Do comparisons.
                self.assertTrue(torch.allclose(x8, x8_pt, atol=1e-2, rtol=1e-2))

    def test_elementwise(self):
        self._fused_elementwise_e2e_helper(
            batch0_sizes=[1024], batch1_sizes=[2], m1=8, m2=16, m3=8, k=1
        )
        self._fused_elementwise_e2e_helper(
            batch0_sizes=[3], batch1_sizes=[100], m1=16, m2=64, m3=8, k=32
        )

        # Stride alignment tests.
        # half v.s. half
        self._fused_elementwise_e2e_helper(
            batch0_sizes=[100, 30], batch1_sizes=[2], m1=1, m2=1, m3=8, k=1
        )
        # half2 v.s. half
        self._fused_elementwise_e2e_helper(
            batch0_sizes=[30], batch1_sizes=[2, 88, 99], m1=2, m2=3, m3=8, k=1
        )
        # half v.s. half2
        self._fused_elementwise_e2e_helper(
            batch0_sizes=[77, 89, 188], batch1_sizes=[1, 2, 4], m1=3, m2=2, m3=8, k=1
        )
        # half4 v.s. half
        self._fused_elementwise_e2e_helper(
            batch0_sizes=[2], batch1_sizes=[1, 3, 1024], m1=4, m2=5, m3=8, k=1
        )
        # half v.s. half8
        self._fused_elementwise_e2e_helper(
            batch0_sizes=[2], batch1_sizes=[1, 3, 1024], m1=3, m2=8, m3=8, k=1
        )
        # half4 v.s. half2
        self._fused_elementwise_e2e_helper(
            batch0_sizes=[2], batch1_sizes=[1, 3, 1024], m1=4, m2=6, m3=8, k=1
        )
        # half2 v.s. half8
        self._fused_elementwise_e2e_helper(
            batch0_sizes=[2], batch1_sizes=[1, 3, 1024], m1=6, m2=8, m3=8, k=1
        )
        # half4 v.s. half8
        self._fused_elementwise_e2e_helper(
            batch0_sizes=[2], batch1_sizes=[1, 3, 1024], m1=12, m2=16, m3=8, k=1
        )

        # Offset alignment tests.
        # offset alignment = 1
        self._fused_elementwise_e2e_helper(
            batch0_sizes=[2, 59, 88], batch1_sizes=[20], m1=3, m2=4, m3=5, k=1
        )
        # offset alignment = 2
        self._fused_elementwise_e2e_helper(
            batch0_sizes=[2, 59, 88], batch1_sizes=[20], m1=6, m2=8, m3=2, k=1
        )
        # offset alignment = 4
        self._fused_elementwise_e2e_helper(
            batch0_sizes=[2, 59, 88], batch1_sizes=[20], m1=12, m2=16, m3=4, k=1
        )

    def test_elementwise_cat_1(self):
        BATCH_SIZE = 1024
        NUM_FLOAT_FEATURES = 1456

        X1 = Tensor(
            shape=[IntImm(BATCH_SIZE), IntImm(NUM_FLOAT_FEATURES)],
            dtype="float16",
            name="float_features",
            is_input=True,
        )
        X2 = ops.elementwise(FuncEnum.SIGN)(X1)  # Sign
        X3 = ops.elementwise(FuncEnum.ABS)(X1)  # Abs
        X4 = ops.elementwise(FuncEnum.LOGE)(
            ops.elementwise(FuncEnum.ADD)(X3, Tensor(shape=[], value=1.0))
        )  # Log1p
        X5 = ops.elementwise(FuncEnum.MUL)(X2, X4)  # Mul
        X6 = ops.concatenate()([X5, X1], dim=1)  # Concat
        X6._attrs["name"] = "output0"
        X6._attrs["is_output"] = True

        # Gen module.
        target = detect_target()
        with compile_model(
            [X6],
            target,
            "./tmp",
            "test_elementwise_cat_1",
        ) as module:
            float_features = torch.randn(BATCH_SIZE, NUM_FLOAT_FEATURES).cuda().half()
            x1_pt = torch.sign(float_features)  # Sign
            x2_pt = torch.abs(float_features)  # Abs
            x3_pt = torch.log1p(x2_pt)  # Log1p
            x4_pt = x1_pt * x3_pt  # Mul
            x5_pt = torch.cat([x4_pt, float_features], dim=1)  # Concat

            # Run AITemplate module.
            x6 = torch.empty(x5_pt.size()).cuda().half()
            module.run_with_tensors([float_features], [x6])

            # Do comparisons.
            self.assertTrue(torch.allclose(x6, x5_pt, atol=1e-2, rtol=1e-2))

    def _fused_gemm_e2e_helper(self, m: int, k: int, n1: int, n2: int, n3: int):
        # Construct one graph with 3 gemms + 1 cat.
        X1 = Tensor(
            shape=[IntImm(m), IntImm(k)],
            dtype="float16",
            name="X1",
            is_input=True,
        )
        W1 = Tensor(
            shape=[IntImm(n1), IntImm(k)],
            dtype="float16",
            name="W1",
            is_input=True,
        )
        X2 = Tensor(
            shape=[IntImm(m), IntImm(k)],
            dtype="float16",
            name="X2",
            is_input=True,
        )
        W2 = Tensor(
            shape=[IntImm(n2), IntImm(k)],
            dtype="float16",
            name="W2",
            is_input=True,
        )
        B2 = Tensor(
            shape=[IntImm(n2)],
            dtype="float16",
            name="B2",
            is_input=True,
        )
        X3 = Tensor(
            shape=[IntImm(m), IntImm(k)],
            dtype="float16",
            name="X3",
            is_input=True,
        )
        W3 = Tensor(
            shape=[IntImm(k), IntImm(n3)],
            dtype="float16",
            name="W3",
            is_input=True,
        )
        X4 = Tensor(
            shape=[IntImm(m), IntImm(n2)],
            dtype="float16",
            name="X4",
            is_input=True,
        )

        X5 = ops.gemm_rcr()(X1, W1)
        X6 = ops.gemm_rcr_bias()(X2, W2, B2)
        X7 = ops.gemm_rrr()(X3, W3)
        X8 = ops.gemm_rcr_bias_add_add_relu()(X2, W2, B2, X4, X4)
        X9 = ops.concatenate()([X5, X6, X7, X8], dim=1)
        X9._attrs["name"] = "output0"
        X9._attrs["is_output"] = True

        # Gen module.
        target = detect_target()
        with compile_model(
            [X9],
            target,
            "./tmp",
            "fused_gemm_m_{}_k_{}_n1_{}_n2_{}_n3_{}".format(m, k, n1, n2, n3),
        ) as module:

            # Verify the generated graph.
            sorted_graph = module.debug_sorted_graph
            self.assertEqual(len(sorted_graph), 9)
            sorted_ops = graph_utils.get_sorted_ops(sorted_graph)
            self.assertEqual(len(sorted_ops), 4)

            # Run PyTorch baseline.
            x1_pt = torch.randn(m, k).cuda().half()
            w1_pt = torch.randn(n1, k).cuda().half()
            x2_pt = torch.randn(m, k).cuda().half()
            w2_pt = torch.randn(n2, k).cuda().half()
            b2_pt = torch.randn(n2).cuda().half()
            x3_pt = torch.randn(m, k).cuda().half()
            w3_pt = torch.randn(k, n3).cuda().half()
            x4_pt = torch.randn(m, n2).cuda().half()

            x5_pt = torch.nn.functional.linear(x1_pt, w1_pt)
            x6_pt = torch.nn.functional.linear(x2_pt, w2_pt, b2_pt)
            x7_pt = torch.nn.functional.linear(x3_pt, torch.transpose(w3_pt, 0, 1))
            x8_pt = torch.relu(
                torch.nn.functional.linear(x2_pt, w2_pt, b2_pt) + x4_pt + x4_pt
            )

            x9_pt = torch.cat([x5_pt, x6_pt, x7_pt, x8_pt], dim=1)

            # Run AITemplate module.
            inputs = [0] * 8
            name_to_idx = module.get_input_name_to_index_map()
            inputs[name_to_idx["X1"]] = x1_pt
            inputs[name_to_idx["X2"]] = x2_pt
            inputs[name_to_idx["X3"]] = x3_pt
            inputs[name_to_idx["X4"]] = x4_pt

            inputs[name_to_idx["W1"]] = w1_pt
            inputs[name_to_idx["W2"]] = w2_pt
            inputs[name_to_idx["W3"]] = w3_pt

            inputs[name_to_idx["B2"]] = b2_pt

            x9 = torch.empty([m, n1 + n2 + n3 + n2]).cuda().half()
            module.run_with_tensors(inputs, [x9])

            # Do comparisons.
            self.assertTrue(torch.allclose(x9, x9_pt, atol=1e-1, rtol=1e-1))

    def test_gemm(self):
        self._fused_gemm_e2e_helper(m=1024, k=256, n1=5, n2=32, n3=4)
        self._fused_gemm_e2e_helper(m=1024, k=256, n1=32, n2=32, n3=64)
        self._fused_gemm_e2e_helper(m=1024, k=128, n1=16, n2=32, n3=8)
        self._fused_gemm_e2e_helper(m=1024, k=256, n1=8, n2=16, n3=32)

    def _fused_gemm_alignment_e2e_helper(
        self, gemm_op, input_n: int, m: int, k: int, n: int
    ):
        # Construct one graph with 1 input + 1 gemm_bias_add + 1 cat.
        Input1 = Tensor(
            shape=[IntImm(m), IntImm(input_n)],
            dtype="float16",
            name="Input1",
            is_input=True,
        )
        X1 = Tensor(
            shape=[IntImm(m), IntImm(k)],
            dtype="float16",
            name="X1",
            is_input=True,
        )
        W1 = Tensor(
            shape=[IntImm(n), IntImm(k)],
            dtype="float16",
            name="W1",
            is_input=True,
        )
        B1 = Tensor(
            shape=[IntImm(n)],
            dtype="float16",
            name="B1",
            is_input=True,
        )

        gemm_op_kind = gemm_op._attrs["op"]
        if gemm_op_kind == "gemm_rcr_bias_add":
            num_inputs = 5
            X2 = Tensor(
                shape=[IntImm(m), IntImm(n)],
                dtype="float16",
                name="X2",
                is_input=True,
            )
            Y1 = gemm_op(X1, W1, B1, X2)
        elif gemm_op_kind == "gemm_rcr_bias":
            num_inputs = 4
            Y1 = gemm_op(X1, W1, B1)
        else:
            assert 0, f"unsupported gemm kind: {gemm_op_kind}"

        Y = ops.concatenate()([Input1, Y1], dim=1)
        Y._attrs["name"] = "output"
        Y._attrs["is_output"] = True

        # Gen module.
        target = detect_target()
        with compile_model(
            [Y],
            target,
            "./tmp",
            f"fused_{gemm_op_kind}_alignment_input_n_{input_n}_m_{m}_n_{n}_k_{k}",
        ) as module:

            # Verify the generated graph.
            sorted_graph = module.debug_sorted_graph
            if gemm_op_kind == "gemm_rcr_bias_add":
                # 5 inputs + 1 output
                self.assertEqual(len(sorted_graph), num_inputs + 1)
            else:
                # 4 inputs + 1 output
                self.assertEqual(len(sorted_graph), num_inputs + 1)
            sorted_ops = graph_utils.get_sorted_ops(sorted_graph)
            self.assertEqual(len(sorted_ops), 2)
            Y_src_ops = list(Y._attrs["src_ops"])
            np.testing.assert_equal(len(Y_src_ops), 2)
            if Y_src_ops[0]._attrs["op"] == "concatenate":
                concat_op = Y_src_ops[0]
            else:
                concat_op = Y_src_ops[1]
            np.testing.assert_equal(concat_op._attrs["input_masks"], [True, False])

            # Run PyTorch baseline.
            input_pt = torch.randn(m, input_n).cuda().half()
            x1_pt = torch.randn(m, k).cuda().half()
            w1_pt = torch.randn(n, k).cuda().half()
            b1_pt = torch.randn(n).cuda().half()

            y1_pt = torch.nn.functional.linear(x1_pt, w1_pt)
            y1_pt = torch.nn.functional.linear(x1_pt, w1_pt, b1_pt)
            if gemm_op_kind == "gemm_rcr_bias_add":
                x2_pt = torch.randn(m, n).cuda().half()
                y1_pt += x2_pt

            y_pt = torch.cat([input_pt, y1_pt], dim=1)

            # Run AITemplate module.
            inputs = [0] * num_inputs
            name_to_idx = module.get_input_name_to_index_map()
            inputs[name_to_idx["Input1"]] = input_pt
            inputs[name_to_idx["X1"]] = x1_pt
            if gemm_op_kind == "gemm_rcr_bias_add":
                inputs[name_to_idx["X2"]] = x2_pt
            inputs[name_to_idx["W1"]] = w1_pt
            inputs[name_to_idx["B1"]] = b1_pt

            y = torch.empty([m, input_n + n]).cuda().half()
            module.run_with_tensors(inputs, [y])

            # Do comparisons.
            self.assertTrue(torch.allclose(y, y_pt, atol=1e-1, rtol=1e-1))

    def test_gemm_alignment(self):
        self._fused_gemm_alignment_e2e_helper(
            gemm_op=ops.gemm_rcr_bias_add(), input_n=1, m=2, k=2, n=4
        )
        self._fused_gemm_alignment_e2e_helper(
            gemm_op=ops.gemm_rcr_bias_add(), input_n=2, m=4, k=8, n=1
        )
        self._fused_gemm_alignment_e2e_helper(
            gemm_op=ops.gemm_rcr_bias_add(), input_n=4, m=4, k=4, n=2
        )
        self._fused_gemm_alignment_e2e_helper(
            gemm_op=ops.gemm_rcr_bias_add(), input_n=7, m=4, k=4, n=8
        )

    # Tests to ensure that we correctly update epilogue alignment values
    def test_gemm_update_epilogue_alignment(self):
        # Note that we have to force profiling in ci. Otherwise, we would not
        # be able to fetch cached config.
        target = detect_target()
        old_force_ci = os.environ.get("FORCE_PROFILE", None)
        if target.in_ci_env():
            os.environ["FORCE_PROFILE"] = "1"

        # a smaller epilogue alignment 1
        self._fused_gemm_alignment_e2e_helper(
            gemm_op=ops.gemm_rcr_bias(), input_n=1, m=2, k=2, n=4
        )
        # a larger epilogue alignment 4
        self._fused_gemm_alignment_e2e_helper(
            gemm_op=ops.gemm_rcr_bias(), input_n=4, m=2, k=2, n=4
        )

        # a smaller epilogue alignment 1
        self._fused_gemm_alignment_e2e_helper(
            gemm_op=ops.gemm_rcr_bias_add(), input_n=2, m=3, k=2, n=4
        )
        # a larger epilogue alignment 4
        self._fused_gemm_alignment_e2e_helper(
            gemm_op=ops.gemm_rcr_bias_add(), input_n=4, m=3, k=2, n=4
        )

        # restore old env
        if target.in_ci_env():
            if old_force_ci is None:
                del os.environ["FORCE_PROFILE"]
            else:
                os.environ["FORCE_PROFILE"] = old_force_ci

    def _fused_layernorm_e2e_helper(
        self,
        m: int,
        n1: int,
        n2: int,
        cat_dim: int,
        batch_size: Optional[IntVar] = None,
        gamma_is_none: bool = False,
        beta_is_none: bool = False,
    ):
        logging.info(
            f"_fused_layernorm_e2e: m={m}, n1={n1}, n2={n2}, cat_dim={cat_dim}, batch_size={batch_size}"
            f"gamma_is_none={gamma_is_none}, beta_is_none={beta_is_none}"
        )

        def _maybe_add_batch_size_ait(shape: List[IntVar]) -> List[IntVar]:
            return shape if batch_size is None else [batch_size] + shape

        # Construct one graph with 2 layernorms + 1 cat.
        X1 = Tensor(
            shape=_maybe_add_batch_size_ait([IntImm(m), IntImm(n1)]),
            dtype="float16",
            name="X1",
            is_input=True,
        )
        if gamma_is_none:
            GAMMA1 = None
        else:
            GAMMA1 = Tensor(
                shape=[IntImm(n1)],
                dtype="float16",
                name="gamma1",
                is_input=True,
            )
        if beta_is_none:
            BETA1 = None
        else:
            BETA1 = Tensor(
                shape=[IntImm(n1)],
                dtype="float16",
                name="beta1",
                is_input=True,
            )
        X2 = Tensor(
            shape=_maybe_add_batch_size_ait([IntImm(m), IntImm(n2)]),
            dtype="float16",
            name="X2",
            is_input=True,
        )
        if gamma_is_none:
            GAMMA2 = None
        else:
            GAMMA2 = Tensor(
                shape=[IntImm(n2)],
                dtype="float16",
                name="gamma2",
                is_input=True,
            )
        if beta_is_none:
            BETA2 = None
        else:
            BETA2 = Tensor(
                shape=[IntImm(n2)],
                dtype="float16",
                name="beta2",
                is_input=True,
            )
        X3 = ops.layernorm(normalized_shape=[IntImm(n1)])(X1, GAMMA1, BETA1)
        X4 = ops.elementwise(FuncEnum.SIGMOID)(X3)
        X5 = ops.elementwise(FuncEnum.MUL)(X1, X4)
        X6 = ops.layernorm(normalized_shape=[IntImm(n2)])(X2, GAMMA2, BETA2)
        X7 = ops.concatenate()([X5, X6], dim=cat_dim)
        X7._attrs["is_output"] = True
        X7._attrs["name"] = "output"

        def _maybe_add_batch_size_pt(shape: List[int]) -> List[int]:
            return shape if batch_size is None else [batch_size.upper_bound()] + shape

        # Gen module.
        target = detect_target()
        with compile_model(
            [X7],
            target,
            "./tmp",
            "fused_layernorm",
        ) as module:
            # Verify the generated graph.
            sorted_graph = module.debug_sorted_graph
            num_tensors = 7
            if gamma_is_none:
                num_tensors -= 2
            if beta_is_none:
                num_tensors -= 2
            self.assertEqual(len(sorted_graph), num_tensors)
            sorted_ops = graph_utils.get_sorted_ops(sorted_graph)
            self.assertEqual(len(sorted_ops), 2)

            # Run PyTorch baseline.
            x1_pt = torch.randn(_maybe_add_batch_size_pt([m, n1])).cuda().half()
            if gamma_is_none:
                gamma1_pt = None
            else:
                gamma1_pt = torch.randn(n1).cuda().half()
            if beta_is_none:
                beta1_pt = None
            else:
                beta1_pt = torch.randn(n1).cuda().half()
            x2_pt = torch.randn(_maybe_add_batch_size_pt([m, n2])).cuda().half()
            if gamma_is_none:
                gamma2_pt = None
            else:
                gamma2_pt = torch.randn(n2).cuda().half()
            if beta_is_none:
                beta2_pt = None
            else:
                beta2_pt = torch.randn(n2).cuda().half()

            x3_pt = torch.nn.functional.layer_norm(
                x1_pt, x1_pt.size()[-1:], gamma1_pt, beta1_pt
            )
            x5_pt = torch.mul(x1_pt, torch.sigmoid(x3_pt))
            x6_pt = torch.nn.functional.layer_norm(
                x2_pt, x2_pt.size()[-1:], gamma2_pt, beta2_pt
            )
            x7_pt = torch.cat([x5_pt, x6_pt], dim=cat_dim)

            # Run AITemplate module.
            inputs = [x1_pt]
            if not gamma_is_none:
                inputs.append(gamma1_pt)
            if not beta_is_none:
                inputs.append(beta1_pt)
            inputs.append(x2_pt)
            if not gamma_is_none:
                inputs.append(gamma2_pt)
            if not beta_is_none:
                inputs.append(beta2_pt)
            x7 = torch.empty(x7_pt.size()).cuda().half()
            module.run_with_tensors(inputs, [x7])

            # Do comparisons.
            self.assertTrue(
                torch.allclose(x7, x7_pt, atol=1e-2, rtol=1e-2),
                f"max diff: {torch.max(x7 - x7_pt)}, min diff: {torch.min(x7 - x7_pt)}",
            )

    def test_layernorm(self):
        self._fused_layernorm_e2e_helper(m=1024, n1=256, n2=256, cat_dim=1)
        self._fused_layernorm_e2e_helper(m=1024, n1=4, n2=1, cat_dim=1)
        self._fused_layernorm_e2e_helper(m=1024, n1=1025, n2=1, cat_dim=1)
        self._fused_layernorm_e2e_helper(m=1024, n1=1, n2=256, cat_dim=1)
        self._fused_layernorm_e2e_helper(m=1024, n1=1, n2=1, cat_dim=1)
        self._fused_layernorm_e2e_helper(m=1024, n1=256, n2=256, cat_dim=0)
        self._fused_layernorm_e2e_helper(m=1, n1=256, n2=256, cat_dim=0)

        self._fused_layernorm_e2e_helper(
            m=1024, n1=256, n2=256, cat_dim=1, gamma_is_none=True, beta_is_none=True
        )
        self._fused_layernorm_e2e_helper(
            m=1024, n1=256, n2=256, cat_dim=0, gamma_is_none=True
        )

        # Test alignments.
        # half v.s. half4
        self._fused_layernorm_e2e_helper(m=2, n1=3, n2=128, cat_dim=1)
        self._fused_layernorm_e2e_helper(m=2, n1=128, n2=5, cat_dim=1)

        self._fused_layernorm_e2e_helper(
            m=2, n1=3, n2=128, cat_dim=1, gamma_is_none=True, beta_is_none=True
        )

        # Test w/ batch sizes
        self._fused_layernorm_e2e_helper(
            m=1024, n1=256, n2=256, cat_dim=1, batch_size=IntImm(2)
        )
        self._fused_layernorm_e2e_helper(
            m=1024,
            n1=256,
            n2=256,
            cat_dim=1,
            batch_size=IntVar([1, 10], name="batch_size"),
        )

    def _test_group_layernorm_sigmoid_mul_cat_fusion(
        self,
        input_shapes,
        cat_dim=1,
        gamma_is_none=False,
        beta_is_none=False,
        fuse_sigmoid_mul=True,
        use_group_ops=True,
        num_cat_ops=1,
    ):
        assert num_cat_ops in (1, 2), "Only supports testing with num_cat_ops in (1, 2)"
        testname = (
            f"group_layernorm_sigmoid_mul_{num_cat_ops}_cat_fusion"
            if fuse_sigmoid_mul
            else f"group_layernorm_{num_cat_ops}_cat_fusion"
        )
        logging.info(
            f"{testname}: input_shapes={input_shapes}, cat_dim={cat_dim}, "
            f"gamma_is_none={gamma_is_none}, beta_is_none={beta_is_none}"
        )
        inputs = []
        gammas = []
        betas = []
        normalized_shapes = []
        Ns = []
        for i, shape in enumerate(input_shapes):
            inputs.append(
                Tensor(
                    shape=[
                        IntImm(shape[0]),
                        IntImm(shape[1]),
                    ],
                    dtype="float16",
                    name="X_" + str(i),
                    is_input=True,
                )
            )
            gamma = (
                None
                if gamma_is_none
                else Tensor(
                    shape=[IntImm(shape[1])],
                    dtype="float16",
                    name="gamma_" + str(i),
                    is_input=True,
                )
            )
            gammas.append(gamma)
            beta = (
                None
                if beta_is_none
                else Tensor(
                    shape=[IntImm(shape[1])],
                    dtype="float16",
                    name="beta_" + str(i),
                    is_input=True,
                )
            )
            betas.append(beta)
            normalized_shapes.append([IntImm(shape[1])])
            Ns.append(shape[1])

        Y0s = []
        if use_group_ops:
            op = (
                ops.group_layernorm_sigmoid_mul
                if fuse_sigmoid_mul
                else ops.group_layernorm
            )
            Y0s = op()(inputs, gammas, betas, normalized_shapes)
        else:
            for i in range(len(input_shapes)):
                Y0 = ops.layernorm()(
                    inputs[i], gammas[i], betas[i], normalized_shapes[i]
                )
                if fuse_sigmoid_mul:
                    Y1 = ops.elementwise(FuncEnum.SIGMOID)(Y0)
                    Y2 = ops.elementwise(FuncEnum.MUL)(inputs[i], Y1)
                    Y0s.append(Y2)
                else:
                    Y0s.append(Y0)

        if num_cat_ops == 1:
            Ys = [ops.concatenate()(Y0s, dim=cat_dim)]
        else:
            assert (
                len(input_shapes) % 2 == 0
            ), "len(input_shapes) must be even when num_cat_ops == 2"
            half = len(input_shapes) // 2
            Y1 = ops.concatenate()(Y0s[:half], dim=cat_dim)
            Y2 = ops.concatenate()(Y0s[half:], dim=cat_dim)
            Ys = [Y1, Y2]

        for i, Y in enumerate(Ys):
            Y._attrs["is_output"] = True
            Y._attrs["name"] = f"output_{i}"

        target = detect_target()
        with compile_model(
            Ys,
            target,
            "./tmp",
            f"{testname}_{self._test_id}",
        ) as module:
            self._test_id += 1
            # Verify the generated graph.
            sorted_graph = module.debug_sorted_graph
            num_inputs = 3
            if gamma_is_none:
                num_inputs -= 1
            if beta_is_none:
                num_inputs -= 1
            self.assertEqual(
                len(sorted_graph), num_inputs * len(input_shapes) + num_cat_ops
            )
            sorted_ops = graph_utils.get_sorted_ops(sorted_graph)
            self.assertEqual(len(sorted_ops), 1)

            B = len(input_shapes)

            logging.info(
                f"Run test group_layernorm_sigmoid_mul + {num_cat_ops} cat. Input shapes: {input_shapes}"
            )

            xs_pt = []
            gammas_pt = []
            betas_pt = []
            for shape in input_shapes:
                xs_pt.append(torch.randn(shape).cuda().half())
                gamma_pt = (
                    None if gamma_is_none else torch.randn(shape[1]).cuda().half()
                )
                gammas_pt.append(gamma_pt)
                beta_pt = None if beta_is_none else torch.randn(shape[1]).cuda().half()
                betas_pt.append(beta_pt)

            y0s_pt = []
            for i in range(B):
                y0 = torch.nn.functional.layer_norm(
                    xs_pt[i], xs_pt[i].size()[1:], gammas_pt[i], betas_pt[i]
                )
                if fuse_sigmoid_mul:
                    y = torch.mul(xs_pt[i], torch.sigmoid(y0))
                    y0s_pt.append(y)
                else:
                    y0s_pt.append(y0)
            ys_pt = []
            if num_cat_ops == 1:
                ys_pt = [torch.cat(y0s_pt, dim=cat_dim)]
            else:
                half = len(input_shapes) // 2
                y1_pt = torch.cat(y0s_pt[:half], dim=cat_dim)
                y2_pt = torch.cat(y0s_pt[half:], dim=cat_dim)
                ys_pt = [y1_pt, y2_pt]

            input_name_to_index = module.get_input_name_to_index_map()
            total_num_inputs = len(input_shapes) * num_inputs
            inputs = [0 for i in range(total_num_inputs)]
            for i in range(len(input_shapes)):
                inputs[input_name_to_index[f"X_{i}"]] = xs_pt[i]
                if not gamma_is_none:
                    inputs[input_name_to_index[f"gamma_{i}"]] = gammas_pt[i]
                if not beta_is_none:
                    inputs[input_name_to_index[f"beta_{i}"]] = betas_pt[i]
            ys = []
            for y_pt in ys_pt:
                ys.append(torch.empty(y_pt.size()).cuda().half())
            module.run_with_tensors(inputs, ys)
            for y_pt, y in zip(ys_pt, ys):
                self.assertTrue(
                    torch.allclose(y_pt, y, atol=1e-2, rtol=1e-2),
                    f"max diff: {torch.max(y_pt - y)}, min diff: {torch.min(y_pt - y)}",
                )

    def test_group_layernorm_sigmoid_mul_cat_fusion(self):
        for fuse_sigmoid_mul in (True, False):
            self._test_group_layernorm_sigmoid_mul_cat_fusion(
                [[128, 256]], 0, fuse_sigmoid_mul=fuse_sigmoid_mul
            )
            self._test_group_layernorm_sigmoid_mul_cat_fusion(
                [[128, 256]] * 4, 0, fuse_sigmoid_mul=fuse_sigmoid_mul
            )
            self._test_group_layernorm_sigmoid_mul_cat_fusion(
                [[128, 256]] * 3, 1, fuse_sigmoid_mul=fuse_sigmoid_mul
            )
            self._test_group_layernorm_sigmoid_mul_cat_fusion(
                [[128, 64], [128, 256], [128, 125]],
                1,
                fuse_sigmoid_mul=fuse_sigmoid_mul,
            )
            self._test_group_layernorm_sigmoid_mul_cat_fusion(
                [[10, 64], [10, 64], [10, 64]], 0, fuse_sigmoid_mul=fuse_sigmoid_mul
            )
            self._test_group_layernorm_sigmoid_mul_cat_fusion(
                [[128, 1025], [128, 1276], [128, 1023]],
                1,
                fuse_sigmoid_mul=fuse_sigmoid_mul,
            )

            self._test_group_layernorm_sigmoid_mul_cat_fusion(
                [[128, 256]],
                0,
                gamma_is_none=True,
                beta_is_none=True,
                fuse_sigmoid_mul=fuse_sigmoid_mul,
            )
            self._test_group_layernorm_sigmoid_mul_cat_fusion(
                [[10, 64], [10, 64], [10, 64]],
                0,
                beta_is_none=True,
                fuse_sigmoid_mul=fuse_sigmoid_mul,
            )
            self._test_group_layernorm_sigmoid_mul_cat_fusion(
                [[128, 1025], [128, 1276], [128, 1023]],
                1,
                gamma_is_none=True,
                fuse_sigmoid_mul=fuse_sigmoid_mul,
            )
            self._test_group_layernorm_sigmoid_mul_cat_fusion(
                [[128, 256]] * 6, 0, fuse_sigmoid_mul=fuse_sigmoid_mul, num_cat_ops=2
            )
            self._test_group_layernorm_sigmoid_mul_cat_fusion(
                [[128, 256]] * 6, 1, fuse_sigmoid_mul=fuse_sigmoid_mul, num_cat_ops=2
            )
            # test group layernorm fusion (horizontal fusion)
            self._test_group_layernorm_sigmoid_mul_cat_fusion(
                [[128, 256]] * 6,
                1,
                fuse_sigmoid_mul=fuse_sigmoid_mul,
                use_group_ops=False,
                num_cat_ops=2,
            )
            self._test_group_layernorm_sigmoid_mul_cat_fusion(
                [[128, 256]] * 6,
                1,
                fuse_sigmoid_mul=fuse_sigmoid_mul,
                use_group_ops=False,
            )

    def _test_bmm_cat_fusion(self, B, M, Ns, Ks, cat_dim, testname):
        n = len(Ns)
        Cs = []
        dtype = "float16"

        Xs_pt = []
        Ys_pt = []
        Cs_pt = []
        for i in range(n):
            N = Ns[i]
            K = Ks[i]
            X = Tensor(
                shape=[B, M, K],
                dtype=dtype,
                name=f"X{i}",
                is_input=True,
            )
            Y = Tensor(
                shape=[B, N, K],
                dtype=dtype,
                name=f"Y{i}",
                is_input=True,
            )
            if N > 1:
                C = ops.bmm_rcr()(X, Y)
            else:
                C = ops.bmm_rcr_n1()(X, Y)
            Cs.append(C)

            x = torch.randn(B, M, K).cuda().half()
            y = torch.randn(B, N, K).cuda().half()
            c = torch.bmm(x, y.permute([0, 2, 1]))
            Xs_pt.append(x)
            Ys_pt.append(y)
            Cs_pt.append(c)

        Y = ops.concatenate()(Cs, dim=cat_dim)
        Y._attrs["name"] = "output"
        Y._attrs["is_output"] = True
        y_pt = torch.cat(Cs_pt, dim=cat_dim)

        # Gen module.
        target = detect_target()
        with compile_model(Y, target, "./tmp", testname) as module:
            input_name_to_index = module.get_input_name_to_index_map()
            inputs = [0 for i in range(2 * n)]
            for i in range(n):
                inputs[input_name_to_index[f"X{i}"]] = Xs_pt[i]
                inputs[input_name_to_index[f"Y{i}"]] = Ys_pt[i]
            y = torch.empty(y_pt.size()).cuda().half()
            module.run_with_tensors(inputs, [y])
            self.assertTrue(torch.allclose(y, y_pt, atol=1e-2, rtol=1e-2))

    def test_bmm_cat_fusion(self):
        self._test_bmm_cat_fusion(1, 8, [2, 2, 2], [4, 5, 32], 2, "test_bmm_cat_1")
        self._test_bmm_cat_fusion(1, 16, [1, 1, 1], [32, 16, 32], 1, "test_bmm_cat_2")
        self._test_bmm_cat_fusion(1, 16, [1, 1, 1], [32, 16, 32], 2, "test_bmm_cat_3")
        self._test_bmm_cat_fusion(1, 16, [1, 1, 1], [32, 16, 32], -1, "test_bmm_cat_4")

    def _test_bmm_rcr_update_epilogue_alignment(
        self, bmm_op, input_N, B, M, N, K, testname
    ):
        # create a graph with 1 input + 1 bmm + 1 concat
        cat_dim = -1
        dtype = "float16"

        bmm_op_kind = bmm_op._attrs["op"]
        Input1 = Tensor(
            shape=[IntImm(B), IntImm(M), IntImm(input_N)],
            dtype=dtype,
            name="Input1",
            is_input=True,
        )
        X = Tensor(
            shape=[B, M, K],
            dtype=dtype,
            name="X",
            is_input=True,
        )
        if "rcr" in bmm_op_kind:
            w_shape = [B, N, K]
        elif "rrr" in bmm_op_kind:
            w_shape = [B, K, N]
        else:
            assert 0, f"unsupported {bmm_op_kind}"

        W = Tensor(
            shape=w_shape,
            dtype=dtype,
            name="W",
            is_input=True,
        )
        num_inputs = 3
        if bmm_op_kind.endswith("_add"):
            num_inputs += 1
            X2 = Tensor(
                shape=[IntImm(B), IntImm(M), IntImm(N)],
                dtype="float16",
                name="X2",
                is_input=True,
            )
            C = bmm_op(X, W, X2)
        else:
            C = bmm_op(X, W)

        input1_pt = torch.randn(B, M, input_N).cuda().half()
        x_pt = torch.randn(B, M, K).cuda().half()
        w_pt = torch.randn(*w_shape).cuda().half()
        if num_inputs == 4:
            x2_pt = torch.randn(B, M, N).cuda().half()

        if "rcr" in bmm_op_kind:
            c_pt = torch.bmm(x_pt, w_pt.permute([0, 2, 1]))
        elif "rrr" in bmm_op_kind:
            c_pt = torch.bmm(x_pt, w_pt)

        if num_inputs == 4:
            c_pt = c_pt + x2_pt

        Y = ops.concatenate()([Input1, C], dim=cat_dim)
        Y._attrs["name"] = "output"
        Y._attrs["is_output"] = True
        y_pt = torch.cat([input1_pt, c_pt], dim=cat_dim)

        # Gen module.
        target = detect_target()
        module = compile_model(Y, target, "./tmp", testname)

        input_name_to_index = module.get_input_name_to_index_map()
        inputs = [0] * num_inputs
        inputs[input_name_to_index["Input1"]] = input1_pt
        inputs[input_name_to_index["X"]] = x_pt
        inputs[input_name_to_index["W"]] = w_pt
        if num_inputs == 4:
            inputs[input_name_to_index["X2"]] = x2_pt
        y = torch.empty(y_pt.size()).cuda().half()
        module.run_with_tensors(inputs, [y])
        self.assertTrue(torch.allclose(y, y_pt, atol=1e-2, rtol=1e-2))

    # Test to ensure we update epilogue alignment values
    def test_bmm_rcr_update_epilogue_alignment(self):
        # Note that we have to force profiling in ci. Otherwise, we would not
        # be able to fetch cached config.
        target = detect_target()
        old_force_ci = os.environ.get("FORCE_PROFILE", None)
        if target.in_ci_env():
            os.environ["FORCE_PROFILE"] = "1"

        # a smaller epilogue value 2
        self._test_bmm_rcr_update_epilogue_alignment(
            bmm_op=ops.bmm_rrr_add(),
            input_N=3,
            B=3,
            M=4,
            N=5,
            K=8,
            testname="test_bmm_rcr_epilogue_3",
        )
        # a larger epilogue value 4
        self._test_bmm_rcr_update_epilogue_alignment(
            bmm_op=ops.bmm_rrr_add(),
            input_N=8,
            B=3,
            M=4,
            N=5,
            K=8,
            testname="test_bmm_rcr_epilogue_4",
        )

        # a smaller epilogue value 2
        self._test_bmm_rcr_update_epilogue_alignment(
            bmm_op=ops.bmm_rcr(),
            input_N=2,
            B=3,
            M=5,
            N=4,
            K=8,
            testname="test_bmm_rcr_epilogue_1",
        )
        # a larger epilogue value 4
        self._test_bmm_rcr_update_epilogue_alignment(
            bmm_op=ops.bmm_rcr(),
            input_N=4,
            B=3,
            M=5,
            N=4,
            K=8,
            testname="test_bmm_rcr_epilogue_2",
        )

        # restore old env
        if target.in_ci_env():
            if old_force_ci is None:
                del os.environ["FORCE_PROFILE"]
            else:
                os.environ["FORCE_PROFILE"] = old_force_ci

    def _test_reduce_cat_fusion_1(
        self,
        input_shape,
        reduction_dim,
        keepdim,
        cat_dim,
        new_cat_dim_val,
        test_name,
        input_type="float16",
    ):
        torch.manual_seed(0)
        logging.info(
            f"Test reduce_cat_fusion_1 with input shape {input_shape}, "
            f"reduction_dim {reduction_dim}, and cat_dim {cat_dim}"
        )
        target = detect_target()

        X1 = Tensor(shape=input_shape, dtype=input_type, name="input_1", is_input=True)

        x2_shape = []
        for idx in range(len(input_shape)):
            if idx == reduction_dim:
                if keepdim:
                    x2_shape.append(1)
            else:
                x2_shape.append(input_shape[idx])
        # set concat_dim to a new value for testing
        x2_shape[cat_dim] = new_cat_dim_val
        X2 = Tensor(shape=x2_shape, dtype=input_type, name="input_2", is_input=True)

        reduce_op = ops.reduce_mean(reduction_dim, keepdim=keepdim, dtype=None)
        Y1 = reduce_op(X1)
        Y = ops.concatenate()([Y1, X2], dim=cat_dim)
        Y._attrs["name"] = "output_0"
        Y._attrs["is_output"] = True
        y_shape = [dim._attrs["values"][0] for dim in Y._attrs["shape"]]
        y_dtype = Y._attrs["dtype"]

        logging.info("AITemplate output_shape: {}".format(y_shape))
        logging.info("AITemplate output_type: {}".format(y_dtype))

        with compile_model(Y, target, "./tmp", test_name) as module:
            Y_src_ops = list(Y._attrs["src_ops"])
            np.testing.assert_equal(len(Y_src_ops), 2)
            if Y_src_ops[0]._attrs["op"] == "concatenate":
                concat_op = Y_src_ops[0]
                np.testing.assert_equal(Y_src_ops[1], reduce_op)
            else:
                concat_op = Y_src_ops[1]
                np.testing.assert_equal(Y_src_ops[0], reduce_op)
            np.testing.assert_equal(concat_op._attrs["input_masks"], [False, True])

            X1_pt = get_random_torch_tensor(input_shape, input_type)
            X2_pt = get_random_torch_tensor(x2_shape, input_type)
            Y1_pt = torch.mean(X1_pt, dim=reduction_dim, keepdim=keepdim)
            Y_pt = torch.cat([Y1_pt, X2_pt], dim=cat_dim)

            inputs = [X1_pt, X2_pt]
            y = torch.empty_like(Y_pt)
            module.run_with_tensors(inputs, [y])

            self.assertTrue(
                torch.allclose(Y_pt, y, atol=1e-2, rtol=1e-2, equal_nan=True)
            )

    def test_reduce_cat_fusion_1(self):
        self._test_reduce_cat_fusion_1(
            input_shape=[4, 2],
            reduction_dim=1,
            keepdim=True,
            cat_dim=1,
            new_cat_dim_val=5,
            test_name="test_reduce_cat_1_0",
        )
        self._test_reduce_cat_fusion_1(
            input_shape=[7, 8, 2],
            reduction_dim=2,
            keepdim=True,
            cat_dim=1,
            new_cat_dim_val=4,
            test_name="test_reduce_cat_1_1",
        )
        self._test_reduce_cat_fusion_1(
            input_shape=[7, 5, 2],
            reduction_dim=2,
            keepdim=False,
            cat_dim=1,
            new_cat_dim_val=4,
            test_name="test_reduce_cat_1_2",
        )
        self._test_reduce_cat_fusion_1(
            input_shape=[7, 500, 200],
            reduction_dim=2,
            keepdim=False,
            cat_dim=1,
            new_cat_dim_val=9,
            test_name="test_reduce_cat_1_3",
        )

    def _test_reduce_cat_fusion_2(
        self,
        input_shape,
        reduction_dim,
        keepdim,
        cat_dim,
        new_cat_dim_val,
        test_name,
        input_type="float16",
    ):
        torch.manual_seed(0)
        logging.info(
            f"Test reduce_cat_fusion_1 with input shape {input_shape}, "
            f"reduction_dim {reduction_dim}, and cat_dim {cat_dim}"
        )
        target = detect_target()

        X1 = Tensor(shape=input_shape, dtype=input_type, name="input_1", is_input=True)

        x2_shape = []
        for idx in range(len(input_shape)):
            if idx == reduction_dim:
                if keepdim:
                    x2_shape.append(1)
            else:
                x2_shape.append(input_shape[idx])
        # set concat_dim to a new value for testing
        x2_shape[cat_dim] = new_cat_dim_val
        X2 = Tensor(shape=x2_shape, dtype=input_type, name="input_2", is_input=True)

        reduce_mean_op = ops.reduce_mean(reduction_dim, keepdim=keepdim, dtype=None)
        Y1 = reduce_mean_op(X1)
        reduce_var_op = ops.var(
            dim=reduction_dim, unbiased=True, keepdim=keepdim, dtype=None
        )
        Y2 = reduce_var_op(X1)
        Y3 = ops.concatenate()([X2, Y1, Y2], dim=cat_dim)

        x3_shape = [d._attrs["values"][0] for d in Y3._attrs["shape"]]
        X3 = Tensor(shape=x3_shape, dtype=input_type, name="input_3", is_input=True)

        add_op = ops.elementwise(FuncEnum.ADD)
        Y = add_op(Y3, X3)
        Y._attrs["name"] = "output_0"
        Y._attrs["is_output"] = True
        y_shape = [dim._attrs["values"][0] for dim in Y._attrs["shape"]]
        y_dtype = Y._attrs["dtype"]

        logging.info("AITemplate output_shape: {}".format(y_shape))
        logging.info("AITemplate output_type: {}".format(y_dtype))

        with compile_model(Y, target, "./tmp", test_name) as module:
            Y_src_ops = list(Y._attrs["src_ops"])
            np.testing.assert_equal(len(Y_src_ops), 1)
            fused_add_op = Y_src_ops[0]
            add_op_inputs = fused_add_op._attrs["inputs"]
            if add_op_inputs[0]._attrs["name"] == "input_3":
                concat_op_output = add_op_inputs[1]
            else:
                concat_op_output = add_op_inputs[0]
            Y3_src_ops = list(concat_op_output._attrs["src_ops"])
            np.testing.assert_equal(len(Y3_src_ops), 3)
            if Y3_src_ops[0]._attrs["op"] == "concatenate":
                concat_op = Y3_src_ops[0]
            elif Y3_src_ops[1]._attrs["op"] == "concatenate":
                concat_op = Y3_src_ops[1]
            elif Y3_src_ops[2]._attrs["op"] == "concatenate":
                concat_op = Y3_src_ops[2]
            np.testing.assert_equal(
                concat_op._attrs["input_masks"], [True, False, False]
            )

            X1_pt = get_random_torch_tensor(input_shape, input_type)
            X2_pt = get_random_torch_tensor(x2_shape, input_type)
            X3_pt = get_random_torch_tensor(x3_shape, input_type)
            Y1_pt = torch.mean(X1_pt, dim=reduction_dim, keepdim=keepdim)
            Y2_pt = torch.var(X1_pt, dim=reduction_dim, unbiased=True, keepdim=keepdim)
            Y3_pt = torch.cat([X2_pt, Y1_pt, Y2_pt], dim=cat_dim)
            Y_pt = Y3_pt + X3_pt
            inputs = [X1_pt, X2_pt, X3_pt]
            y = torch.empty_like(Y_pt)
            module.run_with_tensors(inputs, [y])
            self.assertTrue(
                torch.allclose(Y_pt, y, atol=1e-2, rtol=1e-2, equal_nan=True)
            )

    def test_reduce_cat_fusion_2(self):
        self._test_reduce_cat_fusion_2(
            input_shape=[10, 22, 16],
            reduction_dim=2,
            keepdim=True,
            cat_dim=2,
            new_cat_dim_val=5,
            test_name="test_reduce_cat_2_0",
        )
        self._test_reduce_cat_fusion_2(
            input_shape=[10, 22, 16],
            reduction_dim=1,
            keepdim=False,
            cat_dim=1,
            new_cat_dim_val=5,
            test_name="test_reduce_cat_2_1",
        )
        self._test_reduce_cat_fusion_2(
            input_shape=[1, 130, 1],
            reduction_dim=2,
            keepdim=True,
            cat_dim=2,
            new_cat_dim_val=1,
            test_name="test_reduce_cat_2_2",
        )
        self._test_reduce_cat_fusion_2(
            input_shape=[1, 1000000, 6],
            reduction_dim=2,
            keepdim=True,
            cat_dim=2,
            new_cat_dim_val=1,
            test_name="test_reduce_cat_2_3",
        )
        self._test_reduce_cat_fusion_2(
            input_shape=[3, 10000, 5],
            reduction_dim=2,
            keepdim=True,
            cat_dim=2,
            new_cat_dim_val=4,
            test_name="test_reduce_cat_2_4",
        )

    def _test_reduce_cat_fusion_3(
        self,
        input_shape,
        reduction_dim,
        keepdim,
        cat_dim,
        new_cat_dim_val,
        test_name,
        input_type="float16",
    ):
        torch.manual_seed(0)
        logging.info(
            f"Test reduce_cat_fusion_3 with input shape {input_shape}, "
            f"reduction_dim {reduction_dim}, and cat_dim {cat_dim}"
        )
        target = detect_target()

        X1 = Tensor(shape=input_shape, dtype=input_type, name="input_1", is_input=True)

        x2_shape = []
        for idx in range(len(input_shape)):
            if idx == reduction_dim:
                if keepdim:
                    x2_shape.append(1)
            else:
                x2_shape.append(input_shape[idx])
        # set concat_dim to a new value for testing
        x2_shape[cat_dim] = new_cat_dim_val
        X2 = Tensor(shape=x2_shape, dtype=input_type, name="input_2", is_input=True)

        reduce_op = ops.reduce_mean(reduction_dim, keepdim=keepdim, dtype=None)
        Y1 = reduce_op(X1)
        Y = ops.concatenate()([X2, Y1, X2], dim=cat_dim)
        Y._attrs["name"] = "output_0"
        Y._attrs["is_output"] = True
        y_shape = [dim._attrs["values"][0] for dim in Y._attrs["shape"]]
        y_dtype = Y._attrs["dtype"]

        logging.info("AITemplate output_shape: {}".format(y_shape))
        logging.info("AITemplate output_type: {}".format(y_dtype))

        with compile_model(Y, target, "./tmp", test_name) as module:
            Y_src_ops = list(Y._attrs["src_ops"])
            np.testing.assert_equal(len(Y_src_ops), 2)
            if Y_src_ops[0]._attrs["op"] == "concatenate":
                concat_op = Y_src_ops[0]
                np.testing.assert_equal(Y_src_ops[1], reduce_op)
            else:
                concat_op = Y_src_ops[1]
                np.testing.assert_equal(Y_src_ops[0], reduce_op)
            np.testing.assert_equal(
                concat_op._attrs["input_masks"], [True, False, True]
            )

            X1_pt = get_random_torch_tensor(input_shape, input_type)
            X2_pt = get_random_torch_tensor(x2_shape, input_type)
            Y1_pt = torch.mean(X1_pt, dim=reduction_dim, keepdim=keepdim)
            Y_pt = torch.cat([X2_pt, Y1_pt, X2_pt], dim=cat_dim)

            inputs = [X1_pt, X2_pt]
            y = torch.empty_like(Y_pt)
            module.run_with_tensors(inputs, [y])

            self.assertTrue(
                torch.allclose(Y_pt, y, atol=1e-2, rtol=1e-2, equal_nan=True)
            )

    def test_reduce_cat_fusion_3(self):
        self._test_reduce_cat_fusion_3(
            input_shape=[10, 22, 16],
            reduction_dim=1,
            keepdim=True,
            cat_dim=0,
            new_cat_dim_val=5,
            test_name="test_reduce_cat_3_0",
        )
        self._test_reduce_cat_fusion_3(
            input_shape=[3, 11, 16],
            reduction_dim=2,
            keepdim=False,
            cat_dim=0,
            new_cat_dim_val=10,
            test_name="test_reduce_cat_3_1",
        )

    def _test_reduce_cat_fusion_batch(
        self,
        batch_sizes,
        input_shape,
        reduction_dim,
        keepdim,
        cat_dim,
        new_cat_dim_val,
        test_name,
        input_type="float16",
    ):
        torch.manual_seed(0)
        logging.info(
            f"Test reduce_cat_fusion_1 with input shape {input_shape}, "
            f"reduction_dim {reduction_dim}, and cat_dim {cat_dim}"
        )
        target = detect_target()
        batch_dim_name = "input_batch"
        batch_dim = shape_utils.gen_int_var_min_max(
            values=batch_sizes, name=batch_dim_name
        )

        X1 = Tensor(
            shape=[batch_dim, *input_shape],
            dtype=input_type,
            name="input_1",
            is_input=True,
        )

        x2_shape = []
        for idx in range(len(input_shape)):
            if idx == reduction_dim:
                if keepdim:
                    x2_shape.append(1)
            else:
                x2_shape.append(input_shape[idx])
        assert (
            cat_dim != 0
        ), f"cat_dim is not allowed to be 0 in this test but got {cat_dim}"
        # set concat_dim to a new value for testing
        x2_shape[cat_dim - 1] = new_cat_dim_val
        X2 = Tensor(
            shape=[batch_dim, *x2_shape],
            dtype=input_type,
            name="input_2",
            is_input=True,
        )

        ord_kind = 2
        reduce_op = ops.vector_norm(
            ord_kind=ord_kind, dim=reduction_dim, keepdim=keepdim
        )
        Y1 = reduce_op(X1)
        Y = ops.concatenate()([Y1, X2], dim=cat_dim)
        Y._attrs["name"] = "output_0"
        Y._attrs["is_output"] = True
        y_dtype = Y._attrs["dtype"]

        logging.info("AITemplate output_type: {}".format(y_dtype))

        with compile_model(Y, target, "./tmp", test_name) as module:
            Y_src_ops = list(Y._attrs["src_ops"])
            np.testing.assert_equal(len(Y_src_ops), 2)
            if Y_src_ops[0]._attrs["op"] == "concatenate":
                concat_op = Y_src_ops[0]
                np.testing.assert_equal(Y_src_ops[1], reduce_op)
            else:
                concat_op = Y_src_ops[1]
                np.testing.assert_equal(Y_src_ops[0], reduce_op)
            np.testing.assert_equal(concat_op._attrs["input_masks"], [False, True])

            for batch in batch_sizes:
                X1_pt = get_random_torch_tensor([batch, *input_shape], input_type)
                X2_pt = get_random_torch_tensor([batch, *x2_shape], input_type)
                Y1_pt = torch.linalg.vector_norm(
                    X1_pt, ord=ord_kind, dim=reduction_dim, keepdim=keepdim
                )
                Y_pt = torch.cat([Y1_pt, X2_pt], dim=cat_dim)

                inputs = [X1_pt, X2_pt]
                y = torch.empty_like(Y_pt)
                module.run_with_tensors(inputs, [y])
                self.assertTrue(
                    torch.allclose(Y_pt, y, atol=1e-2, rtol=1e-2, equal_nan=True)
                )

    def test_reduce_cat_fusion_batch(self):
        self._test_reduce_cat_fusion_batch(
            batch_sizes=[5, 20],
            input_shape=[4, 2],
            reduction_dim=2,
            keepdim=True,
            cat_dim=2,
            new_cat_dim_val=5,
            test_name="test_reduce_cat_1_0",
        )

    def test_col_reduce_cat_fusion(self):
        torch.manual_seed(0)
        input_a_shape = [1, 4096]
        input_b_shape = [1, 250, 256]
        input_type = "float16"
        reduction_dim = 1
        cat_dim = -1
        test_name = "test_col_reduce_sum_cat"

        target = detect_target()
        A = Tensor(shape=input_a_shape, dtype=input_type, name="input_a", is_input=True)
        B = Tensor(shape=input_b_shape, dtype=input_type, name="input_b", is_input=True)

        X = ops.reduce_sum(dim=reduction_dim)(B)
        Y = ops.concatenate()([A, X], dim=cat_dim)
        Y._attrs["name"] = "output"
        Y._attrs["is_output"] = True

        module = compile_model(Y, target, "./tmp", test_name)
        sorted_graph = module.debug_sorted_graph
        sorted_ops = graph_utils.get_sorted_ops(sorted_graph)
        self.assertEqual(len(sorted_ops), 2)
        concat_op = sorted_ops[1]
        np.testing.assert_equal(concat_op._attrs["input_masks"], [True, True])

        a_pt = get_random_torch_tensor(input_a_shape, input_type)
        b_pt = get_random_torch_tensor(input_b_shape, input_type)
        x_pt = torch.sum(b_pt, dim=reduction_dim)
        y_pt = torch.cat([a_pt, x_pt], dim=cat_dim)

        y = torch.empty(y_pt.size()).cuda().half()
        inputs = {"input_a": a_pt, "input_b": b_pt}
        module.run_with_tensors(inputs, [y])
        y_pt = y_pt.cpu().numpy()

        np.testing.assert_allclose(y_pt, y.cpu().numpy(), atol=0.05, rtol=0.05)


if __name__ == "__main__":
    unittest.main()
