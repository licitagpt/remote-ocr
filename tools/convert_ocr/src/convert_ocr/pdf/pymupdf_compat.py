"""Ajustes ao PyMuPDF 2.x no processo.

Desde o PyMuPDF 2.0, `import pymupdf` faz `import pymupdf.layout` por conta propria quando o pacote
`pymupdf-layout` esta instalado, e esse import chama `activate()`: o modelo de layout (onnxruntime) e carregado em
TODO processo que importa o PyMuPDF (medido: 0,06 s / 51 MB -> 0,42 s / 142 MB), com sessoes do onnxruntime nas
opcoes padrao (todos os nucleos, spin-wait), antes de qualquer codigo nosso rodar. O convert_pdf mantem o modelo
num processo auxiliar (runtime/layout_proc.py) justamente para o processo principal nao paga-lo, e o convert_ocr
precisa criar o modelo na GPU ANTES de alguem o ativar em CPU.

O mesmo `import pymupdf` tambem faz `import pymupdf4llm` (para `pymupdf._to_markdown` & cia., que nao usamos); se o
pymupdf4llm for importado enquanto o pymupdf.layout esta bloqueado, ele se fixa em "modo legado" (`_use_layout =
False`) para o resto do processo, mesmo que o modelo seja ativado depois. Por isso os DOIS imports sao bloqueados
juntos e liberados juntos (medido, `import pymupdf`: 0,42 s / 142 MB sem bloqueio; 0,21 s / 87 MB bloqueando so o
layout; 0,06 s / 56 MB bloqueando ambos, o custo do PyMuPDF 1.x).

`block_layout_autoload()` instala um finder em `sys.meta_path` que deixa `importlib.util.find_spec(...)` responder
normalmente (os pacotes continuam "instalados" para quem pergunta: layout.available(), a recomendacao do PyMuPDF)
mas faz `import pymupdf.layout` e `import pymupdf4llm` falharem com ImportError, que o PyMuPDF trata como "nao
instalado". Quem quiser o modelo chama `allow_layout()` e importa (`import_pymupdf4llm()` ja garante o modo layout).
O bloqueio so vale se for instalado ANTES do primeiro `import pymupdf` do processo: os pacotes convert_pdf e
convert_ocr o instalam no `__init__`, entao `python -m convert_pdf ...` e `python -m convert_ocr ...` ficam
protegidos; um script que importe o pymupdf antes deles so perde a economia (a extracao de texto e tabelas nao
muda com o modelo ativo). Efeito colateral aceito: `pymupdf.pymupdf4llm` fica None (`pymupdf._to_markdown` nao funciona).
"""

from __future__ import annotations

import importlib.abc
import importlib.machinery
import importlib.util
import os
import sys

BLOCKED = ("pymupdf.layout", "pymupdf4llm")


class _BlockedLoader(importlib.abc.Loader):
    def create_module(self, spec):
        raise ImportError(f"{spec.name} bloqueado pelo convert_pdf (pymupdf_compat.allow_layout() libera)", name=spec.name)

    def exec_module(self, module):  # pragma: no cover  (create_module ja levantou)
        raise ImportError(module.__name__, name=module.__name__)


class _BlockLayoutFinder(importlib.abc.MetaPathFinder):
    """Responde ao find_spec dos modulos bloqueados com um spec cujo loader recusa carregar."""

    def find_spec(self, fullname, path=None, target=None):
        if fullname not in BLOCKED:
            return None
        return importlib.machinery.ModuleSpec(fullname, _BlockedLoader(), is_package=True)


_finder: _BlockLayoutFinder | None = None


def block_layout_autoload() -> bool:
    """Impede o carregamento automatico do pymupdf.layout. Devolve True se o bloqueio esta ativo (instalado agora
    ou antes); False se ja era tarde (pymupdf ja importado neste processo)."""
    global _finder
    # A recomendacao "Consider using the pymupdf_layout package" sai por print() no stdout, de dentro do
    # find_tables; e as mensagens do PyMuPDF (`pymupdf.message`, ex.: excecao no find_tables) tambem vao ao stdout
    # por default. No server-mode o stdout e o canal NDJSON: ambas vao para o stderr. Lidas na importacao /
    # chamada, respeitando o que o ambiente ja definiu.
    os.environ.setdefault("PYMUPDF_SUGGEST_LAYOUT_ANALYZER", "0")
    os.environ.setdefault("PYMUPDF_MESSAGE", "fd:2")
    if _finder is not None:
        return True
    if "pymupdf" in sys.modules or any(m in sys.modules for m in BLOCKED):
        return False
    _finder = _BlockLayoutFinder()
    sys.meta_path.insert(0, _finder)
    return True


def allow_layout() -> None:
    """Remove o bloqueio: o proximo `import pymupdf.layout` carrega (e ativa) o modelo de verdade, e o proximo
    `import pymupdf4llm` nasce em modo layout. Um import que falhou nao deixa nada em sys.modules, entao basta
    tirar o finder."""
    global _finder
    if _finder is not None:
        try:
            sys.meta_path.remove(_finder)
        except ValueError:
            pass
        _finder = None
        importlib.invalidate_caches()


def layout_blocked() -> bool:
    return _finder is not None


def import_pymupdf4llm():
    """`import pymupdf4llm` com o modo layout garantido (chamar depois de allow_layout() e do pymupdf.layout ativo).
    Se o pymupdf4llm ja tinha sido importado em modo legado (importado enquanto o layout estava bloqueado, ou
    sem o pymupdf-layout instalado na hora), `use_layout(True)` o vira; sem o pacote de layout fica legado mesmo."""
    import pymupdf
    import pymupdf4llm
    if not getattr(pymupdf4llm, "_use_layout", True) and hasattr(pymupdf4llm, "use_layout"):
        try:
            if not callable(getattr(pymupdf, "_get_layout", None)):
                import pymupdf.layout  # noqa: F401  (ativa, se instalado)
            pymupdf4llm.use_layout(True)
        except ImportError:
            pass
    return pymupdf4llm
