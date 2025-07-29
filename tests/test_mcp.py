import pytest
from fastmcp import Client

from franc.server import mcp


@pytest.mark.asyncio
async def test_list_schema(client, add_mock_response, httpx_mock):
    add_mock_response(
        httpx_mock,
        mockname="schemas.json",
        method="GET",
        url="http://localhost:8000/api/schema?branch=main",
        is_reusable=True,
    )
    async with Client(mcp) as test_client:
        response = await test_client.call_tool("list_schema_nodes")
        assert isinstance(response.data, dict)
        assert response.data.get("IpamPrefix") == "Prefix"


@pytest.mark.asyncio
async def test_get_node_filters(client, add_mock_response, httpx_mock):
    add_mock_response(
        httpx_mock,
        mockname="schemas.json",
        method="GET",
        url="http://localhost:8000/api/schema?branch=main",
        is_reusable=True,
    )
    async with Client(mcp) as test_client:
        response = await test_client.call_tool("get_node_filters", {"kind": "DcimPhysicalDevice"})
        assert isinstance(response.data, dict)
        expected = {
            "position__value": "String",
            "position__values": "List[String]",
            "serial__value": "String",
            "serial__values": "List[String]",
            "rack_face__value": "String",
            "rack_face__values": "List[String]",
            "status__value": "String",
            "status__values": "List[String]",
            "role__value": "String",
            "role__values": "List[String]",
            "os_version__value": "String",
            "os_version__values": "List[String]",
            "description__value": "String",
            "description__values": "List[String]",
            "name__value": "String",
            "name__values": "List[String]",
            "interfaces__name__value": "String",
            "interfaces__name__values": "List[String]",
            "interfaces__description__value": "String",
            "interfaces__description__values": "List[String]",
            "interfaces__status__value": "String",
            "interfaces__status__values": "List[String]",
            "interfaces__role__value": "String",
            "interfaces__role__values": "List[String]",
        }
        for k, v in expected.items():
            assert response.data.get(k) == v


async def test_get_objects(client, add_mock_response, httpx_mock):
    add_mock_response(
        httpx_mock,
        mockname="schemas.json",
        method="GET",
        url="http://localhost:8000/api/schema?branch=main",
        is_reusable=True,
    )
    add_mock_response(
        httpx_mock,
        mockname="physical_devices.json",
        method="POST",
        url="http://localhost:8000/graphql/main",
    )
    async with Client(mcp) as client:
        devices = await client.call_tool("get_objects", {"kind": "DcimPhysicalDevice"})
        assert isinstance(devices.data, list)
        assert "ktw-1-oob-02" in devices.data
        assert len(devices.data) == 12


async def test_get_object(client, add_mock_response, httpx_mock):
    add_mock_response(
        httpx_mock,
        mockname="schemas.json",
        method="GET",
        url="http://localhost:8000/api/schema?branch=main",
        is_reusable=True,
    )
    add_mock_response(
        httpx_mock,
        mockname="device.json",
        method="POST",
        url="http://localhost:8000/graphql/main",
    )
    async with Client(mcp) as client:
        device = await client.call_tool(
            "get_object_details",
            {"kind": "DcimPhysicalDevice", "filters": {"name": "ktw-1-leaf-01"}},
        )
        assert isinstance(device.data, dict)
        object_data = {
            "__typename": "DcimPhysicalDevice",
            "artifacts": [],
            "description": None,
            "device_service": [
                "ktw-1-leaf-01 OSPF UNDERLAY",
                "ktw-1-leaf-01 -> ktw-1-spine-01 iBGP Session",
                "ktw-1-leaf-01 -> ktw-1-spine-02 iBGP Session",
            ],
            "device_type": "CCS-720DP-48S-2F",
            "display_label": "ktw-1-leaf-01",
            "hfid": ["ktw-1-leaf-01"],
            "id": "1856ac3a-c229-be0b-35f6-c51e674ed24c",
            "interfaces": [
                "Console",
                "Ethernet1",
                "Ethernet10",
                "Ethernet11",
                "Ethernet12",
                "Ethernet13",
                "Ethernet14",
                "Ethernet15",
                "Ethernet16",
                "Ethernet17",
                "Ethernet18",
                "Ethernet19",
                "Ethernet2",
                "Ethernet20",
                "Ethernet21",
                "Ethernet22",
                "Ethernet23",
                "Ethernet24",
                "Ethernet25",
                "Ethernet26",
                "Ethernet27",
                "Ethernet28",
                "Ethernet29",
                "Ethernet3",
                "Ethernet30",
                "Ethernet31",
                "Ethernet32",
                "Ethernet33",
                "Ethernet34",
                "Ethernet35",
                "Ethernet36",
                "Ethernet37",
                "Ethernet38",
                "Ethernet39",
                "Ethernet4",
                "Ethernet40",
                "Ethernet41",
                "Ethernet42",
                "Ethernet43",
                "Ethernet44",
                "Ethernet45",
                "Ethernet46",
                "Ethernet47",
                "Ethernet48",
                "Ethernet49",
                "Ethernet5",
                "Ethernet50",
                "Ethernet51",
                "Ethernet52",
                "Ethernet6",
                "Ethernet7",
                "Ethernet8",
                "Ethernet9",
                "Management1",
                "loopback0",
            ],
            "location": "KTW-1",
            "member_of_groups": [
                "arista_leaf",
                "create_dc-a6b020a364cc64b258db74db5dfb64b4",
                "topology_cabling__a6b020a364cc64b258db74db5dfb64b4",
            ],
            "name": "ktw-1-leaf-01",
            "object_template": "CCS-720DP-48S-2F_LEAF",
            "os_version": None,
            "platform": "Arista EOS",
            "position": None,
            "primary_address": "172.20.2.9/24",
            "profiles": [],
            "rack_face": "front",
            "role": "leaf",
            "serial": None,
            "status": "active",
            "subscriber_of_groups": [],
            "tags": [],
            "topology": "KTW-1",
        }

        assert device.data == object_data
