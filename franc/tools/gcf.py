"""GCF (Graph Compact Format) encoding/decoding tools for token-efficient data transmission."""

from typing import Annotated, Any

from fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from franc.utils import (
    MCPResponse,
    MCPToolStatus,
    _log_and_return_error,
    decode_from_gcf,
    encode_with_gcf,
    estimate_token_savings,
)

mcp: FastMCP = FastMCP(name="GCF Compression")


@mcp.tool(tags={"gcf", "compression", "encoding"}, annotations=ToolAnnotations(readOnlyHint=True))
async def gcf_encode(
    ctx: Context,
    data: Annotated[Any, Field(description="Python data structure to encode with GCF (dict, list, primitives, etc.)")],
    show_stats: Annotated[bool, Field(default=True, description="Include encoding statistics showing token savings")],
) -> MCPResponse:
    """
    Encode data using the GCF (Graph Compact Format) generic profile for token-efficient transmission.

    GCF achieves large token reductions vs JSON for typical data structures
    while preserving full information (lossless round-trip) and remaining human-readable.

    Parameters:
        data: Any Python object (dict, list, str, int, etc.)
        show_stats: Include statistics about savings

    Returns:
        MCPResponse with GCF-encoded string and optional statistics
    """
    await ctx.info(f"Encoding data with GCF (type: {type(data).__name__})")

    try:
        gcf_encoded = encode_with_gcf(data)

        response_data: dict[str, Any] = {
            "gcf_encoded": gcf_encoded,
        }

        if show_stats:
            stats, _ = estimate_token_savings(data)
            response_data["statistics"] = stats
            await ctx.info(f"GCF encoding complete: {stats['savings_percent']}% token reduction")

        return MCPResponse(status=MCPToolStatus.SUCCESS, data=response_data)

    except Exception as e:  # noqa: BLE001
        return await _log_and_return_error(
            ctx, f"GCF encoding failed: {e}", remediation="Ensure data is JSON-serializable (dicts, lists, primitives)"
        )


@mcp.tool(tags={"gcf", "compression", "decoding"}, annotations=ToolAnnotations(readOnlyHint=True))
async def gcf_decode(
    ctx: Context,
    gcf_string: Annotated[str, Field(description="GCF-encoded string to decode back to Python objects")],
) -> MCPResponse:
    """
    Decode GCF-encoded data back to standard Python objects.

    Parameters:
        gcf_string: GCF-encoded string

    Returns:
        MCPResponse with decoded Python object
    """
    await ctx.info("Decoding GCF-encoded data")

    try:
        decoded_data = decode_from_gcf(gcf_string)

        await ctx.info(f"GCF decoding complete (type: {type(decoded_data).__name__})")

        return MCPResponse(status=MCPToolStatus.SUCCESS, data={"decoded": decoded_data})

    except Exception as e:  # noqa: BLE001
        return await _log_and_return_error(
            ctx, f"GCF decoding failed: {e}", remediation="Ensure input is a valid GCF-encoded string"
        )


@mcp.tool(tags={"gcf", "compression", "analysis"}, annotations=ToolAnnotations(readOnlyHint=True))
async def gcf_analyze(
    ctx: Context,
    data: Annotated[Any, Field(description="Python data structure to analyze for potential compression savings")],
) -> MCPResponse:
    """
    Analyze potential token savings from GCF encoding without actually encoding.

    Provides size comparison and recommendations:
    - <20% savings: Not worth encoding
    - 20-40% savings: Good candidate
    - >40% savings: Excellent candidate

    Best compression for nested data, lists of similar objects, repeated patterns, and numeric data.

    Parameters:
        data: Python object to analyze

    Returns:
        MCPResponse with size comparison and savings estimate
    """
    await ctx.info("Analyzing potential GCF compression savings")

    try:
        stats, _ = estimate_token_savings(data)

        savings_pct = stats["savings_percent"]
        if savings_pct < 20:
            recommendation = "Low savings - consider keeping original format"
        elif savings_pct < 40:
            recommendation = "Moderate savings - good candidate for GCF encoding"
        else:
            recommendation = "Excellent savings - strongly recommend GCF encoding"

        return MCPResponse(status=MCPToolStatus.SUCCESS, data={**stats, "recommendation": recommendation})

    except Exception as e:  # noqa: BLE001
        return await _log_and_return_error(
            ctx, f"GCF analysis failed: {e}", remediation="Ensure data is JSON-serializable"
        )
