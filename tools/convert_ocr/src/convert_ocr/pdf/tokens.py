"""Contagem de tokens do markdown (tiktoken), para o campo `token_count` do resultado (spec workers/12).

O encoder e carregado uma vez por processo, na primeira conversao (o tiktoken baixa e guarda em cache o arquivo
do encoding na primeira vez; `TIKTOKEN_CACHE_DIR` controla onde). Se o tiktoken nao estiver instalado, ou o
encoding for desconhecido, a contagem fica em 0 e um aviso sai no log uma unica vez: a conversao nunca falha
por causa da contagem.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

log = logging.getLogger("convert_ocr.pdf.tokens")

_lock = threading.Lock()
_encoders: dict[str, Any] = {}
_failed: set[str] = set()


def _encoder(name: str):
    with _lock:
        if name in _encoders:
            return _encoders[name]
        if name in _failed:
            return None
        try:
            import tiktoken
            enc = tiktoken.get_encoding(name)
        except Exception as e:  # noqa: BLE001  (sem tiktoken, sem rede na 1a vez, encoding invalido)
            _failed.add(name)
            log.warning("token_count desativado: tiktoken.get_encoding(%r) falhou (%s: %s)", name, type(e).__name__, e)
            return None
        _encoders[name] = enc
        return enc


def count_tokens(text: str, encoding: str) -> int:
    """Tokens de `text` no encoding dado; 0 se a contagem estiver desligada (encoding vazio) ou indisponivel."""
    if not encoding or not text:
        return 0
    enc = _encoder(encoding)
    if enc is None:
        return 0
    try:
        # disallowed_special=() : texto de PDF pode conter literais como "<|endoftext|>"; nao e erro.
        return len(enc.encode(text, disallowed_special=()))
    except Exception as e:  # noqa: BLE001
        log.warning("token_count falhou (%s: %s); devolvendo 0", type(e).__name__, e)
        return 0
