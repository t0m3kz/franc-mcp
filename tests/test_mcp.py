import pytest
from fastmcp import Client

from franc.server import mcp


def _unwrap(result):
    """
    Compatibility helper: fastmcp CallToolResult.data may be a dataclass Root(status, data, error, remediation)
    introduced when tools return structured MCPResponse objects. Tests originally expected the raw inner data.
    """
    data = result.data
    if hasattr(data, "data") and hasattr(data, "status"):
        return data.data
    return data


@pytest.mark.asyncio
async def test_list_schema(client, add_mock_response, httpx_mock):
    mcp.test_client = client
    add_mock_response(
        httpx_mock,
        mockname="schemas.json",
        method="GET",
        url="http://localhost:8000/api/schema?branch=main",
        is_reusable=True,
    )
    try:
        async with Client(mcp) as test_client:
            response = await test_client.call_tool("get_schema_mapping")
            data = _unwrap(response)
            assert isinstance(data, dict)
            assert data.get("IpamPrefix") == "Prefix"
    finally:
        mcp.test_client = None


@pytest.mark.asyncio
async def test_get_node_filters(client, add_mock_response, httpx_mock):
    mcp.test_client = client
    add_mock_response(
        httpx_mock,
        mockname="schemas.json",
        method="GET",
        url="http://localhost:8000/api/schema?branch=main",
        is_reusable=True,
    )
    try:
        async with Client(mcp) as test_client:
            response = await test_client.call_tool("get_node_filters", {"kind": "DcimPhysicalDevice"})
            data = _unwrap(response)
            assert isinstance(data, dict)
            expected = {
                "position__value": "String",
                "position__values": "List[String]",
                "serial__value": "String",
                "serial__values": "List[String]",
                "rack_face__value": "String",
                "rack_face__values": "List[String]",
                "status__value": "String",
                "status__values": "List[String]",
                "role__value": "String",
                "role__values": "List[String]",
                "os_version__value": "String",
                "os_version__values": "List[String]",
                "description__value": "String",
                "description__values": "List[String]",
                "name__value": "String",
                "name__values": "List[String]",
                "interfaces__name__value": "String",
                "interfaces__name__values": "List[String]",
                "interfaces__description__value": "String",
                "interfaces__description__values": "List[String]",
                "interfaces__status__value": "String",
                "interfaces__status__values": "List[String]",
                "interfaces__role__value": "String",
                "interfaces__role__values": "List[String]",
            }
            for key, value in expected.items():
                assert data.get(key) == value
    finally:
        mcp.test_client = None


@pytest.mark.asyncio
async def test_get_nodes_compressed(client, add_mock_response, httpx_mock):
    mcp.test_client = client
    add_mock_response(
        httpx_mock,
        mockname="schemas.json",
        method="GET",
        url="http://localhost:8000/api/schema?branch=main",
        is_reusable=True,
    )
    add_mock_response(
        httpx_mock,
        mockname="physical_devices.json",
        method="POST",
        url="http://localhost:8000/graphql/main",
        is_reusable=True,
    )
    try:
        async with Client(mcp) as client_session:
            devices = await client_session.call_tool("get_nodes", {"kind": "DcimPhysicalDevice"})
            data = _unwrap(devices)

            # With >10 items, result is auto-compressed
            assert isinstance(data, dict)
            assert "nodes_gcf" in data
            assert data["count"] == 12

            # Decode to verify content
            from franc.utils import decode_from_gcf

            decoded = decode_from_gcf(data["nodes_gcf"])
            assert isinstance(decoded, list)
            assert "ktw-1-oob-02" in decoded
            assert len(decoded) == 12
    finally:
        mcp.test_client = None


@pytest.mark.asyncio
async def test_get_object(client, add_mock_response, httpx_mock):
    mcp.test_client = client
    add_mock_response(
        httpx_mock,
        mockname="schemas.json",
        method="GET",
        url="http://localhost:8000/api/schema?branch=main",
        is_reusable=True,
    )
    add_mock_response(
        httpx_mock,
        mockname="device.json",
        method="POST",
        url="http://localhost:8000/graphql/main",
        is_reusable=True,  # Allow multiple POST requests for retry logic
    )
    try:
        async with Client(mcp) as client_session:
            # Use get_nodes - supports GraphQL filters, returns display labels
            devices = await client_session.call_tool(
                "get_nodes",
                {
                    "kind": "DcimPhysicalDevice",
                    "filters": {"name__value": "ktw-1-leaf-01"},
                    "branch": "main",
                },
            )
            data = _unwrap(devices)
            assert isinstance(data, dict)
            assert data["count"] >= 1
            assert data["has_more"] is False
            assert data["offset"] == 0

            # get_nodes returns display labels (strings)
            device_label = data["nodes"][0]
            assert isinstance(device_label, str)
            assert "ktw" in device_label.lower() or "leaf" in device_label.lower()
    finally:
        mcp.test_client = None


@pytest.mark.asyncio
async def test_get_nodes_pagination_has_more(client, add_mock_response, httpx_mock):
    """A full page triggers a count query, so has_more reflects the true total."""
    mcp.test_client = client
    add_mock_response(
        httpx_mock,
        mockname="schemas.json",
        method="GET",
        url="http://localhost:8000/api/schema?branch=main",
        is_reusable=True,
    )
    add_mock_response(
        httpx_mock,
        mockname="physical_devices.json",
        method="POST",
        url="http://localhost:8000/graphql/main",
        is_reusable=True,
    )
    try:
        async with Client(mcp) as client_session:
            result = await client_session.call_tool("get_nodes", {"kind": "DcimPhysicalDevice", "limit": 12})
            data = _unwrap(result)
            assert data["count"] == 12
            assert data["limit"] == 12
            # The mock returns count=12 for every query, so the page is exactly the total.
            assert data["total_count"] == 12
            assert data["has_more"] is False
    finally:
        mcp.test_client = None


@pytest.mark.asyncio
async def test_search_nodes(client, add_mock_response, httpx_mock):
    mcp.test_client = client
    add_mock_response(
        httpx_mock,
        mockname="schemas.json",
        method="GET",
        url="http://localhost:8000/api/schema?branch=main",
        is_reusable=True,
    )
    add_mock_response(
        httpx_mock,
        mockname="device.json",
        method="POST",
        url="http://localhost:8000/graphql/main",
        is_reusable=True,
    )
    try:
        async with Client(mcp) as client_session:
            result = await client_session.call_tool("search_nodes", {"query": "leaf", "kind": "DcimPhysicalDevice"})
            data = _unwrap(result)
            assert data["limit"] == 10
            assert data["count"] >= 1
            assert all(isinstance(n, str) for n in data["nodes"])
    finally:
        mcp.test_client = None
