import pytest
from fastmcp import Client

from franc.server import mcp


class FakeBranch:
    async def create(
        self,
        branch_name: str,
        sync_with_git: bool = False,
        background_execution: bool = False,
        **_: object,
    ):
        class _BranchObj:
            name = branch_name

        return _BranchObj()


class FakeNode:
    def __init__(self, kind: str):
        self.id = "fake-node-id"
        self.kind = kind


class FakeClient:
    def __init__(self):
        self.branch = FakeBranch()

    async def create(self, kind: str, data: dict, branch: str):
        # Simulate successful creation returning a node with an id
        return FakeNode(kind=kind)


@pytest.mark.asyncio
async def test_create_datacenter_deployment():
    # Inject fake client into MCP server lifespan
    mcp.test_client = FakeClient()

    params = {
        "site_name": "DC-BER-1",
        "metro_location": "BERLIN",
        "design": "M-Standard",
        "strategy": "ebgp-evpn",
            "provider": "Internal",
    }

    try:
        async with Client(mcp) as test_client:
            response = await test_client.call_tool("create_datacenter_deployment", params)

        # FastMCP may wrap structured output in a Root/MCPResponse model.
        raw = response.data
        if hasattr(raw, "data") and isinstance(getattr(raw, "data"), dict):
            data = raw.data  # Unwrap MCPResponse
        else:
            data = raw  # Already a dict

        assert isinstance(data, dict)
        assert "branch" in data
        assert data.get("status") == "created"
        topo = data.get("topology", {})
        assert topo.get("name") == "DC-BER-1"
        assert topo.get("location") == "BERLIN"
            assert "prefix_nodes" not in data
    finally:
        # Ensure subsequent tests use a real InfrahubClient instead of FakeClient
        mcp.test_client = None
