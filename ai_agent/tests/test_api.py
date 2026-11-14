"""Тесты через HTTP API, как его будет вызывать бэкенд. Без Postgres и ключей: SQLite + offline-модель.

    python -m unittest discover -s tests -v
"""
import json
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")  # чтобы import marketer.api не требовал БД

from fastapi.testclient import TestClient  # noqa: E402

from marketer.api import create_app  # noqa: E402
from marketer.checks import check_draft  # noqa: E402
from marketer.config import Settings  # noqa: E402
from marketer.llm import LLM  # noqa: E402

GLASS = CAFE = None
PACK = ROOT / "demo_data/salavatsteklo"
PROFILE = json.loads((PACK / "profile.json").read_text(encoding="utf-8"))
DOC_B = (ROOT / "demo_data/company_b/o_kompanii.md").read_text(encoding="utf-8")


def make_settings(tmp: str, **kw) -> Settings:
    s = Settings()
    # TEST_DATABASE_URL=postgresql+asyncpg://... — прогон на настоящем Postgres
    s.database_url = os.getenv("TEST_DATABASE_URL", f"sqlite+aiosqlite:///{tmp}/test.sqlite")
    s.media_dir, s.llm_provider, s.image_provider = Path(tmp) / "media", "offline", "offline"
    s.media_base_url, s.api_key = "http://ai-agent:8000/media", "secret"
    for k, v in kw.items():
        setattr(s, k, v)
    return s


class ApiTest(unittest.TestCase):
    def setUp(self):
        global GLASS, CAFE
        GLASS, CAFE = uuid.uuid4(), uuid.uuid4()  # новые компании в каждом тесте
        self.tmp = tempfile.TemporaryDirectory()
        self.s = make_settings(self.tmp.name)
        self.client = TestClient(create_app(self.s), headers={"X-Internal-Key": "secret"})
        self.client.__enter__()
        self.setup_companies()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.tmp.cleanup()

    def setup_companies(self):
        c = self.client
        self.assertEqual(c.put(f"/v1/companies/{GLASS}", json=PROFILE).status_code, 200)
        for doc in sorted(PACK.glob("0*.md")):
            kind = PROFILE.get("document_kinds", {}).get(doc.name, "facts")
            r = c.post(f"/v1/companies/{GLASS}/documents", files={"file": (doc.name, doc.read_bytes(), "text/markdown")},
                       data={"kind": kind})
            self.assertEqual(r.status_code, 200, r.text)
        c.put(f"/v1/companies/{CAFE}", json={"name": "Кофейня Б"})
        c.post(f"/v1/companies/{CAFE}/documents/text", json={"name": "о нас", "text": DOC_B})

    def msg(self, text, company=None, chat="1"):
        r = self.client.post(f"/v1/companies/{company or GLASS}/messages", json={"chat_id": chat, "text": text})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def test_auth_required(self):
        r = TestClient(create_app(self.s)).get(f"/v1/companies/{GLASS}")
        self.assertEqual(r.status_code, 401)

    def test_post_contract_and_image(self):  # критерии 3, 4, 6
        task = str(uuid.uuid4())
        r = self.client.post(f"/v1/companies/{GLASS}/messages",
                             json={"chat_id": "1", "text": "Подготовь пост об архитектурном стекле и его применении "
                                   "в современных зданиях", "task_id": task}).json()
        d = r["draft"]
        self.assertEqual((r["kind"], d["status"], d["task_id"]), ("draft", "draft", task))
        self.assertEqual(set(d["content"]), {"text", "images", "cta_buttons", "source_url"})
        text = d["content"]["text"]
        self.assertIn("стекл", text.lower())
        self.assertNotRegex(text, r"\[[KS]\d")                      # служебные метки не попадают в пост
        self.assertIn("не является фотографией", text)               # подпись к AI-иллюстрации
        self.assertEqual(d["check"]["issues"], [])
        img = d["content"]["images"][0].replace("http://ai-agent:8000", "")
        self.assertEqual(self.client.get(img).headers["content-type"], "image/png")

    def test_revision_and_context(self):  # критерии 2, 5
        v1 = self.msg("Подготовь пост об архитектурном стекле и его применении в современных зданиях")["draft"]
        v2 = self.client.post(f"/v1/companies/{GLASS}/drafts/{v1['draft_id']}/revise",
                              json={"comment": "сделай текст короче и добавь явную ссылку на каталог",
                                    "chat_id": "1"}).json()["draft"]
        self.assertEqual((v2["version"], v2["parent_draft_id"]), (2, v1["draft_id"]))
        self.assertIn("salstek.ru/products", v2["content"]["text"])
        self.assertEqual(v2["content"]["source_url"], "https://salstek.ru/products/")
        old = self.client.get(f"/v1/companies/{GLASS}/drafts/{v1['draft_id']}").json()
        self.assertEqual(old["status"], "superseded")
        # приёмка из пакета Лизы: «тот же пост, но для архитекторов» меняет содержание, сохраняя тему
        r3 = self.msg("Сделай следующий пост на эту же тему, но для архитекторов")
        self.assertEqual(r3["draft"]["topic"], v1["topic"])
        self.assertEqual(r3["draft"]["audience"], "архитекторы и проектировщики")
        self.assertIn("Изучите каталог и технические материалы", r3["draft"]["content"]["text"])
        self.assertNotEqual(r3["draft"]["content"]["text"], v1["content"]["text"])
        self.assertIn("Продолжаю тему", r3["text"])
        self.assertIn("ссылку на каталог", r3["text"])  # помнит замечание

    def test_revise_via_chat_message(self):
        self.msg("Подготовь пост о закалённом стекле")
        r = self.msg("Доработай: убери рекламные обещания")
        self.assertEqual(r["draft"]["version"], 2)

    def test_isolation(self):  # критерий 9
        v = self.msg("Подготовь пост о стекле")["draft"]
        r = self.msg("Что такое Лавандовый раф Б-42?")
        self.assertNotIn("Б-42", json.dumps(r["sources"], ensure_ascii=False))
        # черновик Стекла не виден через Кофейню и не меняется ей
        self.assertEqual(self.client.get(f"/v1/companies/{CAFE}/drafts/{v['draft_id']}").status_code, 404)
        self.assertEqual(self.client.post(f"/v1/companies/{CAFE}/drafts/{v['draft_id']}/status",
                                          json={"status": "approved"}).status_code, 404)
        self.assertEqual(self.client.get(f"/v1/companies/{uuid.uuid4()}").status_code, 404)
        self.assertEqual(self.client.get(f"/media/{CAFE}/..%2F{GLASS}%2F{v['draft_id']}.png").status_code, 404)

    def test_state_after_restart(self):  # критерий 10
        self.msg("Помни: никогда не пиши про цены", chat="7")
        self.client.__exit__(None, None, None)
        self.client = TestClient(create_app(self.s), headers={"X-Internal-Key": "secret"})
        self.client.__enter__()
        r = self.msg("Подготовь пост о стекле", chat="7")
        self.assertIn("никогда не пиши про цены", r["text"])

    def test_scheduler_draft_without_dialog(self):  # задача 20
        r = self.client.post(f"/v1/companies/{GLASS}/drafts", json={"request": "Утренний пост о флоат-стекле",
                                                                    "platform": "vk"}).json()
        self.assertEqual(r["draft"]["platforms"], ["vk"])

    def test_llm_branch_with_fake_model(self):
        class Fake(LLM):
            name, engine_label = "fake", "fake"

            async def complete(self, system, user, json_mode=False):
                self.last = (system, user)
                return "```json\n" + json.dumps({"title": "T", "text": "Мы лучший завод. Over Size — стекло до 12 м [K1]. "
                                                 "Толщина 77 мм. АО «Салаватстекло» выпускает более 50 млн м² стекла в год.",
                                                 "hashtags": ["#стекло"], "image_prompt": "glass", "source_ids": ["K1"],
                                                 "audience": "a", "summary_for_client": "ok"}) + "\n```"
        fake = Fake()
        self.client.app.state.agent.llm = fake
        d = self.msg("Подготовь пост о стекле Over Size")["draft"]
        self.assertNotIn("лучший", d["content"]["text"])
        self.assertNotIn("[K1]", d["content"]["text"])
        self.assertTrue(any("77" in i for i in d["check"]["issues"]))            # число не из материалов
        self.assertTrue(any("группы компаний" in i for i in d["check"]["issues"]))  # 50 млн — показатель группы
        self.assertFalse(any("12" in i for i in d["check"]["issues"]))           # 12 м есть в материалах
        system, user = fake.last
        self.assertIn("[K1]", system)
        self.assertIn("не смешивай факты АО и группы компаний", system)          # инструкция компании
        self.assertIn("https://salstek.ru/products/", system)                    # метки [S…] расшифрованы


    def test_required_link_enforced_when_model_forgets(self):
        class Lazy(LLM):
            name, engine_label = "fake", "fake"

            async def complete(self, system, user, json_mode=False):
                return json.dumps({"title": "T", "text": "Архитектурное стекло помогает создавать светлые пространства.",
                                   "hashtags": [], "image_prompt": "glass", "source_ids": ["K1"],
                                   "audience": "a", "summary_for_client": "ok"})
        self.client.app.state.agent.llm = Lazy()
        v1 = self.msg("Подготовь пост об архитектурном стекле")["draft"]
        self.assertNotIn("salstek.ru/products", v1["content"]["text"])  # не просили — не добавляем
        v2 = self.client.post(f"/v1/companies/{GLASS}/drafts/{v1['draft_id']}/revise",
                              json={"comment": "сделай короче и добавь явную ссылку на каталог"}).json()["draft"]
        self.assertIn("https://salstek.ru/products/", v2["content"]["text"])
        self.assertEqual(v2["content"]["source_url"], "https://salstek.ru/products/")
        r3 = self.msg("Сделай следующий пост на эту же тему, но для архитекторов")["draft"]
        self.assertIn("Изучите каталог и технические материалы", r3["content"]["text"])  # CTA сегмента


    def test_rules_docs_not_used_as_facts(self):
        # запрос дословно совпадает со сценарием из 06_kontent_mvp.md, но это документ-правило
        r = self.msg("Подготовь пост для нашего тестового Telegram-канала об архитектурном стекле АО «Салаватстекло» "
                     "и его применении в современных зданиях. Сделай текст деловым, добавь изображение")
        docs = {s["doc"] for s in r["sources"]} | {s["doc"] for s in r["draft"]["sources_used"]}
        self.assertFalse(docs & {"06_kontent_mvp.md", "05_tone_of_voice.md"}, docs)
        kinds = {d["name"]: d["kind"] for d in self.client.get(f"/v1/companies/{GLASS}").json()["documents"]}
        self.assertEqual(kinds["06_kontent_mvp.md"], "rules")
        self.assertEqual(kinds["02_produkciya.md"], "facts")
        bad = self.client.post(f"/v1/companies/{GLASS}/documents/text", json={"name": "x", "text": "y", "kind": "other"})
        self.assertEqual(bad.status_code, 422)

    def test_group_catalog_attribution(self):
        class Fake(LLM):
            name, engine_label = "fake", "fake"

            async def complete(self, system, user, json_mode=False):
                return json.dumps({"title": "T", "text": "В нашем каталоге представлены решения с покрытиями. "
                                   "Стекло отличается высоким качеством.", "hashtags": [], "image_prompt": "g",
                                   "source_ids": ["K1"], "audience": "a", "summary_for_client": "ok"})
        self.client.app.state.agent.llm = Fake()
        d = self.msg("Подготовь пост об архитектурном стекле")["draft"]
        self.assertTrue(any("каталог группы компаний" in i for i in d["check"]["issues"]))
        self.assertNotIn("высоким качеством", d["content"]["text"])


    def test_self_review_rewrites_bad_draft(self):
        class Twice(LLM):
            name, engine_label = "fake", "fake"
            calls = []

            async def complete(self, system, user, json_mode=False):
                self.calls.append(user)
                text = ("В каталоге АО «Салаватстекло» представлены надёжные продукты." if len(self.calls) == 1 else
                        "В каталоге группы компаний «Салаватстекло» представлены архитектурные продукты, "
                        "в том числе Ultraview R с повышенным светопропусканием.")
                return json.dumps({"title": "T", "text": text, "hashtags": [], "image_prompt": "g",
                                   "source_ids": ["K1"], "audience": "a", "summary_for_client": "ok"})
        fake = Twice()
        self.client.app.state.agent.llm = fake
        d = self.msg("Подготовь пост об архитектурном стекле")["draft"]
        self.assertEqual(len(fake.calls), 2)                       # была вторая попытка
        self.assertIn("надёжн", fake.calls[1])                     # с конкретным замечанием
        self.assertIn("Ultraview R", d["content"]["text"])
        self.assertEqual(d["check"]["issues"], [])


    def test_backend_contract_kb_isolation(self):  # контракт Данила + E2E «знания не смешиваются»
        c = self.client
        a = {"id": 1, "company_id": "comp-a", "external_id": "ext-a", "name": "Стекольный завод А"}
        b = {"id": 2, "company_id": "comp-b", "external_id": "ext-b", "name": "Кофейня Б"}
        kb_a = [{"title": "О заводе", "content": "Завод выпускает закалённое стекло Кристалл-7 с 1985 года.",
                 "source": "https://a.example/about"}]
        kb_b = [{"title": "Меню", "content": "Кофейня варит фирменный раф Лаванда и печёт круассаны.",
                 "source": "https://b.example/menu"}]
        ra = c.post("/v1/generate", json={"prompt": "Подготовь пост о продукции", "company": a,
                                          "history": [], "knowledge_base": kb_a})
        self.assertEqual(ra.status_code, 200, ra.text)
        self.assertTrue(ra.json()["text"].strip())
        self.assertIsNone(ra.json()["image_url"])  # офлайн-картинка не имеет публичного https-адреса
        self.assertEqual(ra.json()["kind"], "draft")
        rb = c.post("/generate", json={"prompt": "Подготовь пост о продукции", "company": b,
                                       "history": [{"role": "user", "content": "привет"}], "knowledge_base": kb_b})
        self.assertEqual(rb.status_code, 200, rb.text)
        self.assertNotIn("Кристалл", rb.json()["text"])
        self.assertNotIn("Лаванд", ra.json()["text"])
        # вопрос к компании Б не видит знаний А, повтор того же KB не задваивает базу
        q = c.post("/", json={"prompt": "Что за стекло Кристалл-7?", "company": b, "knowledge_base": kb_b})
        self.assertEqual(q.status_code, 200)
        self.assertNotIn("1985", q.json()["text"])
        self.assertNotEqual(ra.json()["agent_company_id"], rb.json()["agent_company_id"])
        # пустая база знаний и пустая история — без ошибки, text не пустой
        e = c.post("/v1/generate", json={"prompt": "Расскажи о компании", "company": {"id": 3, "company_id": "comp-c"},
                                         "history": [], "knowledge_base": []})
        self.assertEqual(e.status_code, 200, e.text)
        self.assertTrue(e.json()["text"].strip())
        # UUID-строка используется как есть: компания из /admin и из бэкенда — одна и та же
        g = c.post("/v1/generate", json={"prompt": "Подготовь пост о стекле Ultraview R", "company": {"id": 5, "company_id": str(GLASS)}})
        self.assertEqual(g.json()["agent_company_id"], str(GLASS))
        self.assertIn("Ultraview", g.json()["text"])
        self.assertEqual(c.post("/v1/generate", json={"prompt": "x", "company": {}}).status_code, 422)
        # воркер Данила: черновик ответа на комментарий — короткий текст, без поста и картинки
        r = c.post("/v1/generate", json={"prompt": "Подготовь вежливый черновик ответа на комментарий к посту нашей "
                                                   "компании. Комментарий: «Когда будет в наличии?». Ответ — от имени компании, коротко.",
                                         "company": {**a, "tone_of_voice": "дружелюбный"}, "history": [], "knowledge_base": []})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["kind"], "comment_reply")
        self.assertIsNone(r.json()["image_url"])
        self.assertLess(len(r.json()["text"]), 400)
        # агент опубликован наружу по https — бэкенд получает картинку с публичного адреса
        self.s.public_media_base_url = "https://agent.example.ru/media"
        r = c.post("/v1/generate", json={"prompt": "Подготовь пост о продукции", "company": a, "knowledge_base": kb_a})
        self.assertTrue(r.json()["image_url"].startswith("https://agent.example.ru/media/"))
        self.s.public_media_base_url = ""
        self.assertEqual(TestClient(c.app).post("/v1/generate", json={"prompt": "x", "company": a}).status_code, 401)

    def test_admin_form_and_duplicates(self):
        page = self.client.get("/admin")
        self.assertEqual(page.status_code, 200)
        self.assertIn("/v1/companies/", page.text)          # форма шлёт данные в API агента
        cid = uuid.uuid4()
        body = {"name": "Новая компания", "description": "Описание", "products": "Окна", "benefits": "",
                "approval_mode": "scheduled", "platform": "vk", "channel_handle": "@test"}
        self.assertEqual(self.client.put(f"/v1/companies/{cid}", json=body).status_code, 200)
        prof = self.client.get(f"/v1/companies/{cid}").json()
        self.assertEqual((prof["products"], prof["approval_mode"], prof["platform"]), ("Окна", "scheduled", "vk"))
        doc = ("о_нас.md", "Компания делает окна с 2001 года.".encode(), "text/markdown")
        first = self.client.post(f"/v1/companies/{cid}/documents", files={"file": doc}, data={"kind": "facts"}).json()
        again = self.client.post(f"/v1/companies/{cid}/documents", files={"file": doc}, data={"kind": "facts"}).json()
        self.assertEqual((first["duplicate"], again["duplicate"]), (False, True))
        self.assertEqual(first["document_id"], again["document_id"])
        self.assertEqual(len(self.client.get(f"/v1/companies/{cid}").json()["documents"]), 1)


class ChecksTest(unittest.TestCase):
    def test_checks(self):
        rep = check_draft({"text": "Мы лучшие. Размер 1605х1300 мм. Толщина 99 мм.", "source_ids": ["S1"],
                           "image_path": "x"}, ["размер 1605х1300 мм"], 1000)
        self.assertNotIn("лучшие", rep["text"])
        self.assertTrue(any("99" in i for i in rep["issues"]))
        self.assertFalse(any("1605" in i for i in rep["issues"]))


if __name__ == "__main__":
    unittest.main()
