"""Извлечение и выбор email (PDF, HTML-страницы, приоритет источников)."""
from __future__ import annotations

import re

import fitz

from .text import alpha

TLD = (r"(?:ru|com|org|net|edu|gov|io|info|biz|name|eu|de|fr|uk|us|cn|jp|kr"
       r"|in|it|es|nl|se|fi|no|ch|at|cz|pl|by|kz|ua)")

EMAIL_RE = re.compile(rf"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+?\.{TLD}(?![A-Za-z])", re.IGNORECASE)

BRACE_RE = re.compile(rf"\{{([^{{}}@]+)\}}@([A-Za-z0-9.\-]+?\.{TLD})(?![A-Za-z])", re.IGNORECASE)  # {a,b}@domain

def match_authors(local_part: str, authors: list[tuple[str, set[str]]]) -> list[str]:
    """person_id, чья фамилия встречается в локальной части адреса."""
    lp = alpha(local_part)
    if not lp:
        return []
    return [pid for pid, surnames in authors if any(s in lp for s in surnames)]

def emails_from_pdf_text(text: str) -> set[str]:
    found = set()
    for inside, dom in BRACE_RE.findall(text):
        for part in re.split(r"[;,]", inside):
            part = part.strip().strip(".")
            if part:
                found.add(f"{part}@{dom}".lower())
    for m in EMAIL_RE.findall(text):
        found.add(m.lower().rstrip("."))
    return found

def emails_from_pdf(path) -> set[str]:
    try:
        doc = fitz.open(path)
    except Exception:
        return set()
    out: set[str] = set()
    for page in doc:
        out |= emails_from_pdf_text(page.get_text() or "")
    doc.close()
    return out

def usable_email(e: str | None) -> str | None:
    e = (e or "").strip().lower()
    return e if e and "@" in e and "noreply" not in e else None

def pick_email(cands: list[tuple[str, str]]) -> str | None:
    """Из кандидатов (src, email) — институциональный, иначе по порядку источника."""
    inst = [e for _, e in cands if e.endswith(("@itmo.ru", "ifmo.ru"))]
    if inst:
        return inst[0]
    order = {"orcid": 0, "page": 1, "pdf": 2, "commit": 3}
    return min(cands, key=lambda x: order.get(x[0], 9))[1] if cands else None

MAILTO_RE = re.compile(r"mailto:([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})", re.IGNORECASE)

SKIP_HOSTS = ("github.com", "linkedin.com", "scholar.google", "researchgate",
              "orcid.org", "twitter.com", "x.com", "facebook", "youtube",
              "t.me", "vk.com", "semanticscholar", "dblp.org", "publons")

def is_page(u: str | None) -> bool:
    u = (u or "").lower()
    return u.startswith("http") and not any(s in u for s in SKIP_HOSTS)

def deobfuscate(t: str) -> str:
    t = re.sub(r"&#0*64;|&#x0*40;|&commat;", "@", t, flags=re.IGNORECASE)
    t = re.sub(r"\s*[\[\(\{<]\s*at\s*[\]\)\}>]\s*", "@", t, flags=re.IGNORECASE)
    t = re.sub(r"\s*[\[\(\{<]\s*dot\s*[\]\)\}>]\s*", ".", t, flags=re.IGNORECASE)
    t = re.sub(r"\s*@\s*", "@", t)
    return t

def emails_from_html(html: str) -> set[str]:
    found = {m.lower() for m in MAILTO_RE.findall(html)}
    found |= {m.lower() for m in EMAIL_RE.findall(deobfuscate(html))}
    return found
