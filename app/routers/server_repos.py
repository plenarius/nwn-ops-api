from fastapi import APIRouter, Request

from app.auth import require_feature
from app.services import repo_overview

router = APIRouter(prefix="/api/v1/server", tags=["server"])


@router.get("/repos", dependencies=[require_feature("github")])
async def server_repos(request: Request):
    """The repositories in REPOS: last release, what is merged since, CI runs, the staging run and open pull requests, with links."""
    state = request.app.state
    return await repo_overview.overview(state.http_session, state.settings)
