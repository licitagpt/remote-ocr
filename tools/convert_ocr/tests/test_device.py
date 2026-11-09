"""Escolha do dispositivo de inferencia (GPU por default quando disponivel) e integracao com os motores."""

from __future__ import annotations

import onnxruntime as ort
import pytest

from convert_ocr.config import Settings
from convert_ocr.core import device as dev

CPU, CUDA, DML = dev.PROVIDERS["cpu"], dev.PROVIDERS["cuda"], dev.PROVIDERS["dml"]


def test_choose_auto_prefers_gpu_when_present():
    assert dev.choose("auto", [CUDA, CPU]) == ("cuda", "")
    assert dev.choose("auto", ["TensorrtExecutionProvider", CUDA, CPU]) == ("cuda", "")
    assert dev.choose("auto", [DML, CPU]) == ("dml", "")
    kind, note = dev.choose("auto", [CPU], "onnxruntime")
    assert kind == "cpu" and "onnxruntime" in note
    assert dev.choose("cpu", [CUDA, CPU]) == ("cpu", "")


def test_choose_explicit_request_unavailable_falls_back_with_hint():
    kind, note = dev.choose("cuda", [CPU])
    assert kind == "cpu" and "CUDAExecutionProvider" in note and "setup_gpu.py --cuda" in note
    kind, note = dev.choose("dml", [CUDA, CPU])
    assert kind == "cpu" and "setup_gpu.py --dml" in note
    assert dev.choose("cuda", [CUDA, CPU]) == ("cuda", "")


def test_rapid_params_by_device():
    d = dev.Device("auto", "cuda", CUDA, gpu_id=1)
    assert dev.rapid_params(d) == {"EngineConfig.onnxruntime.use_cuda": True,
                                   "EngineConfig.onnxruntime.cuda_ep_cfg.device_id": 1}
    assert dev.rapid_params(dev.Device("auto", "dml", DML)) == {"EngineConfig.onnxruntime.use_dml": True}
    assert dev.rapid_params(dev.Device("auto", "cpu", CPU)) == {}
    d.fallback("x")
    assert d.kind == "cpu" and d.provider == CPU and d.note == "x" and not d.gpu


def test_settings_validate_device():
    with pytest.raises(ValueError):
        Settings(ocr_device="xyz").validate()
    with pytest.raises(ValueError):
        Settings(ocr_gpu_id=-1).validate()
    Settings(ocr_device="cuda").validate()


def test_probe_matches_installed_runtime(settings):
    dev.reset_cache()
    d = dev.probe(settings)
    available = ort.get_available_providers()
    expected = "cuda" if CUDA in available else "dml" if DML in available else "cpu"
    assert d.requested == "auto" and d.kind == expected and d.provider == dev.PROVIDERS[expected]
    assert d.ort_version and d.ort_package.startswith("onnxruntime")
    assert d.to_dict()["available"] == list(available)
    assert dev.probe(settings) is d                                    # cache por processo
    forced = dev.probe(Settings(ocr_device="cpu"))
    assert forced.kind == "cpu" and forced is not d


@pytest.mark.ocr
def test_recognizer_reports_active_provider(settings):
    from convert_ocr.core.engines.base import RapidRecognizer
    RapidRecognizer.get(settings)
    d = dev.probe(settings)
    assert d.active in dev.PROVIDERS.values()
    assert d.active == d.provider                                      # o que foi escolhido e o que esta em uso
    info = __import__("convert_ocr.core.engines", fromlist=["get_engine"]).get_engine("rapidocr", settings).info()
    assert info["device"]["active"] == d.active and info["device"]["kind"] == d.kind


@pytest.mark.layout
def test_activate_layout_follows_device(settings):
    pytest.importorskip("pymupdf.layout")
    import pymupdf
    d = dev.probe(settings)
    got = dev.activate_layout(d)
    assert callable(pymupdf._get_layout)
    assert got == d.layout
    if d.kind == "cuda" and d.layout == "cuda":
        assert got == "cuda"
    else:
        assert got == "cpu"
