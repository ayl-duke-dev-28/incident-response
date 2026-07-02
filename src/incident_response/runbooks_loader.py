"""Load markdown runbooks from disk. Each file may include a small frontmatter block:

---
title: Redis cache poisoning
tags: [cache, redis, checkout]
---

Body here.
"""

from __future__ import annotations

import re
from pathlib import Path

from .models import Runbook

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}, text
    raw = match.group(1)
    body = text[match.end() :]
    meta: dict[str, str] = {}
    for line in raw.splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            meta[key.strip()] = value.strip()
    return meta, body


def _parse_tags(raw: str) -> list[str]:
    stripped = raw.strip().strip("[]")
    if not stripped:
        return []
    return [t.strip().strip("\"'") for t in stripped.split(",") if t.strip()]


def load_runbooks(directory: Path) -> list[Runbook]:
    if not directory.exists():
        return []
    books: list[Runbook] = []
    for path in sorted(directory.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        meta, body = _parse_frontmatter(text)
        books.append(
            Runbook(
                slug=path.stem,
                title=meta.get("title", path.stem.replace("-", " ").title()),
                tags=_parse_tags(meta.get("tags", "")),
                content=body.strip(),
                path=str(path),
            )
        )
    return books
