import secrets

from fastapi import Depends, HTTPException, Request


def require_api_key(*expected_keys: str, access: str = ""):
    """Accept a bearer token matching any of the non-empty expected keys. `access` names the key group (full, admin, team):
    /api/v1/meta/systems lists each route under it."""
    keys = [k for k in expected_keys if k]

    async def _verify(request: Request):
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
        token = auth[7:]
        if not token or not keys:
            raise HTTPException(status_code=401, detail="Invalid API key")
        # Compare against every key so the timing doesn't reveal which one matched.
        matched = False
        for key in keys:
            matched |= secrets.compare_digest(token, key)
        if not matched:
            raise HTTPException(status_code=401, detail="Invalid API key")

    _verify.access = access
    return Depends(_verify)


def require_feature(*names: str):
    """A route that needs a part of the API (Settings.features): 503 naming the missing settings when it is off."""

    async def _check(request: Request):
        s = request.app.state.settings
        on = s.features()
        missing = [n for n in names if not on.get(n)]
        if missing:
            raise HTTPException(503, "Not configured: set " + "; ".join(s.NEEDS[n] for n in missing))

    return Depends(_check)
