from typing import Any

from pydantic import BaseModel, Field


class LoginForm(BaseModel):
    username: str = Field(min_length=1, max_length=40)
    password: str = Field(min_length=1, max_length=128)


class CaseForm(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    payload: dict[str, Any]


class ReviewForm(BaseModel):
    note: str = Field(min_length=1, max_length=500)


class HandoverBookForm(BaseModel):
    title: str | None = Field(default=None, max_length=120)
    request_key: str | None = Field(default=None, max_length=80)
    batch_size: int | None = Field(default=100, ge=1, le=1000)
    build: bool = True
