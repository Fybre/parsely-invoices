"""
Pydantic models for dashboard API requests.
"""
from pydantic import BaseModel
from typing import Optional


class StatusUpdate(BaseModel):
    status: str   # needs_review | ready


class CorrectionsUpdate(BaseModel):
    corrections: dict   # { "field_path": "corrected_value", … }


class NotesUpdate(BaseModel):
    notes: str


class AdminDataUpdate(BaseModel):
    headers: list[str]
    rows: list[dict]


class UserCreate(BaseModel):
    username: str
    password: str
    role: str   # "admin" | "user"


class SupplierCreate(BaseModel):
    name: str
    abn: str = ""
    acn: str = ""
    email: str = ""
    phone: str = ""
    address: str = ""


class UserUpdate(BaseModel):
    password: Optional[str] = None
    role: Optional[str] = None
