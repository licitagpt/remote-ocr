"""Lotes de revisao: um unico .json (<out_dir>/lotes.json) com todos os lotes.

Lote = N paginas de OCR sorteadas entre os documentos que ja tem resultado gravado para os motores escolhidos
(<out_dir>/<motor>/...). Cada pagina (item) e vista lado a lado e recebe um veredito opcional: qual motor saiu
melhor (`best` = nome do motor, "empate" ou "nenhum") e uma observacao. O lote guarda a semente do sorteio para
ser reproduzivel.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

SCHEMA = 1
VERDICT_OTHER = ("empate", "nenhum")      # alem do nome de um motor do lote


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Store:
    """{"schema", "lotes": [...]}; gravado por arquivo temporario + rename a cada alteracao."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self.data: dict[str, Any] = {"schema": SCHEMA, "lotes": []}
        if self.path.is_file():
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
            self.data.setdefault("lotes", [])

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)

    # ------------------------------------------------------------------ lotes
    @property
    def lotes(self) -> list[dict[str, Any]]:
        return self.data["lotes"]

    def get(self, lid: int) -> dict[str, Any] | None:
        return next((b for b in self.lotes if b["id"] == lid), None)

    def new(self, name: str, size: int, seed: int, engines: list[str], items: list[dict[str, Any]],
            version: str, universe: dict[str, int]) -> dict[str, Any]:
        with self._lock:
            lid = max((b["id"] for b in self.lotes), default=0) + 1
            lote = {"id": lid, "name": name or f"lote {lid}", "created_at": now(), "version": version,
                    "seed": seed, "size": len(items), "requested": size, "engines": list(engines),
                    "universe": universe, "items": items}
            self.lotes.append(lote)
            self.save()
            return lote

    def delete(self, lid: int) -> bool:
        with self._lock:
            before = len(self.lotes)
            self.data["lotes"] = [b for b in self.lotes if b["id"] != lid]
            if len(self.lotes) != before:
                self.save()
                return True
            return False

    # ------------------------------------------------------------------ vereditos
    def set_verdict(self, lid: int, item_id: int, best: str | None, note: str) -> dict[str, Any]:
        """best = motor | "empate" | "nenhum"; None apaga o veredito (a observacao sozinha e mantida)."""
        with self._lock:
            lote = self.get(lid)
            if lote is None:
                raise KeyError(f"lote {lid} nao existe")
            item = next((it for it in lote["items"] if it["id"] == item_id), None)
            if item is None:
                raise KeyError(f"item {item_id} nao existe no lote {lid}")
            if best is not None and best not in lote["engines"] and best not in VERDICT_OTHER:
                raise ValueError(f"veredito {best!r} invalido: use um motor do lote, 'empate' ou 'nenhum'")
            note = (note or "").strip()
            if best is None and not note:
                item.pop("verdict", None)
            else:
                item["verdict"] = {"best": best, "note": note, "at": now()}
            self.save()
            return item


# ---------------------------------------------------------------------- estatisticas
def lote_stats(lote: dict[str, Any]) -> dict[str, Any]:
    items = lote.get("items", [])
    tally: dict[str, int] = {e: 0 for e in lote["engines"]}
    tally.update({k: 0 for k in VERDICT_OTHER})
    reviewed = 0
    for it in items:
        v = it.get("verdict") or {}
        if v.get("best"):
            reviewed += 1
            tally[v["best"]] = tally.get(v["best"], 0) + 1
    notes = [{"item_id": it["id"], "pncp": it["pncp"], "file": it["file"], "page": it["page"],
              "best": it["verdict"].get("best"), "note": it["verdict"].get("note", "")}
             for it in items if (it.get("verdict") or {}).get("note")]
    return {"total": len(items), "reviewed": reviewed, "pending": len(items) - reviewed, "tally": tally,
            "notes": notes}


def lote_summary(lote: dict[str, Any]) -> dict[str, Any]:
    st = lote_stats(lote)
    return {k: v for k, v in lote.items() if k != "items"} | {
        "total": st["total"], "reviewed": st["reviewed"], "pending": st["pending"], "tally": st["tally"]}


__all__ = ["Store", "SCHEMA", "VERDICT_OTHER", "lote_stats", "lote_summary", "now"]
