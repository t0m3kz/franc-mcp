"""Tests for device SSH troubleshooting tools (franc/tools/ssh.py, franc/ssh.py)."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp import Context

from franc.server import mcp
from franc.tools.ssh import execute_show_command, get_bgp_summary, get_interface_status
from franc.utils import MCPToolStatus


class _FakeAttr:
    def __init__(self, value):
        self.value = value


class _FakeRelatedNode:
    """Mimics infrahub_sdk.node.RelatedNode: .initialized / .fetch() / .peer."""

    def __init__(self, peer=None, initialized: bool = True):
        self.peer = peer
        self.initialized = initialized

    async def fetch(self):
        self.initialized = True


class _FakeAddress:
    def __init__(self, address: str):
        self.address = _FakeAttr(address)


class _FakePlatform:
    def __init__(self, netmiko_device_type: str):
        self.netmiko_device_type = _FakeAttr(netmiko_device_type)


class _FakeDevice:
    def __init__(self, host: str | None, netmiko_device_type: str | None):
        self.primary_address = _FakeRelatedNode(_FakeAddress(host) if host else None)
        self.platform = _FakeRelatedNode(_FakePlatform(netmiko_device_type) if netmiko_device_type else None)


class _FakeClient:
    def __init__(self, device: _FakeDevice | None):
        self._device = device

    async def filters(self, **_kwargs):
        return [self._device] if self._device else []


@pytest.fixture
def mock_context():
    ctx = MagicMock(spec=Context)
    ctx.info = AsyncMock()
    ctx.debug = AsyncMock()
    ctx.error = AsyncMock()
    # require_client() checks request_context.server.test_client first, then falls back to
    # franc.server.mcp.test_client. Force server=None so it takes the fallback we inject via
    # mcp.test_client below (mirrors how tests/test_mcp.py's real Client(mcp) resolves it).
    ctx.request_context.server = None
    return ctx


@pytest.fixture(autouse=True)
def _clear_test_client():
    yield
    mcp.test_client = None


@pytest.fixture
def ssh_env(monkeypatch):
    monkeypatch.setenv("SSH_USERNAME", "admin")
    monkeypatch.setenv("SSH_PASSWORD", "admin")


def _inject_client(device: _FakeDevice | None) -> None:
    mcp.test_client = _FakeClient(device)


@pytest.mark.asyncio
async def test_execute_show_command_rejects_config_command(mock_context, ssh_env):
    """A non-show command must be refused before any SSH connection is attempted."""
    _inject_client(_FakeDevice("10.0.0.1", "cisco_ios"))

    result = await execute_show_command(mock_context, device_name="leaf-01", command="configure terminal", branch=None)

    assert result.status == MCPToolStatus.ERROR
    assert result.error is not None
    assert "not a recognized troubleshooting command" in result.error


@pytest.mark.asyncio
async def test_execute_show_command_requires_ssh_credentials(mock_context, monkeypatch):
    """Missing SSH_USERNAME/SSH_PASSWORD must produce a clear, actionable error."""
    monkeypatch.delenv("SSH_USERNAME", raising=False)
    monkeypatch.delenv("SSH_PASSWORD", raising=False)
    _inject_client(_FakeDevice("10.0.0.1", "cisco_ios"))

    result = await execute_show_command(
        mock_context, device_name="leaf-01", command="show ip interface brief", branch=None
    )

    assert result.status == MCPToolStatus.ERROR
    assert result.error is not None
    assert "SSH_USERNAME" in result.error


@pytest.mark.asyncio
async def test_execute_show_command_requires_primary_address(mock_context, ssh_env):
    """A device with no primary_address configured must fail with a clear remediation."""
    _inject_client(_FakeDevice(host=None, netmiko_device_type="cisco_ios"))

    result = await execute_show_command(
        mock_context, device_name="leaf-01", command="show ip interface brief", branch=None
    )

    assert result.status == MCPToolStatus.ERROR
    assert result.error is not None
    assert "primary_address" in result.error


@pytest.mark.asyncio
async def test_execute_show_command_device_not_found(mock_context, ssh_env):
    _inject_client(None)

    result = await execute_show_command(
        mock_context, device_name="ghost-01", command="show ip interface brief", branch=None
    )

    assert result.status == MCPToolStatus.ERROR
    assert result.error is not None
    assert "not found" in result.error


@pytest.mark.asyncio
async def test_execute_show_command_parses_output(mock_context, ssh_env, monkeypatch):
    """Raw SSH output is parsed with ntc-templates when a template matches platform/command."""
    _inject_client(_FakeDevice("10.0.0.1", "cisco_ios"))

    raw = (
        "Interface                  IP-Address      OK? Method Status                Protocol\n"
        "GigabitEthernet0/0          10.0.0.1        YES NVRAM  up                    up\n"
    )
    monkeypatch.setattr("franc.tools.ssh.run_ssh_command", AsyncMock(return_value=raw))

    result = await execute_show_command(
        mock_context, device_name="leaf-01", command="show ip interface brief", branch=None
    )

    assert result.status == MCPToolStatus.SUCCESS
    assert result.data is not None
    assert result.data["raw_output"] == raw
    assert result.data["platform"] == "cisco_ios"
    assert result.data["parsed"] == [
        {"interface": "GigabitEthernet0/0", "ip_address": "10.0.0.1", "status": "up", "proto": "up"}
    ]


@pytest.mark.asyncio
async def test_execute_show_command_fail_open_on_unknown_template(mock_context, ssh_env, monkeypatch):
    """No matching ntc-templates template must not error — raw_output stays populated, parsed is None."""
    _inject_client(_FakeDevice("10.0.0.1", "linux"))
    monkeypatch.setattr("franc.tools.ssh.run_ssh_command", AsyncMock(return_value="whatever output"))

    result = await execute_show_command(mock_context, device_name="leaf-01", command="show version", branch=None)

    assert result.status == MCPToolStatus.SUCCESS
    assert result.data is not None
    assert result.data["raw_output"] == "whatever output"
    assert result.data["parsed"] is None


@pytest.mark.asyncio
async def test_execute_show_command_ssh_connection_failure(mock_context, ssh_env, monkeypatch):
    _inject_client(_FakeDevice("10.0.0.1", "cisco_ios"))
    monkeypatch.setattr("franc.tools.ssh.run_ssh_command", AsyncMock(side_effect=OSError("connection refused")))

    result = await execute_show_command(
        mock_context, device_name="leaf-01", command="show ip interface brief", branch=None
    )

    assert result.status == MCPToolStatus.ERROR
    assert result.error is not None
    assert "connection refused" in result.error


@pytest.mark.asyncio
async def test_get_interface_status_filters_by_interface(mock_context, ssh_env, monkeypatch):
    _inject_client(_FakeDevice("10.0.0.1", "cisco_ios"))
    raw = (
        "Interface                  IP-Address      OK? Method Status                Protocol\n"
        "GigabitEthernet0/0          10.0.0.1        YES NVRAM  up                    up\n"
        "GigabitEthernet0/1          unassigned      YES NVRAM  administratively down down\n"
    )
    monkeypatch.setattr("franc.tools.ssh.run_ssh_command", AsyncMock(return_value=raw))

    result = await get_interface_status(mock_context, device_name="leaf-01", interface="Ethernet0/0", branch=None)

    assert result.status == MCPToolStatus.SUCCESS
    assert result.data is not None
    assert len(result.data["parsed"]) == 1
    assert result.data["parsed"][0]["interface"] == "GigabitEthernet0/0"


@pytest.mark.asyncio
async def test_get_bgp_summary_picks_platform_command(mock_context, ssh_env, monkeypatch):
    _inject_client(_FakeDevice("10.0.0.1", "juniper_junos"))
    monkeypatch.setattr("franc.tools.ssh.run_ssh_command", AsyncMock(return_value="bgp summary output"))

    result = await get_bgp_summary(mock_context, device_name="leaf-01", branch=None)

    assert result.status == MCPToolStatus.SUCCESS
    assert result.data is not None
    assert result.data["command"] == "show bgp summary"
