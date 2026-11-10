"""Baixa e carrega uma vez os modelos que o convert_ocr usa (RapidOCR `pt` de reconhecimento + deteccao, e o modelo de
layout do pymupdf4llm quando instalado), para a imagem docker do remote-ocr ja sair com eles e a primeira subida dos
processos nao depender da rede. Roda em CPU (a GPU nao existe no build da imagem).

    python scripts/prefetch_models.py
"""

from __future__ import annotations

import sys


def main() -> int:
    from convert_ocr.config import Settings
    from convert_ocr.core.engines.base import RapidRecognizer

    settings = Settings(storage_dir=".", ocr_device="cpu", tiktoken_encoding="", no_color=True)
    RapidRecognizer.get(settings)
    print("rapidocr: modelos carregados")

    try:
        # o __init__ do convert_ocr bloqueia o import automatico do pymupdf.layout; aqui ele e liberado de proposito
        from convert_ocr.pdf.pymupdf_compat import allow_layout
        allow_layout()
        import pymupdf.layout  # noqa: F401
        print("pymupdf.layout: disponivel (modelo de layout carregado)")
    except ImportError as e:
        print(f"pymupdf.layout: nao instalado ({e}); o motor pymupdf4llm fica indisponivel")

    try:
        import tiktoken
        tiktoken.get_encoding("o200k_base")
        print("tiktoken: o200k_base em cache")
    except Exception as e:  # noqa: BLE001
        print(f"tiktoken: nao foi possivel baixar o encoding ({e}); o token_count fica em 0 sem rede")
    return 0


if __name__ == "__main__":
    sys.exit(main())
