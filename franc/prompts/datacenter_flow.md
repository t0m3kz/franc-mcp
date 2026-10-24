# Data Center Deployment Flow (Infrahub)

Use this prompt when a user wants to explore, deploy, or extend a datacenter in Infrahub.
Source of truth for existing DCs: `infrahub-demo/data/demos/100_universal_topology/02_datacenter/`.

## Existing datacenters in the demo

| DC | Location | Metro | Fabric platform | Pods | Notes |
| --- | --- | --- | --- | --- | --- |
| DC1 | Frankfurt | `fra` | Arista EOS | 2 | iBGP underlay, iBGP EVPN overlay, MLAG, LAGs, Juniper SRX firewalls per pod |
| DC2 | Paris | `par` | Dell SONiC + Nokia SR-OS border | 2 | eBGP fabric, Nokia border-leafs |
| DC3 | Amsterdam | `ams` | Edgecore (SONiC-based) | 4 | eBGP EVPN, super-spines, CP-26000 VSX chassis, customer boarding target |

### DC1 — Frankfurt (Arista, 2 pods)

- **Topology file:** `02_datacenter/dc1/02_topology.yml`
- Pools: `DC1-VLAN-POOL`, `DC1-VNI-POOL`, `GLOBAL-L3VNI`
- Pods: 2 × `DCS-7050CX3-32C-R_SPINE`
- Underlay: iBGP RFC 5549 unnumbered; Overlay: iBGP EVPN (BL1/BL2 as route-reflectors)
- Loopbacks: `10.255.11.0/24` (namespace `FABRIC-DC1`)
- Security: per-pod Juniper SRX-1500 active-passive pairs (`DC1-CUST1-FW-01/02`, `DC1-CUST2-FW-01/02`)
- Files: `01_locations`, `02_topology`, `03_racks`, `04_devices`, `05_cabling`, `05_capabilities`,
  `06_lag_mlag`, `07_security`, `TOPOLOGY.md`

### DC2 — Paris (Dell SONiC + Nokia SR-OS, 2 pods)

- **Topology file:** `02_datacenter/dc2/02_topology.yml`
- Pools: `DC2-VLAN-POOL`, `DC2-VNI-POOL`, `GLOBAL-L3VNI`
- Pods: 2 × `N9K-C9336C-FX2_SPINE`
- Nokia SR-OS devices at the border; Dell SONiC fabric

### DC3 — Amsterdam (Edgecore/SONiC, 4 pods, customer boarding target)

- **Topology file:** `02_datacenter/dc3/02_topology.yml`
- Pools: `DC3-VLAN-POOL`, `DC3-VNI-POOL`, `GLOBAL-L3VNI`
- 2 super-spines (`Edgecore-7726-32X-O_SUPER_SPINE`), 4 pods (`Edgecore-7726-32X-O_SPINE`)
- Underlay: eBGP RFC 5549 unnumbered; Overlay: eBGP EVPN (per-device unique AS)
- AS assignments: SS1=65300, SS2=65301, BL1=65370, BL2=65371, pods=65311–65342
- Loopbacks: `10.255.13.0/24` (namespace `FABRIC-DC3`)
- **Security:** shared CP-26000 VSX chassis pair (`DC3-FW-01` active / `DC3-FW-02` standby),
  uplinked to border-leafs DC3-BL1/BL2; PBR steering per customer namespace
- Pod layout: POD1+POD2 = Kubernetes (cross-pod multi-homed nodes), POD3 = VMware vSphere, POD4 = Nutanix AHV
- All customer boarding (C001, C002, C003) lives on DC3

## Creating a new datacenter

### Required inputs

| Field | Description | Example |
| --- | --- | --- |
| `site_name` | DC name (short, uppercase) | `DC4` |
| `metro_location` | Parent metro building shortname | `lon` (London) |
| `design` | TopologyDataCenterDesign pattern | `M-Standard` |
| `strategy` | Fabric underlay+overlay strategy | `ebgp-evpn` |
| `provider` | Owner/provider label | `Internal` |

Available strategies (`TopologyDataCenterDesign.routing_strategy`): `ebgp-ebgp`, `ebgp-ibgp`, `ospf-ibgp`.

Design patterns (bootstrap `TopologyDataCenterDesign` nodes, `{SIZE}_{UNDERLAY}_{OVERLAY}`):
`S_OSPF_IBGP`, `S_EBGP_IBGP`, `S_EBGP_EBGP`, `M_OSPF_IBGP`, `M_EBGP_EBGP`, `M_EBGP_IBGP`,
`L_EBGP_EBGP`, `XL_EBGP_EBGP`.

### Pod inputs (per pod)

| Field | Description | Default |
| --- | --- | --- |
| `spine_template` | Device template for pod spines | e.g. `Edgecore-7726-32X-O_SPINE` |
| `index` | Pod index (1-based) | sequential |
| `amount_of_spines` | Spines per pod (max 8, `TopologyPod.amount_of_spines`) | 4 |
| `rows` | Rows of racks (`TopologyPodDesign.rows`) | design-defined |
| `max_leafs_per_network_rack` | Max leaf switches per network rack (`TopologyPodDesign`) | 4 |
| `max_tors_per_network_rack` | Max ToR switches per network rack (`TopologyPodDesign`) | 2 |

### Topology YAML structure (reference from DC3)

```yaml
apiVersion: infrahub.app/v1
kind: Object
spec:
  kind: TopologyDataCenter
  data:
    - name: DC3
      index: 1
      amount_of_super_spines: 2
      super_spine_template: Edgecore-7726-32X-O_SUPER_SPINE
      vlan_pool: DC3-VLAN-POOL
      vni_pool: DC3-VNI-POOL
      l3_vni_pool: GLOBAL-L3VNI
      children:
        kind: TopologyPod
        data:
          - index: 1
            spine_template: Edgecore-7726-32X-O_SPINE
          - index: 2
            spine_template: Edgecore-7726-32X-O_SPINE
          - index: 3
            spine_template: Edgecore-7726-32X-O_SPINE
          - index: 4
            spine_template: Edgecore-7726-32X-O_SPINE
```

## Interaction script

1. **Discover** — call `discover_datacenter_options()`. Report available metros, existing DCs,
   available designs and strategies.
2. **Collect** — ask for `site_name`, `metro_location`, `design`, `strategy`, `provider`.
   Suggest defaults: `ebgp-evpn`, `M-Standard`, `Internal`. Then collect pod specs.
3. **Confirm** — echo the full plan. No writes without user confirmation.
4. **Execute** — call `create_datacenter_deployment(...)`. Report branch and DC name.
5. **Validate** — call `validate_datacenter_deployment(branch, site_name)`. Flag missing objects.
6. **Summary** — one-line status. Next steps: merge branch → run topology generator.

## Guardrails

- `metro_location` must reference an existing `LocationBuilding` shortname.
  Existing: `fra` (Frankfurt), `par` (Paris), `ams` (Amsterdam). Ask for others.
- Never invent pool names — use `DC<N>-VLAN-POOL`, `DC<N>-VNI-POOL`, `GLOBAL-L3VNI` pattern.
- Design and strategy values must be exact strings from the lists above.
- If the DC already exists, suggest `get_topology_summary(datacenter=...)` to inspect it instead.
