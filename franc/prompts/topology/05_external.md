# External / Internet / Partners Topology — Diagram Layout Rules

## Infrahub schema

External entities are modelled as `DcimPhysicalDevice` nodes with a deployment that
starts with a recognisable prefix (e.g. `EXT-*` for external, `SAAS-*` for SaaS).
They are NOT part of a datacenter or colocation topology — they represent
third-party network elements that peer with the internal infrastructure.

SaaS security services follow a separate hierarchy:
- `TopologySaas` — the SaaS provider (e.g. Zscaler, Cloudflare)
  - `TopologySaasRegion` — geographic PoP (e.g. Europe West, Europe Central)

BGP sessions to external entities: `TopologyManagedBGPSession`
Physical circuits to ISP/IXP: `TopologyPhysicalCircuit`
IPsec tunnels to SaaS PoPs: `TopologyVirtualCircuit` (link_type: vpn_ipsec)

## Queries

```graphql
# Discover external device deployments (name prefix EXT-)
query {
  DcimPhysicalDevice(deployment__name__value__icontains: "EXT") {
    edges { node { name { value }  role { value }  deployment { node { name { value } } } } }
  }
}

# BGP sessions to external peers
query {
  TopologyManagedBGPSession {
    edges { node {
      name { value }
      local_device { node { name { value } } }
      remote_device { node { name { value } } }
      remote_as { node { asn { value } } }
      session_type { value }
    } }
  }
}

# Zscaler SaaS regions
query {
  TopologySaas(name__value: "Zscaler") {
    edges { node {
      name { value }
      regions { edges { node { name { value }  status { value } } } }
    } }
  }
}
```

## Object loader example

An external entity YAML (ISP router or IXP peer):

```yaml
apiVersion: infrahub.app/v1
kind: Object
spec:
  kind: DcimPhysicalDevice
  data:
    - name: EXT-INET-ISP-01
      object_template: GENERIC_EXTERNAL_ROUTER
      status: active
      deployment: EXT-INET
      role: external
```

SaaS provider and region:

```yaml
apiVersion: infrahub.app/v1
kind: Object
spec:
  kind: TopologySaas
  data:
    - name: Zscaler
      regions:
        kind: TopologySaasRegion
        data:
          - name: SAAS-ZSC-EU-WEST-01
            status: active
          - name: SAAS-ZSC-EU-CENTRAL-01
            status: active
```

## Conceptual hierarchy (top → bottom in diagram)

```
Layer 0 — our edge routers  (colocation edge, uplink-facing ports)
Layer 1 — ISP / IXP peer    (transit provider router, route-server)
Layer 2 — B2B partner edge  (partner BGP peer)
```

## Zscaler SASE PoP structure

Each SaaS region (`TopologySaasRegion`) represents a Zscaler PoP:
- Primary IPsec tunnel from nearest VCG hub
- Secondary tunnel from backup VCG (cross-region HA)
- Direct tunnels from branch VCEs (local internet breakout, GE INET port)

## Connections to other domains

| From | To | Via |
|------|----|-----|
| Colocation edge | ISP transit router | `TopologyPhysicalCircuit` |
| Colocation edge | IXP peering point | `TopologyPhysicalCircuit` |
| Colocation edge | B2B partner router | `TopologyPhysicalCircuit` |
| Colocation VCG hubs | Zscaler PoP (IPsec) | `TopologyVirtualCircuit` vpn_ipsec |
| Branch VCE (INET port) | Zscaler PoP (IPsec) | `TopologyVirtualCircuit` vpn_ipsec |

## Draw.io layout guidance

- External entities at top or sides of diagram; internet = top cloud shape
- Our edge routers in the middle row
- Draw BGP sessions as labelled edges with ASN annotation
- Zscaler shown as a cloud shape with PoPs listed inside
- IPsec tunnels as dotted lines
- Colour palette:
  - our edge: `#1a3a5c` (dark blue)
  - ISP/IXP: `#888888` (grey, cloud shape)
  - partner: `#5a3a1a` (brown, rectangle)
  - SaaS/Zscaler: `#ff6600` (orange, cloud shape)
  - BGP session: solid line with ASN label
  - IPsec tunnel: dotted orange line
