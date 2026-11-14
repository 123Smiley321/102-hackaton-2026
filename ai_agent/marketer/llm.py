"""Провайдеры языковой модели (async, HTTPX). Меняются переменной LLM_PROVIDER без потери данных компаний.

- offline : без сети и ключей, шаблонный писатель (тесты, план Б на защите)
- openai  : любой OpenAI-совместимый API (OpenAI, YandexGPT, DeepSeek через агрегатор)
- codex   : аккаунт ChatGPT через Codex CLI (`codex login --device-auth`), см. docs/chatgpt_connection.md
"""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import httpx

from .config import Settings


class LLMError(RuntimeError):
    pass


def find_exe(name: str) -> str | None:
    """Путь к программе. На Windows npm кладёт рядом shell-скрипт без расширения, codex.cmd и codex.ps1 —
    запускать можно только .exe/.cmd/.bat, поэтому ищем их первыми."""
    import os
    if os.name == "nt" and not os.path.splitext(name)[1]:
        for ext in (".exe", ".cmd", ".bat"):
            found = shutil.which(name + ext)
            if found:
                return found
    return shutil.which(name)


class LLM:
    name = "base"
    engine_label = "base"

    async def complete(self, system: str, user: str, json_mode: bool = False) -> str:  # pragma: no cover
        raise NotImplementedError


class OfflineLLM(LLM):
    """Не вызывает модель. Агент видит name == 'offline' и использует шаблонный писатель."""
    name = "offline"
    engine_label = "offline (шаблоны, без модели)"

    async def complete(self, system: str, user: str, json_mode: bool = False) -> str:
        raise LLMError("offline provider: генерация идёт через offline_writer")


class OpenAICompatibleLLM(LLM):
    name = "openai"

    def __init__(self, s: Settings):
        if not s.openai_api_key:
            raise LLMError("OPENAI_API_KEY не задан")
        self.s = s
        self.engine_label = f"openai-compatible ({s.openai_model} @ {s.openai_base_url})"

    async def complete(self, system: str, user: str, json_mode: bool = False) -> str:
        body = {
            "model": self.s.openai_model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": 0.6,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        url = self.s.openai_base_url.rstrip("/") + "/chat/completions"
        headers = auth_headers(self.s.openai_base_url, self.s.openai_api_key, self.s.openai_model,
                               self.s.openai_auth_scheme)
        try:
            async with httpx.AsyncClient(timeout=180, follow_redirects=True) as client:
                for attempt in range(4):  # 429 «слишком часто»: ждём и повторяем
                    r = await client.post(url, json=body, headers=headers)
                    if r.status_code != 429 or attempt == 3:
                        break
                    wait = float(r.headers.get("retry-after") or self.s.llm_retry_wait)
                    await asyncio.sleep(min(wait, 60))
                if r.status_code == 400 and "response_format" in body:
                    # не все провайдеры поддерживают JSON-режим — повторяем без него, JSON достанет parse_json
                    body.pop("response_format")
                    r = await client.post(url, json=body, headers=headers)
                if r.status_code >= 400:
                    raise LLMError(f"Ошибка LLM API {r.status_code}: {r.text[:300]}")
        except httpx.HTTPError as e:
            raise LLMError(f"Ошибка LLM API: {e}") from e
        return extract_content(r)


class CodexChatGPTLLM(LLM):
    """Аккаунт ChatGPT: запросы идут через `codex exec` под входом администратора в ChatGPT."""
    name = "codex"
    engine_label = "codex (ChatGPT account)"

    def __init__(self, s: Settings):
        if not find_exe(s.codex_bin):
            raise LLMError("Codex CLI не найден: npm i -g @openai/codex && codex login --device-auth")
        self.s = s
        # Без подмены: работаем только если вход выполнен именно аккаунтом ChatGPT.
        # API-ключ в Codex — это оплата по токенам, а не подписка, поэтому его здесь не принимаем.
        method, raw = self.login_status()
        if method != "chatgpt":
            raise LLMError(f"LLM_PROVIDER=codex, но вход через аккаунт ChatGPT не выполнен ({raw}). "
                           "Выполните: codex login --device-auth")
        self.login_raw = raw

    def login_status(self) -> tuple[str, str]:
        """chatgpt | api_key | none | unknown и сырой вывод `codex login status` (без секретов)."""
        exe = find_exe(self.s.codex_bin) or self.s.codex_bin
        p = subprocess.run([exe, "login", "status"], capture_output=True, text=True, timeout=30,
                           encoding="utf-8", errors="replace")
        raw = (p.stdout + p.stderr).strip().splitlines()[0] if (p.stdout + p.stderr).strip() else ""
        low = raw.lower()
        if "not logged in" in low:
            return "none", raw
        if "chatgpt" in low:
            return "chatgpt", raw
        if "api key" in low:
            return "api_key", raw
        return "unknown", raw

    async def complete(self, system: str, user: str, json_mode: bool = False) -> str:
        prompt = f"{system}\n\n---\n\n{user}\n\nОтветь только текстом ответа, ничего не выполняй и не меняй файлы."
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "last.txt"
            cmd = [self.s.codex_bin, "exec", "--skip-git-repo-check", "--sandbox", "read-only",
                   "--output-last-message", str(out)]
            if self.s.codex_model:
                cmd += ["--model", self.s.codex_model]
            cmd.append("-")  # задание читается из stdin: на Windows длинный многострочный аргумент обрезается
            exe = find_exe(cmd[0]) or cmd[0]  # на Windows это codex.cmd
            proc = await asyncio.create_subprocess_exec(exe, *cmd[1:], cwd=tmp, stdin=asyncio.subprocess.PIPE,
                                                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            try:
                _, err = await asyncio.wait_for(proc.communicate(prompt.encode("utf-8")), timeout=300)
            except asyncio.TimeoutError as e:
                proc.kill()
                raise LLMError("codex exec: превышено время ожидания") from e
            if proc.returncode != 0 or not out.exists():
                raise LLMError(f"codex exec завершился с ошибкой: {err.decode(errors='ignore')[-500:]}")
            return out.read_text(encoding="utf-8")


def auth_headers(base_url: str, key: str, model: str = "", scheme: str = "") -> dict:
    """Заголовки авторизации. OPENAI_API_KEY=none — без ключа (анонимный Pollinations).
    Yandex Cloud: API-ключ сервисного аккаунта передаётся как «Api-Key <ключ>», плюс ID каталога
    (берём из модели gpt://<folder_id>/...). OPENAI_AUTH_SCHEME=Bearer|Api-Key — принудительно."""
    if not key or key.lower() == "none":
        return {}
    yandex = "api.cloud.yandex.net" in base_url
    scheme = scheme or ("Api-Key" if yandex else "Bearer")
    headers = {"Authorization": f"{scheme} {key}"}
    if yandex and model.startswith("gpt://"):
        headers["x-folder-id"] = model[len("gpt://"):].split("/", 1)[0]
    return headers


def extract_content(r: httpx.Response) -> str:
    """Текст ответа модели или понятная ошибка: что именно пришло вместо JSON (без секретов)."""
    try:
        return r.json()["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as e:
        ctype = r.headers.get("content-type", "—")
        snippet = r.text[:300].replace("\n", " ") if r.text else "(пустое тело)"
        raise LLMError(f"Модель вернула не тот формат: HTTP {r.status_code}, content-type {ctype}, "
                       f"адрес {r.url}, начало ответа: {snippet}") from e


def make_llm(s: Settings) -> LLM:
    if s.llm_provider == "openai":
        return OpenAICompatibleLLM(s)
    if s.llm_provider == "codex":
        return CodexChatGPTLLM(s)
    return OfflineLLM()


def parse_json(text: str) -> dict:
    """Достаёт JSON из ответа модели (модели иногда оборачивают его в ```json)."""
    text = text.strip()
    if "```" in text:
        text = text.split("```")[1]
        text = text[4:] if text.startswith("json") else text
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise LLMError("Модель не вернула JSON")
    return json.loads(text[start:end + 1])
