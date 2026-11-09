"""CLI: `convert_ocr local <pdf|dir>`, `convert_ocr server` e `convert_ocr review`. Todos os parametros tem default (ver --help)."""

from __future__ import annotations

import argparse
import sys

from convert_ocr import __version__
from convert_ocr.config import add_common_args, settings_from_args
from convert_ocr.logging_setup import get_logger, setup_logging


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="convert_ocr",
                                 description="OCR das paginas [OCR:n] dos .md do convert_pdf (auto | rapidocr | pymupdf4llm).")
    ap.add_argument("--version", action="version", version=f"convert_ocr {__version__}")
    sub = ap.add_subparsers(dest="mode", required=True)

    loc = sub.add_parser("local", help="trata um .pdf (com o .md ao lado) ou um diretorio inteiro")
    loc.add_argument("source", help="arquivo .pdf ou diretorio")
    loc.add_argument("--md", default=None, help="caminho do .md de entrada (so com um unico PDF; default: .md ao lado do .pdf)")
    loc.add_argument("--out", default=None,
                     help="diretorio de saida: grava o .md resultante ali (mesma arvore relativa), ignorando --output (testes)")
    loc.add_argument("--no-recursive", action="store_true", help="nao descer em subdiretorios")
    loc.add_argument("--report", default=None, help="prefixo dos relatorios .csv/.json")
    loc.add_argument("--list", dest="pdf_list", default=None, help="arquivo com a lista de PDFs a tratar (um por linha)")
    loc.add_argument("--no-file-log", action="store_true", help="nao gravar log em arquivo")
    add_common_args(loc)

    srv = sub.add_parser("server", help="server-mode: NDJSON por stdin/stdout para o Workers (.NET)")
    add_common_args(srv)

    rev = sub.add_parser("review", help="validacao: aplicacao web para comparar a pagina do PDF com o resultado do OCR")
    rev.add_argument("--host", default="127.0.0.1")
    rev.add_argument("--port", type=int, default=8766)
    rev.add_argument("--dir", default=None, help="pasta dos resultados da validacao (default: <storage>/review/ocr)")
    rev.add_argument("--no-open", action="store_true", help="nao abrir o navegador")
    rev.add_argument("--no-file-log", action="store_true", help="nao gravar log em arquivo")
    add_common_args(rev)
    return ap


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    try:
        settings = settings_from_args(args)
    except ValueError as e:
        ap.error(str(e))
        return 2
    setup_logging(settings, file_logging=not getattr(args, "no_file_log", False))
    log = get_logger("cli")
    log.debug("settings: %s", {k: v for k, v in settings.__dict__.items() if k != "sources"})
    if args.mode == "server":
        from convert_ocr.runtime.server import run_server
        return run_server(settings)
    if args.mode == "review":
        from pathlib import Path
        from convert_ocr.review.server import serve
        storage = Path(settings.storage_dir)
        out_dir = Path(args.dir) if args.dir else storage / "review" / "ocr"
        return serve(settings, out_dir, args.host, args.port, open_browser=not args.no_open)
    from convert_ocr.runtime.local import run_local
    return run_local(settings, args.source, args.md, args.out, not args.no_recursive, args.report, args.pdf_list)


if __name__ == "__main__":
    sys.exit(main())
