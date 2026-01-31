"""Toon encoding/decoding tools for token-efficient data transmission."""

from typing import Annotated, Any

from fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from franc.utils import (
    MCPResponse,
    MCPToolStatus,
    _log_and_return_error,
    decode_from_toon,
    encode_with_toon,
    estimate_token_savings,
)

mcp: FastMCP = FastMCP(name="Toon Compression")


@mcp.tool(tags={"toon", "compression", "encoding"}, annotations=ToolAnnotations(readOnlyHint=True))
async def toon_encode(
    ctx: Context,
    data: Annotated[Any, Field(description="Python data structure to encode with toon (dict, list, primitives, etc.)")],
    show_stats: Annotated[bool, Field(default=True, description="Include encoding statistics showing token savings")],
) -> MCPResponse:
    """
    Encode data using the toon protocol for token-efficient transmission.

    Toon format achieves 30-60% fewer tokens than JSON for typical data structures
    while preserving full information and maintaining readability.

    Parameters:
        data: Any Python object (dict, list, str, int, etc.)
        show_stats: Include statistics about savings

    Returns:
        MCPResponse with toon-encoded string and optional statistics
    """
    await ctx.info(f"Encoding data with toon protocol (type: {type(data).__name__})")

    try:
        toon_encoded = encode_with_toon(data)

        response_data: dict[str, Any] = {
            "toon_encoded": toon_encoded,
        }

        if show_stats:
            stats = estimate_token_savings(data)
            response_data["statistics"] = stats
            chars_saved = stats["json_length"] - stats["toon_length"]
            await ctx.info(f"Toon encoding complete: {stats['savings_percent']}% reduction ({chars_saved} chars saved)")

        return MCPResponse(status=MCPToolStatus.SUCCESS, data=response_data)

    except Exception as e:
        return await _log_and_return_error(
            ctx, f"Toon encoding failed: {e}", remediation="Ensure data is JSON-serializable (dicts, lists, primitives)"
        )


@mcp.tool(tags={"toon", "compression", "decoding"}, annotations=ToolAnnotations(readOnlyHint=True))
async def toon_decode(
    ctx: Context,
    toon_string: Annotated[str, Field(description="Toon-encoded string to decode back to Python objects")],
) -> MCPResponse:
    """
    Decode toon-encoded data back to standard Python objects.

    Parameters:
        toon_string: Toon-encoded string

    Returns:
        MCPResponse with decoded Python object
    """
    await ctx.info("Decoding toon-encoded data")

    try:
        decoded_data = decode_from_toon(toon_string)

        await ctx.info(f"Toon decoding complete (type: {type(decoded_data).__name__})")

        return MCPResponse(status=MCPToolStatus.SUCCESS, data={"decoded": decoded_data})

    except Exception as e:
        return await _log_and_return_error(
            ctx, f"Toon decoding failed: {e}", remediation="Ensure input is a valid toon-encoded string"
        )


@mcp.tool(tags={"toon", "compression", "analysis"}, annotations=ToolAnnotations(readOnlyHint=True))
async def toon_analyze(
    ctx: Context,
    data: Annotated[Any, Field(description="Python data structure to analyze for potential compression savings")],
) -> MCPResponse:
    """
    Analyze potential token savings from toon encoding without actually encoding.

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
    await ctx.info("Analyzing potential toon compression savings")

    try:
        stats = estimate_token_savings(data)

        # Add recommendation
        savings_pct = stats["savings_percent"]
        if savings_pct < 20:
            recommendation = "Low savings - consider keeping original format"
        elif savings_pct < 40:
            recommendation = "Moderate savings - good candidate for toon encoding"
        else:
            recommendation = "Excellent savings - strongly recommend toon encoding"

        stats["recommendation"] = recommendation

        await ctx.info(f"Analysis complete: {savings_pct}% potential reduction - {recommendation}")

        return MCPResponse(status=MCPToolStatus.SUCCESS, data=stats)

    except Exception as e:
        return await _log_and_return_error(
            ctx, f"Toon analysis failed: {e}", remediation="Ensure data is JSON-serializable"
        )
