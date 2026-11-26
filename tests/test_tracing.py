"""Request tracing (app/tracing.py): off without an OTLP endpoint; when on, a request continues the caller's trace (W3C traceparent), its
database queries are children of it with the statement but never the values, nothing records the Authorization header, the polled routes
get no span, and a query outside a request starts no trace. The database part needs OPS_TEST_MARIADB (user:password@host:port) and is
skipped without it."""

from __future__ import annotations

import os
import re

import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient

from app import tracing

TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
PARENT_ID = "00f067aa0ba902b7"
TRACEPARENT = f"00-{TRACE_ID}-{PARENT_ID}-01"

_m = re.match(r"^([^:@]+):([^@]*)@([^:]+):(\d+)$", os.getenv("OPS_TEST_MARIADB", ""))


# --- the pieces -------------------------------------------------------------------------------------------------------------------------------


def test_off_without_an_endpoint(monkeypatch):
    for k in ("OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "OTEL_SDK_DISABLED", "OTEL_TRACES_EXPORTER"):
        monkeypatch.delenv(k, raising=False)
    assert not tracing.wanted()
    assert tracing.setup() is False and not tracing.enabled()
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:4318")
    assert tracing.wanted()
    monkeypatch.setenv("OTEL_TRACES_EXPORTER", "none")
    assert not tracing.wanted()
    monkeypatch.delenv("OTEL_TRACES_EXPORTER")
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    assert not tracing.wanted()


def test_app_is_made_with_tracing_off():
    from app.main import create_app
    from tests.conftest import make_settings

    app = create_app(make_settings())
    assert not tracing.enabled()
    assert app._telemetry["exclude"] is tracing.excluded


@pytest.mark.parametrize("path,skip", [
    ("/health", True),
    ("/api/v1/server/progress", True),
    ("/api/v1/server/status", False),
    ("/api/v1/server/restart", False),
])
def test_excluded_routes(path, skip):
    assert tracing.excluded({"path": path}) is skip


def test_sql_without_values():
    assert tracing.sanitize_sql("SELECT a FROM t WHERE id = %s AND n = %s") == "SELECT a FROM t WHERE id = %s AND n = %s"
    assert tracing.sanitize_sql("SELECT * FROM t1 WHERE name = 'O''Brien' AND x IN (1, 2, 3) LIMIT 50") == \
        "SELECT * FROM t1 WHERE name = ? AND x IN (?, ?, ?) LIMIT ?"
    assert tracing.sanitize_sql('UPDATE t SET v = "secret", c2 = -1.5e3') == "UPDATE t SET v = ?, c2 = ?"
    assert len(tracing.sanitize_sql("SELECT " + ", ".join(f"c{i}" for i in range(2000)) + " FROM t")) == tracing.MAX_STATEMENT


@pytest.mark.parametrize("sql,want", [
    ("SELECT a FROM `characters` WHERE x", ("SELECT", "characters")),
    ("  select a from game.characters c join x", ("SELECT", "characters")),
    ("INSERT INTO queue (a) VALUES (%s)", ("INSERT", "queue")),
    ("REPLACE INTO pwdata VALUES (%s)", ("REPLACE", "pwdata")),
    ("UPDATE IGNORE players SET a = 1", ("UPDATE", "players")),
    ("DELETE FROM items WHERE id = %s", ("DELETE", "items")),
    ("ALTER TABLE reports ADD COLUMN x INT", ("ALTER", None)),
    ("(SELECT 1)", ("SELECT", None)),
])
def test_sql_summary(sql, want):
    assert tracing.sql_summary(sql) == want


def test_redaction():
    assert tracing.redact_url("https://discord.com/api/webhooks/123/abcDEF-token?wait=true") == "https://discord.com/api/webhooks/123/REDACTED"
    assert tracing.redact_url("https://api.github.com/repos/x/y/pulls?state=open#a") == "https://api.github.com/repos/x/y/pulls"
    assert tracing.redact_query("session=abc&c=thing&lines=100&token=t") == "session=REDACTED&c=thing&lines=100&token=REDACTED"


def test_ssh_program():
    from app.ssh import _program

    assert _program("docker exec -i mariadb sh -c 'mysql nwn'") == "docker"
    assert _program("cd /srv/game && FOO=1 /usr/bin/cat x") == "cat"
    assert _program("timeout 5 ls /x") == "ls"
    assert _program("") == "sh"


# --- a request, end to end ----------------------------------------------------------------------------------------------------------------


@pytest.fixture
def traced():
    """Tracing on with an in-memory exporter (the provider is not made the global one), undone afterwards."""
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider(sampler=tracing.requests_only(tracing._env_sampler()))
    provider.add_span_processor(tracing.redact_query_processor())
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    assert tracing.setup(provider=provider)
    try:
        yield provider, exporter
    finally:
        tracing.uninstrument()
        provider.shutdown()


def _app(provider, query: str | None = None) -> FastAPI:
    import aiomysql

    async def key(request: Request):
        assert request.headers["Authorization"] == "Bearer secret-key-value"

    app = FastAPI(telemetry={**tracing.fastapi_telemetry(), "tracer_provider": provider})

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/api/v1/things/{thing_id}", dependencies=[Depends(key)])
    async def thing(thing_id: int):
        if query is None:
            return {"id": thing_id}
        user, password, host, port = _m.groups()
        pool = await aiomysql.create_pool(host=host, port=int(port), user=user, password=password, db="mysql", minsize=1, maxsize=1)
        try:
            async with pool.acquire() as conn, conn.cursor() as cur:
                await cur.execute(query, (thing_id, "Someone's secret"))
                await cur.fetchall()
                await cur.executemany("SELECT %s, %s", [(1, "a"), (2, "b")])
        finally:
            pool.close()
            await pool.wait_closed()
        return {"id": thing_id}

    return app


def _get(app: FastAPI, path: str):
    with TestClient(app) as c:
        r = c.get(path, headers={"traceparent": TRACEPARENT, "Authorization": "Bearer secret-key-value"})
    assert r.status_code == 200, r.text
    return r


def test_request_continues_the_callers_trace(traced):
    provider, exporter = traced
    _get(_app(provider), "/api/v1/things/42?session=person-1&c=thing")
    spans = exporter.get_finished_spans()
    server = [s for s in spans if s.kind.name == "SERVER"]
    assert len(server) == 1
    s = server[0]
    assert s.name == "GET /api/v1/things/{thing_id}"
    assert f"{s.context.trace_id:032x}" == TRACE_ID and f"{s.parent.span_id:016x}" == PARENT_ID
    assert s.attributes["url.query"] == "session=REDACTED&c=thing"
    assert {x.name for x in spans} >= {"fastapi.dependencies", "fastapi.endpoint"}
    assert all(f"{x.context.trace_id:032x}" == TRACE_ID for x in spans)
    assert "secret-key-value" not in repr([dict(x.attributes) for x in spans])


def test_polled_routes_get_no_span(traced):
    provider, exporter = traced
    _get(_app(provider), "/health")
    assert exporter.get_finished_spans() == ()


@pytest.mark.skipif(not _m, reason="OPS_TEST_MARIADB (user:password@host:port) not set")
def test_queries_are_children_without_values(traced):
    provider, exporter = traced
    _get(_app(provider, "SELECT %s AS id, %s AS who FROM DUAL"), "/api/v1/things/42")
    spans = exporter.get_finished_spans()
    endpoint = next(s for s in spans if s.name == "fastapi.endpoint")
    db = [s for s in spans if s.attributes.get("db.system.name") == "mysql" and s.name != "pool wait mysql"]
    assert [s.name for s in db] == ["SELECT mysql", "SELECT mysql"]
    one, many = db
    assert one.parent.span_id == endpoint.context.span_id
    assert one.attributes["db.query.text"] == "SELECT %s AS id, %s AS who FROM DUAL"
    assert one.attributes["db.namespace"] == "mysql"
    assert many.attributes["db.operation.batch.size"] == 2 and many.attributes["db.query.text"] == "SELECT %s, %s"
    assert "Someone" not in repr([dict(s.attributes) for s in spans])


@pytest.mark.skipif(not _m, reason="OPS_TEST_MARIADB (user:password@host:port) not set")
def test_query_outside_a_request_starts_no_trace(traced):
    import asyncio

    import aiomysql

    provider, exporter = traced

    async def job():
        user, password, host, port = _m.groups()
        conn = await aiomysql.connect(host=host, port=int(port), user=user, password=password, db="mysql")
        try:
            async with conn.cursor() as cur:
                await cur.execute("SELECT 1")
        finally:
            conn.close()

    asyncio.run(job())
    assert exporter.get_finished_spans() == ()
