from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.auth import require_feature
from app.services import dev as dev_svc

# Loading is the team key's (a bot's builders), status the read key's (a status page).
team_router = APIRouter(prefix="/api/v1/dev", tags=["dev"])
router = APIRouter(prefix="/api/v1/dev", tags=["dev"])


class LoadRequest(BaseModel):
    target: str
    requested_by: str = "the API"


@team_router.post("/load", dependencies=[require_feature("dev")])
async def load(body: LoadRequest, request: Request):
    """Start the dev load workflow for `pr-<n>`, `main` or `rollback`; 409 while a load is still running."""
    state = request.app.state
    try:
        return await dev_svc.load(state.http_session, state.settings, body.target, body.requested_by)
    except dev_svc.DevError as e:
        raise HTTPException(e.status, e.message) from e


@router.get("/status", dependencies=[require_feature("dev")])
async def status(request: Request):
    """What the dev server runs (its status file: build, commit, content version, health) and the last load workflow run."""
    state = request.app.state
    return await dev_svc.status(state.http_session, state.settings)
