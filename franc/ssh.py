"""
SSH device access helper.

This module is pure infrastructure — no fastmcp, no MCP tool definitions.
It is imported by franc.tools.ssh to power live device troubleshooting.
Mirrors franc.traversal's shape (pure helper module powering a tools/ submodule).

Targets are lab/emulated devices (containerlab), not production gear — known_hosts is
intentionally disabled since emulated nodes don't have stable host keys across recreations.
"""

from __future__ import annotations

import os

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


async def run_ssh_command(host: str, command: str, username: str, password: str) -> str:
    """Open an SSH connection to `host` and return the stdout of `command`."""
    async with asyncssh.connect(
        host,
        username=username,
        password=password,
        known_hosts=None,
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
