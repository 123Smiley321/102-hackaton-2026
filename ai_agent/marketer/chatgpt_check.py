"""Задача 5 / критерий 12: проверка технической возможности использовать существующий аккаунт ChatGPT.

    python -m marketer.chatgpt_check                       # локально
    docker compose run --rm ai-agent python -m marketer.chatgpt_check

Что проверяется (каждый шаг пишет результат, ничего не скрывается):
  1. установлен ли Codex CLI и какой версии;
  2. каким способом выполнен вход: аккаунт ChatGPT (подписка) или API-ключ (оплата по токенам) — это разные вещи,
     и API-ключ НЕ считается «существующим аккаунтом ChatGPT»;
  3. реальный тестовый запрос через `codex exec` с проверочным словом;
  4. что агент не подменяет ChatGPT молча: при LLM_PROVIDER=codex без входа сервис отвечает ошибкой,
     а не переключается на другую модель.
Итог сохраняется в docs/chatgpt_check_result.md (без токенов и секретов).
"""
from __future__ import annotations

import asyncio
import json
import platform
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from .config import Settings
from .llm import find_exe

MARKER = "ПРОВЕРКА-CHATGPT-OK"


def codex_version(bin_: str) -> str | None:
    exe = find_exe(bin_)
    if not exe:
        return None
    p = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=30, encoding="utf-8", errors="replace")
    return (p.stdout or p.stderr).strip()


def login_status(bin_: str) -> tuple[str, str]:
    """Возвращает (метод, сырой вывод): chatgpt | api_key | none | unknown."""
    exe = find_exe(bin_) or bin_
    p = subprocess.run([exe, "login", "status"], capture_output=True, text=True, timeout=30, encoding="utf-8", errors="replace")
    raw = (p.stdout + p.stderr).strip()
    low = raw.lower()
    if "not logged in" in low:
        return "none", raw
    if "chatgpt" in low:
        return "chatgpt", raw
    if "api key" in low:
        return "api_key", raw
    return "unknown", raw


async def test_request(s: Settings) -> tuple[bool, str, float]:
    from .llm import CodexChatGPTLLM, LLMError
    t0 = time.monotonic()
    try:
        answer = await CodexChatGPTLLM(s).complete("Ты тестовый агент.", f"Ответь ровно одной строкой: {MARKER}")
    except LLMError as e:
        return False, str(e), time.monotonic() - t0
    return MARKER in answer, answer.strip()[:200], time.monotonic() - t0


async def no_silent_fallback(s: Settings, method: str) -> tuple[bool, str]:
    """При LLM_PROVIDER=codex агент либо работает через аккаунт ChatGPT, либо явно отказывается стартовать."""
    from .llm import LLMError, make_llm
    s2 = Settings(**{**s.__dict__, "llm_provider": "codex"})
    try:
        llm = make_llm(s2)
    except LLMError as e:
        if method == "chatgpt":
            return False, f"вход через ChatGPT есть, но провайдер не создан: {e}"
        return True, f"без входа через ChatGPT агент явно отказывается работать: «{str(e)[:120]}»"
    if llm.name != "codex":
        return False, f"вместо codex выбран {llm.name}"
    return True, "выбран провайдер codex, каждый ответ помечен engine = «codex (ChatGPT account)»"


async def run(s: Settings) -> dict:
    r: dict = {"checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "host": platform.platform(), "codex_bin": s.codex_bin}
    r["codex_version"] = codex_version(s.codex_bin)
    if not r["codex_version"]:
        r.update(login_method="none", login_raw="Codex CLI не установлен", request_ok=False,
                 request_detail="пропущено", request_seconds=0.0)
    else:
        r["login_method"], r["login_raw"] = login_status(s.codex_bin)
        if r["login_method"] == "chatgpt":
            r["request_ok"], r["request_detail"], sec = await test_request(s)
            r["request_seconds"] = round(sec, 1)
        else:
            r.update(request_ok=False, request_detail="пропущено: нет входа через аккаунт ChatGPT", request_seconds=0.0)
    r["no_silent_fallback"], r["no_silent_fallback_detail"] = await no_silent_fallback(s, r["login_method"])
    r["verdict"] = verdict(r)
    return r


def verdict(r: dict) -> str:
    if r["login_method"] == "chatgpt" and r["request_ok"]:
        return ("ПОДТВЕРЖДЕНО: агент генерирует текст через существующий аккаунт ChatGPT (вход в Codex CLI), "
                "запросы идут в счёт лимитов подписки.")
    if not r["codex_version"]:
        return "НЕ ПРОВЕРЕНО: на этой машине нет Codex CLI (npm i -g @openai/codex)."
    if r["login_method"] == "none":
        return "НЕ ПРОВЕРЕНО: Codex CLI установлен, но вход в аккаунт ChatGPT не выполнен (codex login --device-auth)."
    if r["login_method"] == "api_key":
        return ("НЕ ПОДТВЕРЖДЕНО: вход выполнен API-ключом, а не аккаунтом ChatGPT. Это оплата по токенам, "
                "а не подписка — так и указываем, без подмены.")
    if r["login_method"] == "chatgpt":
        return ("НЕ ПОДТВЕРЖДЕНО: вход через ChatGPT есть, но тестовый запрос не прошёл "
                f"({r['request_detail']}). Возможные причины: лимит подписки, регион, сеть.")
    return "НЕ ПРОВЕРЕНО: неизвестный статус входа, см. вывод codex login status."


def to_markdown(r: dict) -> str:
    ok = lambda b: "да" if b else "нет"  # noqa: E731
    method = {"chatgpt": "аккаунт ChatGPT (подписка)", "api_key": "API-ключ (оплата по токенам)",
              "none": "не выполнен", "unknown": "не распознан"}[r["login_method"]]
    return f"""# Результат проверки подключения аккаунта ChatGPT

Сформировано автоматически: `python -m marketer.chatgpt_check`. Секреты и токены в отчёт не попадают.

**Итог: {r['verdict']}**

| Проверка | Результат |
|---|---|
| Дата проверки (UTC) | {r['checked_at']} |
| Машина | {r['host']} |
| Codex CLI | {r['codex_version'] or 'не установлен'} |
| Способ входа | {method} — `{r['login_raw'].splitlines()[0] if r['login_raw'] else ''}` |
| Тестовый запрос через `codex exec` | {ok(r['request_ok'])}, {r['request_seconds']} с — {r['request_detail']} |
| Нет скрытой подмены другой моделью | {ok(r['no_silent_fallback'])} — {r['no_silent_fallback_detail']} |
{f"| Наблюдение при входе | {r['manual_note']} |" if r.get('manual_note') else ""}

## Что доступно через аккаунт ChatGPT в нашем решении
- Генерация и доработка текстов постов, ответы на вопросы о компании, анализ материалов — через `codex exec`.
- Каждый ответ агента несёт поле `engine`; текущий движок и способ входа видны в `GET /v1/engine`.

## Ограничения
- Подписка ChatGPT не включает OpenAI API; использование аккаунта возможно только через официальный вход Codex CLI.
- Генерации изображений в этом режиме нет: картинки делает отдельный провайдер (`IMAGE_PROVIDER`), он указан в `image_engine`.
- Действуют лимиты подписки; при их исчерпании агент отвечает ошибкой, а не переключается молча.
- Токен входа хранится в томе `codex_auth` и не попадает в репозиторий; при истечении нужен повторный `codex login --device-auth`.
- Доступность сервисов OpenAI зависит от региона и сети сервера.

<details><summary>JSON</summary>

```json
{json.dumps(r, ensure_ascii=False, indent=2)}
```
</details>
"""


def main() -> None:
    """python -m marketer.chatgpt_check [--note "наблюдение при ручной проверке"]"""
    import argparse
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--note", default="", help="что произошло при входе в ChatGPT (попадёт в отчёт)")
    args = ap.parse_args()
    s = Settings.load()
    r = asyncio.run(run(s))
    r["manual_note"] = args.note
    out = Path(__file__).resolve().parents[1] / "docs" / "chatgpt_check_result.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(to_markdown(r), encoding="utf-8")
    print(r["verdict"])
    print(f"Отчёт: {out}")


if __name__ == "__main__":
    main()
