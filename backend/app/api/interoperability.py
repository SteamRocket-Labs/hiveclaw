"""Machine-readable interoperability profile endpoints."""

from fastapi import APIRouter, Request

from app.services.interoperability import build_interoperability_profile

router = APIRouter(prefix="/interoperability", tags=["interoperability"])


@router.get("/profile")
async def get_interoperability_profile(request: Request):
    return build_interoperability_profile(base_url=str(request.base_url).rstrip("/"))
