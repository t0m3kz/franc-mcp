# Cloud Topology — Diagram Layout Rules

## Infrahub schema

Cloud infrastructure follows a three-level hierarchy:

- `TopologyCloud` — the cloud provider (e.g. AWS, Azure, GCP)
  - `TopologyCloudRegion` — geographic region (e.g. eu-central-1, westeurope)
    - `TopologyCloudZone` — availability zone within the region (e.g. eu-central-1a)

Cloud resources modelled in Infrahub:
- `CloudComputeInstance` — EC2, VM, AKS node
- `CloudLoadBalancer` — ALB, NLB, Azure LB
- `CloudNetwork` — VPC or VNet (customer network boundary)
- `CloudRouteTable` / `CloudRouteEntry` — routing within the cloud
- Connectivity to on-prem: `TopologyVirtualCircuit` (DirectConnect / ExpressRoute)

## Queries

```graphql
# Discover cloud hierarchy
query {
  TopologyCloud {
    edges { node {
      name { value }
      regions { edges { node {
        name { value }
        zones { edges { node { name { value } } } }
      } } }
    } }
  }
}

# Virtual circuits connecting cloud to on-prem
query {
  TopologyVirtualCircuit(link_type__value: "private_vif") {
    edges { node {
      name { value }  link_type { value }  bandwidth { value }
      locations { edges { node { name { value } } } }
    } }
  }
}
```

## Object loader example

Cloud topology YAML defines the region and zone objects:

```yaml
apiVersion: infrahub.app/v1
kind: Object
spec:
  kind: TopologyCloudRegion
  data:
    - name: eu-central-1
      provider: aws
      topology:
        kind: TopologyCloud
        data:
          - name: AWS
```

Virtual circuit connecting on-prem colocation to cloud:

```yaml
apiVersion: infrahub.app/v1
kind: Object
spec:
  kind: TopologyVirtualCircuit
  data:
    - name: VC-AWS-FR2-001
      link_type: private_vif
      bandwidth: 1000
      provider: AWS DirectConnect
      locations:
        - name: FR2
        - name: eu-central-1
```

## Logical layer hierarchy (top → bottom in diagram)

```
Layer 0 — region           (eu-central-1 / westeurope / etc.)
Layer 1 — availability zone (1a / 1b / 1c)
Layer 2 — VPC / VNet       (customer network boundary)
Layer 3 — subnet           (public / private / management)
Layer 4 — load balancer    (ALB/NLB/Azure LB)
Layer 5 — compute          (EC2, AKS nodes, VMs)
Layer 6 — external gateway  (Internet Gateway, NAT Gateway)
```

## Connections to other domains

| Target | Via | Circuit kind |
|--------|-----|--------------|
| Colocation edge routers | AWS DirectConnect | `TopologyVirtualCircuit` private_vif |
| Colocation edge routers | Azure ExpressRoute | `TopologyVirtualCircuit` expressroute |
| Internet (outbound) | Internet Gateway / NAT GW | — |

## Draw.io layout guidance

- Direction: top-to-bottom, region at top
- Use dashed borders for cloud boundaries (VPC/VNet)
- Group by availability zone using background bands
- Show on-prem connection as a dashed line entering from the left
- Colour palette:
  - region boundary: `#e3f2fd` (light blue background)
  - VPC/VNet: `#bbdefb` (blue dashed border)
  - subnet public: `#c8e6c9` (light green)
  - subnet private: `#fff9c4` (light yellow)
  - load balancer: `#7a5c1a` (amber, ellipse)
  - compute/VM: `#4a4a4a` (grey, rectangle)
  - internet gateway: `#1a3a5c` (dark blue, hexagon)
