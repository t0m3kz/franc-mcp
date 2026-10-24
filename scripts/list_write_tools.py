"""List the Franc tools that FRANC_READ_ONLY=true hides (tagged write/create)."""

import asyncio

from franc.mcp_middleware import WRITE_TAGS
from franc.server import mcp


async def main() -> None:
    tools = await mcp.list_tools()
    hidden = sorted(t.name for t in tools if WRITE_TAGS & set(t.tags or ()))
    print(f"{len(hidden)} of {len(tools)} tools hidden in read-only mode:")
    for name in hidden:
        print(f"  {name}")


if __name__ == "__main__":
    asyncio.run(main())
