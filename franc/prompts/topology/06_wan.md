# WAN / Full Network Map — Diagram Layout Rules

Use this when the user wants to see how multiple domains connect — inter-DC,
DC-to-cloud, office-to-colo, or any cross-domain traffic path.

## Layout model: geographic columns, left-to-right

```
[OFFICES]  →  [COLOCATION]  →  [DATACENTER]
                   ↕
             [CLOUD / EXTERNAL]
```

Arrange domain boxes as geographic columns based on region proximity.
Each domain = one rounded rectangle (not a swimlane container).

## Infrahub queries for WAN diagram

```graphql
# All physical circuits (cross-connects, dark fiber)
query {
  TopologyPhysicalCircuit {
    edges { node {
      name { value }
      circuit_type { value }
      committed_rate { value }
      locations { edges { node { name { value } } } }
    } }
  }
}

# All virtual circuits (IPsec, DirectConnect, ExpressRoute, SD-WAN)
query {
  TopologyVirtualCircuit {
    edges { node {
      name { value }
      link_type { value }
      bandwidth { value }
      provider { value }
      locations { edges { node { name { value } } } }
    } }
  }
}

# Topology deployments (DC + office sites)
query {
  TopologyDeployment {
    edges { node {
      name { value }
      type { value }
      parent { node { name { value } } }
    } }
  }
}
```

## Object loader example

A physical circuit YAML connecting a colocation zone to a datacenter border-leaf:

```yaml
apiVersion: infrahub.app/v1
kind: Object
spec:
  kind: TopologyPhysicalCircuit
  data:
    - name: PHYS-FR2-DC1-001
      circuit_type: cross_connect
      committed_rate: 10000
      provider: Equinix
      locations:
        - name: FR2
        - name: DC1
```

A virtual circuit for SD-WAN underlay (branch to colocation):

```yaml
apiVersion: infrahub.app/v1
kind: Object
spec:
  kind: TopologyVirtualCircuit
  data:
    - name: VC-SDWAN-OFC-SITE-01-FR2
      link_type: dmpo
      bandwidth: 500
      provider: Arista SD-WAN
      locations:
        - name: OFC-SITE-01
        - name: FR2
```

## Circuit line styles for WAN diagram

| Circuit kind | Line style | Label |
|-------------|-----------|-------|
| `TopologyPhysicalCircuit` cross_connect | solid thick | committed rate (Mbps) |
| `TopologyPhysicalCircuit` dark_fiber | solid thin | "Dark Fiber" |
| `TopologyVirtualCircuit` vpn_ipsec | dotted orange | "IPsec" |
| `TopologyVirtualCircuit` private_vif | dashed blue | "DirectConnect" |
| `TopologyVirtualCircuit` expressroute | dashed purple | "ExpressRoute" |
| `TopologyVirtualCircuit` dmpo | dashed green | "SD-WAN" |
| BGP session | thin solid grey | "BGP / ASN" |

## Zoom levels

| What user asks | Show |
|---------------|------|
| "full WAN map" | All domains as boxes, circuits as edges, no device detail |
| "how does DC X connect to cloud?" | DC → nearest colo → cloud; highlight circuits |
| "how does a branch reach a DC?" | Branch VCE → colo VCG → SD-WAN → colo edge → DC BL |
| "internet path from DC X" | DC BL → colo edge → ISP / IXP cross-connect |
| "Zscaler coverage" | All VCEs/VCGs → Zscaler PoPs (cloud shapes) |

## Draw.io layout guidance

- Geographic left-right layout (west → central → east by region)
- Each domain = one rounded rectangle with domain type label
- Circuits = edges between domain boxes with line style from the table above
- Bandwidth labels on circuit edges
- Zscaler/SaaS shown as a cloud shape floating above the colocation layer
- Cloud (AWS/Azure) shown as clouds beside the corresponding colocation metro
- Colour per domain type:
  - datacenter: `#1a5c3a` border, `#e8f5e9` fill (dark green)
  - colocation: `#1a3a5c` border, `#e3f2fd` fill (dark blue)
  - office/branch: `#2d6a8a` border, `#e8f0fe` fill (steel blue)
  - cloud: `#e3f2fd` fill, no border (light blue cloud shape)
  - external/internet: `#888888` border (grey)
  - SaaS: `#ff6600` (orange cloud shape)
