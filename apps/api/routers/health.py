"""Health check router."""

from fastapi import APIRouter

from recoveryos.config import get_settings

router = APIRouter()


@router.get("/health", summary="Health check")
async def health():
    settings = get_settings()
    return {
        "status": "ok",
        "env": settings.env.value,
        "service": "recoveryos-api",
    }
