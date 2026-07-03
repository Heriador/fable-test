"""Knowledge base of known DCM4CHEE error signatures with causes and fixes."""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

_KB_PATH = Path(__file__).parent / "knowledge_base.yaml"


@dataclass(frozen=True)
class KBEntry:
    id: str
    title: str
    signatures: tuple[re.Pattern[str], ...]
    cause: str
    resolution: tuple[str, ...]
    severity: str
    references: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "severity": self.severity,
            "cause": self.cause,
            "resolution": list(self.resolution),
        }


@lru_cache
def load_entries() -> tuple[KBEntry, ...]:
    with open(_KB_PATH, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    entries = []
    for item in raw["entries"]:
        entries.append(
            KBEntry(
                id=item["id"],
                title=item["title"],
                signatures=tuple(
                    re.compile(sig, re.IGNORECASE) for sig in item["signatures"]
                ),
                cause=item["cause"].strip(),
                resolution=tuple(item["resolution"]),
                severity=item["severity"],
                references=tuple(item.get("references", ())),
            )
        )
    return tuple(entries)


def match(text: str) -> list[KBEntry]:
    """Return KB entries whose signatures match ``text``, best match first.

    Ranked by number of matching signature patterns, then by earliest match
    position in the text.
    """
    scored: list[tuple[int, int, KBEntry]] = []
    for entry in load_entries():
        hits = 0
        first_pos = len(text)
        for rx in entry.signatures:
            m = rx.search(text)
            if m:
                hits += 1
                first_pos = min(first_pos, m.start())
        if hits:
            scored.append((-hits, first_pos, entry))
    scored.sort(key=lambda item: (item[0], item[1]))
    return [entry for _, _, entry in scored]
