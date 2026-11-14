"""Проверка связи с текстовой моделью: python -m marketer.llm_check
Печатает, куда идёт запрос, код ответа и начало ответа. Токен не печатается.
Для Yandex Cloud дополнительно перебирает формат авторизации и модель, чтобы найти причину 401/403."""
import asyncio
import sys

import httpx

from .config import Settings
from .llm import auth_headers


async def try_once(client, url, model, headers):
    body = {"model": model, "messages": [{"role": "user", "content": "Ответь одним словом: работает"}]}
    try:
        r = await client.post(url, json=body, headers=headers)
    except httpx.HTTPError as e:
        return None, f"НЕТ СВЯЗИ: {type(e).__name__}: {e}"
    return r, (r.text[:300] or "(пустое тело)").replace("\n", " ")


async def main() -> int:
    s = Settings.load()
    key = s.openai_api_key
    print(f"Провайдер: {s.llm_provider} | модель: {s.openai_model} | адрес: {s.openai_base_url}")
    shown = "без ключа" if key.lower() == "none" else (key[:6] + "…" if key else "—")
    print(f"Ключ: {shown} | длина: {len(key)}")
    if s.llm_provider != "openai" or not key:
        print("Проверка нужна для LLM_PROVIDER=openai с заданным OPENAI_API_KEY.")
        return 1
    url = s.openai_base_url.rstrip("/") + "/chat/completions"
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as c:
        headers = auth_headers(s.openai_base_url, key, s.openai_model, s.openai_auth_scheme)
        r, text = await try_once(c, url, s.openai_model, headers)
        scheme = headers.get("Authorization", "—").split(" ")[0]
        print(f"[основная настройка: {scheme}] HTTP {r.status_code if r is not None else '—'} | {text}")
        if r is not None and r.status_code == 200:
            try:
                print("ОТВЕТ МОДЕЛИ:", r.json()["choices"][0]["message"]["content"])
                return 0
            except Exception:
                pass
        if "api.cloud.yandex.net" not in s.openai_base_url:
            return 1
        # Диагностика Yandex: другой формат ключа и другая модель
        folder = s.openai_model[len("gpt://"):].split("/", 1)[0] if s.openai_model.startswith("gpt://") else ""
        variants = []
        for sch in ("Api-Key", "Bearer"):
            for model in (s.openai_model, f"gpt://{folder}/yandexgpt-lite/latest"):
                variants.append((sch, model))
        print("\nДиагностика Yandex Cloud (формат ключа × модель):")
        ok = False
        for sch, model in variants:
            h = {"Authorization": f"{sch} {key}"}
            if folder:
                h["x-folder-id"] = folder
            r2, t2 = await try_once(c, url, model, h)
            code = r2.status_code if r2 is not None else "—"
            print(f"  {sch:8} | {model.split('/', 3)[-2] if model.startswith('gpt://') else model:16} | HTTP {code} | {t2[:120]}")
            ok = ok or code == 200
        if not ok:
            print("\nВсе варианты отклонены. Проверьте: роль ai.languageModels.user выдана сервисному аккаунту "
                  f"на каталог {folder or '?'}; у API-ключа область yc.ai.languageModels.execute; "
                  "после выдачи роли подождите 1–2 минуты.")
        return 0 if ok else 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(asyncio.run(main()))
