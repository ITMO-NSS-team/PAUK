"""Нормализация имён/строк, разбор вариантов, UnionFind."""
from __future__ import annotations

import ast
import json
import re
import unicodedata


def alpha(s: str | None) -> str:
    s = "".join(c for c in unicodedata.normalize("NFKD", s or "") if not unicodedata.combining(c))
    return re.sub(r"[^a-z]", "", s.lower())

def orcid_tail(u: str | None) -> str | None:
    return u.rstrip("/").split("/")[-1] if u else None

def author_surnames(name_en: str, variants: str | None) -> set[str]:
    """Фамилии из name_en и name_variants, длиной >= 4."""
    surnames = set()
    try:
        names = [name_en] + (json.loads(variants) if variants else [])
    except (TypeError, ValueError):
        names = [name_en]
    for n in names:
        toks = [alpha(t) for t in (n or "").split() if alpha(t)]
        if len(toks) >= 2 and len(toks[-1]) >= 4:
            surnames.add(toks[-1])
    return surnames

def surname_match(family: str, surnames: set[str]) -> bool:
    return any(s == family or s in family or family in s for s in surnames)

def parse_variants(raw: str | None) -> list[str]:
    """name_variants хранится как JSON или python-list-литерал — парсим оба."""
    if not raw:
        return []
    for loader in (json.loads, ast.literal_eval):
        try:
            v = loader(raw)
            if isinstance(v, list):
                return [str(x).strip() for x in v if str(x).strip()]
        except (ValueError, SyntaxError, TypeError):
            continue
    return []

def tail_id(url: str | None) -> str | None:
    """Последний сегмент URL - для orcid/openalex/ror id."""
    return url.rstrip("/").split("/")[-1] if url else None

def norm(s: str | None) -> str:
    if not s:
        return ""
    s = re.sub(r"[^a-zа-я0-9\s]", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()

def tail(v: str | None) -> str | None:
    return v.rstrip("/").split("/")[-1] if v else None

def jloads(s):
    try:
        return json.loads(s) if s else []
    except (TypeError, ValueError):
        return []

def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))

def norm_name(s: str | None) -> str:
    if not s:
        return ""
    s = strip_accents(s).lower()
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()

def norm_email(e: str | None) -> str:
    return (e or "").strip().lower()

class UnionFind:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb

    def groups(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for node in self.parent:
            out.setdefault(self.find(node), []).append(node)
        return out

def normalize(s: str) -> str:
    if not s:
        return ""
    s = re.sub(r'["“”‘’«»]', "", s)
    return re.sub(r"\s+", " ", s).strip().lower()
