"""Адаптация канонического текста под площадки. Чистые функции, без I/O."""
import re

TG_TEXT_LIMIT = 4096
TG_CAPTION_LIMIT = 1024


def html_to_tg_markdown(text: str) -> str:
    """ВК-разметка -> Telegram Markdown. Экранируем спецсимволы вне тегов."""
    text = re.sub(r"<br\s*/?>", "\n", text)
    text = re.sub(r"</?b>", "**", text)
    text = re.sub(r"</?i>", "__", text)
    text = re.sub(r"</?u>", "__", text)
    # экранируем Markdown-спецсимволы, которые не являются частью нашей разметки
    text = re.sub(r"(?<!\*)\*(?!\*)", r"\\*", text)
    return text


def tg_to_vk_html(text: str) -> str:
    """Telegram Markdown -> безопасный текст для ВК (ВК примет и plain text)."""
    text = text.replace("**", "")
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", text)  # inline-ссылки -> явные
    return text


def truncate_by_paragraphs(text: str, limit: int) -> str:
    """Обрезка по абзацам, а не посередине слова. Возвращает текст <= limit."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    last_break = max(cut.rfind("\n\n"), cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    if last_break > limit // 2:
        return cut[:last_break].rstrip() + "…"
    return cut.rstrip() + "…"


def adapt_for_platform(text: str, platform: str, has_image: bool) -> str:
    """Канонический текст -> готовый для площадки."""
    if platform == "telegram":
        out = html_to_tg_markdown(text)
        limit = TG_CAPTION_LIMIT - 200 if has_image else TG_TEXT_LIMIT - 100
        return truncate_by_paragraphs(out, limit)
    if platform == "vk":
        return truncate_by_paragraphs(tg_to_vk_html(text), 4500)
    return text
