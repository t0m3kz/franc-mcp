"""
SSH device access helper.

This module is pure infrastructure — no fastmcp, no MCP tool definitions.
It is imported by franc.tools.ssh to power live device troubleshooting.
Mirrors franc.traversal's shape (pure helper module powering a tools/ submodule).

Targets are lab/emulated devices (containerlab), not production gear — host-key checking is
off by default since emulated nodes don't have stable host keys across recreations. Set
SSH_KNOWN_HOSTS=<path> to turn verification on.

Because the password is sent to whatever address Infrahub holds as primary_address, targets are
restricted to private/loopback/link-local IPs (override with SSH_ALLOWED_NETWORKS=<cidr,cidr>).
"""

from __future__ import annotations

import ipaddress
import os
import re

import asyncssh
from ntc_templates.parse import parse_output

_SSH_LOGIN_TIMEOUT = 20.0
_SSH_COMMAND_TIMEOUT = 20.0


def ssh_credentials() -> tuple[str, str]:
    """Read SSH_USERNAME/SSH_PASSWORD from the environment.

    Raises RuntimeError if either is missing — no hardcoded fallback, since these
    credentials target real (if emulated) devices, unlike the lab-only Infrahub/Neo4j
    defaults elsewhere in this repo.
    """
    username = os.getenv("SSH_USERNAME")
    password = os.getenv("SSH_PASSWORD")
    if not username or not password:
        raise RuntimeError("SSH_USERNAME and SSH_PASSWORD environment variables must both be set.")
    return username, password


# Whole-word match so "getent" or "showmount" do not pass as "get"/"show".
_ALLOWED_COMMAND_RE = re.compile(r"^(show|display|get|ping|traceroute)\b", re.IGNORECASE)
# Never legitimate in a read-only CLI command, and the building blocks of shell injection on Linux-based nodes.
_FORBIDDEN_COMMAND_CHARS = frozenset(";&`$<>\\\n\r")
# Output filters a network CLI interprets itself (Cisco/Arista/Junos/Huawei style). Anything else after "|" is refused.
_ALLOWED_PIPE_FILTERS = (
    "include",
    "exclude",
    "begin",
    "section",
    "grep",
    "egrep",
    "match",
    "except",
    "count",
    "find",
    "last",
    "more",
    "no-more",
    "display",
    "json",
)
_PIPE_FILTER_RE = re.compile(rf"^({'|'.join(_ALLOWED_PIPE_FILTERS)})\b", re.IGNORECASE)


def validate_show_command(command: str) -> str | None:
    """Return a reason the command is refused, or None if it is a plain read-only troubleshooting command.

    The command must start with an allowed verb, contain no shell metacharacters, and may only be piped
    into known output filters (``show run | include bgp``).
    """
    cmd = command.strip()
    if not cmd:
        return "Command is empty."
    if bad := sorted(c for c in _FORBIDDEN_COMMAND_CHARS if c in cmd):
        return f"Command contains forbidden character(s): {' '.join(repr(c) for c in bad)}."
    if not _ALLOWED_COMMAND_RE.match(cmd):
        return "Command must start with one of: show, display, get, ping, traceroute."
    for stage in cmd.split("|")[1:]:
        if not _PIPE_FILTER_RE.match(stage.strip()):
            return (
                f"Pipe target '{stage.strip()}' is not an allowed output filter ({', '.join(_ALLOWED_PIPE_FILTERS)})."
            )
    return None


# Explicit ranges rather than ipaddress.is_private, which also accepts documentation, benchmarking and
# reserved space (203.0.113.0/24, 198.18.0.0/15, 240.0.0.0/4, ...).
_DEFAULT_ALLOWED_NETWORKS = tuple(
    ipaddress.ip_network(n)
    for n in (
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "::1/128",
        "fc00::/7",
        "fe80::/10",
    )
)


def ssh_host_allowed(host: str) -> bool:
    """True if ``host`` is an IP address inside the allowed target networks.

    Default: RFC1918, ULA, loopback and link-local. SSH_ALLOWED_NETWORKS (comma-separated CIDRs) replaces that
    default; a malformed entry fails the whole check closed. Hostnames are refused — Infrahub stores IPs,
    and a name would be resolved by DNS.
    """
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = _DEFAULT_ALLOWED_NETWORKS
    if raw := os.getenv("SSH_ALLOWED_NETWORKS", "").strip():
        try:
            networks = tuple(ipaddress.ip_network(n.strip(), strict=False) for n in raw.split(",") if n.strip())
        except ValueError:
            return False
    return any(ip.version == net.version and ip in net for net in networks)


async def run_ssh_command(host: str, command: str, username: str, password: str) -> str:
    """Open an SSH connection to `host` and return the stdout of `command`."""
    async with asyncssh.connect(
        host,
        username=username,
        password=password,
        known_hosts=os.getenv("SSH_KNOWN_HOSTS") or None,
        login_timeout=_SSH_LOGIN_TIMEOUT,
    ) as conn:
        result = await conn.run(command, timeout=_SSH_COMMAND_TIMEOUT)
        stdout = result.stdout
        if isinstance(stdout, bytes):
            return stdout.decode()
        return stdout or ""


def parse_with_ntc(platform: str, command: str, raw_output: str) -> list[dict] | None:
    """Parse raw CLI output with ntc-templates, or return None if no template matches.

    Fail-open: an unparseable command is not an error — the caller still has raw_output.
    """
    try:
        return parse_output(platform=platform, command=command, data=raw_output)
    except Exception:  # noqa: BLE001
        return None
