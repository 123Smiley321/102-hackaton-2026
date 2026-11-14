"""База знаний компании (задача 14): загрузка документов и сайта, нарезка, поиск.

Поиск — BM25 по нормализованным словам (без внешних сервисов и эмбеддингов, работает офлайн).
Если понадобится — можно заменить на векторный поиск, интерфейс search() останется тем же.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path

import httpx

from .storage import CompanyRepo

WORD_RE = re.compile(r"[a-zа-яё0-9]+", re.IGNORECASE)
STOP = set("и в во на с со по для о об от до из к ко а но или не что это как же ли бы то у за при над под "
           "the of and a to in is for".split())


def normalize(word: str) -> str:
    """Грубый стемминг для русского: первые 5 букв (стекло/стекле/стеклом -> стекл)."""
    w = word.lower().replace("ё", "е")
    return w if w.isdigit() else w[:5]


def tokens(text: str) -> list[str]:
    return [normalize(w) for w in WORD_RE.findall(text) if w.lower() not in STOP and len(w) > 1]


# ---------- загрузка ----------
class _TextFromHTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts, self._skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "nav", "footer", "noscript"):
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style", "nav", "footer", "noscript") and self._skip:
            self._skip -= 1
        if tag in ("p", "div", "li", "h1", "h2", "h3", "br", "tr"):
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    p = _TextFromHTML()
    p.feed(html)
    text = "".join(p.parts)
    return re.sub(r"\n\s*\n+", "\n\n", re.sub(r"[ \t]+", " ", text)).strip()


def read_file(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in (".txt", ".md", ".csv"):
        return path.read_text(encoding="utf-8", errors="ignore")
    if ext in (".html", ".htm"):
        return html_to_text(path.read_text(encoding="utf-8", errors="ignore"))
    if ext == ".pdf":
        from pypdf import PdfReader  # pip install pypdf
        return "\n\n".join(page.extract_text() or "" for page in PdfReader(str(path)).pages)
    if ext == ".docx":
        import docx  # pip install python-docx
        return "\n\n".join(p.text for p in docx.Document(str(path)).paragraphs)
    raise ValueError(f"Формат {ext} пока не поддерживается")


async def fetch_url(url: str) -> str:
    async with httpx.AsyncClient(timeout=30, follow_redirects=True,
                                 headers={"User-Agent": "Mozilla/5.0 (AI-marketer bot)"}) as client:
        r = await client.get(url)
        r.raise_for_status()
        return html_to_text(r.text)


def read_upload(filename: str, data: bytes) -> str:
    """Файл, пришедший через API (multipart): сохраняем во временный файл и читаем тем же кодом."""
    import tempfile
    suffix = Path(filename).suffix.lower()
    if suffix in (".txt", ".md", ".csv"):
        return data.decode("utf-8", errors="ignore")
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
        f.write(data)
        tmp = Path(f.name)
    try:
        return read_file(tmp)
    finally:
        tmp.unlink(missing_ok=True)


def chunk_text(text: str, size: int = 700) -> list[str]:
    """Режем по абзацам, склеивая их до ~size символов, чтобы фрагмент был осмысленным."""
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks, buf = [], ""
    for p in paras:
        if len(buf) + len(p) > size and buf:
            chunks.append(buf)
            buf = ""
        while len(p) > size:  # очень длинный абзац
            cut = p.rfind(". ", 0, size)
            cut = cut + 1 if cut > size // 2 else size
            chunks.append(p[:cut].strip())
            p = p[cut:].strip()
        buf = f"{buf}\n\n{p}".strip()
    if buf:
        chunks.append(buf)
    return chunks


class KnowledgeBase:
    def __init__(self, repo: CompanyRepo):
        self.repo = repo

    async def add_text(self, name: str, text: str, source: str = "upload", kind: str = "facts"):
        """Возвращает (id документа, duplicate). Тот же текст повторно не добавляется."""
        chunks = chunk_text(text)
        if not chunks:
            raise ValueError(f"В документе {name} нет текста (если это скан PDF — нужен текстовый слой)")
        import hashlib
        digest = hashlib.sha256(" ".join(text.split()).encode("utf-8")).hexdigest()
        existing = await self.repo.find_document_by_hash(digest)
        if existing:
            await self.repo.log("document_duplicate", f"{name} = {existing.name}")
            return existing.id, True
        return await self.repo.add_document(name, source, chunks, kind=kind, content_hash=digest), False

    async def add_file(self, path: str | Path):
        path = Path(path)
        return await self.add_text(path.name, read_file(path), source=f"file:{path.name}")

    async def add_upload(self, filename: str, data: bytes, kind: str = "facts"):
        return await self.add_text(filename, read_upload(filename, data), source=f"upload:{filename}", kind=kind)

    async def add_url(self, url: str):
        return await self.add_text(url, await fetch_url(url), source=url)

    async def search(self, query: str, k: int = 5) -> list[dict]:
        """Возвращает k лучших фрагментов: [{'sid': 'S1', 'text', 'doc', 'score'}]."""
        return rank(await self.repo.all_chunks(), query, k)


def rank(rows: list[dict], query: str, k: int = 5) -> list[dict]:
    """BM25 по фрагментам одной компании."""
    if not rows:
        return []
    docs = [tokens(r["text"]) for r in rows]
    q = set(tokens(query))
    n, avg = len(docs), max(1.0, sum(map(len, docs)) / len(docs))
    df = Counter(t for d in docs for t in set(d))
    scored = []
    for r, d in zip(rows, docs):
        tf, score = Counter(d), 0.0
        for t in q:
            if t in tf:
                idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
                score += idf * tf[t] * 2.2 / (tf[t] + 1.2 * (0.25 + 0.75 * len(d) / avg))
        scored.append((score, r))
    scored.sort(key=lambda x: -x[0])
    top = [x for x in scored[:k] if x[0] > 0] or scored[:min(2, len(scored))]
    return [{"sid": f"K{i + 1}", "text": r["text"], "doc": r["doc"], "score": round(s, 2)}
            for i, (s, r) in enumerate(top)]


def format_sources(chunks: list[dict]) -> str:
    return "\n\n".join(f"[{c['sid']}] ({c['doc']}) {c['text']}" for c in chunks) or "(материалы не загружены)"
