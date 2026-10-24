"""
Shared entry point for scripts/ CLIs: a real fastmcp Client wired to the actual server.

Every script under scripts/ exercises franc through the same path a real MCP client
would use — fastmcp.Client(mcp) — instead of hand-rolling a fake Context. This means
scripts get the real lifespan (InfrahubClient from INFRAHUB_ADDRESS/INFRAHUB_API_TOKEN,
same defaults documented in franc/server.py), real ctx.info/warning/error delivery, and
call tools by name/kwargs exactly like tests/test_mcp.py and any other MCP client would.

Usage:
    from scripts._common import call_tool, print_error_and_exit

    async def main():
        result = await call_tool("get_office_summary", branch="main")
        ...
"""

from __future__ import annotations

import sys
from typing import Any

from fastmcp import Client

from franc.server import mcp
from franc.utils import decode_from_gcf


async def call_tool(name: str, **arguments: Any) -> Any:
    """Call an MCP tool by name through a real fastmcp Client and return its MCPResponse data."""
    async with Client(mcp) as client:
        result = await client.call_tool(name, arguments)
    return result.data


def print_error_and_exit(result: Any) -> None:
    """Print an MCPResponse's error/remediation and exit(1). Call when result.data is None."""
    print(f"ERROR: {result.error}")
    if result.remediation:
        print(f"HINT:  {result.remediation}")
    sys.exit(1)


def decode_gcf_field(data: Any, list_key: str, gcf_key: str) -> list[dict]:
    """Return the list a tool call produced, decoding it if it arrived GCF-compressed.

    maybe_compress (franc/utils.py) only compresses when GCF wins on tokens, so a tool's
    result may be: a bare list, {list_key: [...], "count": N}, or {gcf_key: "<gcf>", "count": N}.
    """
    if isinstance(data, list):
        return data
    if list_key in data:
        return data[list_key]
    return decode_from_gcf(data[gcf_key])
