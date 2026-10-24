# Network Domain Map — Topology Overview

## Domain types and their Infrahub schemas

| Domain type | Infrahub kind | Description |
|-------------|---------------|-------------|
| Datacenter | `TopologyDeployment` + `TopologyDataCenter` | Spine-leaf fabric, physical servers |
| Colocation | `TopologyColocation` → `TopologyColocationMetro` → `TopologyColocationZone` | IXP/colo facilities with edge routers and SD-WAN gateways |
| Cloud | `TopologyCloud` → `TopologyCloudRegion` → `TopologyCloudZone` | AWS, Azure — logical regions and availability zones |
| Office/Branch | `TopologyDeployment` | SD-WAN branch sites with VCE edge devices |
| External/Internet | `DcimPhysicalDevice` with deployment prefix `EXT-*` | ISP routers, IXP peering, partner edges |
| SaaS | `TopologySaas` → `TopologySaasRegion` | Cloud-delivered services (e.g. SASE/security) |
| Cluster | `VirtCluster` → `VirtNodePool` | k8s, VMware, Nutanix, EKS, AKS workload clusters |

## How domains interconnect

```
[OFFICE BRANCHES]
   VCE edge (SD-WAN spoke)
        │ TopologyPhysicalCircuit (cross-connect underlay)
        ▼
[COLOCATION]
   Edge routers + VCG gateways (SD-WAN hub)
        │ TopologyPhysicalCircuit (cross-connect to DC)
        │ TopologyVirtualCircuit  (DirectConnect / ExpressRoute to cloud)
        │ TopologyVirtualCircuit  (IPsec to SaaS)
        ▼                    ▼                    ▼
[DATACENTER]           [CLOUD]              [SAAS/INTERNET]
 BL→spine→leaf    AWS/Azure regions    ISP transit, IXP, Zscaler
```

## Circuit types in Infrahub

| Kind | link_type values | Typical use |
|------|-----------------|-------------|
| `TopologyPhysicalCircuit` | `cross_connect`, `dark_fiber` | DC ↔ colo, office ↔ colo, colo ↔ internet |
| `TopologyVirtualCircuit` | `vpn_ipsec`, `private_vif`, `expressroute`, `dmpo` | Zscaler tunnels, AWS DirectConnect, Azure ExpressRoute, SD-WAN overlay |
| `DcimCable` | (physical patch) | Within-DC or within-colo device-to-device |

## Diagram type selection

| When user asks about | Use layout from |
|----------------------|-----------------|
| Datacenter fabric (spine-leaf, DC) | `topology://datacenter` |
| Colocation facility | `topology://colocation` |
| Cloud region (AWS, Azure) | `topology://cloud` |
| Branch / office / SD-WAN site | `topology://office` |
| ISP / IXP / partner / SaaS | `topology://external` |
| Full WAN / cross-domain map | `topology://wan` |
| Compute cluster internals | `topology://clusters` |
