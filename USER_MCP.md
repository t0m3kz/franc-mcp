# Franc — Network Intelligence MCP

Franc is a network-aware AI assistant connected to Infrahub. It understands your physical infrastructure, VXLAN segments, application deployments, firewall policy, BGP sessions, cloud connectivity, and more.

Ask Franc in plain English. It translates your question into graph queries, traces paths, and returns structured answers.

---

## What you can ask

### Infrastructure overview

> "What does our network look like?"
> "Show me DC1 topology"
> "Give me a fabric health report for DC2"

```
DC1 — fabric health
  devices: 42  spines: 4  leafs: 16  border-leafs: 4  firewalls: 2
  BGP sessions: 128  established: 128  broken: 0
  segments: 14  deployments: 3
  health: ✓ ok
```

---

> "What devices are in DC1 pod 1?"
> "List all border-leafs in DC2"

```
DC1 / pod-1
  DC1-POD1-SP1   spine        Arista EOS
  DC1-POD1-SP2   spine        Arista EOS
  DC1-POD1-L1    leaf         Arista EOS
  DC1-POD1-L2    leaf         Arista EOS
  DC1-POD1-SRV-01  server     —
```

---

### Customers and segments

> "Which customers are onboarded?"
> "Show me the VXLAN segments for customer c001"
> "What namespaces does tenant c002 have?"

```
Tenant: c001 — ACME Corp
  Segments: 3
    c001-prod-web    vlan=100  vni=10100  pbr=yes  fw=DC1-C001-FW-01
    c001-prod-app    vlan=101  vni=10101  pbr=yes  fw=DC1-C001-FW-01
    c001-mgmt        vlan=200  vni=10200  pbr=no
  Namespaces: c001-prod, c001-mgmt
```

---

### Applications

> "What applications do we have?"
> "List apps for customer c001"
> "What components does trade-portal have?"

```
Applications
  c001-trade-portal      components: 3  (web-frontend, api-gateway, dispatch-portal)
  c001-fleet-manager     components: 2  (fleet-api, dispatch-portal)
  c002-auth-platform     components: 2  (auth-api, identity-store)
```

---

### Path tracing — device to device

> "Find the path from DC1-POD1-SRV-01 to DC1-BL1"
> "Is there a path between DC1-POD1-SP1 and DC2-POD1-SP1?"
> "Show all ECMP paths from DC1-POD1-SRV-01 to DC2-BL1"

BFS traverses cables, physical circuits, virtual circuits (MPLS/SD-WAN), and cloud circuits in one pass.

```
DC1-POD1-SRV-01  →  DC1-BL1

  hop 1  DC1-POD1-SRV-01/eth1   =[CBL-DC1-POD1-SRV01-L1]=   DC1-POD1-L1/Eth1
  hop 2  DC1-POD1-L1/Eth49      =[CBL-DC1-POD1-SP1-L1]=      DC1-POD1-SP1/Eth1
  hop 3  DC1-POD1-SP1/Eth49     =[CBL-DC1-BL1-POD1-SP1]=     DC1-BL1/Eth1

  paths found: 4 (ECMP)  hops: 3  health: ✓ ok
```

You can constrain which link types are traversed:

> "Trace from DC1-BL1 to Equinix edge router using only physical circuits"
> "Find path from DC1-BL1 to the AWS Direct Connect endpoint via cloud circuits"

---

### Path tracing — pinned to a specific interface

> "Trace from DC1-POD1-SRV-01 Eth1 to DC1-BL1"
> "What is reachable from DC1-BL1 Eth5?"

Use `device:interface` notation to start or end the BFS at a specific port.

```
DC1-POD1-SRV-01:Eth1  →  DC1-BL1

  hop 1  DC1-POD1-SRV-01/Eth1  =[CBL-DC1-POD1-SRV01-L1]=  DC1-POD1-L1/Eth1
  hop 2  DC1-POD1-L1/Eth49     =[CBL-DC1-POD1-SP1-L1]=     DC1-POD1-SP1/Eth1
  hop 3  DC1-POD1-SP1/Eth49    =[CBL-DC1-BL1-POD1-SP1]=    DC1-BL1/Eth1
```

---

### Path tracing — across topologies (DC → colo → cloud)

> "Trace from DC1-POD1-SRV-01 to the AWS us-east-1 instance"
> "How does a server in DC1 reach the Azure ExpressRoute endpoint?"
> "Show the path from DC2 border-leaf to Equinix AM7"

Franc follows the full chain: DC fabric → border-leaf → physical circuit → Equinix edge → cloud circuit → cloud instance.

```
DC1-POD1-SRV-01  →  aws-instance-prod-01

  hop 1  DC1-POD1-SRV-01/eth1   =[CBL-DC1-POD1-SRV01-L1]=         DC1-POD1-L1/Eth1
  hop 2  DC1-POD1-L1/Eth49      =[CBL-DC1-POD1-SP1-L1]=            DC1-POD1-SP1/Eth1
  hop 3  DC1-POD1-SP1/Eth49     =[CBL-DC1-BL1-POD1-SP1]=           DC1-BL1/Eth1
  hop 4  DC1-BL1/Eth10          =[PHYS-DC1-EQUINIX-AM7-XC]=        AM7-EDGE-01/xe-0/0/1
  hop 5  AM7-EDGE-01/xe-0/0/2   =[VIRT-AWS-DIRECT-CONNECT-c001]=   aws-instance-prod-01/subnet-prod

  health: ✓ ok
```

---

### Path tracing — segment to cloud (end-to-end VXLAN)

> "Trace the c001-prod-web segment to cloud"
> "Which cloud instances does c001-prod-app reach?"
> "Verify PBR routing for the c002-mgmt segment"

Shows the full path from every server carrying the segment through the firewall (if PBR) to cloud endpoints.

```
Segment: c001-prod-web
  CIDR: 10.10.1.0/24   namespace: c001-prod   vlan=100  vni=10100
  PBR: yes  →  firewall: DC1-C001-FW-01 (active)

  Server: DC1-POD1-SRV-01
    SRV-01 → L1 → SP1 → BL1 → FW-01 → BL1 → [Direct Connect] → aws-prod-01
    cloud endpoint: aws-prod-01 / subnet-prod  ✓ ok

  Server: DC1-POD1-SRV-02
    SRV-02 → L2 → SP2 → BL1 → FW-01 → BL1 → [Direct Connect] → aws-prod-01
    cloud endpoint: aws-prod-01 / subnet-prod  ✓ ok
```

---

### Device neighbors

> "What is DC1-POD1-SP1 connected to?"
> "Show all cables on DC1-BL1"
> "What circuits terminate on AM7-EDGE-01?"

```
DC1-POD1-SP1 — neighbors

  Eth1   cable    CBL-DC1-POD1-SP1-L1     →  DC1-POD1-L1/Eth49
  Eth2   cable    CBL-DC1-POD1-SP1-L2     →  DC1-POD1-L2/Eth49
  Eth49  cable    CBL-DC1-BL1-POD1-SP1    →  DC1-BL1/Eth1
  Eth50  cable    CBL-DC1-BL1-POD1-SP1    →  DC1-BL1/Eth2
```

---

### Routing connections from a device

> "Show routing connections from DC1-BL1"
> "Which BGP sessions does DC2-BL2 have and are they healthy?"
> "Trace all paths out of DC1-C001-FW-01"

Returns reachable destinations, full hop paths with interface health, and BGP session status.

```
DC1-BL1 — routing connections

  → DC1-POD1-SP1   hops=1  health: ✓ ok
  → DC1-POD1-SP2   hops=1  health: ✓ ok
  → DC1-C001-FW-01 hops=1  health: ✓ ok
  → AM7-EDGE-01    hops=2  health: ✓ ok  [via physical circuit PHYS-DC1-EQUINIX-AM7-XC]

  BGP sessions:
    DC1-BL1→AM7-EDGE-01   eBGP  established  ✓
    DC1-BL1→DC1-POD1-SP1  iBGP  established  ✓
```

---

### Path tracing — application to application

> "Trace traffic between trade-portal and fleet-manager"
> "Can trade-portal reach fleet-manager?"

```
=== trade-portal → fleet-manager ===
  pairs=81  routed=34  unreachable=38  co-located=9  health=ok

+----+---------------------+---------------------+------ ... ------+------+--------+---------------+
| #  | src_server          | dst_server          | path            | hops | ecmp   | status        |
+----+---------------------+---------------------+------ ... ------+------+--------+---------------+
| 1  | DC1-POD1-SRV-01     | DC1-POD2-SRV-01     | SRV-01 =[ ... ]= DC1-C001-FW-01 =[ ... ]= SRV-01 | 8 | 16 ×16 | ✓ ok |
| 2  | DC1-POD1-SRV-01     | DC1-POD1-SRV-01     | —               | —    | —      | ≡ co_located  |
| 3  | DC1-POD1-SRV-01     | DC2-POD1-SRV-01     | —               | —    | —      | — unreachable |
```

One representative path per server pair is shown. The `ecmp` column shows how many equal-cost paths exist.

To expand all ECMP paths:
> "Show me all ECMP paths between trade-portal and fleet-manager"

---

### Path tracing — single component

> "Trace the web-frontend component"
> "How does api-gateway reach the firewall?"

```
Component: api-gateway  →  segment: c001-prod-app
  Server: DC1-POD1-SRV-01
    hop 1  DC1-POD1-SRV-01/eth1  =[CBL-DC1-POD1-SRV01-L1]=  DC1-POD1-L1/Eth1
    hop 2  DC1-POD1-L1/Eth49     =[CBL-DC1-POD1-SP1-L1]=     DC1-POD1-SP1/Eth1
    hop 3  DC1-POD1-SP1/Eth49    =[CBL-DC1-BL1-POD1-SP1]=    DC1-BL1/Eth1
    hop 4  DC1-BL1/Eth5          =[CBL-DC1-C001-FW01-BL1]=   DC1-C001-FW-01/eth1
```

---

### Firewall and PBR

> "Which firewall handles traffic for trade-portal?"
> "Is PBR enabled on the c001-prod-web segment?"
> "Show HA group for DC1-C001-FW-01"

```
Segment: c001-prod-web
  pbr_enabled: yes
  firewall HA group:
    DC1-C001-FW-01  role=firewall-active
    DC1-C001-FW-02  role=firewall-passive
  active FW: DC1-C001-FW-01
```

---

### BGP and routing

> "Show BGP sessions in DC1"
> "Are there any broken BGP sessions?"
> "List external BGP peers"

```
DC1 — BGP sessions (128 total)
  iBGP underlay:  64  established / 0 broken
  iBGP overlay:   48  established / 0 broken
  eBGP external:  16  established / 0 broken
```

---

### Cloud connectivity

> "What cloud deployments do we have?"
> "How does DC1 connect to AWS?"
> "Show Direct Connect circuits"

```
Cloud connectivity
  AWS Direct Connect:   2 connections  (DC1→us-east-1, DC2→eu-west-1)
  Azure ExpressRoute:   1 circuit      (DC1→westeurope)
  Equinix cross-connect via DC1-BL1 and DC2-BL1
```

---

### Colocation and offices

> "What's in Equinix AM7?"
> "List WAN circuits for our London office"
> "Show office devices"

---

### Bill of Materials (BOM)

> "Give me the BOM for DC1"
> "Show all devices in our London office"
> "What hardware is deployed at the FR2 colocation site?"
> "Include circuits in the DC3 BOM"

Franc produces a full inventory for any scope: datacenter, office, colocation, or everything at once.

```
BOM: datacenter DC1  (branch=main)

Physical devices: 34
  by_role:     border-leaf=2  spine=4  leaf=8  server=12  firewall=2  patch-panel=1
  by_status:   active=33  planned=1
  by_platform: Arista EOS=26  —=8
  by_template: DCS-7280CR3=26  PATCH-48=1  ...

Virtual devices: 4
  by_role:   firewall=4
  by_status: active=4
```

Use `scope` to narrow the query:

| scope | covers |
|---|---|
| `datacenter` | DC fabric nodes — border-leaf, spine, leaf, server |
| `office` | Branch/WAN edge — VCE routers, access switches, APs |
| `colocation` | Colo edge — patch panels, edge routers, VCG gateways |
| `all` | Everything in Infrahub |

Pass `scope_name` to restrict to a specific site (e.g. `DC1`, `OFC-LON-BRANCH-01`, `FR2`).
Pass `include_circuits=true` to also count physical circuits (cross-connects, MPLS, dark-fiber).

---

### Differential BOM — what changed?

> "What hardware changed in DC1 on this branch?"
> "Show me what was added or removed in the colocation sites vs main"
> "Diff the BOM for OFC-LON-BRANCH-01 between branch feature-x and main"

Franc compares the BOM on two branches and returns only the delta — no need to read the full inventory twice.

```
BOM diff: feature-x  vs  main  (scope=datacenter DC1)

Physical devices  — 3 changes
  added:   DC1-POD2-L5  (leaf / Arista EOS / planned)
  removed: —
  changed:
    DC1-POD1-SRV-01   status: planned → active

Virtual devices — 0 changes
```

Diff sections:
- **added** — present on the feature branch, not on base
- **removed** — present on base, not on the feature branch
- **changed** — same device, different attributes (role / platform / status / template)

---

### Infrastructure changes (branch-based)

> "Board a new customer c003 onto DC1"
> "Deploy a new datacenter DC4"
> "Create an application for c001 called inventory-portal"

Franc creates all changes on an **isolated branch** in Infrahub — nothing touches main until you review and approve a Proposed Change.

```
Branch created: c003-boarding-xxxx
  ✓ namespace created
  ✓ segments provisioned (prod-web, prod-app, mgmt)
  ✓ VLAN/VNI allocated from pool
  ✓ firewall group assigned

Review: GET /proposed-changes/...
```

---

### Topology diagrams — Draw.io

> "Draw the DC1 topology"
> "Generate a diagram for DC3"
> "Show me DC2 with servers"
> "Show only pod 1 in DC1"
> "Draw DC1 down to the leaf layer"
> "Show me just the spine and border-leaf layer"

Franc generates a Draw.io XML diagram with devices arranged in layers (border-leaf → spine → leaf → servers), colour-coded by role, with cables as edges. Open the result in VS Code with the hediet.vscode-drawio extension.

Use `pod` to restrict the diagram to a single pod (e.g. `pod='POD1'`).
Use `max_role` to limit the diagram to a specific layer ceiling — e.g. `max_role='leaf'` shows border-leaf + firewall + spine + leaf only, `max_role='spine'` shows only the core without leafs.

```
DC1 / POD-1 topology (max_role=leaf)
  layers:  Border Leaf | Firewall | Spine | Leaf
  devices: 14   cables: 28
  saved:   dc1_topology.drawio
```

---

### Cluster diagrams — Draw.io

> "Show me the compute clusters in DC3"
> "Draw the Kubernetes cluster DC3-K8S-PROD"
> "Show all VMware clusters"

Renders a swimlane diagram — each cluster is a column, each node pool is a colour-coded row (control plane, worker, storage, hypervisor, management) with node count badges.

```
DC3 clusters
  clusters: 3   node_pools: 9
  saved:    dc3_clusters.drawio
```

---

### Path diagrams — Draw.io

> "Draw the path from DC1-BL1 to DC1-POD1-L1"
> "Show all paths from DC2-POD1-SRV-01 to the London office switch"
> "Diagram the route from DC1-POD1-SRV-01 to OFC-LON-BRANCH-ACC-01"

BFS traverses `DcimCable`, `TopologyPhysicalCircuit`, and `TopologyVirtualCircuit` in a single pass. All shortest paths are rendered with interface labels next to each device.

Line styles:
- **Solid grey** — physical cable (`DcimCable`)
- **Dashed blue** — physical circuit (cross-connect, dark fiber)
- **Dotted orange** — virtual circuit (SD-WAN, IPsec, DirectConnect, ExpressRoute)

```
DC2-POD1-SRV-01  →  OFC-LON-BRANCH-ACC-01
  paths: 4   hops: 6   devices: 9   cables: 11
  saved: dc2_to_ofc_path.drawio
```

---

## Calling tools directly

Everything above is a Franc tool, so any MCP client can call it with no chat in between (for example the
MCP inspector: `npx @modelcontextprotocol/inspector uv run fastmcp run franc/server.py`).

| Task | Tool |
|------|------|
| List applications, segments, VMs | `discover_application_options(customer_id?)` |
| Trace paths between two applications | `trace_app_to_app(src_app, dst_app)` |
| Inspect segments (owner, environment, PBR) | `get_segment_inventory(customer_id?, environment?)` |
| ECMP paths between two devices | `find_physical_path(src, dst, all_paths=true)` |
| DC fabric diagram | `generate_topology_diagram(datacenter, format="drawio")` |
| Diagram of the path between two devices | `generate_path_diagram(source, target)` |
| Containerlab topology for a path | `generate_containerlab_topology(source, target)` |
| Bill of materials | `get_bill_of_materials(scope)` and `get_bom_diff(branch, base_branch)` |

Check each tool's full parameter list in its docstring or via `tools/list`.

---

## Output conventions

| Symbol | Meaning |
|--------|---------|
| `✓ ok` | Path healthy, all interfaces up |
| `⚠ warning` | At least one interface in a degraded state |
| `✗ broken` | Path broken — one or more interfaces down |
| `— unreachable` | No path found between these servers |
| `≡ co_located` | Source and destination are on the same server |
| `×16` | 16 equal-cost paths available (ECMP) |
| `=[CBL-...]= ` | Physical cable traversed on this hop |

---

## Infrastructure recommendations

Franc does not have a dedicated "recommend" tool — instead it reasons over the data it already collects and surfaces observations when you ask. All recommendations are read-only analysis; any resulting changes go through the normal branch workflow.

---

### Capacity and redundancy

> "Is DC1 at risk if a spine goes down?"
> "Does DC2 have single points of failure?"
> "How many ECMP paths does DC1-POD1-SRV-01 have to the firewall?"

Franc traces all ECMP paths and identifies bottlenecks — devices that appear on every path (single point of failure) vs devices that appear on only some paths (partial redundancy).

```
DC1-POD1-SRV-01  →  DC1-C001-FW-01
  ECMP paths: 4
  Devices on ALL paths:   DC1-BL1           ← single point of failure
  Devices on SOME paths:  DC1-POD1-SP1, DC1-POD1-SP2  (2/4 each)

  Recommendation: DC1-BL1 has no redundancy on this path.
  Adding a second border-leaf would provide full ECMP failover.
```

---

### Firewall placement and PBR

> "Is the firewall placement optimal for c001?"
> "Which servers are routing through a remote firewall?"
> "Are any segments missing PBR where it should be enabled?"

`check_firewall_placement` checks whether each server carrying a PBR-enabled segment is in the same DC as the assigned firewall. A "remote" result means traffic hairpins across DCs.

```
Firewall placement check:
  Segments checked: 3  (1 with remote hairpin, 2 fully local)

  ✗ c001-prod-web  fw=DC1-C001-FW-01 (DC1)
      DC1-POD1-SRV-01 (DC1)  →  ✓ local
      DC1-POD1-SRV-02 (DC1)  →  ✓ local
      DC2-POD1-SRV-01 (DC2)  →  ✗ remote

  ✓ c001-mgmt  fw=DC1-C001-FW-01 (DC1)
      DC1-POD1-SRV-01 (DC1)  →  ✓ local
```

`check_pbr_coverage` flags segments where PBR config and firewall assignment are inconsistent.

```
PBR coverage check:
  Segments: 5 total  (2 gaps, 3 ok)

  ✗ c002-prod-web  [critical]
      pbr=True  firewall=none  issue=pbr_enabled_no_firewall

  ⚠ c001-mgmt     [warning]
      pbr=False  firewall=DC1-C001-FW-01  issue=firewall_assigned_pbr_disabled
```

---

### BGP session health and peer coverage

> "Are there any non-established BGP sessions in DC1?"
> "Does DC1-BL1 have full eBGP redundancy to Equinix?"
> "Which devices have BGP sessions that are not established?"

```
DC1 — BGP health check

  Non-established sessions: 2
    DC1-BL2 → AM7-EDGE-02  eBGP  idle  ✗ broken
    DC1-BL1 → AM7-EDGE-02  eBGP  idle  ✗ broken

  Recommendation: Both sessions to AM7-EDGE-02 are down.
  Check physical circuit PHYS-DC1-EQUINIX-AM7-XC-02 and verify
  BGP timer/auth config on AM7-EDGE-02.
```

---

### Segment deployment coverage

> "Which customers are only deployed in one DC?"
> "Does c002 have segments in DC2?"
> "Are all segments deployed in all DCs?"

Franc cross-references segment deployments against the DC inventory and flags gaps.

```
Segment deployment coverage

  c001-prod-web    DC1 ✓  DC2 ✓  DC3 ✗  → not deployed in DC3
  c001-prod-app    DC1 ✓  DC2 ✓  DC3 ✗  → not deployed in DC3
  c002-prod-web    DC1 ✓  DC2 ✗  DC3 ✗  → single DC only

  Recommendation: c002 has no DC2/DC3 redundancy.
  Extend deployment to DC2 for active-active resilience.
```

---

### Monitoring coverage

> "Which interfaces should I monitor for the path from trade-portal to fleet-manager?"
> "Give me the monitoring inventory for DC1-POD1-SRV-01 to DC1-BL1"

Franc de-duplicates all ECMP paths and returns the exact flat list of devices and interfaces that monitoring must cover — not thousands of path permutations.

```
Monitoring inventory: DC1-POD1-SRV-01  →  DC1-BL1
  unique devices: 5   unique interfaces: 12   ECMP paths: 4

  device              interface   role    status   health
  DC1-POD1-SRV-01     eth1        server  active   ✓
  DC1-POD1-L1         Eth1        leaf    active   ✓
  DC1-POD1-L1         Eth49       leaf    active   ✓
  DC1-POD1-SP1        Eth1        spine   active   ✓
  DC1-POD1-SP1        Eth49       spine   active   ✓
  DC1-POD1-SP2        Eth1        spine   active   ✓
  DC1-POD1-SP2        Eth49       spine   active   ✓
  DC1-BL1             Eth1        bl      active   ✓
  ...
```

---

### Impact analysis before a change

> "What breaks if I take DC1-BL1 offline?"
> "Which applications are affected if DC1-C001-FW-01 goes down?"
> "If I disable this cable, which paths lose redundancy?"

Franc traces which applications and server pairs use the device, then assesses whether remaining ECMP paths can absorb the loss.

```
Impact: DC1-BL1 goes offline

  Affected applications:
    trade-portal     → 12 server pairs lose all paths  ✗ full outage
    fleet-manager    → 12 server pairs lose all paths  ✗ full outage
    auth-platform    → 0 affected (DC2 only)           ✓ no impact

  Root cause: DC1-BL1 is the only border-leaf in DC1.
  Recommendation: Add DC1-BL2 before scheduling maintenance.
```

---

## Things Franc will NOT do

- Execute raw writes without a branch (all changes are branch-isolated)
- Delete customer data without explicit confirmation
- Modify BGP policy or firewall rules directly (those go through the change workflow)
