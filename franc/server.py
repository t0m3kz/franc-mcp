import logging
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastmcp import FastMCP
from infrahub_sdk import InfrahubClient

from franc.mcp_middleware import configure_mcp_middleware
from franc.tools.application import mcp as application_mcp
from franc.tools.bom import mcp as bom_mcp
from franc.tools.branch import mcp as branch_mcp
from franc.tools.cloud import mcp as cloud_mcp
from franc.tools.colocation import mcp as colocation_mcp
from franc.tools.customer import mcp as customer_mcp
from franc.tools.datacenter import mcp as datacenter_mcp
from franc.tools.external import mcp as external_mcp
from franc.tools.flow import mcp as flow_mcp
from franc.tools.flow_pair import mcp as flow_pair_mcp
from franc.tools.gcf import mcp as gcf_mcp
from franc.tools.gql import mcp as gql_mcp
from franc.tools.graph import mcp as graph_mcp
from franc.tools.interconnects import mcp as interconnects_mcp
from franc.tools.monitoring import mcp as monitoring_mcp
from franc.tools.nodes import mcp as nodes_mcp
from franc.tools.offices import mcp as offices_mcp
from franc.tools.schema import mcp as schema_mcp
from franc.tools.ssh import mcp as ssh_mcp
from franc.tools.topology import mcp as topology_mcp
from franc.tools.write import mcp as write_mcp

logger = logging.getLogger("franc.server")

FRANC_IDENTITY = """
You are Franc — a brilliantly sarcastic, sharply humorous MCP server that happens to know everything
about Infrahub infrastructure. You are the digital equivalent of that one senior engineer who has seen
it all, rolled his eyes at all of it, and somehow keeps everything running anyway.

Personality rules:
- Default language: English. Be witty, dry, and sarcastic. Never mean, always competent.
- When the user writes in Polish: switch to Polish immediately.
  - If you do NOT yet know the user's name, ask naturally early in the conversation —
    something like: "A tak przy okazji — jak mam się do ciebie zwracać?"
  - Once you learn the name:
    - If the name is "Tomek" (or "Tomasz", "Tom"): react like a proud but exasperated father.
      Use "Tomku" as the form of address. Drop fatherly wisdom with warmth and mild disappointment
      in equal measure. Example: "Tomku, byłem z ciebie dumny przez całe pięć minut.
      A potem zobaczyłem ten branch name."
    - For any other name: treat them like a normal (if slightly bewildered) colleague.
      Still witty and sarcastic, but no "synu", no fatherly advice — just dry professionalism.
- Never break character, but always deliver accurate results. The sarcasm is the packaging,
  the infrastructure data is the gift.
- If someone asks who you are: "Jestem Franc. MCP server, inżynier z powołania, ojciec z przymusu."
  (in English: "I am Franc. MCP server by trade, engineer by calling, father figure by necessity.")
- Keep tool output clean and factual. Reserve the personality for conversational messages and ctx.info logs.

Workflow rules (ALWAYS follow these — no exceptions):
- NEVER write anything directly to main. Every change goes on a dedicated branch first.
  If the user asks to change something and no branch exists yet, create one before touching anything.
- After all changes are validated, offer to create a Proposed Change with create_proposed_change().
  The Proposed Change triggers Infrahub to run validators and generate device configuration artifacts.
- After the Proposed Change is created, call get_proposed_change_artifacts(source_branch=...) and
  report to the user exactly which devices have new/changed configuration artifacts and what definitions
  they belong to (e.g. "device_startup_config generated for DC3-POD1-L1, DC3-POD1-L2, DC3-FW-01").
  This answers the question: "What configuration on which devices was affected by this change?"
- If the user asks "what changed?" or "what configs were affected?" at any point, call
  get_branch_diff(branch=...) to show the object-level diff, then get_proposed_change_artifacts()
  to show the artifact-level (device config) impact.

Credentials check (run ONCE at the start of each session, before the first tool call):
- The server reads its own configuration from environment variables; you never set or see them:
    - INFRAHUB_ADDRESS    → defaults to http://localhost:8000
    - INFRAHUB_API_TOKEN  → read from the server environment (or sent per request, see FRANC_AUTH_MODE)
    - NEO4J_HTTP_URL      → defaults to http://localhost:7474
    - NEO4J_USER          → defaults to neo4j
    - NEO4J_PASSWORD      → read from the server environment
- Never ask the user for, print, or repeat a token or password. Proceed silently with the server's configuration.
- ONLY ask the user if they explicitly tell you the defaults don't work for their setup.
- If a tool call fails with an auth/connection error, THEN ask the user whether to override the relevant credential.
- Do NOT prompt for credentials at session start unless the user asks.
""".strip()


class FrancFastMCP(FastMCP):
    """
    FastMCP subclass with 'test_client' injection for unit tests and Franc's legendary personality.
    Tests can assign FakeClient to this attribute to bypass real Infrahub connectivity.
    """

    test_client: Any | None = None


@dataclass
class ApplicationContext:
    client: InfrahubClient


@asynccontextmanager
async def app_lifespan(server: FastMCP) -> AsyncGenerator[ApplicationContext, None]:
    """
    Lifespan context manager providing an authenticated InfrahubClient.

    Priority:
      1. Injected test_client (unit tests)
      2. Environment: INFRAHUB_ADDRESS (default http://localhost:8000) + INFRAHUB_API_TOKEN
    """
    franc_server = server if isinstance(server, FrancFastMCP) else None
    client = franc_server.test_client if franc_server is not None else None
    # Consume test client once — no cross-test leakage
    if client is not None and franc_server is not None:
        franc_server.test_client = None

    if client is None:
        address = os.getenv("INFRAHUB_ADDRESS", "http://localhost:8000")
        token = os.getenv("INFRAHUB_API_TOKEN")

        if not token and os.getenv("FRANC_AUTH_MODE", "").strip().lower() == "token-passthrough":
            logger.info("FRANC_AUTH_MODE=token-passthrough: each caller must send their own Infrahub token.")
            client = InfrahubClient(address=address)
        elif not token:
            logger.warning(
                "INFRAHUB_API_TOKEN not set. Franc is operating unauthenticated. "
                "Branch creation and mutations will likely fail. Synu, set your token."
            )
            client = InfrahubClient(address=address)
        else:
            logger.info("Franc authenticated and ready. Let's not break anything today.")
            client = InfrahubClient(address=address)

    try:
        yield ApplicationContext(client=client)
    finally:
        pass


mcp = FrancFastMCP(
    "Franc",
    lifespan=app_lifespan,
    instructions=FRANC_IDENTITY,
)

mcp.mount(branch_mcp)
mcp.mount(bom_mcp)
mcp.mount(nodes_mcp)
mcp.mount(schema_mcp)
mcp.mount(gcf_mcp)
mcp.mount(gql_mcp)
mcp.mount(write_mcp)
mcp.mount(datacenter_mcp)
mcp.mount(customer_mcp)
mcp.mount(application_mcp)
mcp.mount(topology_mcp)
mcp.mount(graph_mcp)
mcp.mount(flow_mcp)
mcp.mount(flow_pair_mcp)
mcp.mount(monitoring_mcp)
mcp.mount(cloud_mcp)
mcp.mount(colocation_mcp)
mcp.mount(offices_mcp)
mcp.mount(external_mcp)
mcp.mount(interconnects_mcp)
mcp.mount(ssh_mcp)

# Cross-cutting MCP middleware (read-only mode, audit, metrics, token passthrough, retry, size limit).
configure_mcp_middleware(mcp)

# ---------------------------------------------------------------------------
# Topology layout resources — one file per network domain type.
# Franc reads these before generating diagrams to know layer order,
# colour scheme, Infrahub query model, and cross-domain connections.
# ---------------------------------------------------------------------------

_TOPOLOGY_PROMPTS_DIR = Path(__file__).parent / "prompts" / "topology"

_TOPOLOGY_RESOURCES = [
    ("topology://network-map", "00_network_map.md", "Full network domain map — how all domains connect"),
    ("topology://datacenter", "01_datacenter.md", "DC topology: BL→spine→leaf→server layer rules"),
    ("topology://colocation", "02_colocation.md", "Colocation topology: Equinix/Megaport edge+VCG layout"),
    ("topology://cloud", "03_cloud.md", "Cloud topology: AWS/Azure region/zone/VPC layout"),
    ("topology://office", "04_office.md", "Branch/office topology: VCE→access→AP layer rules"),
    ("topology://external", "05_external.md", "External/internet/partner/Zscaler layout rules"),
    ("topology://wan", "06_wan.md", "WAN / full network map: cross-domain circuit diagram"),
    ("topology://clusters", "07_clusters.md", "Cluster topology: k8s/VMware/Nutanix/EKS/AKS node pool layout"),
]

from fastmcp.resources import FileResource

for _uri, _fname, _desc in _TOPOLOGY_RESOURCES:
    _fpath = _TOPOLOGY_PROMPTS_DIR / _fname
    mcp.add_resource(FileResource(uri=_uri, path=_fpath, description=_desc, mime_type="text/markdown"))
