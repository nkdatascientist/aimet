# /usr/bin/env python3.6
# -*- mode: python -*-
# =============================================================================
#  @@-COPYRIGHT-START-@@
#
#  Copyright (c) 2022, Qualcomm Innovation Center, Inc. All rights reserved.
#
#  Redistribution and use in source and binary forms, with or without
#  modification, are permitted provided that the following conditions are met:
#
#  1. Redistributions of source code must retain the above copyright notice,
#     this list of conditions and the following disclaimer.
#
#  2. Redistributions in binary form must reproduce the above copyright notice,
#     this list of conditions and the following disclaimer in the documentation
#     and/or other materials provided with the distribution.
#
#  3. Neither the name of the copyright holder nor the names of its contributors
#     may be used to endorse or promote products derived from this software
#     without specific prior written permission.
#
#  THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
#  AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
#  IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
#  ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
#  LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
#  CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
#  SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
#  INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
#  CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
#  ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
#  POSSIBILITY OF SUCH DAMAGE.
#
#  SPDX-License-Identifier: BSD-3-Clause
#
#  @@-COPYRIGHT-END-@@
# =============================================================================

import contextlib
from dataclasses import dataclass
import itertools
from unittest.mock import patch, MagicMock
import os
from aimet_torch.qc_quantize_op import StaticGridQuantWrapper
import pytest
import shutil
from typing import Callable
import torch
from torch.utils.data import Dataset, DataLoader

from aimet_torch import utils
from aimet_torch.auto_quant import _AutoQuantV2 as AutoQuant
from aimet_torch.adaround.adaround_weight import AdaroundParameters
from aimet_torch.quantsim import QuantizationSimModel, OnnxExportApiArgs
from aimet_torch.qc_quantize_op import StaticGridQuantWrapper
from aimet_torch.save_utils import SaveUtils
from aimet_common.defs import QuantScheme


class Model(torch.nn.Module):
    """
    Model
    """

    def __init__(self):
        super(Model, self).__init__()
        self._conv_0 = torch.nn.Conv2d(in_channels=3, out_channels=3, kernel_size=(3, 3), padding=1)
        self._relu = torch.nn.ReLU()

        # Test flags
        self.register_buffer("applied_bn_folding", torch.tensor(False, dtype=torch.bool), persistent=True)
        self.register_buffer("applied_cle", torch.tensor(False, dtype=torch.bool), persistent=True)
        self.register_buffer("applied_adaround", torch.tensor(False, dtype=torch.bool), persistent=True)

    def forward(self, x: torch.Tensor):
        # Return the test flags along with the forward pass results so that the test flags
        # don't get discarded when the model is converted to GraphModule by model preparer.
        return self._relu(self._conv_0(x)),\
               self.applied_bn_folding,\
               self.applied_cle,\
               self.applied_adaround


class InvalidModel(Model):
    def forward(self, x):
        # This if statement throws error during model preparer
        # since `x` is a torch.fx.Proxy object which cannot be converted to bool
        if x[0,0,0,0]:
            pass
        return super().forward(x)


@pytest.fixture(scope="session")
def cpu_model():
    return Model().cpu()


@pytest.fixture(scope="session")
def gpu_model():
    return Model().cuda()


@pytest.fixture(scope="session")
def dummy_input():
    return torch.randn((1, 3, 8, 8))


@pytest.fixture(scope="session")
def unlabeled_data_loader(dummy_input):
    class MyDataset(Dataset):
        def __init__(self, data):
            self.data = data

        def __getitem__(self, index):
            return self.data[index]

        def __len__(self):
            return len(self.data)

    dataset = MyDataset([dummy_input[0, :] for _ in range(10)])
    return DataLoader(dataset)


def assert_applied_techniques(
        output_model, acc, encoding_path,
        target_acc, bn_folded_acc, cle_acc, adaround_acc,
):
    # Batchnorm folding is always applied.
    assert output_model.applied_bn_folding

    # If accuracy is good enough after batchnorm folding
    if bn_folded_acc >= target_acc:
        assert acc == bn_folded_acc
        assert encoding_path.endswith("batchnorm_folding.encodings")
        assert not output_model.applied_cle
        assert not output_model.applied_adaround
        return

    # If accuracy is good enough after cle
    if cle_acc >= target_acc:
        assert acc == cle_acc
        assert encoding_path.endswith("cross_layer_equalization.encodings")
        assert output_model.applied_cle
        assert not output_model.applied_adaround
        return

    # CLE should be applied if and only if it brings accuracy gain
    assert output_model.applied_cle == (bn_folded_acc < cle_acc)

    # If accuracy is good enough after adaround
    if adaround_acc >= target_acc:
        assert acc == adaround_acc
        assert encoding_path.endswith("adaround.encodings")
        assert output_model.applied_adaround
        return

    assert acc == max(bn_folded_acc, cle_acc, adaround_acc)

    if max(bn_folded_acc, cle_acc, adaround_acc) == bn_folded_acc:
        assert encoding_path.endswith("batchnorm_folding.encodings")
    elif max(bn_folded_acc, cle_acc, adaround_acc) == cle_acc:
        assert encoding_path.endswith("cross_layer_equalization.encodings")
    else:
        assert encoding_path.endswith("adaround.encodings")


FP32_ACC = .8
W32_ACC = FP32_ACC # Assume W32 accuracy is equal to FP32 accuracy


@contextlib.contextmanager
def patch_ptq_techniques(bn_folded_acc, cle_acc, adaround_acc, fp32_acc=None, w32_acc=None):
    if fp32_acc is None:
        fp32_acc = FP32_ACC

    if w32_acc is None:
        w32_acc = W32_ACC

    const_true = torch.tensor(True, dtype=torch.bool)

    def bn_folding(model: Model, *_, **__):
        model.applied_bn_folding.copy_(const_true)
        return tuple()

    def cle(model: Model, *_, **__):
        model.applied_bn_folding.copy_(const_true)
        model.applied_cle.copy_(const_true)

    def adaround(sim, *_, **__):
        sim.model.applied_adaround.copy_(const_true)
        SaveUtils.remove_quantization_wrappers(sim.model)
        return sim.model

    class _QuantizationSimModel(QuantizationSimModel):
        def compute_encodings(self, *_):
            pass

        def set_and_freeze_param_encodings(self, _):
            pass

    def mock_eval_callback(model, _):
        if not isinstance(model._conv_0, StaticGridQuantWrapper):
            # Not quantized: return fp32 accuracy
            return fp32_acc
        if model._conv_0.param_quantizers["weight"].bitwidth == 32:
            # W32 evaluation for early exit. Return W32 accuracy
            return w32_acc

        acc = bn_folded_acc
        if model.applied_cle:
            acc = cle_acc
        if model.applied_adaround:
            acc = adaround_acc
        return acc

    @dataclass
    class Mocks:
        eval_callback: Callable
        QuantizationSimModel: MagicMock
        fold_all_batch_norms: MagicMock
        equalize_model: MagicMock
        apply_adaround: MagicMock

    with patch("aimet_torch.auto_quant_v2.QuantizationSimModel", side_effect=_QuantizationSimModel) as mock_qsim,\
            patch("aimet_torch.auto_quant_v2.fold_all_batch_norms", side_effect=bn_folding) as mock_bn_folding,\
            patch("aimet_torch.auto_quant_v2.equalize_model", side_effect=cle) as mock_cle,\
            patch("aimet_torch.auto_quant_v2.Adaround._apply_adaround", side_effect=adaround) as mock_adaround:
        try:
            yield Mocks(
                eval_callback=mock_eval_callback,
                QuantizationSimModel=mock_qsim,
                fold_all_batch_norms=mock_bn_folding,
                equalize_model=mock_cle,
                apply_adaround=mock_adaround,
            )
        finally:
            pass


@pytest.fixture(autouse=True)
def patch_dependencies():
    def render(*_, **__):
        return ""

    with patch("aimet_torch.auto_quant_v2.jinja2.environment.Template.render", side_effect=render):
         yield


class TestAutoQuant:
    def test_auto_quant_default_values(self, unlabeled_data_loader):
        auto_quant = AutoQuant(
            allowed_accuracy_drop=0.0,
            unlabeled_dataset_iterable=unlabeled_data_loader,
            eval_callback=MagicMock(),
        )
        assert auto_quant.adaround_params.data_loader is unlabeled_data_loader
        assert auto_quant.adaround_params.num_batches is len(unlabeled_data_loader)

    @pytest.mark.parametrize(
        "bn_folded_acc, cle_acc, adaround_acc",
        itertools.permutations([.5, .6, .7])
    )
    @pytest.mark.parametrize("allowed_accuracy_drop", [.05, .15])
    def test_auto_quant_cpu(
            self, cpu_model, dummy_input, unlabeled_data_loader,
            allowed_accuracy_drop, bn_folded_acc, cle_acc, adaround_acc,
    ):
        self._test_auto_quant(
            cpu_model, dummy_input, unlabeled_data_loader,
            allowed_accuracy_drop, bn_folded_acc, cle_acc, adaround_acc,
        )

    @pytest.mark.parametrize(
        "bn_folded_acc, cle_acc, adaround_acc",
        itertools.permutations([.5, .6, .7])
    )
    @pytest.mark.parametrize("allowed_accuracy_drop", [.05, .15])
    @pytest.mark.cuda
    def test_auto_quant_gpu(
            self, gpu_model, dummy_input, unlabeled_data_loader,
            allowed_accuracy_drop, bn_folded_acc, cle_acc, adaround_acc,
    ):
        self._test_auto_quant(
            gpu_model, dummy_input, unlabeled_data_loader,
            allowed_accuracy_drop, bn_folded_acc, cle_acc, adaround_acc,
        )

    def _test_auto_quant(
            self, model, dummy_input, unlabeled_data_loader,
            allowed_accuracy_drop, bn_folded_acc, cle_acc, adaround_acc,
    ):
        with patch_ptq_techniques(
            bn_folded_acc, cle_acc, adaround_acc
        ) as mocks:
            auto_quant = AutoQuant(
                allowed_accuracy_drop=allowed_accuracy_drop,
                unlabeled_dataset_iterable=unlabeled_data_loader,
                eval_callback=mocks.eval_callback,
            )
            self._do_test_apply_auto_quant(
                auto_quant, model, dummy_input,
                allowed_accuracy_drop, bn_folded_acc, cle_acc, adaround_acc
            )

    def _do_test_apply_auto_quant(
            self, auto_quant, input_model, dummy_input,
            allowed_accuracy_drop, bn_folded_acc, cle_acc, adaround_acc,
    ):
        with create_tmp_directory() as results_dir:
            target_acc = FP32_ACC - allowed_accuracy_drop

            if utils.get_device(input_model) == torch.device("cpu"):
                output_model, acc, encoding_path =\
                    auto_quant.apply(input_model,
                                     dummy_input_on_cpu=dummy_input.cpu(),
                                     results_dir=results_dir,
                                     strict_validation=True)
            else:
                output_model, acc, encoding_path =\
                    auto_quant.apply(input_model,
                                     dummy_input_on_cpu=dummy_input.cpu(),
                                     dummy_input_on_gpu=dummy_input.cuda(),
                                     results_dir=results_dir,
                                     strict_validation=True)

            assert utils.get_device(output_model) == utils.get_device(input_model)
            assert_applied_techniques(
                output_model, acc, encoding_path,
                target_acc, bn_folded_acc, cle_acc, adaround_acc,
            )

    def test_auto_quant_invalid_input(self, unlabeled_data_loader):
        # Allowed accuracy drop < 0
        with pytest.raises(ValueError):
            _ = AutoQuant(-1.0, unlabeled_data_loader, MagicMock(), MagicMock())

        # Bitwidth < 4 or bitwidth > 32
        with pytest.raises(ValueError):
            _ = AutoQuant(0, unlabeled_data_loader, MagicMock(), default_param_bw=2)

        with pytest.raises(ValueError):
            _ = AutoQuant(0, unlabeled_data_loader, MagicMock(), default_param_bw=64)

        with pytest.raises(ValueError):
            _ = AutoQuant(0, unlabeled_data_loader, MagicMock(), default_output_bw=2)

        with pytest.raises(ValueError):
            _ = AutoQuant(0, unlabeled_data_loader, MagicMock(), default_output_bw=64)

    def test_auto_quant_model_preparer(self, unlabeled_data_loader, dummy_input):
        allowed_accuracy_drop = 0.0
        bn_folded_acc, cle_acc, adaround_acc = 40., 50., 60.

        with patch_ptq_techniques(
            bn_folded_acc, cle_acc, adaround_acc
        ) as mocks:
            auto_quant = AutoQuant(
                allowed_accuracy_drop=allowed_accuracy_drop,
                unlabeled_dataset_iterable=unlabeled_data_loader,
                eval_callback=mocks.eval_callback,
            )

            # If strict_validation is True (default), AutoQuant crashes with an exception.
            with pytest.raises(torch.fx.proxy.TraceError):
                auto_quant.apply(InvalidModel(), dummy_input, strict_validation=True)

            # If strict_validation is False, AutoQuant ignores the errors and proceed. 
            auto_quant.apply(InvalidModel(), dummy_input, strict_validation=False)

    @pytest.mark.cuda
    def test_auto_quant_invalid_input_gpu(self, unlabeled_data_loader, dummy_input):
        auto_quant = AutoQuant(0, unlabeled_data_loader, MagicMock())
        # If model is on cuda device, dummy input on gpu should be provided.
        with pytest.raises(ValueError):
            auto_quant.apply(Model().cuda(), dummy_input.cpu(), strict_validation=True)

    def test_auto_quant_fallback(
        self, cpu_model, dummy_input, unlabeled_data_loader,
    ):
        def error_fn(*_, **__):
            raise Exception

        allowed_accuracy_drop = 0.0
        bn_folded_acc, cle_acc, adaround_acc = .4, .5, .6
        with patch_ptq_techniques(
            bn_folded_acc, cle_acc, adaround_acc
        ) as mocks:
            auto_quant = AutoQuant(
                allowed_accuracy_drop=allowed_accuracy_drop,
                unlabeled_dataset_iterable=unlabeled_data_loader,
                eval_callback=mocks.eval_callback,
            )

            with patch("aimet_torch.auto_quant_v2.fold_all_batch_norms", side_effect=error_fn):
                # If batchnorm folding fails, should return Adaround results
                _, acc, _ = auto_quant.apply(cpu_model, dummy_input, strict_validation=False)
                assert acc == adaround_acc

            with patch("aimet_torch.auto_quant_v2.equalize_model", side_effect=error_fn):
                # If CLE fails, should return Adaround results
                _, acc, _ = auto_quant.apply(cpu_model, dummy_input, strict_validation=False)
                assert acc == adaround_acc

            with patch("aimet_torch.auto_quant_v2.Adaround._apply_adaround", side_effect=error_fn):
                # If adaround fails, should return CLE results
                _, acc, _ = auto_quant.apply(cpu_model, dummy_input, strict_validation=False)
                assert acc == cle_acc

            with patch("aimet_torch.auto_quant_v2.fold_all_batch_norms", side_effect=error_fn),\
                    patch("aimet_torch.auto_quant_v2.equalize_model", side_effect=error_fn),\
                    patch("aimet_torch.auto_quant_v2.Adaround._apply_adaround", side_effect=error_fn):
                # If everything fails, should raise an error
                with pytest.raises(RuntimeError):
                    auto_quant.apply(cpu_model, dummy_input, strict_validation=False)

    def test_auto_quant_early_exit(self, cpu_model, dummy_input, unlabeled_data_loader):
        allowed_accuracy_drop = 0.1
        w32_acc = FP32_ACC - (allowed_accuracy_drop * 2)

        with create_tmp_directory() as results_dir:
            with patch_ptq_techniques(
                bn_folded_acc=0, cle_acc=0, adaround_acc=0, w32_acc=w32_acc
            ) as mocks:
                auto_quant = AutoQuant(
                    allowed_accuracy_drop=allowed_accuracy_drop,
                    unlabeled_dataset_iterable=unlabeled_data_loader,
                    eval_callback=mocks.eval_callback,
                )
                output_model, acc, encoding_path =\
                    auto_quant.apply(cpu_model,
                                     dummy_input_on_cpu=dummy_input.cpu(),
                                     results_dir=results_dir)
        assert output_model is None
        assert acc is None
        assert encoding_path is None

    def test_auto_quant_caching(
        self, cpu_model, dummy_input, unlabeled_data_loader,
    ):
        allowed_accuracy_drop = 0.0
        bn_folded_acc, cle_acc, adaround_acc = .4, .5, .6
        with patch_ptq_techniques(
            bn_folded_acc, cle_acc, adaround_acc
        ) as mocks:
            auto_quant = AutoQuant(
                allowed_accuracy_drop=allowed_accuracy_drop,
                unlabeled_dataset_iterable=unlabeled_data_loader,
                eval_callback=mocks.eval_callback,
            )

            with create_tmp_directory() as results_dir:
                cache_id = "unittest"
                cache_files  = [
                    os.path.join(results_dir, ".auto_quant_cache", cache_id, f"{key}.pkl")
                    for key in ("batchnorm_folding", "cle", "adaround")
                ]

                # No previously cached results
                auto_quant.apply(cpu_model, dummy_input, results_dir=results_dir,
                                 cache_id=cache_id, strict_validation=True)

                for cache_file in cache_files:
                    assert os.path.exists(cache_file)

                assert mocks.fold_all_batch_norms.call_count == 1
                assert mocks.equalize_model.call_count == 1
                assert mocks.apply_adaround.call_count == 1

                # Load cached result
                auto_quant.apply(cpu_model, dummy_input, results_dir=results_dir,
                                 cache_id=cache_id, strict_validation=True)

                # PTQ functions should not be called twice.
                assert mocks.fold_all_batch_norms.call_count == 1
                assert mocks.equalize_model.call_count == 1
                assert mocks.apply_adaround.call_count == 1

    def test_auto_quant_scheme_selection(
        self, cpu_model, dummy_input, unlabeled_data_loader,
    ):
        allowed_accuracy_drop = 0.0
        bn_folded_acc, cle_acc, adaround_acc = 40., 50., 60.
        with patch_ptq_techniques(
            bn_folded_acc, cle_acc, adaround_acc
        ) as mocks:
            def eval_callback(model, _):
                # Assumes the model's eval score drops to zero
                # unless param_quant_scheme == tf and output_quant_scheme == tfe
                if isinstance(model._conv_0, StaticGridQuantWrapper):
                    if model._conv_0.param_quantizers["weight"].quant_scheme != QuantScheme.post_training_tf:
                        return 0.0
                    if model._conv_0.output_quantizers[0].quant_scheme != QuantScheme.post_training_tf_enhanced:
                        return 0.0
                return mocks.eval_callback(model, _)

            real_auto_quant_main = AutoQuant._auto_quant_main
            def auto_quant_main_fn(self, *args, **kwargs):
                # Since all the other candidates (tf-tf, tfe-tf, and tfe-tfe) yields zero accuracy,
                # it is expected that tf-tfe is selected as the quant scheme for AutoQuant.
                assert self.default_quant_scheme.param_quant_scheme == QuantScheme.post_training_tf
                assert self.default_quant_scheme.output_quant_scheme == QuantScheme.post_training_tf_enhanced
                return real_auto_quant_main(self, *args, **kwargs)

            with patch("aimet_torch.auto_quant_v2._AutoQuantV2._auto_quant_main", auto_quant_main_fn):
                auto_quant = AutoQuant(
                    allowed_accuracy_drop=allowed_accuracy_drop,
                    unlabeled_dataset_iterable=unlabeled_data_loader,
                    eval_callback=eval_callback,
                )
                auto_quant.apply(cpu_model, dummy_input)

    def test_set_additional_params(self, cpu_model, dummy_input, unlabeled_data_loader):
        allowed_accuracy_drop = 0
        bn_folded_acc = 0
        cle_acc = 0
        adaround_acc = 0
        with patch_ptq_techniques(bn_folded_acc, cle_acc, adaround_acc) as mocks:
            export = QuantizationSimModel.export

            def export_wrapper(*args, **kwargs):
                assert kwargs["onnx_export_args"].opset_version == 10
                assert kwargs["propagate_encodings"]
                return export(*args, **kwargs)

            try:
                setattr(QuantizationSimModel, "export", export_wrapper)
                auto_quant = AutoQuant(
                    allowed_accuracy_drop=0,
                    unlabeled_dataset_iterable=unlabeled_data_loader,
                    eval_callback=mocks.eval_callback,
                )
                adaround_params = AdaroundParameters(unlabeled_data_loader, 1)
                auto_quant.set_adaround_params(adaround_params)

                auto_quant.set_export_params(OnnxExportApiArgs(10), True)

                self._do_test_apply_auto_quant(
                    auto_quant, cpu_model, dummy_input,
                    allowed_accuracy_drop, bn_folded_acc, cle_acc, adaround_acc
                )
                adaround_args, _ = mocks.apply_adaround.call_args
                _, _, _, actual_adaround_params = adaround_args
                assert adaround_params == actual_adaround_params
            finally:
                setattr(QuantizationSimModel, "export", export)


@contextlib.contextmanager
def create_tmp_directory(dirname: str = "/tmp/.aimet_unittest"):
    success = False
    try:
        os.makedirs(dirname, exist_ok=True)
        success = True
    except FileExistsError:
        raise

    try:
        yield dirname
    finally:
        if success:
            shutil.rmtree(dirname)
