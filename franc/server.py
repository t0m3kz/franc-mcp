import logging
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator

from fastmcp import FastMCP

# from franc.tools.gql import mcp as graphql_mcp
# from franc.tools.nodes import mcp as nodes_mcp
# from franc.tools.schema import mcp as schema_mcp
from infrahub_sdk import InfrahubClient

from franc.tools.branch import mcp as branch_mcp
from franc.tools.datacenter import mcp as datacenter_mcp
from franc.tools.nodes import mcp as nodes_mcp
from franc.tools.schema import mcp as schema_mcp
from franc.tools.toon import mcp as toon_mcp

logger = logging.getLogger("franc.server")


class FrancFastMCP(FastMCP):
    """
    FastMCP subclass declaring a 'test_client' attribute for unit test injection.
    Tests can assign FakeClient to this attribute to bypass real Infrahub connectivity.
    """

    test_client: Any | None = None


@dataclass
class ApplicationContext:
    client: InfrahubClient


@asynccontextmanager
async def app_lifespan(server: FastMCP) -> AsyncIterator[ApplicationContext]:
    """
    Lifespan context manager that provides an authenticated InfrahubClient.

    Priority:
      1. Use injected test_client (for unit tests)
      2. Create client from environment:
         - INFRAHUB_ADDRESS (default http://localhost:8000)
         - INFRAHUB_API_TOKEN (optional, but required for branch create)
    """
    client = getattr(server, "test_client", None)
    # Consume injected test client only once to avoid cross-test leakage.
    if client is not None:
        setattr(server, "test_client", None)

    if client is None:
        address = os.getenv("INFRAHUB_ADDRESS", "http://localhost:8000")
        token = os.getenv("INFRAHUB_API_TOKEN")

        if not token:
            # Branch creation and other privileged ops will likely fail without a token.
            logger.warning(
                "INFRAHUB_API_TOKEN not set; proceeding without authentication. "
                "Branch creation may result in authorization errors."
            )
            client = InfrahubClient(address=address)
        else:
            # The infrahub_sdk reads INFRAHUB_API_TOKEN from the environment; constructor has no 'token' param.
            # Ensure the variable is present (it already is if we got here) and instantiate the client.
            logger.info("Authenticated InfrahubClient initialization using INFRAHUB_API_TOKEN (masked).")
            client = InfrahubClient(address=address)

    try:
        yield ApplicationContext(client=client)
    finally:
        pass


mcp = FrancFastMCP("Franc MCP", lifespan=app_lifespan)

mcp.mount(branch_mcp)
mcp.mount(nodes_mcp)
mcp.mount(schema_mcp)
mcp.mount(toon_mcp)
mcp.mount(datacenter_mcp)
