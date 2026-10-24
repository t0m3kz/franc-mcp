"""Tests for MCP-level middleware, token passthrough and the traversal/schema additions."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from infrahub_sdk.exceptions import NodeNotFoundError
from mcp.types import ToolAnnotations

from franc.mcp_middleware import (
    MetricsMiddleware,
    ReadOnlyMiddleware,
    ResponseSizeGuardMiddleware,
    SafeRetryMiddleware,
    TokenPassthroughMiddleware,
    configure_mcp_middleware,
)
from franc.server import mcp as franc_mcp
from franc.traversal import NodeResolutionError, is_uuid, resolve_node_ref
from franc.utils import _TOKEN_CLIENTS, _client_for_token, request_token

READ_ANNOTATIONS = ToolAnnotations(readOnlyHint=True)


def _server(*middleware) -> FastMCP:
    """Tiny server: one read tool, one write tool, one flaky read tool and one flaky write tool."""
    server = FastMCP("t")
    calls = {"flaky_read": 0, "flaky_write": 0}

    @server.tool(tags={"retrieve"}, annotations=READ_ANNOTATIONS)
    def read_thing() -> str:
        return "ok"

    @server.tool(tags={"create"})
    def create_thing() -> str:
        return "created"

    @server.tool(tags={"write"})
    def write_thing() -> str:
        return "written"

    @server.tool(tags={"retrieve"}, annotations=READ_ANNOTATIONS)
    def flaky_read() -> str:
        calls["flaky_read"] += 1
        if calls["flaky_read"] == 1:
            raise ConnectionError("blip")
        return "recovered"

    @server.tool(tags={"create"})
    def flaky_write() -> str:
        calls["flaky_write"] += 1
        raise ConnectionError("blip")

    @server.tool(tags={"retrieve"}, annotations=READ_ANNOTATIONS)
    def big() -> str:
        return "x" * 5000

    server.calls = calls  # ty: ignore[unresolved-attribute]
    for m in middleware:
        server.add_middleware(m)
    return server


# ---------------------------------------------------------------------------
# Read-only mode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_only_hides_and_blocks_write_tools():
    server = _server(ReadOnlyMiddleware())
    async with Client(server) as c:
        names = {t.name for t in await c.list_tools()}
        assert "read_thing" in names
        assert not names & {"create_thing", "write_thing", "flaky_write"}

        assert (await c.call_tool("read_thing", {})).data == "ok"
        for blocked in ("create_thing", "write_thing"):
            with pytest.raises(ToolError, match="read-only"):
                await c.call_tool(blocked, {})


@pytest.mark.asyncio
async def test_write_tools_visible_without_read_only():
    async with Client(_server()) as c:
        assert {"create_thing", "write_thing"} <= {t.name for t in await c.list_tools()}


def test_real_server_write_tools_are_tagged():
    """Every Franc tool that changes Infrahub state must carry a tag ReadOnlyMiddleware recognises."""
    import asyncio

    from franc.mcp_middleware import WRITE_TAGS

    tools = {t.name: t for t in asyncio.run(franc_mcp.list_tools())}
    for name in (
        "branch_create",
        "node_upsert",
        "node_delete",
        "mutate_graphql",
        "create_proposed_change",
        "create_datacenter_deployment",
        "create_customer_boarding",
        "create_application_deployment",
    ):
        assert WRITE_TAGS & set(tools[name].tags), f"{name} is not tagged as a write tool"


# ---------------------------------------------------------------------------
# Retry (reads only)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_safe_retry_retries_reads_but_never_writes():
    server = _server(SafeRetryMiddleware(max_retries=2, base_delay=0.0, retry_exceptions=(ConnectionError,)))
    async with Client(server) as c:
        assert (await c.call_tool("flaky_read", {})).data == "recovered"
        assert server.calls["flaky_read"] == 2  # ty: ignore[unresolved-attribute]

        with pytest.raises(ToolError):
            await c.call_tool("flaky_write", {})
        assert server.calls["flaky_write"] == 1  # ty: ignore[unresolved-attribute]


# ---------------------------------------------------------------------------
# Response size guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_size_guard_replaces_oversized_response_with_guidance():
    server = _server(ResponseSizeGuardMiddleware(max_size=1000))
    async with Client(server) as c:
        assert (await c.call_tool("read_thing", {})).data == "ok"
        with pytest.raises(ToolError, match="Narrow the request"):
            await c.call_tool("big", {})


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_metrics_counts_calls_and_errors():
    metrics = MetricsMiddleware()
    server = _server(metrics)
    async with Client(server) as c:
        await c.call_tool("read_thing", {})
        await c.call_tool("read_thing", {})
        with pytest.raises(ToolError):
            await c.call_tool("flaky_write", {})

    snap = metrics.snapshot()
    assert snap["read_thing"]["calls"] == 2
    assert snap["flaky_write"]["errors"] == 1
    text = metrics.prometheus_text()
    assert 'franc_tool_calls_total{tool="read_thing"} 2' in text
    assert 'franc_tool_errors_total{tool="flaky_write"} 1' in text


# ---------------------------------------------------------------------------
# Token passthrough
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_token_passthrough_rejects_calls_without_a_token():
    """Fail closed: no HTTP headers (e.g. stdio) means no token, so the shared credential is never used."""
    server = _server(TokenPassthroughMiddleware())
    async with Client(server) as c:
        with pytest.raises(ToolError, match="your own Infrahub token"):
            await c.call_tool("read_thing", {})


def test_client_for_token_is_cached_per_token_and_authenticates_as_caller():
    _TOKEN_CLIENTS.clear()
    base = SimpleNamespace(address="http://infrahub.test:8000")
    a1 = _client_for_token(base, "token-a")  # ty: ignore[invalid-argument-type]
    a2 = _client_for_token(base, "token-a")  # ty: ignore[invalid-argument-type]
    b = _client_for_token(base, "token-b")  # ty: ignore[invalid-argument-type]
    assert a1 is a2
    assert a1 is not b
    assert a1.config.api_token == "token-a"
    assert a1.address == "http://infrahub.test:8000"
    assert "token-a" not in _TOKEN_CLIENTS  # keyed by digest, never by raw token
    _TOKEN_CLIENTS.clear()


def test_request_token_defaults_to_shared_credential():
    assert request_token.get() is None


# ---------------------------------------------------------------------------
# Wiring from environment
# ---------------------------------------------------------------------------


def _names(server: FastMCP) -> set[str]:
    return {type(m).__name__ for m in server.middleware if type(m).__module__.startswith("franc")}


def test_configure_defaults(monkeypatch):
    for var in ("FRANC_READ_ONLY", "FRANC_METRICS", "FRANC_AUTH_MODE", "FRANC_OTEL", "FRANC_AUDIT"):
        monkeypatch.delenv(var, raising=False)
    server = FastMCP("t")
    configure_mcp_middleware(server)
    assert _names(server) == {"AuditMiddleware", "SafeRetryMiddleware", "ResponseSizeGuardMiddleware"}


def test_configure_opt_ins(monkeypatch):
    monkeypatch.setenv("FRANC_READ_ONLY", "true")
    monkeypatch.setenv("FRANC_METRICS", "1")
    monkeypatch.setenv("FRANC_AUTH_MODE", "token-passthrough")
    monkeypatch.setenv("FRANC_AUDIT", "false")
    monkeypatch.setenv("FRANC_RETRIES", "0")
    monkeypatch.setenv("FRANC_MAX_RESPONSE_BYTES", "0")
    server = FastMCP("t")
    configure_mcp_middleware(server)
    assert _names(server) == {"MetricsMiddleware", "TokenPassthroughMiddleware", "ReadOnlyMiddleware"}


# ---------------------------------------------------------------------------
# Node reference resolution
# ---------------------------------------------------------------------------


def test_is_uuid_accepts_only_canonical_form():
    assert is_uuid("3f1c2a9e-5b7d-4e86-9a41-c0d2e8b17f35")
    assert not is_uuid("06438eb280194776878c0941b1f1d1ec")
    assert not is_uuid("DcimPhysicalDevice__dc1-fw-01")


@pytest.mark.asyncio
async def test_resolve_node_ref():
    uid = "3f1c2a9e-5b7d-4e86-9a41-c0d2e8b17f35"
    client = SimpleNamespace(get=AsyncMock(return_value="node"))

    assert await resolve_node_ref(client, uid) == uid  # ty: ignore[invalid-argument-type]
    client.get.assert_not_called()

    assert await resolve_node_ref(client, "DcimPhysicalDevice__dc1__fw-01") == "node"  # ty: ignore[invalid-argument-type]
    client.get.assert_awaited_once_with(kind="DcimPhysicalDevice", hfid=["dc1", "fw-01"], branch=None)

    with pytest.raises(NodeResolutionError, match="neither a UUID"):
        await resolve_node_ref(client, "plain-name")  # ty: ignore[invalid-argument-type]

    client.get = AsyncMock(side_effect=NodeNotFoundError(node_type="X", identifier={"hfid": ["a"]}))
    with pytest.raises(NodeResolutionError, match="Could not resolve"):
        await resolve_node_ref(client, "X__a")  # ty: ignore[invalid-argument-type]


# ---------------------------------------------------------------------------
# find_reachable
# ---------------------------------------------------------------------------


def _node(kind: str, label: str) -> SimpleNamespace:
    return SimpleNamespace(id=f"id-{label}", kind=kind, display_label=label, hfid=[label])


@pytest.mark.asyncio
async def test_find_reachable_by_uuid():
    uid = "3f1c2a9e-5b7d-4e86-9a41-c0d2e8b17f35"
    result = SimpleNamespace(
        source=_node("DcimPhysicalDevice", "fw-01"),
        count=1,
        dependencies=[
            SimpleNamespace(
                depth=2,
                node=_node("ManagedAppComponent", "auth-api"),
                path=SimpleNamespace(
                    hops=[
                        SimpleNamespace(
                            node=_node("ManagedVxlanSegment", "seg"), relationship=SimpleNamespace(from_label="segment")
                        ),
                        SimpleNamespace(node=_node("ManagedAppComponent", "auth-api"), relationship=None),
                    ]
                ),
            )
        ],
    )
    fake = SimpleNamespace(reachable_nodes=AsyncMock(return_value=result))
    franc_mcp.test_client = fake
    try:
        async with Client(franc_mcp) as c:
            res = await c.call_tool("find_reachable", {"source": uid, "target_kinds": ["ManagedAppComponent"]})
        data = res.data.data if hasattr(res.data, "data") else res.data
    finally:
        franc_mcp.test_client = None

    assert data["count"] == 1
    dep = data["dependencies"][0]
    assert dep["depth"] == 2
    assert dep["node"]["display_label"] == "auth-api"
    assert dep["path"][0]["relationship"] == "segment"
    kwargs = fake.reachable_nodes.await_args.kwargs
    assert fake.reachable_nodes.await_args.args[0] == uid
    assert kwargs["max_results"] == 20 and kwargs["shortest_paths_only"] is True


@pytest.mark.asyncio
async def test_find_reachable_unknown_device_returns_remediation():
    fake = SimpleNamespace(filters=AsyncMock(return_value=[]), reachable_nodes=AsyncMock())
    franc_mcp.test_client = fake
    try:
        async with Client(franc_mcp) as c:
            res = await c.call_tool("find_reachable", {"source": "nope", "target_kinds": ["X"]})
        payload = res.data
    finally:
        franc_mcp.test_client = None

    assert payload.status == "error"
    assert "not found" in payload.error
    fake.reachable_nodes.assert_not_called()


# ---------------------------------------------------------------------------
# get_schema(expand=True)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_schema_expand_inlines_peer_and_generic_used_by():
    from franc.utils import _SCHEMA_DICT_CACHE

    def attr(name):
        return SimpleNamespace(name=name, kind="Text", optional=False)

    peer = SimpleNamespace(
        kind="CapabilityGeneric",
        label="Capability",
        namespace="Cap",
        attributes=[attr("name")],
        relationships=[],
        used_by=["CapVm", "CapCluster"],
    )
    root = SimpleNamespace(
        model_dump=lambda: {
            "kind": "AppComponent",
            "relationships": [{"name": "capabilities", "peer": "CapabilityGeneric"}, {"name": "x", "peer": "Gone"}],
        }
    )

    async def get(kind, branch=None):
        from infrahub_sdk.exceptions import SchemaNotFoundError

        if kind == "AppComponent":
            return root
        if kind == "CapabilityGeneric":
            return peer
        raise SchemaNotFoundError(identifier=kind, message="missing")

    _SCHEMA_DICT_CACHE.clear()
    franc_mcp.test_client = SimpleNamespace(schema=SimpleNamespace(get=get))
    try:
        async with Client(franc_mcp) as c:
            plain = await c.call_tool("get_schema", {"kind": "AppComponent"})
            expanded = await c.call_tool("get_schema", {"kind": "AppComponent", "expand": True})
    finally:
        franc_mcp.test_client = None
        _SCHEMA_DICT_CACHE.clear()

    def unwrap(r):
        return r.data.data if hasattr(r.data, "data") else r.data["data"]

    assert "peer_schema" not in unwrap(plain)["relationships"][0]
    rels = unwrap(expanded)["relationships"]
    assert rels[0]["peer_schema"]["used_by"] == ["CapCluster", "CapVm"]
    assert rels[0]["peer_schema"]["attributes"] == [{"name": "name", "kind": "Text", "optional": False}]
    assert "peer_schema" not in rels[1]  # peer kind missing -> left as plain reference
