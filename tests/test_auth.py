import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth import require_api_key


def _make_app(api_key: str) -> FastAPI:
    app = FastAPI()

    @app.get("/protected")
    async def protected(dep=require_api_key(api_key)):
        return {"ok": True}

    return app


def test_valid_api_key():
    client = TestClient(_make_app("secret"))
    resp = client.get("/protected", headers={"Authorization": "Bearer secret"})
    assert resp.status_code == 200


def test_missing_api_key():
    client = TestClient(_make_app("secret"))
    resp = client.get("/protected")
    assert resp.status_code == 401


def test_wrong_api_key():
    client = TestClient(_make_app("secret"))
    resp = client.get("/protected", headers={"Authorization": "Bearer wrong"})
    assert resp.status_code == 401


def test_malformed_header():
    client = TestClient(_make_app("secret"))
    resp = client.get("/protected", headers={"Authorization": "Token secret"})
    assert resp.status_code == 401


def test_empty_api_key_rejected():
    client = TestClient(_make_app(""))
    resp = client.get("/protected", headers={"Authorization": "Bearer "})
    assert resp.status_code == 401


def _make_multi_key_app(*keys: str) -> FastAPI:
    app = FastAPI()

    @app.get("/protected")
    async def protected(dep=require_api_key(*keys)):
        return {"ok": True}

    return app


def test_any_of_several_keys_is_accepted():
    client = TestClient(_make_multi_key_app("full", "readonly"))
    for key in ("full", "readonly"):
        assert client.get("/protected", headers={"Authorization": f"Bearer {key}"}).status_code == 200


def test_unset_readonly_key_does_not_open_the_route():
    client = TestClient(_make_multi_key_app("full", ""))
    assert client.get("/protected", headers={"Authorization": "Bearer "}).status_code == 401
    assert client.get("/protected", headers={"Authorization": "Bearer full"}).status_code == 200


def test_readonly_key_is_rejected_by_full_key_routes():
    client = TestClient(_make_app("full"))
    assert client.get("/protected", headers={"Authorization": "Bearer readonly"}).status_code == 401
