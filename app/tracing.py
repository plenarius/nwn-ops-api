"""OpenTelemetry request tracing: a slow call breaks down into the API's own time, each SSH command, each database query and each outgoing
HTTP call, under the caller's trace (W3C `traceparent`).

Off unless an OTLP endpoint is set (OTEL_EXPORTER_OTLP_ENDPOINT or OTEL_EXPORTER_OTLP_TRACES_ENDPOINT): then nothing here is imported or patched,
and FastAPI's own telemetry stays idle because no tracer provider is set. Only the standard OTEL_* variables configure it (.env.example lists them).

- The server spans are FastAPI's own (FastAPI(telemetry=...), since 0.16x): it continues the caller's trace, names each span by its route
  template, adds spans for dependency resolution (the key check), the endpoint and serialization, records no headers and no bodies.
- aiomysql has no official instrumentation, so its cursors are wrapped here: one span per query with the statement as written (placeholders,
  never the values) and a span for a wait of over 5 ms for a free connection from the pool.
- Outgoing HTTP: the official aiohttp client (Coolify, GitHub, Discord) and urllib instrumentations; Redis: the official one (it records
  commands as "GET ?").
- Only requests start traces: a span with no parent that is not a server span (a background task's Redis call) is dropped.
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
import os
import re
import time
from typing import Any, Iterator, MutableMapping

logger = logging.getLogger(__name__)

SERVICE_NAME = "nwn-ops-api"
# Seconds an export to the collector may take (the exporter's own default is 10). Exports run on the batch processor's thread, never in a request.
EXPORT_TIMEOUT = 3

# Routes polled every few seconds (or probes), never traced. Regular expressions searched in the request path. OTEL_PYTHON_FASTAPI_EXCLUDED_URLS
# or OTEL_PYTHON_EXCLUDED_URLS (comma-separated regular expressions, the contrib instrumentation's variables) replace this list.
DEFAULT_EXCLUDED = (
    r"^/health$",  # Docker's health check
    r"^/api/v1/server/progress$",  # a panel polls it every few seconds while something runs
)

# Query string keys whose values are never recorded (FastAPI itself redacts only a few signature keys).
_SECRET_QUERY_KEY = re.compile(r"token|key|secret|sig|pass|auth|code|session", re.I)
# A database span's statement is cut at this length (a long IN list).
MAX_STATEMENT = 2000
# A wait for a pool connection shorter than this gets no span.
POOL_WAIT_SPAN_S = 0.005

_enabled = False
_provider = None
_tracer = None


def wanted() -> bool:
    """Whether the environment asks for tracing: an OTLP endpoint is set and the SDK and trace exporter are not turned off."""
    if os.getenv("OTEL_SDK_DISABLED", "").strip().lower() == "true":
        return False
    if os.getenv("OTEL_TRACES_EXPORTER", "otlp").strip().lower() == "none":
        return False
    return bool(os.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT") or os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"))


def enabled() -> bool:
    return _enabled


def excluded_patterns() -> list[re.Pattern]:
    raw = os.getenv("OTEL_PYTHON_FASTAPI_EXCLUDED_URLS") or os.getenv("OTEL_PYTHON_EXCLUDED_URLS")
    items = [s.strip() for s in raw.split(",")] if raw is not None else list(DEFAULT_EXCLUDED)
    return [re.compile(s) for s in items if s]


_excluded: list[re.Pattern] | None = None


def excluded(scope: MutableMapping[str, Any]) -> bool:
    """FastAPI's telemetry `exclude`: True for a request that gets no span."""
    global _excluded
    if _excluded is None:
        _excluded = excluded_patterns()
    path = scope.get("path", "")
    return any(p.search(path) for p in _excluded)


def fastapi_telemetry() -> dict:
    """FastAPI(telemetry=...): spans only (no metrics or log records), without the polled routes. Idle while no tracer provider is set."""
    return {"metrics": False, "logs": False, "exclude": excluded}


# --- set-up -----------------------------------------------------------------------------------------------------------------------------------


def setup(provider=None) -> bool:
    """Sets up tracing when the environment asks for it (wanted()); returns whether it is on. Safe to call again (a no-op). `provider`: a
    TracerProvider to use instead of building one from the environment (tests); it is not made the global one."""
    global _enabled, _provider, _tracer
    if _enabled:
        return True
    if provider is None and not wanted():
        return False
    from opentelemetry import trace

    if provider is None:
        provider = _build_provider()
        trace.set_tracer_provider(provider)
    _provider = provider
    _tracer = provider.get_tracer("app.tracing")
    # The contrib instrumentations write the stable semantic conventions FastAPI's server spans use (url.full, db.query.text...), not the old ones.
    os.environ.setdefault("OTEL_SEMCONV_STABILITY_OPT_IN", "http,database")
    _instrument_aiomysql()
    _instrument_libraries(provider)
    _enabled = True
    logger.info("Tracing on: spans to %s", os.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT") or os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT") or "the given provider")
    return True


def _build_provider():
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    protocol = os.getenv("OTEL_EXPORTER_OTLP_TRACES_PROTOCOL") or os.getenv("OTEL_EXPORTER_OTLP_PROTOCOL") or "http/protobuf"
    if protocol != "http/protobuf":
        logger.warning("Tracing: OTLP protocol %r is not supported here, sending http/protobuf", protocol)
    # OTEL_SERVICE_NAME and OTEL_RESOURCE_ATTRIBUTES are read by Resource.create; the name defaults to nwn-ops-api rather than "unknown_service".
    attrs = {} if os.getenv("OTEL_SERVICE_NAME") or "service.name=" in os.getenv("OTEL_RESOURCE_ATTRIBUTES", "") else {"service.name": SERVICE_NAME}
    provider = TracerProvider(resource=Resource.create(attrs), sampler=requests_only(_env_sampler()))
    provider.add_span_processor(redact_query_processor())
    timeout = None if (os.getenv("OTEL_EXPORTER_OTLP_TRACES_TIMEOUT") or os.getenv("OTEL_EXPORTER_OTLP_TIMEOUT")) else EXPORT_TIMEOUT
    # The batch processor (OTEL_BSP_* variables) queues ended spans and exports them on its own thread; a full queue drops spans, never waits.
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(timeout=timeout)))
    return provider


def _env_sampler():
    """The sampler OTEL_TRACES_SAMPLER and OTEL_TRACES_SAMPLER_ARG name (parentbased_always_on by default)."""
    try:
        from opentelemetry.sdk.trace.sampling import _get_from_env_or_default

        return _get_from_env_or_default()
    except ImportError:  # a later SDK renamed it
        from opentelemetry.sdk.trace.sampling import ParentBased, ALWAYS_ON

        return ParentBased(ALWAYS_ON)


def _instrument_libraries(provider) -> None:
    from opentelemetry.instrumentation.aiohttp_client import AioHttpClientInstrumentor
    from opentelemetry.instrumentation.redis import RedisInstrumentor
    from opentelemetry.instrumentation.urllib import URLLibInstrumentor

    AioHttpClientInstrumentor().instrument(tracer_provider=provider, url_filter=redact_url)
    RedisInstrumentor().instrument(tracer_provider=provider)
    URLLibInstrumentor().instrument(tracer_provider=provider)


def uninstrument() -> None:
    """Undoes setup(): the libraries back as they were, tracing off (tests)."""
    global _enabled, _originals, _provider, _tracer
    if _originals is not None:
        import aiomysql.cursors
        import aiomysql.pool

        aiomysql.cursors.Cursor.execute, aiomysql.cursors.Cursor.executemany, aiomysql.pool.Pool._acquire = _originals
        _originals = None
    if _enabled:
        from opentelemetry.instrumentation.aiohttp_client import AioHttpClientInstrumentor
        from opentelemetry.instrumentation.redis import RedisInstrumentor
        from opentelemetry.instrumentation.urllib import URLLibInstrumentor

        for i in (AioHttpClientInstrumentor(), RedisInstrumentor(), URLLibInstrumentor()):
            i.uninstrument()
    _enabled, _provider, _tracer = False, None, None


def shutdown() -> None:
    """Exports what is still queued (bounded by the export timeout) and stops; at the app's shutdown."""
    global _enabled
    if _provider is not None and hasattr(_provider, "shutdown"):
        try:
            _provider.shutdown()
        except Exception:  # never let tracing stop a shutdown
            logger.exception("Tracing shutdown failed")
    _enabled = False


# --- sampling and redaction ---------------------------------------------------------------------------------------------------------------


def requests_only(inner):
    """A sampler: a span with no parent that is not a server span is dropped; everything else is up to `inner` (the configured sampler).
    Built here so the SDK is imported only when tracing is on."""
    from opentelemetry import trace
    from opentelemetry.sdk.trace.sampling import Decision, Sampler, SamplingResult

    class RequestsOnly(Sampler):
        def should_sample(self, parent_context, trace_id, name, kind=None, attributes=None, links=None, trace_state=None):
            if kind is not trace.SpanKind.SERVER and not trace.get_current_span(parent_context).get_span_context().is_valid:
                return SamplingResult(Decision.DROP)
            return inner.should_sample(parent_context, trace_id, name, kind, attributes, links, trace_state)

        def get_description(self) -> str:
            return f"RequestsOnly{{{inner.get_description()}}}"

    return RequestsOnly()


def redact_query_processor():
    """A span processor: the values of secret-looking query string keys (sub, code, token...) on a server span become REDACTED."""
    from opentelemetry.sdk.trace import SpanProcessor

    class RedactQuery(SpanProcessor):
        def on_start(self, span, parent_context=None):
            q = (span.attributes or {}).get("url.query")
            if q:
                span.set_attribute("url.query", redact_query(q))

    return RedactQuery()


def redact_query(q: str) -> str:
    from urllib.parse import parse_qsl, urlencode

    return urlencode([(k, "REDACTED" if _SECRET_QUERY_KEY.search(k) else v) for k, v in parse_qsl(q, keep_blank_values=True)])


_WEBHOOK = re.compile(r"(/api/webhooks/\d+/)[^/?#]+")


def redact_url(url) -> str:
    """aiohttp's url_filter: no query string, no fragment, and a Discord webhook's token (its last path segment) replaced."""
    s = str(url).split("#", 1)[0].split("?", 1)[0]
    return _WEBHOOK.sub(r"\1REDACTED", s)


# --- manual spans -------------------------------------------------------------------------------------------------------------------------------


@contextlib.contextmanager
def span(name: str, **attributes) -> Iterator[Any]:
    """A client span around a call to another system (an SSH command), or nothing while tracing is off."""
    if not _enabled:
        yield None
        return
    from opentelemetry import trace

    with _tracer.start_as_current_span(name, kind=trace.SpanKind.CLIENT, attributes=attributes) as s:
        yield s


def _in_request() -> bool:
    from opentelemetry import trace

    return trace.get_current_span().is_recording()


# --- aiomysql -------------------------------------------------------------------------------------------------------------------------------------

_KEYWORD = re.compile(r"^\s*(?:/\*.*?\*/\s*)*\(?\s*([A-Za-z]+)", re.S)
_TABLE = {
    "SELECT": re.compile(r"\bFROM\s+`?([\w$]+)`?(?:\s*\.\s*`?([\w$]+)`?)?", re.I),
    "DELETE": re.compile(r"\bFROM\s+`?([\w$]+)`?(?:\s*\.\s*`?([\w$]+)`?)?", re.I),
    "INSERT": re.compile(r"\bINTO\s+`?([\w$]+)`?(?:\s*\.\s*`?([\w$]+)`?)?", re.I),
    "REPLACE": re.compile(r"\bINTO\s+`?([\w$]+)`?(?:\s*\.\s*`?([\w$]+)`?)?", re.I),
    "UPDATE": re.compile(r"^\s*UPDATE\s+(?:LOW_PRIORITY\s+|IGNORE\s+)*`?([\w$]+)`?(?:\s*\.\s*`?([\w$]+)`?)?", re.I),
}
_STRING = re.compile(r"'(?:[^'\\]|\\.|'')*'|\"(?:[^\"\\]|\\.|\"\")*\"", re.S)
_NUMBER = re.compile(r"(?<![\w$.])-?(?:0x[0-9a-fA-F]+|\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)(?![\w$])")


def sanitize_sql(sql: str) -> str:
    """The statement as written, with any literal written into it (quoted strings, numbers) replaced by ?: queries pass their values as %s
    parameters, which are never recorded, but a few build an IN list or a LIMIT into the text."""
    s = _NUMBER.sub("?", _STRING.sub("?", sql))
    s = " ".join(s.split())
    return s if len(s) <= MAX_STATEMENT else s[: MAX_STATEMENT - 3] + "..."


def sql_summary(sql: str) -> tuple[str, str | None]:
    """(operation, table) for a span name: ("SELECT", "characters"), ("ALTER", None)."""
    m = _KEYWORD.match(sql)
    op = m.group(1).upper() if m else "QUERY"
    pat = _TABLE.get(op)
    t = pat.search(sql) if pat else None
    table = (t.group(2) or t.group(1)) if t else None
    if table and table.upper() == "DUAL":
        table = None
    return op, table


_in_executemany: contextvars.ContextVar[bool] = contextvars.ContextVar("nwn_ops_tracing_executemany", default=False)
_originals: tuple | None = None


def _instrument_aiomysql() -> None:
    global _originals
    if _originals is not None:
        return
    import aiomysql.cursors
    import aiomysql.pool
    from opentelemetry import trace

    Cursor = aiomysql.cursors.Cursor
    Pool = aiomysql.pool.Pool
    orig_execute, orig_executemany, orig_acquire = Cursor.execute, Cursor.executemany, Pool._acquire

    def attrs(cur, query: str) -> tuple[str, dict]:
        op, table = sql_summary(query)
        conn = cur._connection
        db = getattr(conn, "db", None)
        a = {"db.system.name": "mysql", "db.operation.name": op, "db.query.text": sanitize_sql(query)}
        if db:
            a["db.namespace"] = db
        if table:
            a["db.collection.name"] = table
        if getattr(conn, "host", None):
            a["server.address"] = conn.host
        if getattr(conn, "port", None):
            a["server.port"] = conn.port
        return f"{op} {table or db or 'mysql'}", a

    async def traced(cur, query, call, batch: int | None = None):
        if not _enabled or _in_executemany.get() or not _in_request():
            return await call()
        try:
            name, a = attrs(cur, query if isinstance(query, str) else query.decode("utf-8", "replace"))
        except Exception:  # never let tracing break a query
            return await call()
        if batch is not None:
            a["db.operation.batch.size"] = batch
        with _tracer.start_as_current_span(name, kind=trace.SpanKind.CLIENT, attributes=a) as s:
            try:
                rows = await call()
            except Exception as e:
                code = getattr(e, "args", (None,))[0] if getattr(e, "args", None) else None
                s.set_attribute("error.type", type(e).__qualname__)
                if isinstance(code, int):
                    s.set_attribute("db.response.status_code", str(code))
                raise
            if isinstance(rows, int) and rows >= 0:
                s.set_attribute("db.response.returned_rows", rows)
            return rows

    async def execute(self, query, args=None):
        return await traced(self, query, lambda: orig_execute(self, query, args))

    async def executemany(self, query, args):
        async def run():
            token = _in_executemany.set(True)
            try:
                return await orig_executemany(self, query, args)
            finally:
                _in_executemany.reset(token)

        n = len(args) if hasattr(args, "__len__") else None
        return await traced(self, query, run, batch=n)

    async def _acquire(self):
        if not _enabled or not _in_request():
            return await orig_acquire(self)
        t0, ns0 = time.perf_counter(), time.time_ns()
        conn = await orig_acquire(self)
        waited = time.perf_counter() - t0
        if waited >= POOL_WAIT_SPAN_S:
            # Made afterwards, so a quick checkout (nearly all of them) costs no span.
            s = _tracer.start_span(f"pool wait {getattr(conn, 'db', '') or 'mysql'}", start_time=ns0, kind=trace.SpanKind.INTERNAL,
                                   attributes={"db.system.name": "mysql", "db.namespace": getattr(conn, "db", "") or "",
                                               "db.client.connection.pool.size": self.maxsize, "db.client.connection.pool.used": len(self._used)})
            s.end(end_time=ns0 + int(waited * 1e9))
        return conn

    _originals = (orig_execute, orig_executemany, orig_acquire)
    Cursor.execute, Cursor.executemany, Pool._acquire = execute, executemany, _acquire
