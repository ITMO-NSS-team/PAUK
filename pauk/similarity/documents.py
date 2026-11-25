"""Extract descriptive prose while retaining its source line numbers for review."""

import re
from dataclasses import dataclass
from html import unescape
from pathlib import PurePosixPath

_SKIP_SECTION = re.compile(
    r"^(?:cit(?:ation|ing)|how to cite|references|licen[cs]e|authors?|contributors?|"
    r"acknowledg[e]?ments?|contacts?|funding|supported by|contribution guide|table of contents|"
    r"цитирование|как цитировать|авторы|контакты|лицензия|благодарности|оглавление)\b", re.I,
)
_CITATION = re.compile(r"^(?:please cite\b|if you use .*?\bcit(?:e|ations?)\b|to cite\b|для цитирования\b)", re.I)
_BOILERPLATE = re.compile(
    r"^(?:pip3? install\b|python -m pip\b|conda (?:install|create)\b|git clone\b|"
    r"npm (?:install|ci)\b|docker (?:build|run|pull)\b|sudo\b|\$ )", re.I,
)
_LINK = re.compile(r"\[([^\[\]]*)\]\([^()\n]*\)")
_FENCE = re.compile(r"^(\x60{3,}|~{3,})")
_STACK_WORDS = {"python", "java", "javascript", "typescript", "rust", "ruby", "docker", "library",
                "package", "project", "framework", "repository", "библиотека", "проект"}


@dataclass(frozen=True)
class Fragment:
    source: str
    text: str
    line: int | None = None


def _prose(line: str) -> str:
    line = re.sub(r"\x60([^<\x60]+?)\s*<[^>]+>\x60_{1,2}", r"\1", line)
    line = re.sub(r"<[^<>\n]*>", " ", line)
    line = re.sub(r"!\[[^\[\]]*\](?:\([^()\n]*\)|\[[^\[\]]*\])?", "", line)
    line = _LINK.sub(r"\1", line)
    line = re.sub(r"https?://\S+", "", line)
    if "@" in line:
        line = re.sub(r"[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}", "", line)
    line = line.replace("**", "").replace("\x60", "")
    return " ".join(unescape(line).strip(" \t-+*#|\x60").split())


def _meaningful(text: str) -> bool:
    words = re.findall(r"[^\W\d_]+", text.casefold())
    return len(words) >= 2 and not _STACK_WORDS.issuperset(words)


def fragments(path: str, text: str) -> list[Fragment]:
    suffix = PurePosixPath(path).suffix.lower()
    rst = suffix in {".rst", ".rest"}
    adoc = suffix in {".adoc", ".asciidoc", ".asc"}
    lines = text.lstrip("\ufeff").splitlines()
    kept = []
    fence = ""
    skip_level = 0
    underline = -1
    directive = False
    bib_depth = 0
    citation_paragraph = False
    comment = False
    rst_levels: dict[str, int] = {}
    for i, original in enumerate(lines):
        line = original.strip()
        if not line:
            citation_paragraph = False
            continue
        if i == underline:
            continue
        if fence:
            if line == fence or (fence[0] in "\x60~" and line.startswith(fence) and len(set(line)) == 1):
                fence = ""
            continue
        if comment:
            _, end, line = line.partition("-->")
            if not end:
                continue
            comment = False
        if "<!--" in line:
            line = re.sub(r"<!--.*?-->", "", line)
            line, start, _ = line.partition("<!--")
            comment = bool(start)
        if len(line) > 16_384:
            # Embedded images and minified assets can occupy an entire README line.
            continue
        if match := _FENCE.match(line):
            fence = match[1]
            continue
        if adoc and line in {"----", "....", "++++", "////"}:
            fence = line
            continue
        if directive and original[:1].isspace():
            continue
        directive = False
        if rst and line.startswith(".. "):
            directive = True
            continue
        if rst:
            line = re.sub(r"\|[\w.-]+\|", "", line)
        if adoc:
            if (line.startswith(("//", "ifdef::", "ifndef::", "endif::", "include::"))
                    or (i == 1 and lines[0].startswith("= ") and re.search(r"<[^>]+>", line))):
                continue
            line = re.sub(r"\{[\w-]+\}\[([^]]*)\]", r"\1", line)
        if bib_depth or re.match(r"^@\w+\s*\{", line):
            bib_depth = max(0, bib_depth + line.count("{") - line.count("}"))
            citation_paragraph = False
            continue
        heading = re.match(r"^(#{1,6}|={1,6})\s+(.+)", line)
        level, title = (len(heading[1]), heading[2]) if heading else (0, "")
        if not level and i + 1 < len(lines):
            marker = lines[i + 1].strip()
            if re.fullmatch(r"([=\-~^\"*])\1{2,}", marker) and not adoc:
                level = rst_levels.setdefault(marker[0], len(rst_levels) + 1) if rst else (1 if marker[0] == "=" else 2)
                title, underline = line, i + 1
        if level:
            citation_paragraph = False
            if not skip_level or level <= skip_level:
                skip_level = level if _SKIP_SECTION.match(title) else 0
            continue
        if _CITATION.match(line.lstrip("-* ")):
            citation_paragraph = True
        if skip_level or citation_paragraph or _BOILERPLATE.match(line.lstrip("-* ")):
            continue
        if (re.match(r"^\[[^]]+\]:", line) or line.startswith(("image:", ":alt:", ":target:"))
                or adoc and (line.startswith(":") or re.fullmatch(r"\[[^]]+\]", line))):
            continue
        prose = _prose(line)
        if _meaningful(prose):
            kept.append(Fragment("readme", prose, i + 1))
    return kept


def prepare_document(description: str, path: str, body: bytes) -> tuple[list[Fragment], str | None]:
    """A short description can stand alone when the source file is absent or unusable."""
    parts = []
    description = _prose(description)
    if _meaningful(description):
        parts.append(Fragment("description", description))
    try:
        readme = body.decode("utf-8-sig")
        if "\0" in readme:
            return parts, "binary_readme"
    except UnicodeDecodeError:
        return parts, "non_utf8_readme"
    parts.extend(fragments(path, readme))
    return parts, None
