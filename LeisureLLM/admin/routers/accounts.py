"""Admin-only account management routes for the web console."""

from __future__ import annotations

from core.services.web_identity_service import IdentityError, WebIdentityService
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from admin.dependencies import get_current_actor, get_db, require_admin

router = APIRouter(tags=["accounts"], dependencies=[Depends(require_admin)])


class WebAccountCreate(BaseModel):
    username: str
    password: str
    display_name: str | None = None
    role: str = "member"


@router.get("/api/v1/admin/users")
async def api_list_web_users(db=Depends(get_db)):
    service = WebIdentityService(db)
    accounts = await service.list_accounts()
    return {
        "success": True,
        "users": [
            {
                "id": account["id"],
                "stable_id": account["stable_id"],
                "username": account["username"],
                "display_name": account["display_name"],
                "role": account["role"],
                "is_active": bool(account["is_active"]),
                "bootstrap_source": account["bootstrap_source"],
                "last_login_at": account["last_login_at"],
                "created_at": account["created_at"],
                "updated_at": account["updated_at"],
                "actor_stable_id": account["actor_stable_id"],
            }
            for account in accounts
        ],
    }


@router.post("/api/v1/admin/users")
async def api_create_web_user(
    data: WebAccountCreate,
    current_actor=Depends(get_current_actor),
    db=Depends(get_db),
):
    service = WebIdentityService(db)
    try:
        account = await service.create_account(
            username=data.username,
            password=data.password,
            display_name=data.display_name,
            role=data.role,
            created_by_account_id=current_actor.account_id,
        )
    except IdentityError as exc:
        return {"success": False, "error": str(exc)}

    return {
        "success": True,
        "user": {
            "id": account["id"],
            "stable_id": account["stable_id"],
            "username": account["username"],
            "display_name": account["display_name"],
            "role": account["role"],
            "actor_id": account["actor_id"],
            "actor_stable_id": account["actor_stable_id"],
        },
    }