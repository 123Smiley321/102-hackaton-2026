"""Демо AI-модуля через HTTP API — ровно так его вызывает бэкенд.
Шаги 1, 3, 4, 9 сценария MVP + доработка + изоляция компаний.

    python demo.py                                     # без Docker: SQLite во временной папке, offline-модель
    AI_AGENT_URL=http://localhost:8001 python demo.py  # против запущенного docker compose
"""
import json
import os
import sys
import tempfile
import uuid
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).parent
GLASS, CAFE, CHAT = str(uuid.uuid4()), str(uuid.uuid4()), "1001"
TMP = tempfile.mkdtemp()


def make_client():
    if os.getenv("AI_AGENT_URL"):
        import httpx
        return httpx.Client(base_url=os.environ["AI_AGENT_URL"], timeout=300,
                            headers={"X-Internal-Key": os.getenv("AI_AGENT_API_KEY", "")})
    os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{TMP}/demo.sqlite")
    from fastapi.testclient import TestClient
    from marketer.api import create_app
    from marketer.config import Settings
    s = Settings.load(ROOT / ".env")
    s.database_url, s.media_dir, s.api_key = f"sqlite+aiosqlite:///{TMP}/demo.sqlite", ROOT / "demo_output", ""
    c = TestClient(create_app(s))
    c.__enter__()
    return c


def show(title, r):
    r.raise_for_status()
    data = r.json()
    print(f"\n=== {title} ===  [engine: {data['engine']}]")
    print(data["text"])
    if data.get("draft"):
        d = data["draft"]
        print(f"--- content.text (v{d['version']}, status={d['status']}) ---\n" + d["content"]["text"])
        print("картинка:", d.get("image_path"), "| генератор:", d.get("image_engine"))
    return data


c = make_client()

# Шаг 1. Бэкенд при создании компаний передаёт профиль и материалы
# Профиль и 6 документов — пакет материалов от Лизы (задача 8)
PACK = ROOT / "demo_data/salavatsteklo"
PROFILE = json.loads((PACK / "profile.json").read_text(encoding="utf-8"))
c.put(f"/v1/companies/{GLASS}", json=PROFILE).raise_for_status()
for doc in sorted(PACK.glob("0*.md")):
    kind = PROFILE.get("document_kinds", {}).get(doc.name, "facts")  # 05 и 06 — правила/сценарий, не факты
    with open(doc, "rb") as f:
        c.post(f"/v1/companies/{GLASS}/documents", files={"file": (doc.name, f, "text/markdown")},
               data={"kind": kind}).raise_for_status()
c.put(f"/v1/companies/{CAFE}", json={"name": "Кофейня «Тестовая Б»", "tone": "дружелюбный"}).raise_for_status()
c.post(f"/v1/companies/{CAFE}/documents/text",
       json={"name": "о нас", "text": (ROOT / "demo_data/company_b/o_kompanii.md").read_text(encoding="utf-8")})
print("Анализ бизнеса:", c.post(f"/v1/companies/{GLASS}/analyze").json())


def msg(text):
    return c.post(f"/v1/companies/{GLASS}/messages", json={"chat_id": CHAT, "text": text})


# Шаги 2-4
v1 = show("Шаг 4: черновик v1", msg(
    "Подготовь пост для нашего тестового Telegram-канала об архитектурном стекле АО «Салаватстекло» и его "
    "применении в современных зданиях. Сделай текст деловым, добавь изображение и пришли на согласование"))["draft"]
show("Шаг 3: на какие сведения опираешься?", msg("На какие сведения о компании ты опираешься при подготовке поста?"))

# Шаг 6. Кнопка «На доработку» с комментарием
v2 = show("Доработка: v2", c.post(f"/v1/companies/{GLASS}/drafts/{v1['draft_id']}/revise",
                                  json={"comment": "сделай текст короче и добавь явную ссылку на каталог",
                                        "chat_id": CHAT}))["draft"]
c.post(f"/v1/companies/{GLASS}/drafts/{v2['draft_id']}/status", json={"status": "approved"})

# Шаг 9
show("Шаг 9: следующий пост с учётом истории", msg("Сделай следующий пост на эту же тему, но для архитекторов"))

# Критерий 9
leak = msg("Что ты знаешь про Лавандовый раф Б-42?").json()
print("\n=== Изоляция ===\nУтечка данных компании Б:", "Б-42" in str(leak["sources"]))
print("Чужой черновик через другую компанию:",
      c.get(f"/v1/companies/{CAFE}/drafts/{v1['draft_id']}").status_code, "(ожидаем 404)")
