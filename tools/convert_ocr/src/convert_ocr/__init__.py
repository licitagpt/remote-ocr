"""convert_ocr: OCR das paginas marcadas [OCR:n] nos .md do convert_pdf, com os motores auto (default) | rapidocr |
pymupdf4llm."""

from convert_pdf.pymupdf_compat import block_layout_autoload

__version__ = "0.2.1"

# PyMuPDF >= 2.0 ativaria o modelo de layout (em CPU) em todo processo no `import pymupdf`, antes de
# device.activate_layout poder cria-lo na GPU. Tem de vir antes do primeiro `import pymupdf` (ver pymupdf_compat).
block_layout_autoload()
