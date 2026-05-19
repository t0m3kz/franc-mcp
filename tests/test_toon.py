"""Tests for toon compression tools."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp import Context

from franc.tools.toon import toon_analyze, toon_decode, toon_encode
from franc.utils import MCPToolStatus


@pytest.fixture
def mock_context():
    """Create a mock FastMCP Context for testing."""
    ctx = MagicMock(spec=Context)
    ctx.info = AsyncMock()
    ctx.debug = AsyncMock()
    ctx.error = AsyncMock()
    return ctx


@pytest.mark.asyncio
async def test_toon_encode_simple_dict(mock_context: Context):
    """Test encoding a simple dictionary."""
    data = {"name": "device1", "ip": "10.0.0.1", "status": "active"}

    result = await toon_encode(ctx=mock_context, data=data, show_stats=True)

    assert result.status == MCPToolStatus.SUCCESS
    assert result.data is not None
    assert "toon_encoded" in result.data
    assert "statistics" in result.data
    assert result.data["statistics"]["savings_percent"] >= 0


@pytest.mark.asyncio
async def test_toon_encode_list_of_dicts(mock_context: Context):
    """Test encoding a list of similar dictionaries (best compression)."""
    data = [{"name": f"device{i}", "ip": f"10.0.0.{i}", "status": "active"} for i in range(10)]

    result = await toon_encode(ctx=mock_context, data=data, show_stats=True)

    assert result.status == MCPToolStatus.SUCCESS
    assert result.data is not None
    assert result.data["statistics"]["savings_percent"] > 20  # Should have good compression


@pytest.mark.asyncio
async def test_toon_encode_without_stats(mock_context: Context):
    """Test encoding without statistics."""
    data = {"test": "data"}

    result = await toon_encode(ctx=mock_context, data=data, show_stats=False)

    assert result.status == MCPToolStatus.SUCCESS
    assert result.data is not None
    assert "toon_encoded" in result.data
    assert "statistics" not in result.data


@pytest.mark.asyncio
async def test_toon_decode_simple(mock_context: Context):
    """Test decoding toon-encoded data."""
    original_data = {"name": "test", "value": 42}

    encode_result = await toon_encode(ctx=mock_context, data=original_data, show_stats=False)
    assert encode_result.data is not None
    toon_string = encode_result.data["toon_encoded"]

    decode_result = await toon_decode(ctx=mock_context, toon_string=toon_string)

    assert decode_result.status == MCPToolStatus.SUCCESS
    assert decode_result.data is not None
    assert decode_result.data["decoded"] == original_data


@pytest.mark.asyncio
async def test_toon_roundtrip_complex_data(mock_context: Context):
    """Test encode/decode roundtrip with complex nested data."""
    original_data = {
        "datacenters": [
            {
                "name": "DC1",
                "location": "New York",
                "racks": [
                    {"id": 1, "devices": ["dev1", "dev2"]},
                    {"id": 2, "devices": ["dev3", "dev4", "dev5"]},
                ],
            },
            {
                "name": "DC2",
                "location": "London",
                "racks": [
                    {"id": 3, "devices": ["dev6"]},
                ],
            },
        ],
        "total_devices": 6,
        "active": True,
        "threshold": 0.95,
    }

    encode_result = await toon_encode(ctx=mock_context, data=original_data, show_stats=False)
    assert encode_result.data is not None

    decode_result = await toon_decode(ctx=mock_context, toon_string=encode_result.data["toon_encoded"])

    assert decode_result.status == MCPToolStatus.SUCCESS
    assert decode_result.data is not None
    assert decode_result.data["decoded"] == original_data


@pytest.mark.asyncio
async def test_toon_analyze_small_data(mock_context: Context):
    """Test analyzing small data structure."""
    data = {"key": "value"}

    result = await toon_analyze(ctx=mock_context, data=data)

    assert result.status == MCPToolStatus.SUCCESS
    assert result.data is not None
    assert "savings_percent" in result.data
    assert "recommendation" in result.data


@pytest.mark.asyncio
async def test_toon_analyze_large_repetitive_data(mock_context: Context):
    """Test analyzing large repetitive data (should show good savings)."""
    data = [{"device_name": f"switch-{i}", "ip_address": f"192.168.1.{i}", "status": "operational"} for i in range(100)]

    result = await toon_analyze(ctx=mock_context, data=data)

    assert result.status == MCPToolStatus.SUCCESS
    assert result.data is not None
    assert result.data["savings_percent"] > 30  # Should show significant savings
    assert (
        "strongly recommend" in result.data["recommendation"].lower()
        or "good candidate" in result.data["recommendation"].lower()
    )


@pytest.mark.asyncio
async def test_toon_encode_primitives(mock_context: Context):
    """Test encoding various primitive types."""
    test_cases = [42, 3.14, "test string", True, None, [1, 2, 3], ["a", "b", "c"]]

    for data in test_cases:
        result = await toon_encode(ctx=mock_context, data=data, show_stats=False)
        assert result.status == MCPToolStatus.SUCCESS
        assert result.data is not None

        decode_result = await toon_decode(ctx=mock_context, toon_string=result.data["toon_encoded"])
        assert decode_result.data is not None
        assert decode_result.data["decoded"] == data


@pytest.mark.asyncio
async def test_toon_decode_valid_toon(mock_context: Context):
    """Test decoding a valid TOON string."""
    result = await toon_decode(ctx=mock_context, toon_string="name: Alice\nage: 30")

    assert result.status == MCPToolStatus.SUCCESS
    assert result.data is not None
    assert result.data["decoded"] == {"name": "Alice", "age": 30}


@pytest.mark.asyncio
async def test_toon_encode_realistic_infrahub_data(mock_context: Context):
    """Test encoding data structure similar to Infrahub query results."""
    data = {
        "nodes": [
            {
                "id": f"device-{i}",
                "display_label": f"switch-{i}",
                "name": {"value": f"switch-{i}"},
                "description": {"value": f"Core switch in rack {i % 10}"},
                "status": {"value": "active"},
                "ip_address": {"value": f"10.0.{i // 256}.{i % 256}"},
                "serial_number": {"value": f"SN{i:06d}"},
                "rack": {"node": {"id": f"rack-{i % 10}", "display_label": f"Rack-{i % 10}"}},
            }
            for i in range(50)
        ],
        "count": 50,
    }

    result = await toon_encode(ctx=mock_context, data=data, show_stats=True)

    assert result.status == MCPToolStatus.SUCCESS
    assert result.data is not None
    assert result.data["statistics"]["savings_percent"] >= 0


@pytest.mark.asyncio
async def test_toon_statistics_accuracy(mock_context: Context):
    """Test that statistics accurately reflect compression."""
    data = {"a" * 100: "b" * 100}

    result = await toon_encode(ctx=mock_context, data=data, show_stats=True)

    assert result.status == MCPToolStatus.SUCCESS
    assert result.data is not None
    stats = result.data["statistics"]

    expected_savings = ((stats["json_tokens"] - stats["toon_tokens"]) / stats["json_tokens"]) * 100
    assert abs(stats["savings_percent"] - expected_savings) < 0.1
    assert stats["json_tokens"] - stats["toon_tokens"] > 0


@pytest.mark.asyncio
async def test_toon_analyze_recommendation_low_savings(mock_context: Context):
    """Test recommendation for low savings data."""
    data = {"x": 1}

    result = await toon_analyze(ctx=mock_context, data=data)

    assert result.status == MCPToolStatus.SUCCESS
    assert result.data is not None
    if result.data["savings_percent"] < 20:
        assert (
            "low savings" in result.data["recommendation"].lower()
            or "consider keeping" in result.data["recommendation"].lower()
        )


@pytest.mark.asyncio
async def test_toon_encode_empty_structures(mock_context: Context):
    """Test encoding empty data structures."""
    test_cases: list = [{}, [], ""]

    for data in test_cases:
        result = await toon_encode(ctx=mock_context, data=data, show_stats=False)
        assert result.status == MCPToolStatus.SUCCESS


@pytest.mark.asyncio
async def test_toon_encode_mixed_types(mock_context: Context):
    """Test encoding mixed nested types."""
    data = {
        "string": "text",
        "number": 42,
        "float": 3.14159,
        "bool": True,
        "null": None,
        "list": [1, "two", 3.0, False],
        "nested": {"deep": {"value": [1, 2, 3]}},
    }

    result = await toon_encode(ctx=mock_context, data=data, show_stats=False)

    assert result.status == MCPToolStatus.SUCCESS
    assert result.data is not None

    decode_result = await toon_decode(ctx=mock_context, toon_string=result.data["toon_encoded"])
    assert decode_result.data is not None
    assert decode_result.data["decoded"] == data
