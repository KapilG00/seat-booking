from typing import Literal

from pydantic import BaseModel

from app.schemas.common import Username


class TokenRequest(BaseModel):
    username: Username


class TokenResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    user_id: str
    expires_in: int
