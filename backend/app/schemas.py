from pydantic import BaseModel, Field, AliasChoices
from typing import Optional, Literal

class CompanyCreate(BaseModel):
    # Accept both our explicit external_id and Lisa package field company_id.
    external_id: Optional[str] = Field(default=None, min_length=2, max_length=120, validation_alias=AliasChoices("external_id", "company_id"))
    name: str = Field(min_length=2, max_length=200)
    description: str = ""
    tone_of_voice: str = "Деловой, дружелюбный"
    products: str = ""
    target_audience: str = ""
    advantages: str = ""
    brand_guidelines: str = ""
    enabled_platforms: list[Literal["vk", "telegram", "max"]] = Field(default_factory=lambda: ["vk", "telegram"])
    approval_required: bool = True
    approval_mode: Literal["per_platform", "all_platforms"] = "per_platform"
    telegram_chat_id: Optional[str] = None
    approval_chat_id: Optional[str] = None
    vk_group_id: Optional[str] = None
    vk_access_token: Optional[str] = None

class CompanyOut(BaseModel):
    id: int  # internal DB id; use for backend foreign keys
    company_id: str = Field(validation_alias=AliasChoices("company_id", "external_id"))  # stable external id
    external_id: str
    name: str
    description: str
    tone_of_voice: str
    products: str
    target_audience: str
    advantages: str
    brand_guidelines: str
    enabled_platforms: list[str]
    approval_required: bool
    approval_mode: str
    approval_chat_id: Optional[str]
    telegram_chat_id: Optional[str]
    vk_group_id: Optional[str]
    is_active: bool
    class Config:
        from_attributes = True

class GenerateRequest(BaseModel):
    company_id: int
    prompt: str

class ReviseRequest(BaseModel):
    comment: str = Field(min_length=1, max_length=2000)


class CompanyUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=2, max_length=200)
    description: Optional[str] = None
    tone_of_voice: Optional[str] = None
    products: Optional[str] = None
    target_audience: Optional[str] = None
    advantages: Optional[str] = None
    brand_guidelines: Optional[str] = None
    enabled_platforms: Optional[list[Literal["vk", "telegram", "max"]]] = None
    approval_required: Optional[bool] = None
    approval_mode: Optional[Literal["per_platform", "all_platforms"]] = None
    telegram_chat_id: Optional[str] = None
    approval_chat_id: Optional[str] = None
    vk_group_id: Optional[str] = None
    vk_access_token: Optional[str] = None
    is_active: Optional[bool] = None
