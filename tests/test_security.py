"""Regression tests for input guards: Cypher read-only filter, SSH command/target validation, Neo4j credentials."""

from unittest.mock import MagicMock

import pytest
from fastmcp import Client

from franc.server import mcp as franc_mcp
from franc.ssh import ssh_host_allowed, validate_show_command
from franc.traversal import _neo4j_credentials, cypher_write_violation

# ---------------------------------------------------------------------------
# Cypher
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "query",
    [
        "CREATE (n:X)",
        "MERGE (n:X {name: 'a'})",
        "MATCH (n) DETACH DELETE n",
        "match (n) delete n",
        "MATCH (n) SET\nn.x = 1",  # newline after SET used to slip past a "SET " substring match
        "MATCH (n)\tSET n.x = 1",
        "MATCH (n) REMOVE n.x",
        "MATCH (n) /* harmless */ DELETE n",
        "MATCH (n) // it's a trap\nDELETE n",  # a quote inside a comment must not hide what follows
        "CALL apoc.cypher.doIt('CREATE (n)', {})",
        "CALL dbms.security.createUser('a', 'b', false)",
        "LOAD CSV FROM 'http://attacker.example/x.csv' AS row RETURN row",
        "FOREACH (x IN [1] | CREATE (:A))",
        "MATCH (n) RETURN n; MATCH (m) DELETE m",
        "GRANT ROLE admin TO bob",
    ],
)
def test_cypher_guard_blocks_writes_and_procedures(query):
    assert cypher_write_violation(query) is not None


@pytest.mark.parametrize(
    "query",
    [
        "MATCH (n:DcimDevice) RETURN n.name LIMIT 5",
        "MATCH (n)-[r]->(m) WHERE n.name = 'RESET SET DELETE' RETURN n, m",  # keywords inside a string value
        "MATCH (n) RETURN n // CREATE would be a write, but this is a comment",
        "MATCH (n:`SET`) RETURN count(n)",  # quoted identifier
        "MATCH (n) WITH n, n.created AS created_at RETURN created_at",  # 'created' is not CREATE
        "MATCH (a)-[:IS_RELATED]->(b) UNWIND [1,2] AS i RETURN a, b, i",
    ],
)
def test_cypher_guard_allows_plain_reads(query):
    assert cypher_write_violation(query) is None


def test_neo4j_credentials_come_only_from_environment(monkeypatch):
    monkeypatch.setenv("NEO4J_HTTP_URL", "http://neo4j.internal:7474")
    monkeypatch.setenv("NEO4J_USER", "reader")
    monkeypatch.setenv("NEO4J_PASSWORD", "s3cret")
    assert _neo4j_credentials() == ("http://neo4j.internal:7474", "reader", "s3cret")


@pytest.mark.asyncio
async def test_execute_cypher_has_no_url_override_and_never_calls_neo4j_when_blocked(monkeypatch):
    tools = {t.name: t for t in await franc_mcp.list_tools()}
    assert "neo4j_url" not in tools["execute_cypher"].parameters.get("properties", {})

    called = MagicMock()
    monkeypatch.setattr("franc.tools.graph._neo4j_run", called)
    async with Client(franc_mcp) as c:
        res = await c.call_tool("execute_cypher", {"query": "MATCH (n) SET\nn.x = 1"})
    payload = res.data
    assert payload.status == "error"
    assert "SET" in payload.error
    called.assert_not_called()


@pytest.mark.asyncio
async def test_execute_cypher_uses_server_configured_neo4j(monkeypatch):
    monkeypatch.setenv("NEO4J_HTTP_URL", "http://neo4j.internal:7474")
    seen: dict = {}

    def fake_run(query, url, user, password):
        seen.update(url=url, user=user)
        return [{"n": 1}]

    monkeypatch.setattr("franc.tools.graph._neo4j_run", fake_run)
    async with Client(franc_mcp) as c:
        res = await c.call_tool("execute_cypher", {"query": "MATCH (n) RETURN count(n) AS n"})
    assert res.data.status == "success"
    assert seen["url"] == "http://neo4j.internal:7474"


# ---------------------------------------------------------------------------
# SSH
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "show ip interface brief",
        "SHOW VERSION",
        "show run | include bgp",
        "show route | match 10.0 | count",
        "show interfaces | no-more",
        "show configuration | display json",
        "display bgp peer",
        "get system status",
        "ping 10.0.0.1",
        "traceroute 10.0.0.1",
    ],
)
def test_show_command_accepts_plain_reads(command):
    assert validate_show_command(command) is None


@pytest.mark.parametrize(
    "command",
    [
        "",
        "   ",
        "configure terminal",
        "reload",
        "show version; reload",
        "show version && reload",
        "show version || reload",
        "show version & reload",
        "show $(id)",
        "show `id`",
        "show version > /tmp/x",
        "show version < /etc/passwd",
        "show version\nreload",
        "show version\\ncat",
        "getent passwd",  # "get" must be a whole word
        "showmount -e localhost",
        "show version | sh",
        "show version | tee /tmp/x",
        "show run | include bgp | bash",
        "show run | include a; rm x",
    ],
)
def test_show_command_refuses_everything_else(command):
    assert validate_show_command(command) is not None


def test_ssh_host_allowed_defaults_to_private_ranges(monkeypatch):
    monkeypatch.delenv("SSH_ALLOWED_NETWORKS", raising=False)
    for ok in ("10.0.0.1", "172.20.20.5", "192.168.1.10", "127.0.0.1", "169.254.1.1", "fd00::1"):
        assert ssh_host_allowed(ok), ok
    for bad in ("8.8.8.8", "203.0.113.9", "evil.example.com", "", "10.0.0.1/24", "not an ip"):
        assert not ssh_host_allowed(bad), bad


def test_ssh_allowed_networks_override_replaces_default(monkeypatch):
    monkeypatch.setenv("SSH_ALLOWED_NETWORKS", "203.0.113.0/24, 198.51.100.0/24")
    assert ssh_host_allowed("203.0.113.9")
    assert ssh_host_allowed("198.51.100.1")
    assert not ssh_host_allowed("10.0.0.1")  # override means override


def test_ssh_allowed_networks_malformed_fails_closed(monkeypatch):
    monkeypatch.setenv("SSH_ALLOWED_NETWORKS", "10.0.0.0/8, nonsense")
    assert not ssh_host_allowed("10.0.0.1")
