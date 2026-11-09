"""Motores de OCR disponiveis. `get_engine(nome, settings)` devolve a instancia (uma por processo e nome)."""

from __future__ import annotations

from convert_ocr.config import ENGINES, Settings
from convert_ocr.core.engines.base import OcrEngine, PageOcr

_instances: dict[str, OcrEngine] = {}


def get_engine(name: str, settings: Settings) -> OcrEngine:
    name = (name or settings.engine).lower()
    if name not in ENGINES:
        raise ValueError(f"motor desconhecido: {name!r} (use {'|'.join(ENGINES)})")
    eng = _instances.get(name)
    if eng is None:
        if name == "auto":
            from convert_ocr.core.engines.auto import AutoEngine
            eng = AutoEngine(settings)
        elif name == "rapidocr":
            from convert_ocr.core.engines.rapid import RapidEngine
            eng = RapidEngine(settings)
        else:
            from convert_ocr.core.engines.p4llm import Pymupdf4llmEngine
            eng = Pymupdf4llmEngine(settings)
        _instances[name] = eng
    return eng


def close_all() -> None:
    for eng in _instances.values():
        try:
            eng.close()
        except Exception:  # noqa: BLE001
            pass
    _instances.clear()


__all__ = ["OcrEngine", "PageOcr", "get_engine", "close_all"]
