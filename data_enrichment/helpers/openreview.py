"""Клиент OpenReview, подтверждение принадлежности ИТМО, разбор профиля."""
from __future__ import annotations

import time

import requests

from ..config import (
    HTTP_TIMEOUT,
    OPENREVIEW_API_URL,
    OPENREVIEW_PASSWORD,
    OPENREVIEW_RATE_LIMIT_SLEEP,
    OPENREVIEW_USERNAME,
)
from .text import norm, tail

LINK_KEYS = ("homepage", "gscholar", "dblp", "orcid", "github", "linkedin")

def search_terms(name_en: str):
    """Имя целиком, затем «Имя Фамилия» без инициалов."""
    yield name_en
    toks = [t for t in name_en.split() if len(t.strip(".")) > 1]
    if len(toks) >= 2 and f"{toks[0]} {toks[-1]}" != name_en:
        yield f"{toks[0]} {toks[-1]}"

class OpenReviewClient:
    """Логин в OpenReview один раз + поиск профилей по имени."""

    def __init__(self) -> None:
        self.s = requests.Session()
        r = self.s.post(
            f"{OPENREVIEW_API_URL}/login",
            json={"id": OPENREVIEW_USERNAME, "password": OPENREVIEW_PASSWORD},
            timeout=HTTP_TIMEOUT,
        )
        r.raise_for_status()
        self.s.headers["Authorization"] = f"Bearer {r.json()['token']}"

    def search(self, term: str) -> list[dict]:
        try:
            r = self.s.get(
                f"{OPENREVIEW_API_URL}/profiles/search", params={"term": term},
                timeout=HTTP_TIMEOUT,
            )
        except requests.RequestException:
            return []
        if r.status_code == 429:
            time.sleep(OPENREVIEW_RATE_LIMIT_SLEEP)
            return self.search(term)
        if r.status_code != 200:
            return []
        return r.json().get("profiles", [])

def verify(content: dict, name_norms: set[str], our_orcid: str | None) -> str | None:
    """Возвращает способ подтверждения принадлежности ИТМО или None."""
    cand_orcid = tail(content.get("orcid"))
    if our_orcid and cand_orcid and our_orcid == cand_orcid:
        return "orcid"
    cand_names = {norm(n.get("fullname")) for n in content.get("names", []) if n.get("fullname")}
    if not (cand_names & name_norms):
        return None
    emails = content.get("emails") or []
    if any((e or "").lower().endswith("@itmo.ru") for e in emails):
        return "itmo_email"
    if any("itmo" in ((h.get("institution") or {}).get("name") or "").lower()
           for h in content.get("history", [])):
        return "itmo_affil"
    return None

def extract(profile: dict, matched_by: str, name_en: str) -> dict:
    c = profile.get("content", {})
    links = {k: c.get(k) for k in LINK_KEYS}
    return {
        "openreview_id": profile.get("id"),
        "name_en": name_en,
        "matched_by": matched_by,
        "names": [n.get("fullname") for n in c.get("names", []) if n.get("fullname")],
        "emails_masked": c.get("emails") or [],
        "affiliations": [
            {"name": (h.get("institution") or {}).get("name"),
             "position": h.get("position"), "start": h.get("start"), "end": h.get("end")}
            for h in c.get("history", [])
        ],
        "relations": [
            {"relation": r.get("relation"), "name": r.get("name")}
            for r in c.get("relations", [])
        ],
        **links,
    }
