# Office / Branch Topology — Diagram Layout Rules

## Infrahub schema

An office site is modelled as a `TopologyDeployment` (same kind as a datacenter but
with a branch/office role). Each site deployment contains:

- `DcimPhysicalDevice` — physical devices (VCE edge, access switches, APs)
- `DcimVirtualDevice` — optional virtual appliances
- `DcimCable` — internal LAN cabling
- `TopologyPhysicalCircuit` — WAN underlay (site → colocation VCG hub)
- `TopologyVirtualCircuit` — Zscaler IPsec tunnels (local internet breakout)

## Queries

```graphql
# All devices at a branch site
query {
  DcimPhysicalDevice(deployment__name__value: "OFC-SITE-01") {
    edges { node { name { value }  role { value }  status { value } } }
  }
}

# WAN circuits from a branch
query {
  TopologyPhysicalCircuit(locations__name__values: ["OFC-SITE-01"]) {
    edges { node {
      name { value }  circuit_type { value }  committed_rate { value }
      locations { edges { node { name { value } } } }
    } }
  }
}

# Zscaler IPsec tunnels
query {
  TopologyVirtualCircuit(
    link_type__value: "vpn_ipsec"
    locations__name__values: ["OFC-SITE-01"]
  ) {
    edges { node {
      name { value }  provider { value }
      locations { edges { node { name { value } } } }
    } }
  }
}
```

## Object loader example

A branch YAML file defines devices within the site deployment:

```yaml
apiVersion: infrahub.app/v1
kind: Object
spec:
  kind: DcimPhysicalDevice
  data:
    - name: OFC-SITE-01-EDGE-01
      object_template: VCE-610_EDGE
      status: active
      deployment: OFC-SITE-01
    - name: OFC-SITE-01-SW-01
      object_template: C9200L_ACCESS
      status: active
      deployment: OFC-SITE-01
    - name: OFC-SITE-01-AP-01
      object_template: C9130_AP
      status: active
      deployment: OFC-SITE-01
```

## Device role hierarchy (top → bottom in diagram)

```
Layer 0 — edge / VCE       SD-WAN branch edge device
                             WAN port  = SD-WAN underlay to colocation VCG hub
                             INET port = local internet breakout → Zscaler IPsec
Layer 1 — access switch    Wired LAN distribution
Layer 2 — access point     Wireless (omit by default unless detail requested)
Layer 3 — endpoint         Workstations, printers (hide by default)
```

## Site types

**Standard branch** — single VCE edge, one access switch, 1-2 APs.
Single WAN underlay to nearest colocation VCG hub.

**Regional office** — dual VCE edges (active/passive HA), dual hub connectivity
(primary + secondary VCG in different colocation zones), higher bandwidth circuits.

## Connections to other domains

| Target | Via | Circuit kind |
|--------|-----|--------------|
| Colocation VCG hub (primary) | VCE WAN port → cross-connect | `TopologyPhysicalCircuit` |
| Colocation VCG hub (secondary) | VCE WAN port → cross-connect | `TopologyPhysicalCircuit` |
| Zscaler SASE PoP | VCE INET port IPsec | `TopologyVirtualCircuit` vpn_ipsec |
| Corporate DC (via SD-WAN overlay) | DMPO tunnel via VCG → DC BL | `TopologyVirtualCircuit` dmpo |

## Draw.io layout guidance

- Direction: top-to-bottom, VCE at top
- Show WAN interfaces (WAN / INET ports) as labelled ports on the VCE node
- Draw WAN port leaving toward colocation (right), INET port toward Zscaler (left)
- Group all branch devices in a rounded box with site name as label
- For multi-site overview: sites side by side, converging upward to shared VCG hubs
- Colour palette:
  - VCE edge: `#1a5c3a` (dark green, rectangle)
  - access switch: `#2d6a8a` (steel blue)
  - AP: `#3a8a5c` (light green, rounded)
  - WAN link: dashed line with bandwidth label
  - Zscaler tunnel: dotted orange line
