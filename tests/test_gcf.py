"""Tests for GCF (Graph Compact Format) compression tools."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp import Context

from franc.tools.gcf import gcf_analyze, gcf_decode, gcf_encode
from franc.utils import MCPToolStatus, decode_from_gcf, maybe_compress


@pytest.fixture
def mock_context():
    """Create a mock FastMCP Context for testing."""
    ctx = MagicMock(spec=Context)
    ctx.info = AsyncMock()
    ctx.debug = AsyncMock()
    ctx.error = AsyncMock()
    return ctx


@pytest.mark.asyncio
async def test_gcf_encode_simple_dict(mock_context: Context):
    """Test encoding a simple dictionary."""
    data = {"name": "device1", "ip": "10.0.0.1", "status": "active"}

    result = await gcf_encode(ctx=mock_context, data=data, show_stats=True)

    assert result.status == MCPToolStatus.SUCCESS
    assert result.data is not None
    assert "gcf_encoded" in result.data
    assert "statistics" in result.data
    assert result.data["statistics"]["savings_percent"] >= 0


@pytest.mark.asyncio
async def test_gcf_encode_list_of_dicts(mock_context: Context):
    """Test encoding a list of similar dictionaries (best compression)."""
    data = [{"name": f"device{i}", "ip": f"10.0.0.{i}", "status": "active"} for i in range(10)]

    result = await gcf_encode(ctx=mock_context, data=data, show_stats=True)

    assert result.status == MCPToolStatus.SUCCESS
    assert result.data is not None
    assert result.data["statistics"]["savings_percent"] > 20  # Should have good compression


@pytest.mark.asyncio
async def test_gcf_encode_without_stats(mock_context: Context):
    """Test encoding without statistics."""
    data = {"test": "data"}

    result = await gcf_encode(ctx=mock_context, data=data, show_stats=False)

    assert result.status == MCPToolStatus.SUCCESS
    assert result.data is not None
    assert "gcf_encoded" in result.data
    assert "statistics" not in result.data


@pytest.mark.asyncio
async def test_gcf_decode_simple(mock_context: Context):
    """Test decoding GCF-encoded data."""
    original_data = {"name": "test", "value": 42}

    encode_result = await gcf_encode(ctx=mock_context, data=original_data, show_stats=False)
    assert encode_result.data is not None
    gcf_string = encode_result.data["gcf_encoded"]

    decode_result = await gcf_decode(ctx=mock_context, gcf_string=gcf_string)

    assert decode_result.status == MCPToolStatus.SUCCESS
    assert decode_result.data is not None
    assert decode_result.data["decoded"] == original_data


@pytest.mark.asyncio
async def test_gcf_roundtrip_complex_data(mock_context: Context):
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

    encode_result = await gcf_encode(ctx=mock_context, data=original_data, show_stats=False)
    assert encode_result.data is not None

    decode_result = await gcf_decode(ctx=mock_context, gcf_string=encode_result.data["gcf_encoded"])

    assert decode_result.status == MCPToolStatus.SUCCESS
    assert decode_result.data is not None
    assert decode_result.data["decoded"] == original_data


@pytest.mark.asyncio
async def test_gcf_roundtrip_list_of_hop_lists(mock_context: Context):
    """Regression test: list[list[dict]] (e.g. find_physical_path's `paths`) must survive round-trip.

    This exact shape was silently mis-encoded/undecodable with the previous TOON-based encoder.
    """
    original_data = [
        [
            {"src_device": "a", "src_iface": "e1", "connector_type": "cable", "dst_iface": "e2", "dst_device": "b"},
            {"src_device": "b", "src_iface": "e3", "connector_type": "cable", "dst_iface": "e4", "dst_device": "c"},
        ],
        [
            {"src_device": "a", "src_iface": "e5", "connector_type": "cable", "dst_iface": "e6", "dst_device": "d"},
        ],
    ] * 3  # 6 paths — matches the >5 auto-compress threshold

    encode_result = await gcf_encode(ctx=mock_context, data=original_data, show_stats=False)
    assert encode_result.data is not None

    decode_result = await gcf_decode(ctx=mock_context, gcf_string=encode_result.data["gcf_encoded"])

    assert decode_result.status == MCPToolStatus.SUCCESS
    assert decode_result.data is not None
    assert decode_result.data["decoded"] == original_data


@pytest.mark.asyncio
async def test_gcf_analyze_small_data(mock_context: Context):
    """Test analyzing small data structure."""
    data = {"key": "value"}

    result = await gcf_analyze(ctx=mock_context, data=data)

    assert result.status == MCPToolStatus.SUCCESS
    assert result.data is not None
    assert "savings_percent" in result.data
    assert "recommendation" in result.data


@pytest.mark.asyncio
async def test_gcf_analyze_large_repetitive_data(mock_context: Context):
    """Test analyzing large repetitive data (should show good savings)."""
    data = [{"device_name": f"switch-{i}", "ip_address": f"192.168.1.{i}", "status": "operational"} for i in range(100)]

    result = await gcf_analyze(ctx=mock_context, data=data)

    assert result.status == MCPToolStatus.SUCCESS
    assert result.data is not None
    assert result.data["savings_percent"] > 30  # Should show significant savings
    assert (
        "strongly recommend" in result.data["recommendation"].lower()
        or "good candidate" in result.data["recommendation"].lower()
    )


@pytest.mark.asyncio
async def test_gcf_encode_primitives(mock_context: Context):
    """Test encoding various primitive types."""
    test_cases = [42, 3.14, "test string", True, None, [1, 2, 3], ["a", "b", "c"]]

    for data in test_cases:
        result = await gcf_encode(ctx=mock_context, data=data, show_stats=False)
        assert result.status == MCPToolStatus.SUCCESS
        assert result.data is not None

        decode_result = await gcf_decode(ctx=mock_context, gcf_string=result.data["gcf_encoded"])
        assert decode_result.data is not None
        assert decode_result.data["decoded"] == data


@pytest.mark.asyncio
async def test_gcf_decode_valid_gcf(mock_context: Context):
    """Test decoding a valid GCF string."""
    encode_result = await gcf_encode(ctx=mock_context, data={"name": "Alice", "age": 30}, show_stats=False)
    assert encode_result.data is not None

    result = await gcf_decode(ctx=mock_context, gcf_string=encode_result.data["gcf_encoded"])

    assert result.status == MCPToolStatus.SUCCESS
    assert result.data is not None
    assert result.data["decoded"] == {"name": "Alice", "age": 30}


@pytest.mark.asyncio
async def test_gcf_encode_realistic_infrahub_data(mock_context: Context):
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

    result = await gcf_encode(ctx=mock_context, data=data, show_stats=True)

    assert result.status == MCPToolStatus.SUCCESS
    assert result.data is not None
    assert result.data["statistics"]["savings_percent"] >= 0


@pytest.mark.asyncio
async def test_gcf_statistics_accuracy(mock_context: Context):
    """Test that statistics accurately reflect compression."""
    data = {"a" * 100: "b" * 100}

    result = await gcf_encode(ctx=mock_context, data=data, show_stats=True)

    assert result.status == MCPToolStatus.SUCCESS
    assert result.data is not None
    stats = result.data["statistics"]

    expected_savings = ((stats["json_tokens"] - stats["gcf_tokens"]) / stats["json_tokens"]) * 100
    assert abs(stats["savings_percent"] - expected_savings) < 0.1


@pytest.mark.asyncio
async def test_gcf_analyze_recommendation_low_savings(mock_context: Context):
    """Test recommendation for low savings data."""
    data = {"x": 1}

    result = await gcf_analyze(ctx=mock_context, data=data)

    assert result.status == MCPToolStatus.SUCCESS
    assert result.data is not None
    if result.data["savings_percent"] < 20:
        assert (
            "low savings" in result.data["recommendation"].lower()
            or "consider keeping" in result.data["recommendation"].lower()
        )


@pytest.mark.asyncio
async def test_gcf_encode_empty_structures(mock_context: Context):
    """Test encoding empty data structures."""
    test_cases: list = [{}, [], ""]

    for data in test_cases:
        result = await gcf_encode(ctx=mock_context, data=data, show_stats=False)
        assert result.status == MCPToolStatus.SUCCESS


@pytest.mark.asyncio
async def test_gcf_encode_mixed_types(mock_context: Context):
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

    result = await gcf_encode(ctx=mock_context, data=data, show_stats=False)

    assert result.status == MCPToolStatus.SUCCESS
    assert result.data is not None

    decode_result = await gcf_decode(ctx=mock_context, gcf_string=result.data["gcf_encoded"])
    assert decode_result.data is not None
    assert decode_result.data["decoded"] == data


@pytest.mark.asyncio
async def test_maybe_compress_skips_when_gcf_loses_to_json(mock_context: Context):
    """maybe_compress must not compress when GCF's token count isn't smaller than JSON's.

    A minimal payload just above the auto-compress threshold has too little redundancy for
    GCF's `GCF profile=generic\n## [N]: ...` header/format overhead to pay off — candidates
    over policy: measure both, only use GCF when it actually wins.
    """
    data = [True, False, True, False, True, False]

    result = await maybe_compress(mock_context, data, "devices", "devices_gcf")

    assert result is None


@pytest.mark.asyncio
async def test_maybe_compress_compresses_when_gcf_wins(mock_context: Context):
    """maybe_compress must compress when GCF genuinely reduces tokens vs JSON."""
    data = [
        {"name": f"leaf-{i:02d}", "role": "leaf", "platform": "cisco_ios", "status": "active"} for i in range(1, 13)
    ]

    result = await maybe_compress(mock_context, data, "devices", "devices_gcf")

    assert result is not None
    assert result.status == MCPToolStatus.SUCCESS
    assert result.data is not None
    assert "devices_gcf" in result.data
    assert result.data["count"] == len(data)
    assert decode_from_gcf(result.data["devices_gcf"]) == data


@pytest.mark.asyncio
async def test_maybe_compress_fail_open_on_bad_round_trip(mock_context: Context):
    """maybe_compress must skip compression (return None) rather than ship an undecodable payload.

    NaN survives json.dumps but breaks GCF's decode round-trip check.
    """
    data = [{"name": f"device-{i}", "value": float("nan")} for i in range(1, 13)]

    result = await maybe_compress(mock_context, data, "devices", "devices_gcf")

    assert result is None


def test_token_counting_falls_back_when_tiktoken_vocabulary_is_unavailable(monkeypatch):
    """No network for the tiktoken download must degrade to an estimate, not break compression."""
    from franc import utils

    def boom(_name):
        raise OSError("offline")

    utils._token_encoder.cache_clear()
    monkeypatch.setattr(utils.tiktoken, "get_encoding", boom)
    try:
        assert utils._count_tokens("abcdefgh") == 2  # ceil(8 / 4)
        assert utils._count_tokens("") == 0
        stats, _ = utils.estimate_token_savings([{"name": f"dev-{i}", "role": "leaf"} for i in range(8)])
        assert stats["json_tokens"] > 0
    finally:
        utils._token_encoder.cache_clear()
