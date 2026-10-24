# Datacenter Topology — Diagram Layout Rules

## Infrahub schema

A datacenter is modelled as a `TopologyDeployment` hierarchy:
- `TopologyDeployment` (e.g. `DC1`) — the datacenter root
  - `children` → pod-level `TopologyDeployment` nodes (e.g. `DC1-1-POD-1`)

Physical devices are `DcimPhysicalDevice` with a `deployment` relationship pointing to
either the DC root or one of its pod deployments.

Physical connections are `DcimCable` with `endpoints` → `DcimPhysicalInterface` → `device`.

## Queries

```graphql
# Discover DC and its pod sub-deployments
query {
  TopologyDeployment(name__value: "DC1") {
    edges { node {
      name { value }
      children { edges { node { name { value } } } }
    } }
  }
}

# Fetch all devices in a DC (DC + all pod names)
query {
  DcimPhysicalDevice(deployment__name__values: ["DC1", "DC1-1-POD-1", "DC1-1-POD-2"]) {
    edges { node { name { value }  role { value } } }
  }
}

# Fetch cables (use same deployment__name__values list)
query {
  DcimCable(deployment__name__values: ["DC1", "DC1-1-POD-1"]) {
    edges { node {
      name { value }
      endpoints { edges { node {
        ... on DcimPhysicalInterface {
          device { node { name { value } } }
        }
      } } }
    } }
  }
}
```

## Device role hierarchy — top → bottom in diagram

```
Layer 0 — border-leaf       Fabric edge; connects DC to colocation via cross-connect
Layer 1 — super-spine       Optional; present in larger DCs; connects BL to pod spines
Layer 2 — firewall-active   Inline service chain, active chassis
           firewall-passive  HA standby, same row as active
Layer 3 — load-balancer     Inline after firewall
Layer 4 — spine             Per-pod; connects upward to BL or super-spine
Layer 5 — leaf              Per-pod ToR switch
Layer 6 — l2-leaf           Optional access layer
Layer 7 — server            Compute endpoints — hidden by default
```

## Key design patterns

**Without super-spines** — BL connects directly to pod spines. Common in smaller DCs (2 pods).

**With super-spines** — Super-spines sit between BL and pod spines. Used in larger DCs (4+ pods).

**Security service chain** — Border-leafs use PBR to steer traffic through FW → LB before
re-entering the fabric. Active/passive FW pair with back-to-back HA sync cable.

**Virtual firewalls** — `DcimVirtualDevice` with role `firewall` represent per-tenant vFW
contexts (e.g. Check Point VSX virtual systems). Exclude from physical topology diagrams.

## Draw.io layout

- Direction: top-to-bottom, BL at top, servers at bottom
- Background band per layer row (colour-coded by role)
- Label each row on the left side
- Devices in the same pod cluster together horizontally within their row
- BL and SEC rows span full width (shared across all pods)

## Colour palette

| Role | Fill |
|------|------|
| border-leaf | `#1a5c3a` dark green |
| super-spine | `#1a3a5c` dark blue |
| firewall | `#8a2d2d` dark red (ellipse) |
| load-balancer | `#7a5c1a` amber (ellipse) |
| spine | `#2d6a8a` steel blue |
| leaf | `#3a8a5c` green |
| l2-leaf | `#4a9a6a` light green |
| server | `#4a4a4a` grey |
