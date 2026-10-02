from fastapi import APIRouter, Depends

from app.api.deps import get_settings, require_admin
from app.core.config import Settings
from app.core.security import issue_token
from app.schemas import TokenRequest, TokenResponse

router = APIRouter(tags=["auth"])


@router.post("/auth/token", response_model=TokenResponse, dependencies=[Depends(require_admin)])
async def create_token(body: TokenRequest, settings: Settings = Depends(get_settings)) -> TokenResponse:
    """Mint a signed user token for `username` (admin only).

    Stands in for a real identity provider so load tests can create many
    users. It requires the admin token: otherwise anyone could mint a token
    for someone else's username and act as them (e.g. cancel their seats).
    Everything downstream trusts only the token's `sub` claim.
    """
    token, ttl = issue_token(body.username, settings)
    return TokenResponse(access_token=token, user_id=body.username, expires_in=ttl)
