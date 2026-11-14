from contextlib import asynccontextmanager
from uuid import uuid4
from fastapi import FastAPI, Depends, Header, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.config import settings
from app.db import engine, Base, get_db
from app.models import Company, AuditLog, Publication
from app.schemas import CompanyCreate, CompanyOut, CompanyUpdate, GenerateRequest
from app.security import encrypt_secret

@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)  # MVP bootstrap; replace with Alembic migrations for production
    yield
    await engine.dispose()

app = FastAPI(title=settings.app_name, lifespan=lifespan)

async def require_admin(x_admin_key: str | None = Header(default=None)):
    if not x_admin_key or x_admin_key != settings.admin_api_key:
        raise HTTPException(status_code=401, detail="Invalid admin API key")

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.post("/api/companies", response_model=CompanyOut, dependencies=[Depends(require_admin)])
async def create_company(data: CompanyCreate, db: AsyncSession = Depends(get_db)):
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
    if "vk_access_token" in changes:
        token = changes.pop("vk_access_token")
        company.vk_access_token = encrypt_secret(token) if token else None
    for field, value in changes.items():
        setattr(company, field, value)

    db.add(AuditLog(company_id=company.id, actor="admin", action="company_updated",
                    object_type="company", object_id=str(company.id),
                    details=",".join(sorted(changes.keys()))))
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

@app.get("/api/companies/{company_id}", response_model=CompanyOut, dependencies=[Depends(require_admin)])
async def get_company(company_id: int, db: AsyncSession = Depends(get_db)):
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

@app.get("/api/companies/{company_id}/publications", dependencies=[Depends(require_admin)])
async def list_publications(company_id: int, db: AsyncSession = Depends(get_db)):
    if not await db.get(Company, company_id):
        raise HTTPException(404, "Company not found")
    result = await db.execute(select(Publication).where(
        Publication.company_id == company_id
    ).order_by(Publication.created_at.desc()))
    return [{"id": p.id, "status": p.status, "platform": p.platform, "text": p.text,
             "published_url": p.published_url, "created_at": p.created_at} for p in result.scalars().all()]
