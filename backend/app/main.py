from contextlib import asynccontextmanager
from uuid import uuid4
from fastapi import FastAPI, Depends, Header, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy import select
import hmac
import logging
import re
from sqlalchemy.ext.asyncio import AsyncSession
from app.config import settings
from app.db import engine, Base, get_db
from app.models import Company, AuditLog, Publication, CompanyAPIKey, CompanyKnowledgeItem, MessageHistory, now
from app.schemas import CompanyCreate, CompanyOut, CompanyUpdate, GenerateRequest
from app.security import encrypt_secret, hash_api_key, new_api_key
from app.publisher import (
    fetch_vk_comments,
    publish_to_platform,
    send_telegram_notification,
)

logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)  # MVP bootstrap; replace with Alembic migrations for production
    yield
    await engine.dispose()

app = FastAPI(title=settings.app_name, lifespan=lifespan)

async def require_admin(x_admin_key: str | None = Header(default=None)):
    if not x_admin_key or not settings.admin_api_key or settings.admin_api_key == "change-me" or not hmac.compare_digest(x_admin_key, settings.admin_api_key):
        raise HTTPException(status_code=401, detail="Invalid admin API key")


def require_publisher(x_publisher_key: str | None) -> None:
    if (
        not settings.publisher_internal_key
        or not x_publisher_key
        or not hmac.compare_digest(x_publisher_key, settings.publisher_internal_key)
    ):
        raise HTTPException(status_code=401, detail="Invalid publisher credentials")


async def notify_operator_safely(company: Company, text: str) -> bool:
    """Notification failure must not turn a completed remote publication into an error."""
    try:
        await send_telegram_notification(company, text)
        return True
    except Exception as exc:
        logger.error("Operator notification failed: %s", type(exc).__name__)
        return False


async def authorize_company(company_id: int, scope: str, x_admin_key: str | None,
                            x_company_key: str | None, db: AsyncSession, actor: str = "api") -> str:
    if x_admin_key and settings.admin_api_key and hmac.compare_digest(x_admin_key, settings.admin_api_key):
        return "admin"
    if not x_company_key:
        raise HTTPException(401, "Требуется X-Admin-Key или X-Company-Key")
    key_hash = hash_api_key(x_company_key)
    result = await db.execute(select(CompanyAPIKey).where(
        CompanyAPIKey.company_id == company_id, CompanyAPIKey.key_hash == key_hash,
        CompanyAPIKey.revoked_at.is_(None)))
    credential = result.scalar_one_or_none()
    if not credential:
        raise HTTPException(401, "Недействительный или отозванный ключ компании")
    if scope not in (credential.scopes or []):
        raise HTTPException(403, f"Недостаточно прав: требуется scope {scope}")
    credential.last_used_at = now()
    await db.commit()
    return f"company-key:{credential.id}"

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.post("/api/companies", response_model=CompanyOut, dependencies=[Depends(require_admin)])
async def create_company(data: CompanyCreate, db: AsyncSession = Depends(get_db)):
    if data.vk_access_token and not settings.encryption_key:
        raise HTTPException(503, "Перед сохранением VK token настройте ENCRYPTION_KEY")
    external_id = data.external_id.strip() if data.external_id else None
    if external_id:
        existing = await db.execute(select(Company).where(Company.external_id == external_id))
        if existing.scalar_one_or_none():
            raise HTTPException(status_code=409, detail=f"Company identifier already exists: {external_id}")
    company = Company(
        external_id=external_id or f"company-{uuid4().hex}",
        name=data.name, description=data.description, tone_of_voice=data.tone_of_voice,
        products=data.products, target_audience=data.target_audience, advantages=data.advantages,
        brand_guidelines=data.brand_guidelines, enabled_platforms=data.enabled_platforms,
        approval_required=data.approval_required, approval_mode=data.approval_mode,
        telegram_chat_id=data.telegram_chat_id, approval_chat_id=data.approval_chat_id,
        vk_group_id=data.vk_group_id, vk_access_token=encrypt_secret(data.vk_access_token)
    )
    db.add(company)
    await db.flush()
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="Company identifier already exists")
    await db.refresh(company)
    db.add(AuditLog(company_id=company.id, actor="admin", action="company_created",
                    object_type="company", object_id=str(company.id)))
    await db.commit()
    return company

@app.get("/api/companies", response_model=list[CompanyOut], dependencies=[Depends(require_admin)])
async def list_companies(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Company).order_by(Company.id))
    return list(result.scalars().all())


@app.patch("/api/companies/{company_id}", response_model=CompanyOut, dependencies=[Depends(require_admin)])
async def update_company(company_id: int, data: CompanyUpdate, db: AsyncSession = Depends(get_db)):
    company = await db.get(Company, company_id)
    if not company:
        raise HTTPException(404, "Company not found")

    changes = data.model_dump(exclude_unset=True)
    if changes.get("vk_access_token") and not settings.encryption_key:
        raise HTTPException(503, "Перед сохранением VK token настройте ENCRYPTION_KEY")
    secret_changed = "vk_access_token" in changes
    if secret_changed:
        token = changes.pop("vk_access_token")
        company.vk_access_token = encrypt_secret(token) if token else None
    for field, value in changes.items():
        setattr(company, field, value)

    db.add(AuditLog(company_id=company.id, actor="admin", action="company_updated",
                    object_type="company", object_id=str(company.id),
                    details=",".join(sorted(changes.keys()) + (["vk_access_token_updated"] if secret_changed else []))))
    await db.commit()
    await db.refresh(company)
    return company

@app.get("/api/companies/by-external-id/{external_id}", response_model=CompanyOut, dependencies=[Depends(require_admin)])
async def get_company_by_external_id(external_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Company).where(Company.external_id == external_id))
    company = result.scalar_one_or_none()
    if not company:
        raise HTTPException(404, "Company not found")
    return company

@app.get("/api/companies/{company_id}", response_model=CompanyOut)
async def get_company(company_id: int, x_admin_key: str | None = Header(default=None),
                      x_company_key: str | None = Header(default=None), db: AsyncSession = Depends(get_db)):
    await authorize_company(company_id, "company:read", x_admin_key, x_company_key, db)
    company = await db.get(Company, company_id)
    if not company:
        raise HTTPException(404, "Company not found")
    return company

@app.post("/api/tasks", dependencies=[Depends(require_admin)])
async def create_task(data: GenerateRequest, db: AsyncSession = Depends(get_db)):
    company = await db.get(Company, data.company_id)
    if not company:
        raise HTTPException(404, "Company not found")
    from app.models import Task
    task = Task(company_id=company.id, prompt=data.prompt, source="api", status="received")
    db.add(task)
    await db.commit()
    await db.refresh(task)
    return {"task_id": task.id, "status": task.status}

@app.get("/api/companies/{company_id}/publications")
async def list_publications(company_id: int, x_admin_key: str | None = Header(default=None),
                            x_company_key: str | None = Header(default=None),
                            db: AsyncSession = Depends(get_db)):
    await authorize_company(company_id, "publications:read", x_admin_key, x_company_key, db)
    result = await db.execute(select(Publication).where(
        Publication.company_id == company_id).order_by(Publication.created_at.desc()))
    return [{"id": p.id, "status": p.status, "platform": p.platform, "text": p.text,
             "published_post_id": p.published_post_id, "published_url": p.published_url,
             "published_at": p.published_at, "publish_error": p.publish_error,
             "created_at": p.created_at} for p in result.scalars().all()]

@app.post("/api/companies/{company_id}/access-keys", dependencies=[Depends(require_admin)])
async def create_company_access_key(company_id: int, data: dict, db: AsyncSession = Depends(get_db)):
    company = await db.get(Company, company_id)
    if not company:
        raise HTTPException(404, "Company not found")
    name = str(data.get("name", "Integration key"))[:120]
    allowed = {"company:read", "history:read", "publications:read"}
    scopes = data.get("scopes", ["publications:read"])
    if not isinstance(scopes, list) or not scopes or any(x not in allowed for x in scopes):
        raise HTTPException(422, f"Допустимые scopes: {sorted(allowed)}")
    raw_key = new_api_key()
    credential = CompanyAPIKey(company_id=company.id, name=name, key_prefix=raw_key[:12],
                               key_hash=hash_api_key(raw_key), scopes=scopes, created_by="admin")
    db.add(credential)
    await db.flush()
    db.add(AuditLog(company_id=company.id, actor="admin", action="company_api_key_created",
                    object_type="api_key", object_id=str(credential.id), details=",".join(scopes)))
    await db.commit()
    await db.refresh(credential)
    return {"id": credential.id, "name": credential.name, "key": raw_key,
            "scopes": credential.scopes, "warning": "Ключ показывается только один раз. Сохраните его безопасно."}

@app.get("/api/companies/{company_id}/access-keys", dependencies=[Depends(require_admin)])
async def list_company_access_keys(company_id: int, db: AsyncSession = Depends(get_db)):
    if not await db.get(Company, company_id):
        raise HTTPException(404, "Company not found")
    result = await db.execute(select(CompanyAPIKey).where(CompanyAPIKey.company_id == company_id)
                              .order_by(CompanyAPIKey.created_at.desc()))
    return [{"id": k.id, "name": k.name, "key_prefix": k.key_prefix, "scopes": k.scopes,
             "created_at": k.created_at, "revoked_at": k.revoked_at, "last_used_at": k.last_used_at}
            for k in result.scalars().all()]

@app.delete("/api/companies/{company_id}/access-keys/{key_id}", dependencies=[Depends(require_admin)])
async def revoke_company_access_key(company_id: int, key_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(CompanyAPIKey).where(CompanyAPIKey.id == key_id,
        CompanyAPIKey.company_id == company_id, CompanyAPIKey.revoked_at.is_(None)))
    credential = result.scalar_one_or_none()
    if not credential:
        raise HTTPException(404, "Активный ключ не найден")
    credential.revoked_at = now()
    db.add(AuditLog(company_id=company.id, actor="admin", action="company_api_key_revoked",
                    object_type="api_key", object_id=str(key_id)))
    await db.commit()
    return {"status": "revoked", "key_id": key_id}

@app.post("/api/companies/{company_id}/credentials/revoke", dependencies=[Depends(require_admin)])
async def revoke_platform_credentials(company_id: int, data: dict, db: AsyncSession = Depends(get_db)):
    company = await db.get(Company, company_id)
    if not company:
        raise HTTPException(404, "Company not found")
    platform = data.get("platform")
    if platform == "vk":
        company.vk_access_token = None
        company.vk_group_id = None
        company.enabled_platforms = [p for p in (company.enabled_platforms or []) if p != "vk"]
    elif platform == "telegram":
        # The bot token is shared infrastructure; this disconnects only this company's
        # destination chat and leaves other companies untouched.
        company.telegram_chat_id = None
        company.enabled_platforms = [p for p in (company.enabled_platforms or []) if p != "telegram"]
    else:
        raise HTTPException(422, "platform должен быть vk или telegram")
    db.add(AuditLog(company_id=company.id, actor="admin", action="platform_credentials_revoked",
                    object_type="company", object_id=str(company_id), details=platform))
    await db.commit()
    return {"status": "revoked", "company_id": company_id, "platform": platform}

@app.post("/internal/publications/{publication_id}/publish")
async def publish_approved_publication(publication_id: int,
        x_publisher_key: str | None = Header(default=None), db: AsyncSession = Depends(get_db)):
    require_publisher(x_publisher_key)
    # Commit the claim before the remote call. A concurrent request sees `publishing`
    # and cannot send the same approved material twice.
    result = await db.execute(select(Publication).where(Publication.id == publication_id).with_for_update())
    publication = result.scalar_one_or_none()
    if not publication:
        raise HTTPException(404, "Publication not found")
    if publication.status == "published":
        return {"status": "already_published", "publication_id": publication.id,
                "platform": publication.platform, "published_post_id": publication.published_post_id,
                "published_url": publication.published_url, "published_at": publication.published_at}
    if publication.status != "approved":
        raise HTTPException(409, f"Публикация не согласована (status={publication.status})")
    if publication.scheduled_at:
        scheduled = publication.scheduled_at
        if scheduled.tzinfo is None:
            scheduled = scheduled.replace(tzinfo=now().tzinfo)
        if scheduled > now():
            raise HTTPException(409, "Время публикации ещё не наступило")
    company = await db.get(Company, publication.company_id)
    if not company or not company.is_active:
        raise HTTPException(409, "Компания отключена или не найдена")
    if publication.platform not in (company.enabled_platforms or []):
        raise HTTPException(403, "Площадка отключена в профиле компании")
    publication.status = "publishing"
    publication.publish_attempts += 1
    publication.publish_error = None
    db.add(AuditLog(company_id=company.id, actor="publication-worker", action="publish_started",
                    object_type="publication", object_id=str(publication.id),
                    details=f"platform={publication.platform}; attempt={publication.publish_attempts}"))
    await db.commit()
    try:
        post_id, post_url = await publish_to_platform(company, publication)
    except Exception as exc:
        # The remote outcome may be ambiguous. Never auto-retry this publication.
        safe_error = re.sub(r"https?://\S+", "[external URL]", str(exc))
        if settings.telegram_bot_token:
            safe_error = safe_error.replace(settings.telegram_bot_token, "[redacted]")
        safe_error = f"{type(exc).__name__}: {safe_error}"[:1000]
        publication.status = "publish_unknown"
        publication.publish_error = safe_error
        db.add(AuditLog(company_id=company.id, actor="publication-worker", action="publish_unknown",
                        object_type="publication", object_id=str(publication.id), details=safe_error))
        await db.commit()
        await notify_operator_safely(
            company,
            f"⚠️ Публикация №{publication.id} ({publication.platform}) требует ручной сверки. "
            "Автоматический повтор заблокирован: проверьте площадку и разрешите результат через API.",
        )
        raise HTTPException(502, "Результат отправки неоднозначен; автоматический повтор заблокирован. Проверьте площадку.")
    publication.status = "published"
    publication.published_post_id = post_id
    publication.published_url = post_url
    publication.published_at = now()
    db.add(AuditLog(company_id=company.id, actor="publication-worker", action="published",
                    object_type="publication", object_id=str(publication.id),
                    details=f"platform={publication.platform}; remote_id={post_id}"))
    await db.commit()
    link = post_url or f"ID {post_id}"
    notification_sent = await notify_operator_safely(
        company,
        f"✅ Публикация №{publication.id} размещена ({publication.platform}): {link}"
        + (" (по расписанию)" if publication.scheduled_at else ""),
    )
    return {"status": "published", "publication_id": publication.id,
            "platform": publication.platform, "published_post_id": post_id,
            "published_url": post_url, "published_at": publication.published_at,
            "notification_sent": notification_sent}


@app.get("/internal/publications/{publication_id}/comments")
async def internal_publication_comments(publication_id: int,
        x_publisher_key: str | None = Header(default=None), db: AsyncSession = Depends(get_db)):
    """Backend proxy for the worker's VK comment monitor."""
    require_publisher(x_publisher_key)
    publication = await db.get(Publication, publication_id)
    if not publication:
        raise HTTPException(404, "Publication not found")
    if publication.platform != "vk":
        raise HTTPException(422, "Мониторинг комментариев реализован только для VK")
    if publication.status != "published" or not publication.published_post_id:
        raise HTTPException(409, "Комментарии доступны только для опубликованной записи")
    company = await db.get(Company, publication.company_id)
    if not company or not company.is_active:
        raise HTTPException(409, "Компания отключена или не найдена")
    try:
        comments = await fetch_vk_comments(company, publication)
    except Exception as exc:
        logger.error("VK comments fetch failed for publication %s: %s", publication_id, type(exc).__name__)
        raise HTTPException(502, "Не удалось получить комментарии VK") from exc
    return {"publication_id": publication_id, "comments": comments}


@app.post("/internal/publications/{publication_id}/notify")
async def internal_publication_notification(publication_id: int, data: dict,
        x_publisher_key: str | None = Header(default=None), db: AsyncSession = Depends(get_db)):
    """Send worker-originated moderation alerts without sharing the Telegram token."""
    require_publisher(x_publisher_key)
    text = data.get("text")
    if not isinstance(text, str) or not text.strip() or len(text) > 7000:
        raise HTTPException(422, "Нужен непустой text длиной до 7000 символов")
    publication = await db.get(Publication, publication_id)
    if not publication:
        raise HTTPException(404, "Publication not found")
    company = await db.get(Company, publication.company_id)
    if not company or not company.is_active:
        raise HTTPException(409, "Компания отключена или не найдена")
    try:
        await send_telegram_notification(company, text.strip())
    except Exception as exc:
        logger.error("Worker notification failed for publication %s: %s", publication_id, type(exc).__name__)
        raise HTTPException(503, "Не удалось отправить уведомление") from exc
    return {"status": "sent", "publication_id": publication_id}

@app.post("/api/publications/{publication_id}/resolve", dependencies=[Depends(require_admin)])
async def resolve_uncertain_publication(publication_id: int, data: dict, db: AsyncSession = Depends(get_db)):
    publication = await db.get(Publication, publication_id)
    if not publication:
        raise HTTPException(404, "Publication not found")
    if publication.status != "publish_unknown":
        raise HTTPException(409, "Разрешать вручную можно только публикации publish_unknown")
    action = data.get("action")
    if action == "mark_published":
        remote_id = data.get("published_post_id")
        if not isinstance(remote_id, (str, int)) or not str(remote_id).strip():
            raise HTTPException(422, "Укажите published_post_id из кабинета площадки")
        publication.status = "published"
        publication.published_post_id = str(remote_id)
        publication.published_url = data.get("published_url")
        publication.published_at = now()
        audit_action = "publish_unknown_confirmed_published"
    elif action == "retry" and data.get("confirmed_not_published") is True:
        publication.status = "approved"
        publication.publish_error = None
        audit_action = "publish_unknown_confirmed_not_published_retry_allowed"
    else:
        raise HTTPException(422, "action: mark_published либо retry с confirmed_not_published=true")
    db.add(AuditLog(company_id=publication.company_id, actor="admin", action=audit_action,
                    object_type="publication", object_id=str(publication.id),
                    details="Manual reconciliation"))
    await db.commit()
    return {"publication_id": publication.id, "status": publication.status}

@app.get("/api/companies/{company_id}/history")
async def company_history(company_id: int, x_admin_key: str | None = Header(default=None),
                          x_company_key: str | None = Header(default=None),
                          db: AsyncSession = Depends(get_db)):
    await authorize_company(company_id, "history:read", x_admin_key, x_company_key, db)
    result = await db.execute(select(MessageHistory).where(MessageHistory.company_id == company_id)
                              .order_by(MessageHistory.created_at.desc()).limit(100))
    return [{"id": m.id, "chat_id": m.chat_id, "role": m.role, "content": m.content,
             "created_at": m.created_at} for m in result.scalars().all()]

@app.post("/api/companies/{company_id}/knowledge", dependencies=[Depends(require_admin)])
async def add_company_knowledge(company_id: int, data: dict, db: AsyncSession = Depends(get_db)):
    if not await db.get(Company, company_id):
        raise HTTPException(404, "Company not found")
    title, content = data.get("title"), data.get("content")
    if not isinstance(title, str) or not title.strip() or not isinstance(content, str) or not content.strip():
        raise HTTPException(422, "Нужны непустые title и content")
    item = CompanyKnowledgeItem(company_id=company_id, title=title[:300], content=content,
                                source=str(data.get("source", "manual"))[:500])
    db.add(item)
    await db.flush()
    db.add(AuditLog(company_id=company.id, actor="admin", action="knowledge_item_created",
                    object_type="knowledge_item", object_id=str(item.id)))
    await db.commit()
    await db.refresh(item)
    return {"id": item.id, "company_id": item.company_id, "title": item.title, "source": item.source}

@app.get("/api/companies/{company_id}/knowledge")
async def list_company_knowledge(company_id: int, x_admin_key: str | None = Header(default=None),
                                 x_company_key: str | None = Header(default=None),
                                 db: AsyncSession = Depends(get_db)):
    await authorize_company(company_id, "company:read", x_admin_key, x_company_key, db)
    result = await db.execute(select(CompanyKnowledgeItem).where(
        CompanyKnowledgeItem.company_id == company_id).order_by(CompanyKnowledgeItem.created_at.desc()))
    return [{"id": k.id, "title": k.title, "content": k.content, "source": k.source,
             "created_at": k.created_at} for k in result.scalars().all()]


@app.get("/api/companies/{company_id}/audit-log", dependencies=[Depends(require_admin)])
async def company_audit_log(company_id: int, limit: int = 100, db: AsyncSession = Depends(get_db)):
    if not await db.get(Company, company_id):
        raise HTTPException(404, "Company not found")
    limit = max(1, min(limit, 500))
    result = await db.execute(select(AuditLog).where(AuditLog.company_id == company_id)
                              .order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).limit(limit))
    return [{"id": item.id, "actor": item.actor, "action": item.action,
             "object_type": item.object_type, "object_id": item.object_id,
             "details": item.details, "created_at": item.created_at}
            for item in result.scalars().all()]
