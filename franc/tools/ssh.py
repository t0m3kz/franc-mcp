"""
Device SSH troubleshooting tools — live show-command execution + ntc-templates parsing.

This is the direct franc-mcp equivalent of neterse: raw CLI output from a real (or emulated)
device, optionally structured via ntc-templates before being handed to the LLM. Everything
else in this repo reads Infrahub's declared state or the Neo4j graph — this module is the one
place that talks to a live device.

Targets are lab/emulated devices reachable over SSH (e.g. containerlab nodes created by
create_datacenter_deployment/generate_containerlab_topology), identified in Infrahub by
DcimDevice.primary_address. execute_cypher (franc/tools/graph.py) is the closest existing
pattern: a guarded, read-only escape hatch.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any

from fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from franc.constants import KIND_DEVICE
from franc.ssh import parse_with_ntc, run_ssh_command, ssh_credentials, ssh_host_allowed, validate_show_command
from franc.utils import MCPResponse, MCPToolStatus, _log_and_return_error, maybe_compress, require_client

if TYPE_CHECKING:
    from infrahub_sdk import InfrahubClient

mcp: FastMCP = FastMCP(name="Device SSH Troubleshooting")

# Only troubleshooting/read-only commands are allowed (see franc.ssh.validate_show_command) — this is a lab
# escape hatch, not a config-push tool. An allowlist, since config commands vary far more than Cypher keywords.

# device_name -> (interface command, bgp summary command), keyed by DcimPlatform.netmiko_device_type.
# Falls back to the IOS-style syntax, the most common platform in this repo's bootstrap data.
_INTERFACE_CMD_BY_PLATFORM: dict[str, str] = {
    "juniper_junos": "show interfaces terse",
    "arista_eos": "show ip interface brief",
    "cisco_nxos": "show ip interface brief",
}
_DEFAULT_INTERFACE_CMD = "show ip interface brief"

_BGP_SUMMARY_CMD_BY_PLATFORM: dict[str, str] = {
    "juniper_junos": "show bgp summary",
    "arista_eos": "show ip bgp summary",
    "cisco_nxos": "show ip bgp summary",
}
_DEFAULT_BGP_SUMMARY_CMD = "show ip bgp summary"


async def _resolve_device(client: Any, device_name: str, branch: str | None) -> tuple[Any, str] | None:
    """Return (device_node, host) for a device with a usable primary_address, or None."""
    nodes = await client.filters(
        kind=KIND_DEVICE,
        name__value=device_name,
        include=["primary_address", "platform"],
        prefetch_relationships=True,
        branch=branch or "main",
    )
    if not nodes:
        return None
    device = nodes[0]

    primary_address = getattr(device, "primary_address", None)
    if primary_address is None:
        return None
    if not primary_address.initialized:
        await primary_address.fetch()
    addr_peer = primary_address.peer
    if addr_peer is None:
        return None

    host = str(addr_peer.address.value).split("/")[0]
    return device, host


async def _resolve_platform(device: Any) -> str:
    """Return DcimPlatform.netmiko_device_type for a device, or '' if unset/unresolvable."""
    platform_rel = getattr(device, "platform", None)
    if platform_rel is None:
        return ""
    if not platform_rel.initialized:
        await platform_rel.fetch()
    platform_peer = platform_rel.peer
    if platform_peer is None:
        return ""
    netmiko_type = getattr(platform_peer, "netmiko_device_type", None)
    return str(netmiko_type.value) if netmiko_type and netmiko_type.value else ""


@mcp.tool(
    tags={"ssh", "troubleshooting", "device"},
    annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True),
)
async def execute_show_command(
    ctx: Context,
    device_name: Annotated[str, Field(description="Device name to connect to (e.g. DC1-POD1-LEAF-01).")],
    command: Annotated[str, Field(description="Show/troubleshooting command to run (e.g. 'show ip interface brief').")],
    branch: Annotated[
        str | None, Field(default=None, description="Branch to resolve the device from (defaults to main).")
    ],
) -> MCPResponse[dict[str, Any]]:
    """
    SSH into a device and run a read-only show/troubleshooting command.

    Requires SSH_USERNAME and SSH_PASSWORD environment variables. Resolves device_name to
    primary_address (IP) and platform.netmiko_device_type via Infrahub, then parses the raw
    output with ntc-templates when a template exists for that platform/command.

    Returns {"raw_output", "parsed", "platform", "command"}. `parsed` is None if no
    ntc-templates template matches — raw_output is always populated.
    """
    if reason := validate_show_command(command):
        return await _log_and_return_error(
            ctx=ctx,
            error=f"Command '{command}' refused: {reason}",
            remediation="Only plain show/display/get/ping/traceroute commands (optionally piped to an output filter).",
        )

    try:
        username, password = ssh_credentials()
    except RuntimeError as exc:
        return await _log_and_return_error(
            ctx=ctx, error=str(exc), remediation="Set SSH_USERNAME and SSH_PASSWORD environment variables."
        )

    try:
        client: InfrahubClient = require_client(ctx)
    except RuntimeError as exc:
        return await _log_and_return_error(ctx=ctx, error=str(exc), remediation="Start MCP with a configured client.")

    resolved = await _resolve_device(client, device_name, branch)
    if resolved is None:
        return await _log_and_return_error(
            ctx=ctx,
            error=f"Device '{device_name}' not found or has no primary_address configured in Infrahub.",
            remediation="Verify the device name and that primary_address is set (get_object_details).",
        )
    device, host = resolved
    if not ssh_host_allowed(host):
        return await _log_and_return_error(
            ctx=ctx,
            error=f"Refusing to SSH to {host}: not in an allowed target network.",
            remediation="Only private/loopback/link-local addresses by default; set SSH_ALLOWED_NETWORKS to change.",
        )
    platform = await _resolve_platform(device)

    await ctx.info(f"Connecting to {device_name} ({host}) to run: {command}")
    try:
        raw_output = await run_ssh_command(host, command, username, password)
    except Exception as exc:  # noqa: BLE001
        return await _log_and_return_error(
            ctx=ctx,
            error=str(exc),
            remediation=f"Verify {device_name} is reachable at {host} and SSH credentials are correct.",
        )

    parsed = parse_with_ntc(platform, command, raw_output) if platform else None

    if isinstance(parsed, list):
        compressed = await maybe_compress(ctx, parsed, "parsed rows", "parsed_gcf")
        if compressed and isinstance(compressed.data, dict):
            return MCPResponse(
                status=MCPToolStatus.SUCCESS,
                data={"raw_output": raw_output, "platform": platform, "command": command, **compressed.data},
            )

    return MCPResponse(
        status=MCPToolStatus.SUCCESS,
        data={"raw_output": raw_output, "parsed": parsed, "platform": platform, "command": command},
    )


@mcp.tool(
    tags={"ssh", "troubleshooting", "device", "interfaces"},
    annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True),
)
async def get_interface_status(
    ctx: Context,
    device_name: Annotated[str, Field(description="Device name to connect to (e.g. DC1-POD1-LEAF-01).")],
    interface: Annotated[str | None, Field(default=None, description="Filter parsed rows to this interface name.")],
    branch: Annotated[
        str | None, Field(default=None, description="Branch to resolve the device from (defaults to main).")
    ],
) -> MCPResponse[dict[str, Any]]:
    """
    Show live interface status on a device (up/down, IP, protocol state).

    Picks the platform-appropriate command (IOS-style 'show ip interface brief' by default,
    'show interfaces terse' for Junos, etc.) and delegates to execute_show_command.
    """
    try:
        client: InfrahubClient = require_client(ctx)
    except RuntimeError as exc:
        return await _log_and_return_error(ctx=ctx, error=str(exc), remediation="Start MCP with a configured client.")

    resolved = await _resolve_device(client, device_name, branch)
    if resolved is None:
        return await _log_and_return_error(
            ctx=ctx,
            error=f"Device '{device_name}' not found or has no primary_address configured in Infrahub.",
            remediation="Verify the device name and that primary_address is set (get_object_details).",
        )
    device, _host = resolved
    platform = await _resolve_platform(device)
    command = _INTERFACE_CMD_BY_PLATFORM.get(platform, _DEFAULT_INTERFACE_CMD)

    result = await execute_show_command(ctx, device_name=device_name, command=command, branch=branch)
    if result.status != MCPToolStatus.SUCCESS or not isinstance(result.data, dict):
        return result

    parsed = result.data.get("parsed")
    if interface and isinstance(parsed, list):
        interface_lower = interface.lower()
        parsed = [row for row in parsed if interface_lower in str(row.get("interface", "")).lower()]
        result.data["parsed"] = parsed

    return result


@mcp.tool(
    tags={"ssh", "troubleshooting", "device", "bgp"},
    annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True),
)
async def get_bgp_summary(
    ctx: Context,
    device_name: Annotated[str, Field(description="Device name to connect to (e.g. DC1-POD1-LEAF-01).")],
    branch: Annotated[
        str | None, Field(default=None, description="Branch to resolve the device from (defaults to main).")
    ],
) -> MCPResponse[dict[str, Any]]:
    """
    Show live BGP neighbor summary on a device (state, uptime, prefix counts).

    Picks the platform-appropriate command and delegates to execute_show_command. For the
    declared/expected BGP topology instead of live device state, use get_bgp_sessions
    (franc/tools/topology.py), which reads from Infrahub.
    """
    try:
        client: InfrahubClient = require_client(ctx)
    except RuntimeError as exc:
        return await _log_and_return_error(ctx=ctx, error=str(exc), remediation="Start MCP with a configured client.")

    resolved = await _resolve_device(client, device_name, branch)
    if resolved is None:
        return await _log_and_return_error(
            ctx=ctx,
            error=f"Device '{device_name}' not found or has no primary_address configured in Infrahub.",
            remediation="Verify the device name and that primary_address is set (get_object_details).",
        )
    device, _host = resolved
    platform = await _resolve_platform(device)
    command = _BGP_SUMMARY_CMD_BY_PLATFORM.get(platform, _DEFAULT_BGP_SUMMARY_CMD)

    return await execute_show_command(ctx, device_name=device_name, command=command, branch=branch)
