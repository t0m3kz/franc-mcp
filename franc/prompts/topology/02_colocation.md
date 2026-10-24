# Colocation Topology — Diagram Layout Rules

## Infrahub schema

Colocation follows a three-level hierarchy:

- `TopologyColocation` — the provider facility (e.g. Equinix, Megaport)
  - `TopologyColocationMetro` — city-level grouping (e.g. Frankfurt, Amsterdam)
    - `TopologyColocationZone` — individual cage/hall within the metro (e.g. FR2, AM1)

Devices per zone: `DcimPhysicalDevice(deployment__name__value: "<zone-name>")`
Virtual devices (SD-WAN VMs): `DcimVirtualDevice(deployment__name__value: "<zone-name>")`
Cables within zone: `DcimCable(deployment__name__value: "<zone-name>")`

## Queries

```graphql
# Discover colocation hierarchy
query {
  TopologyColocation {
    edges { node {
      name { value }
      metros { edges { node {
        name { value }
        zones { edges { node { name { value } } } }
      } } }
    } }
  }
}

# Devices in a specific zone
query {
  DcimPhysicalDevice(deployment__name__value: "FR2") {
    edges { node { name { value }  role { value } } }
  }
}

# Virtual devices (VCG/VCO) in a zone
query {
  DcimVirtualDevice(deployment__name__value: "FR2") {
    edges { node { name { value }  role { value } } }
  }
}
```

## Object loader example

A colocation zone YAML file defines the zone devices:

```yaml
apiVersion: infrahub.app/v1
kind: Object
spec:
  kind: DcimPhysicalDevice
  data:
    - name: FR2-EDGE-01
      object_template: ISR4451-X_EDGE
      status: active
      deployment: FR2
    - name: FR2-EDGE-02
      object_template: ISR4451-X_EDGE
      status: active
      deployment: FR2
```

A `DcimVirtualDevice` block in the same or separate file covers SD-WAN gateways:

```yaml
apiVersion: infrahub.app/v1
kind: Object
spec:
  kind: DcimVirtualDevice
  data:
    - name: FR2-VCG-01
      role: gateway
      status: active
      deployment: FR2
```

## Device role hierarchy (top → bottom in diagram)

```
Layer 0 — edge         Physical edge routers (uplinks to internet, DCs, partners)
Layer 1 — gateway      SD-WAN VCG hub (virtual, DcimVirtualDevice)
Layer 2 — orchestrator SD-WAN VCO management plane (virtual, DcimVirtualDevice)
Layer 3 — patch-panel  Provider cross-connect patch panels
```

## Connections to other domains

| Target | Via | Circuit kind |
|--------|-----|--------------|
| Datacenter BL | cross-connect to DC fabric edge | `TopologyPhysicalCircuit` cross_connect |
| Office branches | VCE SD-WAN spoke → VCG hub | `TopologyPhysicalCircuit` + SD-WAN overlay |
| Cloud (AWS) | edge router DirectConnect | `TopologyVirtualCircuit` private_vif |
| Cloud (Azure) | edge router ExpressRoute | `TopologyVirtualCircuit` expressroute |
| Internet / ISP | edge router BGP uplink | `TopologyPhysicalCircuit` |
| Zscaler SASE | VCG IPsec to nearest PoP | `TopologyVirtualCircuit` vpn_ipsec |

## Draw.io layout guidance

- Direction: top-to-bottom per zone, zones side-by-side
- Group by zone using swimlane containers (one per zone label)
- Show inter-zone links as horizontal edges between swimlanes
- Colour palette:
  - edge router: `#1a3a5c` (dark blue, rectangle)
  - VCG gateway: `#2d6a8a` (steel blue, rounded)
  - VCO orchestrator: `#5a3a8a` (purple, rounded)
  - patch-panel: `#888888` (grey, thin rectangle)
