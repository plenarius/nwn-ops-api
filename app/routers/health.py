from fastapi import APIRouter

router = APIRouter()


@router.get("/health")
async def health():
    """Whether the API is up (no key: for Docker's health check)."""
    return {"status": "ok"}
