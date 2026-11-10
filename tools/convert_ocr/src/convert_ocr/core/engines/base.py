"""Base dos motores: resultado por pagina, renderizacao e o reconhecedor RapidOCR compartilhado (com auto-rotate).

Um motor recebe uma `pymupdf.Page` e devolve `PageOcr`: as linhas reconhecidas como `Line` do convert_pdf (para o
modelo de cabecalho/rodape e a montagem do markdown) no espaco da pagina EXIBIDA, em pontos; se o auto-rotate
girou a pagina, as linhas e (w, h) estao no espaco girado. O motor pymupdf4llm devolve tambem o markdown pronto.
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass, field

import numpy as np
import pymupdf

from convert_ocr.config import Settings
from convert_ocr.core.boxes import OcrBox, mean_score, quality, to_boxes, vertical_share
from convert_ocr.core.metrics import Timer
from convert_ocr.pdf.textpage import Line

log = logging.getLogger("convert_ocr.engine")

ROTATE_MIN_VERTICAL = 0.5     # fracao de caracteres verticais a partir da qual se tenta girar a pagina
ROTATE_MIN_CHARS = 20
LOW_SCORE = 0.7


@dataclass(slots=True)
class PageOcr:
    pno: int
    w: float
    h: float
    lines: list[Line] = field(default_factory=list)
    line_rows: list[list[Line]] = field(default_factory=list)   # linhas visuais (celulas) - motor rapidocr
    body: float = 0.0                                             # tamanho de fonte do corpo (motor rapidocr)
    md: str | None = None                                         # markdown pronto, sem HF (motor pymupdf4llm)
    rotation: int = 0                                             # 0 | 90 | 270 aplicado pelo auto-rotate
    n_boxes: int = 0
    score: float = 0.0                                            # confianca media (ponderada por chars)
    ms_render: float = 0.0
    ms_ocr: float = 0.0
    engine: str = ""                                              # motor que montou esta pagina (auto: o escolhido)
    fallback: str = ""                                            # motor auto: por que trocou de montagem ("html")

    @property
    def empty(self) -> bool:
        return self.n_boxes == 0

    @property
    def low_score(self) -> bool:
        return self.n_boxes > 0 and self.score < LOW_SCORE


@dataclass(slots=True)
class Recognized:
    """Pagina renderizada e reconhecida pelo RapidOCR (com auto-rotate): o que os motores rapidocr e pymupdf4llm
    compartilham antes de montar o markdown, cada um do seu jeito."""
    boxes: list[OcrBox]
    img: np.ndarray            # imagem usada (ja girada, se o auto-rotate girou)
    rotation: int
    scale: float               # pixels por ponto
    w: float                   # tamanho da pagina exibida, em pontos (espaco girado)
    h: float
    ms_render: float
    ms_ocr: float


# tag HTML: <br>, </td>, <span style="...">, <img .../>, <!-- ... --> (nao casa "a < b > c" nem "<10")
RE_HTML_TAG = re.compile(r"</?[A-Za-z][A-Za-z0-9-]*(?:\s[^<>]*)?/?>|<!--.*?-->", re.S)


def has_html(md: str | None) -> bool:
    """O markdown traz tags HTML? (o pymupdf4llm as emite em celulas de tabela e em alguns layouts)."""
    return bool(md) and RE_HTML_TAG.search(md) is not None


class OcrEngine:
    name = ""
    backend = ""

    def __init__(self, settings: Settings):
        self.settings = settings

    def ocr_page(self, page: pymupdf.Page) -> PageOcr:
        raise NotImplementedError

    def close(self) -> None:
        pass

    def info(self) -> dict:
        from convert_ocr.core.device import probe
        return {"engine": self.name, "backend": self.backend, "device": probe(self.settings).to_dict()}


# ---------------------------------------------------------------------- renderizacao
def render(page: pymupdf.Page, dpi: int) -> tuple[np.ndarray, float]:
    """Imagem RGB (h, w, 3) da pagina como exibida e a escala pixels/ponto."""
    pix = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csRGB, alpha=False)
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n).copy()
    return img, dpi / 72.0


def pixmap_from(img: np.ndarray, dpi: int) -> pymupdf.Pixmap:
    h, w = img.shape[:2]
    pix = pymupdf.Pixmap(pymupdf.csRGB, w, h, np.ascontiguousarray(img).tobytes(), False)
    pix.set_dpi(dpi, dpi)
    return pix


# ---------------------------------------------------------------------- RapidOCR
class RapidRecognizer:
    """Instancia unica do RapidOCR por processo (modelos carregados uma vez)."""

    _lock = threading.Lock()
    _engine = None
    _key: tuple | None = None

    @classmethod
    def get(cls, settings: Settings):
        key = (settings.ocr_lang, settings.ocr_threads, settings.ocr_text_score, settings.ocr_device, settings.ocr_gpu_id)
        with cls._lock:
            if cls._engine is None or cls._key != key:
                from convert_ocr.core import device as dev
                from convert_ocr.logging_setup import quiet_libraries
                from rapidocr import RapidOCR
                d = dev.probe(settings)
                base: dict = {"Rec.lang_type": settings.ocr_lang, "Global.log_level": "warning",
                              "Global.text_score": settings.ocr_text_score}
                if settings.ocr_threads > 0:
                    base["EngineConfig.onnxruntime.intra_op_num_threads"] = settings.ocr_threads
                    base["EngineConfig.onnxruntime.inter_op_num_threads"] = settings.ocr_threads
                engine = None
                for attempt in (d.kind, "cpu"):
                    params = dict(base, **(dev.rapid_params(d) if attempt != "cpu" else {}))
                    try:
                        engine = RapidOCR(params=params)
                        quiet_libraries()
                        _warm_up(engine)          # carrega os modelos (e inicializa a GPU) agora, nao na 1a pagina
                        break
                    except Exception as e:  # noqa: BLE001
                        if attempt == "cpu":
                            raise
                        log.warning("RapidOCR em %s falhou (%s: %s); usando CPU", d.provider, type(e).__name__, e)
                        d.fallback(f"{d.provider} falhou: {type(e).__name__}: {e}")
                dev.note_active(d, engine)
                cls._engine = engine
                cls._key = key
                log.info("RapidOCR carregado (lang=%s, dispositivo=%s, threads=%s)", settings.ocr_lang,
                         d.active or d.provider, settings.ocr_threads or "default")
            return cls._engine

    @classmethod
    def recognize(cls, img: np.ndarray, settings: Settings, scale: float) -> list[OcrBox]:
        res = cls.get(settings)(img)
        return to_boxes(res.boxes, res.txts, res.scores, scale, settings.ocr_text_score)


def _warm_up(rapid_ocr) -> None:
    """Uma inferencia numa imagem pequena com texto: carrega det/cls/rec (lazy no RapidOCR) e cria as sessoes."""
    doc = pymupdf.open()
    try:
        page = doc.new_page(width=320, height=90)
        page.insert_text((20, 55), "Teste de OCR 123", fontsize=26)
        img, _ = render(page, 150)
    finally:
        doc.close()
    rapid_ocr(img)


def recognize_auto_rotate(img: np.ndarray, settings: Settings, scale: float) -> tuple[list[OcrBox], np.ndarray, int]:
    """Reconhece a imagem; se a maior parte do texto estiver na vertical (pagina escaneada deitada), tenta a
    imagem girada 90 e 270 graus e fica com a melhor leitura. Devolve (caixas, imagem usada, rotacao)."""
    boxes = RapidRecognizer.recognize(img, settings, scale)
    if not settings.auto_rotate or sum(len(b.text) for b in boxes) < ROTATE_MIN_CHARS \
            or vertical_share(boxes) < ROTATE_MIN_VERTICAL:
        return boxes, img, 0
    best = (quality(boxes), boxes, img, 0)
    for k, rot in ((1, 270), (3, 90)):          # np.rot90 k=1 gira 90 graus no sentido anti-horario
        rimg = np.ascontiguousarray(np.rot90(img, k))
        rboxes = RapidRecognizer.recognize(rimg, settings, scale)
        q = quality(rboxes)
        if q > best[0] * 1.2 and vertical_share(rboxes) < ROTATE_MIN_VERTICAL:
            best = (q, rboxes, rimg, rot)
    if best[3]:
        log.debug("pagina girada %d graus pelo auto-rotate (%d -> %d caixas)", best[3], len(boxes), len(best[1]))
    return best[1], best[2], best[3]


def recognize_page(page: pymupdf.Page, settings: Settings) -> Recognized:
    """Renderiza a pagina e reconhece com o RapidOCR (auto-rotate incluido)."""
    with Timer() as tr:
        img, scale = render(page, settings.ocr_dpi)
    with Timer() as to:
        boxes, img, rotation = recognize_auto_rotate(img, settings, scale)
    h_px, w_px = img.shape[:2]
    return Recognized(boxes, img, rotation, scale, w_px / scale, h_px / scale, tr.ms, to.ms)


def page_score(boxes: list[OcrBox]) -> float:
    return mean_score(boxes)
