"""server-mode via subprocess: ready, ping, convert (relativo ao storage e com `out`), erros, status, shutdown;
stdout so com JSON mesmo com as bibliotecas de OCR carregadas (que usam print)."""

from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading

import pytest

pytestmark = pytest.mark.ocr


class Client:
    def __init__(self, tmp_path, extra: list[str] = ()):
        cmd = [sys.executable, "-m", "convert_ocr", "server", "--storage", str(tmp_path), "--log-dir", str(tmp_path / "logs"),
               "--no-color", "--log-level", "INFO", "--timeout", "300", "--ocr-dpi", "150", "--tiktoken-encoding", "",
               "--engine", "rapidocr", *extra]
        self.p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.q: queue.Queue = queue.Queue()
        self.raw: list[bytes] = []
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        for line in self.p.stdout:
            self.raw.append(line)
            self.q.put(line)
        self.q.put(None)

    def recv(self, timeout=180):
        line = self.q.get(timeout=timeout)
        return None if line is None else json.loads(line.decode("utf-8"))

    def send_raw(self, text: str):
        self.p.stdin.write(text.encode("utf-8"))
        self.p.stdin.flush()

    def send(self, obj):
        self.send_raw(json.dumps(obj) + "\n")

    def close(self):
        try:
            self.p.stdin.close()
        except OSError:
            pass
        try:
            self.p.wait(timeout=120)
        except subprocess.TimeoutExpired:
            self.p.kill()
        return self.p.stderr.read().decode("utf-8", errors="replace")


@pytest.fixture
def client(storage):
    c = Client(storage)
    yield c
    c.close()


def test_protocol_roundtrip(client, storage, mixed):
    ready = client.recv()
    assert ready["event"] == "ready" and ready["engine"] == "rapidocr" and ready["pymupdf"]
    assert ready["device"]["kind"] in ("cpu", "cuda", "dml") and ready["device"]["active"]   # modelos ja carregados
    assert ready["output"] == "replace"                                                       # default do server
    client.send({"id": "p", "cmd": "ping"})
    assert client.recv()["id"] == "p"
    rel = mixed.relative_to(storage).as_posix()
    out = storage / "out" / "mixed.md"
    client.send({"id": "1", "cmd": "convert", "pdf": rel, "out": str(out)})          # relativo ao storage
    client.send({"id": "2", "cmd": "ocr", "pdf": "files/pncp/2025/12345678000199/000001/semmd.pdf"})
    client.send({"id": "3", "cmd": "convert", "pdf": "nao/existe.pdf"})
    client.send_raw("isto nao e json\n")
    client.send({"id": "4", "cmd": "convert"})
    client.send({"id": "5", "cmd": "convert", "pdf": rel, "engine": "xyz"})
    client.send({"id": "6", "cmd": "xyz"})
    client.send({"id": "7", "cmd": "status"})
    got = {}
    while len(got) < 8:
        m = client.recv()
        got[m.get("id")] = m
    assert got["1"]["ok"] and got["1"]["status"] == "ok" and got["1"]["ocr_done"] == 2 and out.exists()
    assert got["1"]["tamanhoMd"] == out.stat().st_size and got["1"]["engine"] == "rapidocr"
    assert "[OCR:" in mixed.with_suffix(".md").read_text(encoding="utf-8")                # original intacto (out informado)
    assert got["2"]["ok"] is False and got["2"]["error"]["code"] == "NO_MD"
    assert got["3"]["error"]["code"] == "NOT_FOUND"
    assert got[None]["error"]["code"] == "INVALID_REQUEST"
    assert got["4"]["error"]["code"] == "INVALID_REQUEST"
    assert got["5"]["error"]["code"] == "INVALID_REQUEST"
    assert got["6"]["error"]["code"] == "INVALID_REQUEST"
    assert got["7"]["ok"] and "queued" in got["7"] and "inflight" in got["7"] and "done" in got["7"]
    client.send({"id": "8", "cmd": "convert", "pdf": rel})                              # sem out: substitui (server)
    m = client.recv()
    assert m["id"] == "8" and m["ok"] and m["out"] == str(mixed.with_suffix(".md"))
    assert "[OCR:" not in mixed.with_suffix(".md").read_text(encoding="utf-8")
    assert not mixed.with_name("mixed.ocr.md").exists()
    client.send({"id": "z", "cmd": "shutdown"})
    assert client.recv()["shutdown"] is True
    assert client.recv(60) is None
    stderr = client.close()
    assert client.p.returncode == 0
    for line in client.raw:            # stdout so teve JSON
        json.loads(line)
    assert "server-mode pronto" in stderr


def test_eof_shuts_down(client):
    assert client.recv()["event"] == "ready"
    client.p.stdin.close()
    assert client.recv(60) is None
    client.p.wait(timeout=120)
    assert client.p.returncode == 0


def test_memory_limit_recycles_between_documents(storage, mixed):
    """RSS acima de --max-rss-mb depois de um documento: evento exit com retry, codigo 4; o documento ja respondido."""
    c = Client(storage, extra=["--max-rss-mb", "1"])
    try:
        ready = c.recv()
        assert ready["event"] == "ready" and ready["max_rss_mb"] == 1
        rel = mixed.relative_to(storage).as_posix()
        out = storage / "out" / "mixed.md"
        c.send({"id": "1", "cmd": "convert", "pdf": rel, "out": str(out)})
        m = c.recv()
        assert m["id"] == "1" and m["ok"] and out.exists()
        ev = c.recv()
        assert ev["event"] == "exit" and ev["reason"] == "memory" and ev["retry"] is True
        assert ev["rss_mb"] > ev["limit_mb"] == 1 and ev["documents"] == 1
        c.send({"id": "2", "cmd": "convert", "pdf": rel, "out": str(out)})      # chega depois do aviso: sem resposta
        assert c.recv(60) is None
        c.p.wait(timeout=120)
        assert c.p.returncode == 4
    finally:
        stderr = c.close()
    assert "encerrando para reinicio" in stderr


@pytest.mark.skipif(sys.platform == "win32", reason="os.kill com SIGINT nao existe no Windows")
def test_sigint_finishes_inflight_document_and_exits_cleanly(client, storage, mixed):
    """Ctrl+C no terminal do Workers chega tambem a este processo: o documento em voo termina e e respondido
    normalmente (antes virava "INTERNAL: KeyboardInterrupt" e o WorkItem era marcado como erro) e o processo
    encerra sozinho com codigo 0."""
    import os
    import signal
    import time
    assert client.recv()["event"] == "ready"
    rel = mixed.relative_to(storage).as_posix()
    out = storage / "out" / "mixed.md"
    client.send({"id": "1", "cmd": "convert", "pdf": rel, "out": str(out)})
    time.sleep(0.1)                                   # o pedido ja foi lido e esta em voo
    os.kill(client.p.pid, signal.SIGINT)
    m = client.recv()
    assert m["id"] == "1" and m["ok"], m
    assert client.recv(30) is None                    # nada mais: o processo encerrou
    client.p.wait(timeout=60)
    assert client.p.returncode == 0
