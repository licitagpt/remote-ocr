"""Dispositivo de inferencia do onnxruntime: GPU por default quando disponivel.

Os dois motores inferem com o onnxruntime: o RapidOCR (deteccao, classificacao e reconhecimento) e o modelo de
layout do pymupdf4llm (`pymupdf.layout`). O pacote `onnxruntime` do PyPI so traz o CPUExecutionProvider; a GPU
vem de um pacote alternativo com o MESMO modulo `onnxruntime` (so um deles pode estar instalado):

  * `onnxruntime-gpu`      -> CUDAExecutionProvider (NVIDIA; Windows e Linux). As DLLs do CUDA/cuDNN podem vir
                              dos pacotes pip `nvidia-*`; `onnxruntime.preload_dlls()` as carrega de la.
                              `scripts/setup_gpu.py --cuda` instala o conjunto certo para a versao do onnxruntime.
  * `onnxruntime-directml` -> DmlExecutionProvider (qualquer GPU DirectX 12; so Windows). So o RapidOCR usa: o
                              `pymupdf.layout` so conhece CUDA e fica em CPU.

OCR_DEVICE = auto (default: cuda se houver, senao dml, senao cpu) | cpu | cuda | dml. Pedido que nao pode ser
atendido vira aviso e CPU, nunca erro: o pipeline continua. O backend tesseract (motor pymupdf4llm) e sempre CPU.

Uma instancia `Device` por processo (cache por pedido): depois que o RapidOCR carrega os modelos ela registra o
provider EFETIVAMENTE usado pelas sessoes (`active`) e o dispositivo do modelo de layout (`layout`).
"""

from __future__ import annotations

import contextlib
import logging
import sys
import threading
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("convert_ocr.device")

DEVICES = ("auto", "cpu", "cuda", "dml")
PROVIDERS = {"cpu": "CPUExecutionProvider", "cuda": "CUDAExecutionProvider", "dml": "DmlExecutionProvider"}
KIND_BY_PROVIDER = {v: k for k, v in PROVIDERS.items()}
HINTS = {
    "cuda": "instale o onnxruntime-gpu e as bibliotecas CUDA/cuDNN (python scripts/setup_gpu.py --cuda)",
    "dml": "instale o onnxruntime-directml (python scripts/setup_gpu.py --dml; so Windows)",
}


@dataclass
class Device:
    requested: str                       # auto | cpu | cuda | dml
    kind: str                            # cpu | cuda | dml (escolhido)
    provider: str                        # provider do onnxruntime correspondente a `kind`
    ort_package: str = ""                # onnxruntime | onnxruntime-gpu | onnxruntime-directml
    ort_version: str = ""
    cuda_version: str | None = None      # versao do CUDA para a qual o onnxruntime-gpu foi compilado
    available: list[str] = field(default_factory=list)
    gpu_id: int = 0
    note: str = ""                       # motivo de um fallback para CPU
    active: str | None = None            # provider usado de fato pelas sessoes do RapidOCR (apos carregar)
    layout: str | None = None            # dispositivo do modelo de layout do pymupdf4llm: cuda | cpu | none

    @property
    def gpu(self) -> bool:
        return self.kind != "cpu"

    def fallback(self, reason: str) -> None:
        """Rebaixa para CPU (o provider pedido falhou ao criar a sessao)."""
        self.note = reason
        self.kind, self.provider, self.active = "cpu", PROVIDERS["cpu"], None

    def label(self) -> str:
        s = f"{self.kind} ({self.provider}; {self.ort_package} {self.ort_version}"
        if self.cuda_version and self.kind == "cuda":
            s += f", CUDA {self.cuda_version}"
        if self.kind == "cuda" and self.gpu_id:
            s += f", gpu {self.gpu_id}"
        return s + ")"

    def to_dict(self) -> dict[str, Any]:
        return {"requested": self.requested, "kind": self.kind, "provider": self.provider, "active": self.active,
                "layout": self.layout, "onnxruntime": f"{self.ort_package} {self.ort_version}".strip(),
                "cuda": self.cuda_version if self.kind == "cuda" else None, "gpu_id": self.gpu_id,
                "available": list(self.available), "note": self.note or None}


# ---------------------------------------------------------------------- escolha
def choose(requested: str, available: list[str], package: str = "onnxruntime") -> tuple[str, str]:
    """(kind, nota) para o pedido e os providers disponiveis. A nota explica um fallback para CPU."""
    requested = (requested or "auto").lower()
    has = {k: PROVIDERS[k] in available for k in ("cuda", "dml")}
    if requested == "cpu":
        return "cpu", ""
    if requested == "auto":
        if has["cuda"]:
            return "cuda", ""
        if has["dml"]:
            return "dml", ""
        return "cpu", f"nenhum provider de GPU no {package} instalado ({', '.join(available) or 'nenhum'})"
    if requested in has and has[requested]:
        return requested, ""
    return "cpu", f"{PROVIDERS.get(requested, requested)} indisponivel no {package} instalado ({', '.join(available)}); " \
                  f"{HINTS.get(requested, '')}".rstrip("; ")


def rapid_params(device: Device) -> dict[str, Any]:
    """Parametros extras do RapidOCR (EngineConfig.onnxruntime.*) para o dispositivo."""
    if device.kind == "cuda":
        return {"EngineConfig.onnxruntime.use_cuda": True,
                "EngineConfig.onnxruntime.cuda_ep_cfg.device_id": int(device.gpu_id)}
    if device.kind == "dml":
        return {"EngineConfig.onnxruntime.use_dml": True}
    return {}


# ---------------------------------------------------------------------- sondagem (uma por processo e pedido)
_lock = threading.Lock()
_cache: dict[tuple[str, int], Device] = {}


def probe(settings) -> Device:
    key = (str(getattr(settings, "ocr_device", "auto")).lower(), int(getattr(settings, "ocr_gpu_id", 0)))
    with _lock:
        d = _cache.get(key)
        if d is None:
            d = _probe(*key)
            _cache[key] = d
            if d.gpu:
                log.info("OCR na GPU: %s", d.label())
            elif d.note:
                (log.warning if d.requested not in ("auto", "cpu") else log.info)("OCR em CPU: %s", d.note)
        return d


def _probe(requested: str, gpu_id: int) -> Device:
    try:
        import onnxruntime as ort
    except ImportError as e:
        return Device(requested, "cpu", PROVIDERS["cpu"], note=f"onnxruntime nao instalado ({e})")
    package, version, cuda = "onnxruntime", getattr(ort, "__version__", ""), None
    try:
        from onnxruntime.capi import build_and_package_info as bi
        package = getattr(bi, "package_name", package)
        version = getattr(bi, "__version__", version)
        cuda = getattr(bi, "cuda_version", None)
    except Exception:  # noqa: BLE001
        pass
    quiet_onnxruntime()
    try:
        available = list(ort.get_available_providers())
    except Exception as e:  # noqa: BLE001
        available = []
        log.debug("get_available_providers falhou: %s", e)
    kind, note = choose(requested, available, package)
    d = Device(requested, kind, PROVIDERS[kind], package, version, cuda, available, gpu_id, note)
    if kind == "cuda":
        _preload_cuda(ort)
    return d


def _preload_cuda(ort) -> None:
    """Carrega as DLLs do CUDA/cuDNN dos pacotes pip `nvidia-*` (onnxruntime >= 1.21); sem elas a sessao CUDA
    falha ao criar e o RapidRecognizer cai para CPU. A funcao escreve avisos com print(): vao para stderr."""
    fn = getattr(ort, "preload_dlls", None)
    if fn is None:
        return
    try:
        with contextlib.redirect_stdout(sys.stderr):
            fn()
    except Exception as e:  # noqa: BLE001
        log.debug("preload_dlls: %s: %s", type(e).__name__, e)


def quiet_onnxruntime() -> None:
    """Silencia o logger nativo do onnxruntime (avisos de 'plugin EP' a cada sessao) - so erros."""
    try:
        import onnxruntime as ort
        ort.set_default_logger_severity(3)
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------- RapidOCR: provider efetivo
def session_providers(rapid_ocr) -> list[str]:
    """Providers das sessoes onnxruntime ja carregadas pelo RapidOCR (det, cls, rec); [] se nenhuma."""
    for name in ("text_det", "text_rec", "text_cls"):
        comp = getattr(rapid_ocr, name, None)
        sess = getattr(getattr(comp, "session", None), "session", None)
        get = getattr(sess, "get_providers", None)
        if callable(get):
            try:
                return list(get())
            except Exception:  # noqa: BLE001
                continue
    return []


def note_active(device: Device, rapid_ocr) -> None:
    """Registra o provider efetivo do RapidOCR; se a GPU pedida nao esta em uso, rebaixa o Device para CPU."""
    provs = session_providers(rapid_ocr)
    if not provs:
        return
    device.active = provs[0]
    if device.gpu and provs[0] != device.provider:
        log.warning("%s pedido, mas o RapidOCR esta inferindo em %s (providers da sessao: %s)",
                    device.provider, provs[0], provs)
        device.note = f"sessoes do onnxruntime em {provs[0]}"
        device.kind, device.provider = KIND_BY_PROVIDER.get(provs[0], "cpu"), provs[0]


# ---------------------------------------------------------------------- pymupdf.layout
def activate_layout(device: Device) -> str:
    """Ativa o modelo de layout do pymupdf (usado pelo pymupdf4llm) no dispositivo; devolve cuda | cpu | none.

    `import pymupdf.layout` chama `activate()`, que cria o modelo em CPU e o guarda em `pymupdf._get_layout`
    (e e no-op se ja houver um callable la). Para a GPU, poe-se um placeholder antes do import e cria-se o
    modelo com `get_model(use_gpu=True)` (o pymupdf.layout so conhece o CUDAExecutionProvider).

    PyMuPDF 2 faria esse import (em CPU) ja no `import pymupdf`; o __init__ do pacote o bloqueia
    (convert_pdf.pymupdf_compat) e e aqui, no dispositivo escolhido, que o import e liberado.
    """
    from convert_pdf.pymupdf_compat import allow_layout
    import pymupdf
    if device.layout is not None:
        return device.layout
    allow_layout()
    if callable(getattr(pymupdf, "_get_layout", None)):          # alguem ja ativou (CPU)
        device.layout = "cpu"
        return device.layout
    if device.kind != "cuda":
        try:
            import pymupdf.layout  # noqa: F401  (ativa em CPU)
        except ImportError:
            device.layout = "none"
        else:
            device.layout = "cpu"
        return device.layout

    def _placeholder(*_a, **_k):
        return None

    pymupdf._get_layout = _placeholder
    try:
        import pymupdf.layout
        from pymupdf.layout import DocumentLayoutAnalyzer
        with contextlib.redirect_stdout(sys.stderr):
            model = DocumentLayoutAnalyzer.get_model(use_gpu=True)

        def _get_layout(*args, **kwargs):
            return model.predict(*args, **kwargs)

        pymupdf._get_layout = _get_layout
        device.layout = "cuda"
        log.info("pymupdf.layout na GPU (CUDAExecutionProvider)")
    except ImportError:
        pymupdf._get_layout = None
        device.layout = "none"
    except Exception as e:  # noqa: BLE001
        log.warning("pymupdf.layout na GPU falhou (%s: %s); modelo de layout em CPU", type(e).__name__, e)
        pymupdf._get_layout = None
        try:
            import pymupdf.layout as _pl
            _pl.activate()
            device.layout = "cpu"
        except Exception:  # noqa: BLE001
            device.layout = "none"
    return device.layout


def reset_cache() -> None:
    """Para testes."""
    with _lock:
        _cache.clear()
