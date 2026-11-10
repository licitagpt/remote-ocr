"""Fixtures: PDFs "escaneados" sinteticos (pagina de texto renderizada e inserida como imagem) e o .md no formato do
convert_pdf com os marcadores [OCR:n], gerados em runtime (sem binarios no repositorio e sem o pacote convert_pdf:
o .md e montado aqui, no mesmo formato de convert_pdf/core/document.py, etapa 5)."""

from __future__ import annotations

from pathlib import Path

import pymupdf
import pytest

from convert_ocr.config import Settings
from convert_ocr.core import mdfile

W, H = 595, 842
HEADER = "PREFEITURA MUNICIPAL DE TESTE - SECRETARIA DE ADMINISTRACAO"
FOOTER = "Rua das Flores, 123 - Centro - CNPJ 12.345.678/0001-90"
LOREM = ("Torna-se publico que o Municipio realizara licitacao na modalidade pregao eletronico para "
         "aquisicao de materiais de consumo conforme condicoes estabelecidas neste edital e seus anexos.")
TITLES = ["DO OBJETO", "DA JUSTIFICATIVA", "DAS CONDICOES DE PARTICIPACAO", "DO PAGAMENTO", "DAS SANCOES"]


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Motor rapidocr explicito: os testes do pipeline sao sobre a montagem propria; o default do pacote e `auto`."""
    return Settings(storage_dir=str(tmp_path), log_dir=str(tmp_path / "logs"), timeout_s=300.0, no_color=True,
                    ocr_dpi=150, tiktoken_encoding="", engine="rapidocr")


def _text_page(doc: pymupdf.Document, pno: int, n: int, with_table: bool = False) -> pymupdf.Page:
    page = doc.new_page(width=W, height=H)
    page.insert_text((60, 40), HEADER, fontsize=9)
    page.insert_text((60, H - 40), FOOTER, fontsize=8)
    page.insert_text((W / 2 - 30, H - 25), f"Pagina {pno + 1} de {n}", fontsize=8)
    page.insert_text((W / 2 - 80, 95), f"{pno + 1}. {TITLES[pno % len(TITLES)]}", fontsize=15)
    y = 125
    for i in range(3):
        page.insert_textbox(pymupdf.Rect(60, y, W - 60, y + 70), f"{pno + 1}.{i + 1}. {LOREM}", fontsize=10)
        y += 80
    if with_table:
        rows = [["Item", "Descricao", "Und", "Valor"]] + [[f"{i}", f"Servico numero {i}", "m2", f"{i * 125},50"] for i in range(1, 5)]
        cols = [60, 110, 330, 420]
        for r, row in enumerate(rows):
            for c, cell in enumerate(row):
                page.insert_text((cols[c], y + r * 18), cell, fontsize=10)
    return page


def scanned_copy(src: pymupdf.Document, pnos: set[int], dpi: int = 150) -> pymupdf.Document:
    """Copia do documento em que as paginas `pnos` virem imagem (scan), sem texto."""
    out = pymupdf.open()
    for pno in range(src.page_count):
        if pno in pnos:
            pix = src[pno].get_pixmap(dpi=dpi, colorspace=pymupdf.csRGB, alpha=False)
            page = out.new_page(width=W, height=H)
            page.insert_image(page.rect, pixmap=pix)
        else:
            out.insert_pdf(src, from_page=pno, to_page=pno)
    return out


def make_mixed_pdf(path: Path, n: int = 5, scanned: set[int] = frozenset({1, 3})) -> Path:
    """n paginas com o mesmo cabecalho/rodape; as `scanned` sao imagem (vao para OCR), as outras texto."""
    src = pymupdf.open()
    for pno in range(n):
        _text_page(src, pno, n, with_table=(pno == 3))
    doc = scanned_copy(src, set(scanned))
    doc.save(path)
    doc.close()
    src.close()
    return path


def make_all_scanned_pdf(path: Path, n: int = 3) -> Path:
    src = pymupdf.open()
    for pno in range(n):
        _text_page(src, pno, n)
    doc = scanned_copy(src, set(range(n)))
    doc.save(path)
    doc.close()
    src.close()
    return path


def make_blank_scan_pdf(path: Path) -> Path:
    """Scan de pagina em branco (cinza uniforme): o OCR nao acha texto."""
    doc = pymupdf.open()
    page = doc.new_page(width=W, height=H)
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 300, 420), False)
    pix.clear_with(235)
    page.insert_image(page.rect, pixmap=pix)
    doc.save(path)
    doc.close()
    return path


def write_convert_pdf_md(pdf: Path) -> Path:
    """Gera ao lado do PDF o .md como o convert_pdf o gravaria: blocos `> __Página N de M__` + corpo, as paginas sem
    texto (imagem) com `[OCR:n]` (n a partir de 1, na ordem) e as demais com o texto nativo da pagina."""
    doc = pymupdf.open(pdf)
    total = doc.page_count
    parts: list[str] = []
    ocr = 0
    for pno in range(total):
        text = mdfile.sanitize_body(doc[pno].get_text("text"))
        if text:
            body = text
        else:
            ocr += 1
            body = f"[OCR:{ocr}]"
        parts.append(f"{mdfile.page_marker(pno, total)}\n\n{body}")
    doc.close()
    md = pdf.with_suffix(".md")
    md.write_text("\n\n".join(parts) + "\n", encoding="utf-8")
    return md


@pytest.fixture
def storage(tmp_path: Path) -> Path:
    """Storage sintetico: files/pncp/<ano>/<cnpj>/<seq>/*.pdf + .md no formato do convert_pdf."""
    root = tmp_path / "files" / "pncp" / "2025" / "12345678000199" / "000001"
    root.mkdir(parents=True)
    for name, maker in (("mixed.pdf", make_mixed_pdf), ("scan.pdf", make_all_scanned_pdf), ("blank.pdf", make_blank_scan_pdf)):
        pdf = maker(root / name)
        write_convert_pdf_md(pdf)
    (root / "semmd.pdf").write_bytes((root / "scan.pdf").read_bytes())
    return tmp_path


@pytest.fixture
def mixed(storage: Path) -> Path:
    return storage / "files" / "pncp" / "2025" / "12345678000199" / "000001" / "mixed.pdf"
