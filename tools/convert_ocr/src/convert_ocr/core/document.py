"""Pipeline por documento:

ler .md -> localizar paginas [OCR:n] -> abrir PDF -> linhas nativas das outras paginas (p/ cabecalho/rodape)
-> OCR pagina a pagina (motor) -> cabecalho/rodape por repeticao entre TODAS as paginas -> markdown de cada
pagina de OCR -> remontar o .md sem os marcadores [OCR:n] -> gravar (atomico) -> OcrResult

O .md so e reescrito quando TODAS as paginas de OCR foram tratadas: qualquer erro (motor, timeout, PDF ilegivel)
deixa o original intacto e devolve status=error com codigo tipado. Nunca levanta.
"""

from __future__ import annotations

import logging
import os
import re
import time
from pathlib import Path
from typing import Callable

import pymupdf

from convert_ocr.config import OCR_MD_SUFFIX, Settings
from convert_ocr.core import mdfile
from convert_ocr.core.boxes import filter_rows, page_markdown
from convert_ocr.core.engines import PageOcr, get_engine
from convert_ocr.core.metrics import OcrResult, Timer
from convert_pdf.core.hf import HFModel, HFParams
from convert_pdf.core.markdown import lines_to_markdown
from convert_pdf.core.textpage import Line, body_font_size, extract_lines
from convert_pdf.core.tokens import count_tokens

log = logging.getLogger("convert_ocr.document")

Progress = Callable[[int, int, int, float], None]     # (feitas, total, pagina 1-based, ms da pagina)
MIN_KEEP_RATIO = 0.5          # markdown do pymupdf4llm com menos que isto do texto reconhecido -> montagem simples
RE_HEADING = re.compile(r"^#{1,6} ", re.M)
RE_MD_MARK = re.compile(r"[*_#>`|\\\[\]()~<>\-\s]+")


class OcrError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def init_process(settings: Settings) -> None:
    """Uma vez por processo: avisos do MuPDF desligados (vao para o contador `warnings`)."""
    os.environ.setdefault("OMP_NUM_THREADS", str(settings.ocr_threads or 1))
    pymupdf.TOOLS.mupdf_display_errors(False)
    try:
        pymupdf.TOOLS.mupdf_display_warnings(False)
    except Exception:  # noqa: BLE001
        pass


def hf_params_from(settings: Settings) -> HFParams:
    return HFParams(top=settings.hf_top, bottom=settings.hf_bottom, side=settings.hf_side,
                    min_ratio=settings.hf_min_ratio, sim=settings.hf_sim, pos_tol=settings.hf_pos_tol)


def _open(path: Path) -> pymupdf.Document:
    try:
        doc = pymupdf.open(str(path))
    except (pymupdf.FileDataError, pymupdf.EmptyFileError) as e:  # type: ignore[attr-defined]
        raise OcrError("NOT_PDF", f"arquivo nao e um PDF legivel: {e}") from e
    except RuntimeError as e:
        raise OcrError("NOT_PDF", f"falha ao abrir: {e}") from e
    if doc.needs_pass and not doc.authenticate(""):
        doc.close()
        raise OcrError("ENCRYPTED", "PDF protegido por senha")
    if doc.page_count == 0:
        doc.close()
        raise OcrError("EMPTY", "PDF sem paginas")
    return doc


def _write_atomic(out: Path, text: str) -> int:
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".tmp")
    data = text.encode("utf-8")
    tmp.write_bytes(data)
    os.replace(tmp, out)
    return len(data)


def _check_deadline(deadline: float | None, timeout_s: float, pno: int) -> None:
    if deadline is not None and time.monotonic() > deadline:
        raise OcrError("TIMEOUT", f"tempo maximo de {timeout_s:.0f}s excedido (pagina {pno + 1})")


def ocr_md_path(md: Path) -> Path:
    """`X.md` -> `X.ocr.md` (saida ao lado da entrada, sem tocar nela)."""
    name = md.name[:-3] if md.name.lower().endswith(".md") else md.name
    return md.with_name(name + OCR_MD_SUFFIX)


def process(pdf_path: str | os.PathLike, md_path: str | os.PathLike | None, out_path: str | os.PathLike | None,
            settings: Settings, engine: str | None = None, progress: Progress | None = None,
            output: str = "replace") -> OcrResult:
    """Faz o OCR das paginas [OCR:n] do .md de um PDF. `md_path` default: .md ao lado do PDF. Saida: `out_path`
    se informado; senao `output` decide: `replace` = o proprio .md (substituicao) | `ocr-md` = `<nome>.ocr.md`
    ao lado. Nunca levanta."""
    p = Path(pdf_path)
    md = Path(md_path) if md_path else p.with_suffix(".md")
    out = Path(out_path) if out_path else ocr_md_path(md) if output == "ocr-md" else md
    engine_name = (engine or settings.engine).lower()
    r = OcrResult(pdf=str(p), md=str(md), out=str(out), engine=engine_name)
    deadline = time.monotonic() + settings.timeout_s if settings.timeout_s > 0 else None
    doc = None
    with Timer() as t_total:
        try:
            if not p.is_file():
                raise OcrError("NOT_FOUND", f"arquivo nao existe: {p}")
            if not md.is_file():
                raise OcrError("NO_MD", f"o .md do convert_pdf nao existe: {md}")
            r.size_bytes = p.stat().st_size
            try:
                mddoc = mdfile.parse(md.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError) as e:
                raise OcrError("IO", f"falha ao ler {md}: {e}") from e
            except ValueError as e:
                raise OcrError("BAD_MD", str(e)) from e
            ocr_pages = mddoc.ocr_pages
            r.pages, r.ocr_pages = mddoc.total, len(ocr_pages)
            if not ocr_pages:
                r.status = "no_ocr"
                return r
            if len(ocr_pages) > settings.max_ocr_pages:
                raise OcrError("TOO_MANY_PAGES", f"{len(ocr_pages)} paginas de OCR (maximo {settings.max_ocr_pages})")
            eng = get_engine(engine_name, settings)
            r.backend = eng.backend
            pymupdf.TOOLS.mupdf_warnings(reset=True)
            with Timer() as t:
                doc = _open(p)
            r.ms.open = t.ms
            if doc.page_count != mddoc.total:
                raise OcrError("MD_MISMATCH", f"o .md tem {mddoc.total} paginas e o PDF {doc.page_count}; "
                                              "reconverta com o convert_pdf")
            text = _process_doc(doc, mddoc, eng, r, settings, deadline, progress)
            with Timer() as t:
                try:
                    r.md_size_bytes = _write_atomic(out, text)
                except OSError as e:
                    raise OcrError("IO", f"falha ao gravar {out}: {e}") from e
            r.ms.write = t.ms
            with Timer() as t:
                r.token_count = count_tokens(text, settings.tiktoken_encoding)
            r.ms.tokens = t.ms
            r.status = "ok"
        except OcrError as e:
            r.status, r.error_code, r.error_message = "error", e.code, e.message
        except MemoryError:
            r.status, r.error_code, r.error_message = "error", "INTERNAL", "MemoryError"
        except Exception as e:  # noqa: BLE001
            log.exception("erro inesperado em %s", p.name)
            r.status, r.error_code, r.error_message = "error", "INTERNAL", f"{type(e).__name__}: {e}"
        finally:
            if doc is not None:
                try:
                    doc.close()
                except Exception:  # noqa: BLE001
                    pass
            try:
                r.warnings = len([l for l in pymupdf.TOOLS.mupdf_warnings(reset=True).splitlines() if l.strip()])
                pymupdf.TOOLS.store_shrink(100)
            except Exception:  # noqa: BLE001
                pass
    r.ms.total = t_total.ms
    return r


def _process_doc(doc: pymupdf.Document, mddoc: mdfile.MdDoc, eng, r: OcrResult, settings: Settings,
                 deadline: float | None, progress: Progress | None) -> str:
    ocr_pnos = [p.page - 1 for p in mddoc.ocr_pages]
    ocr_set = set(ocr_pnos)

    # 1) linhas nativas das paginas que nao sao de OCR: o cabecalho/rodape e aprendido por repeticao em todas as
    #    paginas do documento (um documento com 1 ou 2 paginas escaneadas no meio de 20 de texto herda o
    #    cabecalho das paginas de texto)
    pages_lines: list[list[Line]] = [[] for _ in range(doc.page_count)]
    sizes: list[tuple[float, float]] = [(0.0, 0.0)] * doc.page_count
    with Timer() as t:
        for pno in range(doc.page_count):
            page = doc[pno]
            sizes[pno] = (page.rect.width or 1.0, page.rect.height or 1.0)
            if pno in ocr_set:
                continue
            try:
                pages_lines[pno], _ = extract_lines(page)
            except Exception as e:  # noqa: BLE001
                log.debug("p%d: linhas nativas indisponiveis (%s)", pno + 1, e)
    r.ms.extract = t.ms

    # 2) OCR pagina a pagina
    results: dict[int, PageOcr] = {}
    for i, pno in enumerate(ocr_pnos):
        _check_deadline(deadline, settings.timeout_s, pno)
        with Timer() as t:
            try:
                po = eng.ocr_page(doc[pno])
            except Exception as e:  # noqa: BLE001
                log.debug("p%d: falha no motor %s", pno + 1, eng.name, exc_info=True)
                raise OcrError("OCR_FAILED", f"pagina {pno + 1}: {type(e).__name__}: {e}") from e
        r.ms.render += po.ms_render
        r.ms.ocr += po.ms_ocr
        results[pno] = po
        pages_lines[pno] = po.lines
        sizes[pno] = (po.w, po.h)
        if po.rotation:
            r.rotated_pages += 1
        if po.fallback == "html":
            r.html_pages += 1
        if po.empty:
            r.empty_pages += 1
        elif po.low_score:
            r.low_score_pages += 1
        log.debug("p%d: %s %d caixas, confianca %.2f, %s%s%s", pno + 1, po.engine or eng.name, po.n_boxes, po.score,
                  f"{t.ms:.0f}ms", f", girada {po.rotation}" if po.rotation else "",
                  f", {po.fallback} -> {po.engine}" if po.fallback else "")
        if progress:
            progress(i + 1, len(ocr_pnos), pno + 1, t.ms)

    # 3) cabecalho/rodape
    with Timer() as t:
        hf = HFModel(hf_params_from(settings)).fit(pages_lines, sizes)
    r.ms.hf = t.ms
    r.hf_groups = len(hf.accepted)

    # 4) markdown de cada pagina de OCR
    bodies: dict[int, str] = {}
    with Timer() as t:
        for pno, po in results.items():
            w, h = sizes[pno]
            if po.md is not None:                                   # motor pymupdf4llm: markdown pronto
                kept = hf.filter_lines(po.lines, w, h, pno)
                md = hf.strip_markdown(po.md, pno) if po.md.strip() else ""
                recognized = sum(len(l.text.strip()) for l in kept)
                if kept and _plain_len(md) < MIN_KEEP_RATIO * recognized:
                    # o modelo de layout descartou a maior parte do texto (pagina curta tomada por cabecalho/rodape,
                    # so assinaturas...): fica com a montagem simples das linhas reconhecidas
                    log.debug("p%d: pymupdf4llm devolveu %d chars de %d reconhecidos; usando as linhas", pno + 1,
                              _plain_len(md), recognized)
                    md, _ = lines_to_markdown(kept, body_font_size([kept]), settings.heading_max_chars)
                nh, nt = len(RE_HEADING.findall(md)), _count_tables(md)
            else:                                                   # motor rapidocr (ou auto com HTML): nossa montagem
                kept = hf.filter_lines(po.lines, w, h, pno)
                rows = filter_rows(po.line_rows, kept)
                md, nh, nt = page_markdown(kept, rows, po.body, settings.heading_max_chars) if kept else ("", 0, 0)
            md = mdfile.sanitize_body(md)
            bodies[pno + 1] = md
            r.lines += len(kept)
            r.headings += nh
            r.tables += nt
            r.chars += len(md)
            if not md and not po.empty:
                r.empty_pages += 1
        text = mdfile.rebuild(mddoc, bodies)
    r.ms.convert = t.ms
    r.hf_removed = hf.removed_lines + hf.removed_cells
    r.ocr_done = len(results)
    return text


def _plain_len(md: str) -> int:
    """Chars de texto do markdown, sem marcacao nem espacos (para comparar com o que o OCR reconheceu)."""
    return len(RE_MD_MARK.sub("", md))


def _count_tables(md: str) -> int:
    n = 0
    prev_table = False
    for line in md.splitlines():
        is_row = line.strip().startswith("|") and line.strip().endswith("|")
        if is_row and not prev_table:
            n += 1
        prev_table = is_row
    return n
