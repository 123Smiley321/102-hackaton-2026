"""Проверка материала перед отправкой на согласование (ТЗ 3.3 «Проверка материалов»)."""
from __future__ import annotations

import re

# Рекламные обещания, которые нельзя давать без подтверждения
BANNED = [
    r"\bлучш\w*", r"\bсам(ый|ое|ая|ые|ого|ой)\b", r"№\s?1\b", r"\bномер один\b", r"\bгарантир\w*",
    r"\bгарантированн\w*", r"\bуникальн\w*", r"\bнепревзойд\w*", r"\bидеальн\w*", r"\b100\s?%", r"\bвечн\w*",
    r"\bво всех\b", r"\bсамая низкая цена\b", r"\bвысок\w* качеств\w*", r"\bвысококачественн\w*",
    r"\bнадёжн\w*|\bнадежн\w*", r"\bпревосходн\w*", r"\bлидер\w*",
]
NUM_RE = re.compile(r"\d[\d\s.,]*\d|\d")
MARK_RE = re.compile(r"\s*\[(?:[KS]\d+(?:\s*[–,-]\s*[KS]?\d+)*)\]")


def strip_marks(text: str) -> str:
    """Убирает служебные метки [K1], [S2], [S1–S5] из текста поста."""
    return MARK_RE.sub("", text)


def _sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?…])\s+", text.strip()) if s]


CTA_RE = re.compile(r"^(подробнее|узнайте|узнать|изучите|смотрите|посмотрите|переходите)\b", re.I)


def _norm(s: str) -> set[str]:
    return {w[:5] for w in re.findall(r"[а-яёa-z0-9]+", s.lower()) if len(w) > 3}


def dedupe_sentences(text: str, fixed: list | None = None) -> str:
    """Убирает повторяющиеся по смыслу предложения (≥80% общих слов) и лишние призывы к действию:
    из нескольких «Подробнее… / Узнайте больше…» остаётся последний."""
    out_paras, seen, cta_positions = [], [], []
    for para in text.split("\n"):
        kept = []
        for s in _sentences(para):
            n = _norm(s)
            if n and any(len(n & p) / max(1, min(len(n), len(p))) >= 0.8 for p in seen):
                if fixed is not None:
                    fixed.append(f"убран повтор: «{s[:80]}»")
                continue
            seen.append(n)
            kept.append(s)
            if CTA_RE.search(s):
                cta_positions.append((len(out_paras), len(kept) - 1))
        out_paras.append(kept)
    for pi, si in cta_positions[:-1]:  # все призывы, кроме последнего
        if fixed is not None:
            fixed.append(f"убран лишний призыв: «{out_paras[pi][si][:80]}»")
        out_paras[pi][si] = ""
    return re.sub(r"\n{3,}", "\n\n", "\n".join(" ".join(x for x in p if x) for p in out_paras)).strip()


def check_draft(draft: dict, source_texts: list[str], max_len: int, rules: list[dict] | None = None) -> dict:
    """Возвращает {'ok': bool, 'issues': [...], 'fixed': [...], 'text': исправленный текст}.

    rules — правила компании из профиля (attribution_rules): если предложение содержит pattern,
    в нём должно быть слово requires (например, показатели группы компаний — только со словом «группа»).
    """
    text = strip_marks(draft.get("text", ""))
    issues, fixed = [], []

    # 1. Рекламные обещания: удаляем предложение целиком и сообщаем об этом
    paragraphs = []
    for para in text.split("\n"):
        kept = []
        for s in _sentences(para):
            m = next((m for p in BANNED if (m := re.search(p, s, re.IGNORECASE))), None)
            if m:
                fixed.append(f"убрано предложение с оценкой «{m.group(0)}»: «{s[:120]}»")
            else:
                kept.append(s)
        paragraphs.append(" ".join(kept))
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(paragraphs)).strip()
    text = dedupe_sentences(text, fixed)

    # 2. Правила атрибуции компании
    for rule in rules or []:
        for s in _sentences(text):
            if re.search(rule["pattern"], s, re.IGNORECASE) and not re.search(rule["requires"], s, re.IGNORECASE):
                issues.append(f"{rule.get('message', 'нарушено правило компании')}: «{s[:80]}»")

    # 3. Числа, которых нет в материалах компании — возможная выдумка
    sources = " ".join(source_texts).replace(" ", "")
    for num in NUM_RE.findall(text):
        clean = num.replace(" ", "").strip(".,")
        if len(clean) >= 2 and clean not in sources:
            issues.append(f"число {clean} не найдено в материалах компании — проверьте факт")

    # 4. Опора на источники
    if not draft.get("source_ids"):
        issues.append("черновик не ссылается ни на один материал компании")

    # 5. Длина под площадку
    if len(text) > max_len:
        issues.append(f"текст длиннее лимита площадки: {len(text)} > {max_len}")

    # 6. Картинка
    if not draft.get("image_path"):
        issues.append("нет изображения")

    return {"ok": not issues, "issues": issues, "fixed": fixed, "text": text}
