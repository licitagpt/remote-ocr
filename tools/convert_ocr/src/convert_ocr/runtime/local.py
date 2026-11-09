"""local-mode: trata um PDF (com o .md ao lado) ou um diretorio inteiro, em sequencia, no processo atual.

Destino (OUTPUT / --output): por default no modo local o resultado vai para `<nome>.ocr.md` ao lado do .md, que fica
intacto; `--output replace` substitui o .md (sem os marcadores [OCR:n]), como o server-mode faz por default. Com
`--out DIR` o resultado vai para DIR (mesma arvore relativa) - o modo para testar/comparar motores.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

import pymupdf

from convert_ocr import __version__
from convert_ocr.config import OCR_MD_SUFFIX, Settings, settings_to_dict
from convert_ocr.logging_setup import fmt_ms, paint, paint_status
from convert_ocr.runtime.engine import Engine, Task

log = logging.getLogger("convert_ocr.local")


def collect_pdfs(src: Path, recursive: bool = True) -> list[Path]:
    if src.is_file():
        return [src]
    if not src.is_dir():
        return []
    it = src.rglob("*") if recursive else src.glob("*")
    return sorted(p for p in it if p.is_file() and p.suffix.lower() == ".pdf")


def output_path(pdf: Path, src: Path, out_dir: Path | None) -> Path | None:
    if out_dir is None:
        return None
    rel = pdf.relative_to(src) if src.is_dir() else Path(pdf.name)
    return (out_dir / rel).with_suffix(".md")


def run_local(settings: Settings, source: str, md: str | None, out_dir: str | None, recursive: bool,
              report: str | None, pdf_list: str | None = None) -> int:
    src = Path(source)
    if pdf_list:
        pdfs = [Path(l.strip()) for l in Path(pdf_list).read_text(encoding="utf-8").splitlines() if l.strip()]
        pdfs = [p if p.is_absolute() else src / p for p in pdfs]
    else:
        pdfs = collect_pdfs(src, recursive)
    if not pdfs:
        log.error("nenhum PDF encontrado em %s", src)
        return 1
    if md and len(pdfs) != 1:
        log.error("--md so pode ser usado com um unico PDF")
        return 1
    out_root = Path(out_dir) if out_dir else None
    engine = Engine(settings, "local")
    dest = str(out_root) if out_root else "substitui o .md original" if engine.output == "replace" \
        else f"ao lado, em <nome>{OCR_MD_SUFFIX} (o .md fica intacto)"
    log.info("convert_ocr %s | PyMuPDF %s | %d PDFs | engine=%s%s | dpi=%d | saida: %s", __version__, pymupdf.version[0],
             len(pdfs), settings.engine, f" ({settings.ocr_backend})" if settings.engine == "pymupdf4llm"
             else " (pymupdf4llm; rapidocr na pagina com HTML)" if settings.engine == "auto" else "",
             settings.ocr_dpi, dest)

    info = engine.warm_up()
    device = info.get("device") or {}
    if device:
        log.info("dispositivo: %s | onnxruntime: %s%s%s", device.get("active") or device.get("provider"),
                 device.get("onnxruntime"), f" | CUDA {device['cuda']}" if device.get("cuda") else "",
                 f" | layout: {device['layout']}" if device.get("layout") else "")
    results: list[dict[str, Any]] = []
    wall0 = time.perf_counter()
    try:
        for i, p in enumerate(pdfs):
            out = output_path(p, src, out_root)
            r = engine.run(Task(id=str(i), pdf=str(p), md=md, out=str(out) if out else None),
                           progress=_progress if settings.log_level.upper() == "DEBUG" else None)
            results.append(r)
            _print_line(i + 1, len(pdfs), r)
    except KeyboardInterrupt:
        log.warning("interrompido pelo usuario")
    finally:
        engine.close()
    wall = time.perf_counter() - wall0
    _summary(results, wall, settings)
    if report:
        _write_report(report, results, settings, wall, device)
    return 0 if all(r.get("ok") for r in results) else 2


def _progress(done: int, total: int, page: int, ms: float) -> None:
    log.debug("  OCR %d/%d (pagina %d) %s", done, total, page, fmt_ms(ms))


def _print_line(i: int, n: int, r: dict[str, Any]) -> None:
    name = os.path.basename(r.get("pdf", ""))
    if r.get("ok"):
        ms = (r.get("ms") or {}).get("total", 0)
        extra = ""
        if r.get("empty_pages"):
            extra += f" vazias={r['empty_pages']}"
        if r.get("rotated_pages"):
            extra += f" giradas={r['rotated_pages']}"
        if r.get("low_score_pages"):
            extra += f" baixa-conf={r['low_score_pages']}"
        if r.get("html_pages"):
            extra += f" html={r['html_pages']}"
        if r.get("tables"):
            extra += f" tab={r['tables']}"
        if r.get("token_count"):
            extra += f" tok={r['token_count']}"
        log.info("[%4d/%d] %-7s %8s ocr=%-3d/%-4dp hf=%-4d%s %s", i, n, paint_status(r.get("status", "")),
                 paint(fmt_ms(ms), "cyan"), r.get("ocr_done", 0), r.get("pages", 0), r.get("hf_removed", 0), extra, name)
    else:
        err = r.get("error", {})
        log.error("[%4d/%d] %-7s %s  %s: %s", i, n, paint_status("error"), name, err.get("code"), err.get("message"))


def _summary(results: list[dict[str, Any]], wall: float, settings: Settings) -> None:
    ok = [r for r in results if r.get("ok")]
    done = sum(r.get("ocr_done", 0) for r in ok)
    by_status: dict[str, int] = {}
    for r in results:
        by_status[r.get("status", "?")] = by_status.get(r.get("status", "?"), 0) + 1
    ms = {k: sum((r.get("ms") or {}).get(k, 0) for r in ok) for k in ("extract", "render", "ocr", "hf", "convert", "write", "total")}
    log.info("=" * 78)
    log.info("RESUMO  %d PDFs, %d paginas de OCR | wall %.1fs -> %.2f pag/s | engine=%s | por status: %s",
             len(results), done, wall, done / wall if wall else 0, settings.engine, by_status)
    if done:
        log.info("  ms/pagina OCR: render %.0f | ocr %.0f | hf %.0f | convert %.0f | total %.0f",
                 ms["render"] / done, ms["ocr"] / done, ms["hf"] / done, ms["convert"] / done, ms["total"] / done)
    log.info("  paginas: vazias %d | giradas %d | baixa confianca %d | html->rapidocr %d | linhas %d | tabelas %d | "
             "titulos %d | cabecalho/rodape removidos %d | %d tokens",
             sum(r.get("empty_pages", 0) for r in ok), sum(r.get("rotated_pages", 0) for r in ok),
             sum(r.get("low_score_pages", 0) for r in ok), sum(r.get("html_pages", 0) for r in ok),
             sum(r.get("lines", 0) for r in ok),
             sum(r.get("tables", 0) for r in ok), sum(r.get("headings", 0) for r in ok),
             sum(r.get("hf_removed", 0) for r in ok), sum(r.get("token_count", 0) for r in ok))
    errors = [r for r in results if not r.get("ok")]
    if errors:
        log.info("  erros (%d):", len(errors))
        for r in errors[:30]:
            e = r.get("error", {})
            log.info("    - %s  %s: %s", os.path.basename(r.get("pdf", "")), e.get("code"), e.get("message"))


def _write_report(prefix: str, results: list[dict[str, Any]], settings: Settings, wall: float,
                  device: dict[str, Any] | None = None) -> None:
    base = Path(prefix)
    base.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for r in results:
        flat = {k: v for k, v in r.items() if not isinstance(v, (dict, list))}
        for k, v in (r.get("ms") or {}).items():
            flat[f"ms_{k}"] = v
        if r.get("error"):
            flat["error_code"] = r["error"].get("code")
            flat["error_message"] = r["error"].get("message")
        rows.append(flat)
    keys: list[str] = []
    for row in rows:
        for k in row:
            if k not in keys:
                keys.append(k)
    with open(base.with_suffix(".csv"), "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=keys)
        w.writeheader()
        for row in rows:
            w.writerow(row)
    base.with_suffix(".json").write_text(json.dumps({
        "version": __version__, "pymupdf": pymupdf.version[0], "wall_seconds": round(wall, 3),
        "device": device or None, "settings": settings_to_dict(settings), "files": results,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    log.info("relatorios: %s | %s", base.with_suffix(".csv"), base.with_suffix(".json"))
    sys.stderr.flush()
