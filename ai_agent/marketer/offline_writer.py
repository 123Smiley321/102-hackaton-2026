"""Шаблонный писатель для режима без модели (тесты, план Б на защите).
Работает по тем же правилам, что и промпт-контракт: берёт только факты из базы знаний компании,
причём только предложения с метками официальных источников [S…], без служебных пояснений.
"""
from __future__ import annotations

import re

from .knowledge import tokens

SRC_RE = re.compile(r"\[(S\d+)(?:\s*[–-]\s*S?(\d+))?\]")
# служебные/методические предложения из документов — не для публикации
META_RE = re.compile(
    r"\b(AI|MVP|ТЗ|гипотез\w*|демонстрац\w*|критери\w*|сценари\w*|согласован\w*|шаблон\w*|запрещ\w*|"
    r"не придумыва\w*|не выдава\w*|не заявля\w*|не указыва\w*|не писать|провер\w*|свер\w*|"
    r"необходимо|нельзя|должен|должны|использовать|пример\w*|примечани\w*|статус|источник\w*|"
    r"сегмент\w*|CTA|задачи аудитории|рабочие)\b", re.I)
SHORT_RE = re.compile(r"\b(короче|кратк\w*|сократи\w*)", re.I)
LINK_RE = re.compile(r"\b(ссылк\w*|каталог\w*)", re.I)


def _clean(text: str) -> list[str]:
    return [l.strip() for l in text.splitlines() if l.strip() and not l.lstrip().startswith("#")]


def _facts(sources: list[dict], query: str, n: int) -> list[tuple[str, str, list[str]]]:
    """(предложение без меток, id фрагмента, метки S…) — самые близкие к запросу факты с официальными источниками."""
    q = set(tokens(query))
    cand = []
    for rank, c in enumerate(sources):
        for line in _clean(c["text"]):
            line_refs = [r[0] for r in SRC_RE.findall(line)]  # метка в конце строки относится ко всей строке
            for s in re.split(r"(?<=[.!?])\s+", line):
                refs = [r[0] for r in SRC_RE.findall(s)] or line_refs
                if not refs or META_RE.search(s) or s.startswith("[S") or len(s) < 30:
                    continue
                plain = SRC_RE.sub("", s).strip().rstrip(" .") + "."
                cand.append((len(q & set(tokens(plain))) - rank * 0.1, plain, c["sid"], refs))
    cand.sort(key=lambda x: -x[0])
    related = [c for c in cand if c[0] >= 1]  # хотя бы одно общее слово с темой
    cand = related or cand[:1]
    out, seen = [], set()
    for _, s, sid, refs in cand:
        if s not in seen:
            seen.add(s)
            out.append((s, sid, refs))
        if len(out) == n:
            break
    return out


def _about(topic: str) -> str:
    return ("об " if topic[:1].lower() in "аоуэиё" else "о ") + topic


def write_post(topic: str, segment: dict | None, profile: dict, sources: list[dict], max_len: int,
               revision: str = "") -> dict:
    short, link = bool(SHORT_RE.search(revision)), bool(LINK_RE.search(revision))
    focus = segment.get("focus", "") if segment else ""
    facts = _facts(sources, f"{topic} {focus} {revision}", 1 if short else 3)
    body = [f"Рассказываем {_about(topic)}." if topic else f"Новости {profile['name']}."]
    if segment:
        body.append(f"Материал для специалистов: {segment['name']}.")
    body += [f for f, _, _ in facts]
    catalog = profile.get("catalog_url")
    cta = segment["cta"] if segment and segment.get("cta") else profile.get("default_cta", "")
    if link and catalog and catalog not in cta:
        body.append(f"Каталог продукции: {catalog}")
    body.append(cta)
    text = "\n\n".join(b for b in body if b)
    if len(text) > max_len:
        text = text[:max_len].rsplit(". ", 1)[0] + "."
    urls = profile.get("source_urls", {})
    refs = [r for _, _, rs in facts for r in rs]
    source_url = catalog if (link and catalog) else (urls.get(refs[0]) if refs else None)
    tag = re.sub(r"[^\wА-Яа-яЁё]", "", profile["name"].replace("АО", ""))
    return {
        "title": topic[:1].upper() + topic[1:] if topic else profile["name"],
        "text": text,
        "hashtags": [f"#{tag}", "#архитектурноестекло" if "архитект" in topic.lower() else "#стекло"],
        "image_prompt": profile.get("image_style") or f"modern building with glass facade, {topic}, no text, no logos",
        "source_ids": sorted({sid for _, sid, _ in facts}),
        "facts_used": [{"fact": f, "sid": sid, "refs": rs, "urls": [urls[r] for r in rs if r in urls]}
                       for f, sid, rs in facts],
        "source_url": source_url,
        "audience": segment["name"] if segment else profile.get("audience", "клиенты компании"),
        "summary_for_client": (f"Тема: {topic}; адресат: {segment['name'] if segment else 'нейтральный B2B-тон'}; "
                               f"опора на материалы {', '.join(sorted({s for _, s, _ in facts})) or '—'}."),
    }
