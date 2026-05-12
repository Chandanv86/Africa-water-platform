from __future__ import annotations

from fastapi import APIRouter
from app.config import get_settings
from app.services.gee_service import auth_status
from app.services.stac_service import get_recent_context

router = APIRouter()


@router.get("")
async def health():
    settings = get_settings()
    return {
        "status": "ok",
        "app_name": settings.app_name,
        "version": settings.app_version,
        "earth_engine": auth_status(),
    }
