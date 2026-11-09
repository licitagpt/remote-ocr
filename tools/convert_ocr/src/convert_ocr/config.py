"""Configuracao: defaults <- linha de comando (mesmo esquema do convert_pdf). Nao ha .env nem variaveis de
ambiente: tudo o que a ferramenta aceita e passado por parametro. `--storage` e o unico obrigatorio.

Um unico lugar define cada parametro (nome, tipo, default, ajuda). A CLI e o `Settings` sao gerados a partir
dessa tabela. Nao ha parametro de workers: cada processo convert_ocr trata um documento por vez e o paralelismo
e decidido por quem o inicia (a aplicacao Workers .NET abre N processos).
"""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

ENGINES = ("auto", "rapidocr", "pymupdf4llm")
BACKENDS = ("rapidocr", "tesseract")
DEVICES = ("auto", "cpu", "cuda", "dml")
OUTPUTS = ("auto", "replace", "ocr-md")
OCR_MD_SUFFIX = ".ocr.md"


@dataclass(frozen=True)
class Param:
    name: str
    type: type
    default: Any
    help: str
    choices: tuple[str, ...] | None = None
    flag: str | None = None       # nome do argumento CLI (default: --nome-com-hifens)
    required: bool = False


PARAMS: tuple[Param, ...] = (
    Param("storage_dir", str, "", "raiz de dados (obrigatorio); paths relativos resolvem contra ela", flag="--storage",
          required=True),
    Param("log_dir", str, "", "pasta dos logs (default: <storage>/logs/convert_ocr)"),
    Param("log_level", str, "INFO", "nivel de log", choices=("DEBUG", "INFO", "WARNING", "ERROR")),
    Param("log_file_max_mb", int, 50, "tamanho maximo do arquivo de log (MB)"),
    Param("log_file_backups", int, 5, "arquivos de log rotacionados mantidos"),
    Param("engine", str, "auto",
          "motor de OCR: auto (RapidOCR reconhece uma vez; o pymupdf4llm monta o markdown e, na pagina em que ele "
          "devolver tags HTML, vale a montagem propria do rapidocr), rapidocr (PyMuPDF renderiza, RapidOCR reconhece, "
          "montagem propria do markdown) ou pymupdf4llm (PyMuPDF renderiza, camada de texto do OCR, modelo de layout "
          "do pymupdf4llm monta o markdown)",
          choices=ENGINES),
    Param("output", str, "auto",
          "destino do resultado: replace = substitui o .md de entrada (sem os [OCR:n]); ocr-md = grava ao lado em "
          "<nome>.ocr.md e deixa o .md intacto; auto = ocr-md no modo local e replace no server-mode "
          "(`--out`/`out` por requisicao tem precedencia; a validacao `review` sempre grava na pasta dela)",
          choices=OUTPUTS),
    Param("ocr_backend", str, "rapidocr",
          "reconhecedor que gera a camada de texto no motor pymupdf4llm: rapidocr ou tesseract (precisa de TESSDATA); "
          "o motor auto usa sempre o rapidocr",
          choices=BACKENDS),
    Param("ocr_device", str, "auto",
          "dispositivo de inferencia do onnxruntime (RapidOCR e modelo de layout): auto = GPU quando o onnxruntime "
          "instalado tiver CUDA (onnxruntime-gpu) ou DirectML (onnxruntime-directml), senao CPU; pedido indisponivel "
          "vira aviso e CPU", choices=DEVICES),
    Param("ocr_gpu_id", int, 0, "indice da GPU para o CUDAExecutionProvider (maquinas com varias GPUs)"),
    Param("ocr_lang", str, "pt", "idioma do modelo de reconhecimento do RapidOCR (Rec.lang_type)"),
    Param("ocr_dpi", int, 200, "resolucao da renderizacao da pagina para o OCR"),
    Param("ocr_threads", int, 0,
          "threads do onnxruntime no RapidOCR deste processo (0 = default da biblioteca; com N processos use ~cpu_count // N)"),
    Param("ocr_text_score", float, 0.5, "confianca minima de uma linha reconhecida pelo RapidOCR"),
    Param("no_auto_rotate", bool, False,
          "nao tentar girar a pagina 90/270 graus quando a maior parte do texto reconhecido estiver na vertical"),
    Param("tessdata", str, "", "pasta dos dados do Tesseract (*.traineddata); vazio = a instalacao do Tesseract que o PyMuPDF achar"),
    Param("tess_lang", str, "por", "idioma(s) do Tesseract (por, por+eng...)"),
    Param("timeout_s", float, 1800.0, "tempo maximo por documento (s)", flag="--timeout"),
    Param("max_ocr_pages", int, 1000, "maximo de paginas [OCR:n] por documento; acima disto o documento e recusado"),
    Param("max_rss_mb", int, 4096,
          "memoria residente (MB) acima da qual o server-mode encerra entre documentos (codigo 4, evento exit com retry) "
          "para o Workers reiniciar o processo: protecao contra crescimento das bibliotecas nativas (MuPDF, onnxruntime, "
          "modelo de layout); 0 desliga"),
    Param("hf_top", float, 0.22, "zona de cabecalho (fracao da altura)"),
    Param("hf_bottom", float, 0.18, "zona de rodape (fracao da altura)"),
    Param("hf_side", float, 0.12, "zona lateral para texto vertical (fracao da largura)"),
    Param("hf_min_ratio", float, 0.30, "linha repetida em >= esta fracao das paginas (min 3) e cabecalho/rodape"),
    Param("hf_sim", float, 0.80, "similaridade difflib minima para juntar variantes (OCR erra letras: menor que no convert_pdf)"),
    Param("hf_pos_tol", float, 0.03, "faixa de posicao (fracao da pagina)"),
    Param("heading_max_chars", int, 150, "tamanho maximo de um titulo"),
    Param("tiktoken_encoding", str, "o200k_base",
          "encoding do tiktoken para token_count (o200k_base, cl100k_base...); vazio desliga a contagem"),
    Param("no_color", bool, False, "desliga cores no console"),
)

_PARAM_BY_NAME = {p.name: p for p in PARAMS}


def _coerce(p: Param, raw: Any) -> Any:
    if raw is None:
        return None
    if p.type is bool:
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() not in ("", "0", "false", "no", "off")
    if p.type is str:
        v = str(raw).strip()
        if p.choices and v.lower() in p.choices:
            v = v.lower()
        return v
    return p.type(raw)


@dataclass(frozen=True)
class Settings:
    storage_dir: str = ""            # obrigatorio na CLI (--storage); vazio so em uso programatico (testes, scripts)
    log_dir: str = ""
    log_level: str = "INFO"
    log_file_max_mb: int = 50
    log_file_backups: int = 5
    engine: str = "auto"
    output: str = "auto"
    ocr_backend: str = "rapidocr"
    ocr_device: str = "auto"
    ocr_gpu_id: int = 0
    ocr_lang: str = "pt"
    ocr_dpi: int = 200
    ocr_threads: int = 0
    ocr_text_score: float = 0.5
    no_auto_rotate: bool = False
    tessdata: str = ""
    tess_lang: str = "por"
    timeout_s: float = 1800.0
    max_ocr_pages: int = 1000
    max_rss_mb: int = 4096
    hf_top: float = 0.22
    hf_bottom: float = 0.18
    hf_side: float = 0.12
    hf_min_ratio: float = 0.30
    hf_sim: float = 0.80
    hf_pos_tol: float = 0.03
    heading_max_chars: int = 150
    tiktoken_encoding: str = "o200k_base"
    no_color: bool = False
    sources: dict = field(default_factory=dict, compare=False, repr=False)   # de onde veio cada valor

    # ------------------------------------------------------------------ derivados
    @property
    def effective_log_dir(self) -> Path:
        return Path(self.log_dir) if self.log_dir else Path(self.storage_dir) / "logs" / "convert_ocr"

    @property
    def effective_tessdata(self) -> str | None:
        """Pasta do tessdata: parametro, TESSDATA_PREFIX ou instalacao do Tesseract no PATH; None se nao houver."""
        if self.tessdata:
            return self.tessdata
        try:
            import pymupdf
            return pymupdf.get_tessdata() or None
        except Exception:  # noqa: BLE001
            return None

    @property
    def auto_rotate(self) -> bool:
        return not self.no_auto_rotate

    def output_for(self, mode: str) -> str:
        """Destino efetivo (`replace` | `ocr-md`) para o modo de execucao (`local` | `server`)."""
        if self.output != "auto":
            return self.output
        return "replace" if mode == "server" else "ocr-md"

    def resolve_path(self, p: str | os.PathLike) -> Path:
        path = Path(p)
        return path if path.is_absolute() else Path(self.storage_dir) / path

    def validate(self) -> None:
        if self.engine not in ENGINES:
            raise ValueError(f"engine deve ser {'|'.join(ENGINES)}, recebido {self.engine!r}")
        if self.ocr_backend not in BACKENDS:
            raise ValueError(f"ocr_backend deve ser {'|'.join(BACKENDS)}, recebido {self.ocr_backend!r}")
        if self.output not in OUTPUTS:
            raise ValueError(f"output deve ser {'|'.join(OUTPUTS)}, recebido {self.output!r}")
        if self.ocr_device not in DEVICES:
            raise ValueError(f"ocr_device deve ser {'|'.join(DEVICES)}, recebido {self.ocr_device!r}")
        if self.ocr_gpu_id < 0:
            raise ValueError("ocr_gpu_id deve ser >= 0")
        if self.timeout_s <= 0:
            raise ValueError("timeout_s deve ser > 0")
        if not 72 <= self.ocr_dpi <= 600:
            raise ValueError("ocr_dpi deve estar entre 72 e 600")
        if self.max_ocr_pages <= 0:
            raise ValueError("max_ocr_pages deve ser > 0")
        if self.max_rss_mb < 0:
            raise ValueError("max_rss_mb deve ser >= 0 (0 desliga)")
        if self.log_level.upper() not in ("DEBUG", "INFO", "WARNING", "ERROR"):
            raise ValueError(f"log_level invalido: {self.log_level!r}")


def load_settings(cli: dict[str, Any] | None = None) -> Settings:
    """Monta o Settings: default de cada parametro, sobreposto pelo que veio da linha de comando (`cli`; None =
    nao informado). Os parametros obrigatorios (`--storage`) precisam estar em `cli`."""
    values: dict[str, Any] = {}
    sources: dict[str, str] = {}
    for p in PARAMS:
        val, src = p.default, "default"
        if cli and cli.get(p.name) is not None:
            val, src = _coerce(p, cli[p.name]), "cli"
        if p.required and val in (None, ""):
            raise ValueError(f"{p.flag or '--' + p.name.replace('_', '-')} e obrigatorio")
        values[p.name] = val
        sources[p.name] = src
    s = Settings(**values, sources=sources)
    s.validate()
    return s


def add_common_args(ap: argparse.ArgumentParser) -> None:
    """Adiciona todos os parametros a um parser; default=None para distinguir 'nao informado'."""
    g = ap.add_argument_group("configuracao (CLI > default)")
    for p in PARAMS:
        flag = p.flag or "--" + p.name.replace("_", "-")
        help_ = f"{p.help} [obrigatorio]" if p.required else f"{p.help} [default: {p.default!r}]"
        kw: dict[str, Any] = dict(dest=p.name, default=None, help=help_, required=p.required)
        if p.type is bool:
            kw["action"] = "store_true"
            kw["default"] = None
        else:
            kw["type"] = p.type
            if p.choices:
                kw["choices"] = list(p.choices)
        g.add_argument(flag, **kw)


def settings_from_args(args: argparse.Namespace) -> Settings:
    cli = {p.name: getattr(args, p.name, None) for p in PARAMS}
    return load_settings(cli)


def settings_to_dict(s: Settings) -> dict[str, Any]:
    return {f.name: getattr(s, f.name) for f in fields(s) if f.name != "sources"}
