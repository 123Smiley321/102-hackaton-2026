"""HTTP API AI-агента (AI_AGENT_URL). Запуск: uvicorn marketer.api:create_app --factory --host 0.0.0.0 --port 8000

Внутренний сервис: его вызывают бэкенд (Р1) и планировщик (Р5) по внутренней сети Docker Compose.
Если задан AI_AGENT_API_KEY, каждый запрос должен нести заголовок X-Internal-Key.
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import httpx

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .agent import MarketerAgent
from .config import Settings
from .llm import LLMError
from .storage import CompanyNotFound, NotFound, create_tables, make_engine


class CompanyIn(BaseModel):
    name: str
    industry: str = ""
    audience: str = ""
    tone: str = "деловой, без рекламных преувеличений"
    approver: str = ""
    platform: str = Field("telegram", pattern="^(telegram|vk|max)$")
    brand_color: str = Field("#1f5f8b", pattern="^#[0-9a-fA-F]{6}$")
    site_url: str | None = None
    # правила компании — заполняются из пакета материалов без программирования (см. demo_data/*/profile.json)
    default_cta: str = ""
    catalog_url: str | None = None
    segments: list[dict] = []          # [{key, name, match (regex), focus, cta}]
    source_urls: dict[str, str] = {}   # {"S1": "https://..."} — метки официальных источников в документах
    attribution_rules: list[dict] = [] # [{pattern, requires, message}]
    forbidden_claims: str = ""
    extra_instructions: str = ""
    image_style: str = ""
    image_caption: str = "AI-иллюстрация."
    example_post: str = ""             # образец хорошего поста компании (стиль и конкретика)
    document_kinds: dict[str, str] = {}
    # поля формы настройки компании (admin/company_setup.html)
    description: str = ""
    products: str = ""
    benefits: str = ""                 # подтверждённые преимущества — со ссылками на источники
    language: str = "Русский"
    approval_mode: str = Field("manual", pattern="^(manual|scheduled)$")
    channel_handle: str = ""


class KBItem(BaseModel):
    title: str = ""
    content: str = ""
    source: str | None = None


class GenerateIn(BaseModel):
    """Финальный контракт Backend -> AI-агент."""
    prompt: str = ""
    company: dict
    history: list = []
    knowledge_base: list[KBItem] = []


class GenerateOut(BaseModel):
    text: str
    image_url: str | None = None
    kind: str | None = None
    engine: str | None = None
    draft_id: str | None = None
    status: str | None = None
    notes: str | None = None
    agent_company_id: str | None = None


class TextDocIn(BaseModel):
    name: str
    text: str
    kind: str = Field("facts", pattern="^(facts|rules)$")


class SiteIn(BaseModel):
    url: str


class MessageIn(BaseModel):
    chat_id: str
    text: str
    task_id: uuid.UUID | None = None


class DraftIn(BaseModel):
    request: str
    platform: str | None = Field(None, pattern="^(telegram|vk|max)$")
    task_id: uuid.UUID | None = None
    chat_id: str | None = None


class ReviseIn(BaseModel):
    comment: str
    chat_id: str | None = None


class StatusIn(BaseModel):
    status: str


def create_app(settings: Settings | None = None) -> FastAPI:
    s = settings or Settings.load()
    engine, sm = make_engine(s.database_url)
    agent = MarketerAgent(s, sm)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await create_tables(engine)
        s.media_dir.mkdir(parents=True, exist_ok=True)
        yield
        await engine.dispose()

    def check_key(x_internal_key: str | None = Header(None)):
        if s.api_key and x_internal_key != s.api_key:
            raise HTTPException(401, "Неверный X-Internal-Key")

    app = FastAPI(title="AI-маркетолог: ядро агента", version="1.0", lifespan=lifespan)
    app.state.agent = agent
    auth = [Depends(check_key)]

    @app.exception_handler(CompanyNotFound)
    async def _no_company(_, e):
        from fastapi.responses import JSONResponse
        return JSONResponse({"detail": f"Компания {e} не настроена в AI-модуле (PUT /v1/companies/{{id}})"}, 404)

    @app.exception_handler(NotFound)
    async def _not_found(_, e):
        from fastapi.responses import JSONResponse
        return JSONResponse({"detail": str(e)}, 404)

    @app.exception_handler(LLMError)
    async def _llm(_, e):
        from fastapi.responses import JSONResponse
        return JSONResponse({"detail": f"Модель недоступна: {e}"}, 502)

    @app.get("/admin", include_in_schema=False)
    async def admin_page():
        """Форма настройки компании без программирования (критерий 11). Ключ доступа вводится в форме."""
        return FileResponse(Path(__file__).resolve().parents[1] / "admin" / "company_setup.html", media_type="text/html")

    @app.get("/health")
    async def health():
        return {"status": "ok", "engine": agent.llm.engine_label}

    @app.get("/v1/engine", dependencies=auth)
    async def engine_info():
        """Критерий 12: какой движок отвечает, как выполнен вход, что доступно и чего нет."""
        llm = agent.llm
        info = {"llm_provider": llm.name, "engine": llm.engine_label,
                "image_provider": s.image_provider, "chatgpt_account": False}
        if llm.name == "codex":
            info.update(chatgpt_account=True, login_status=getattr(llm, "login_raw", ""),
                        available=["тексты постов", "доработка по замечаниям", "ответы о компании", "анализ материалов"],
                        limits=["нет генерации картинок через аккаунт ChatGPT — отдельный IMAGE_PROVIDER",
                                "лимиты подписки ChatGPT", "доступность OpenAI зависит от региона и сети"])
        return info

    # ---------- настройка компании (админка бэкенда) ----------
    async def _generate(body: GenerateIn):
        try:
            return await agent.generate_for_backend(body.prompt, body.company, body.history,
                                                    [i.model_dump() for i in body.knowledge_base])
        except ValueError as e:
            raise HTTPException(422, str(e))

    # один обработчик на трёх адресах: бэкенд может звать AI_AGENT_URL, AI_AGENT_URL/generate или /v1/generate
    for path in ("/v1/generate", "/generate", "/"):
        app.post(path, dependencies=auth, response_model=GenerateOut,
                 include_in_schema=path == "/v1/generate")(_generate)

    @app.put("/v1/companies/{company_id}", dependencies=auth)
    async def upsert_company(company_id: uuid.UUID, body: CompanyIn):
        return await agent.upsert_company(company_id, **body.model_dump())

    @app.get("/v1/companies/{company_id}", dependencies=auth)
    async def get_company(company_id: uuid.UUID):
        return await agent.get_company(company_id)

    @app.post("/v1/companies/{company_id}/documents", dependencies=auth)
    async def upload_document(company_id: uuid.UUID, file: UploadFile = File(...), kind: str = Form("facts")):
        """kind=facts — факты для постов (по умолчанию); kind=rules — правила, сценарии, методички."""
        try:
            return await agent.add_upload(company_id, file.filename or "document.txt", await file.read(), kind=kind)
        except ValueError as e:
            raise HTTPException(422, str(e))

    @app.post("/v1/companies/{company_id}/documents/text", dependencies=auth)
    async def add_text(company_id: uuid.UUID, body: TextDocIn):
        return await agent.add_text(company_id, body.name, body.text, kind=body.kind)

    @app.post("/v1/companies/{company_id}/sites", dependencies=auth)
    async def add_site(company_id: uuid.UUID, body: SiteIn):
        try:
            return await agent.add_site(company_id, body.url)
        except (httpx.HTTPError, ValueError) as e:
            raise HTTPException(502, f"Не удалось загрузить сайт: {e}")

    @app.post("/v1/companies/{company_id}/analyze", dependencies=auth)
    async def analyze(company_id: uuid.UUID):
        return await agent.analyze_business(company_id)

    # ---------- работа агента ----------
    @app.post("/v1/companies/{company_id}/messages", dependencies=auth)
    async def message(company_id: uuid.UUID, body: MessageIn):
        """Любое сообщение пользователя из Telegram. kind=draft -> бэкенд создаёт publication и шлёт на согласование."""
        return (await agent.handle_message(company_id, body.chat_id, body.text, task_id=body.task_id)).dict()

    @app.post("/v1/companies/{company_id}/drafts", dependencies=auth)
    async def create_draft(company_id: uuid.UUID, body: DraftIn):
        """Черновик без диалога — для задач по расписанию."""
        return (await agent.create_post(company_id, body.request, chat_id=body.chat_id, platform=body.platform,
                                        task_id=body.task_id)).dict()

    @app.get("/v1/companies/{company_id}/drafts/{draft_id}", dependencies=auth)
    async def get_draft(company_id: uuid.UUID, draft_id: uuid.UUID):
        return await agent.get_draft(company_id, draft_id)

    @app.post("/v1/companies/{company_id}/drafts/{draft_id}/revise", dependencies=auth)
    async def revise(company_id: uuid.UUID, draft_id: uuid.UUID, body: ReviseIn):
        """Кнопка «На доработку» с комментарием -> новая версия, нужна повторная проверка."""
        return (await agent.revise(company_id, draft_id, body.comment, chat_id=body.chat_id)).dict()

    @app.post("/v1/companies/{company_id}/drafts/{draft_id}/status", dependencies=auth)
    async def set_status(company_id: uuid.UUID, draft_id: uuid.UUID, body: StatusIn):
        """Бэкенд сообщает итог согласования/публикации, чтобы агент учитывал его в истории."""
        try:
            await agent.set_draft_status(company_id, draft_id, body.status)
        except ValueError as e:
            raise HTTPException(422, str(e))
        return {"ok": True}

    # ---------- картинки черновиков (их скачивает модуль публикаций) ----------
    @app.get("/media/{company_id}/{file_name}")
    async def media(company_id: uuid.UUID, file_name: str):
        path = (s.media_dir / str(company_id) / file_name).resolve()
        if path.parent != (s.media_dir / str(company_id)).resolve() or not path.is_file():
            raise HTTPException(404)
        return FileResponse(path, media_type="image/png")

    return app
