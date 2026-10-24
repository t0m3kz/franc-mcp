# Customer Boarding Flow (Infrahub)

Use this prompt when a user wants to onboard a new tenant onto the VXLAN fabric.
Keep inputs concrete, never invent VNIs or CIDRs, and always confirm before writing anything.

## What customer boarding creates

1. **BuiltinIPNamespace** — customer VRF, mapped 1:1 to an L3 VNI distributed via EVPN type-5
2. **DcimVirtualDevice** (×2) — active + standby virtual firewall instance on the shared chassis
3. **ManagedVxlanSegment** (×N) — one per traffic tier, each with PBR pointing to the active vFW

Everything lands on an isolated branch. The generator runs after merge.

## Required inputs

| Field | Description | Example |
| --- | --- | --- |
| `customer_id` | Short alphanumeric ID | `C004` |
| `customer_name` | Full legal name | `Acme Corp.` |
| `datacenter` | Target DC (must exist in Infrahub) | `DC3` |
| `environment` | `p` prod / `n` non-prod / `d` dev | `p` |
| `firewall_type` | Virtual FW device type | `CloudGuard` |
| `firewall_platform` | OS/platform slug | `checkpoint_gaia` |
| `hosting_device_active` | Physical chassis for active vFW | `DC3-FW-01` |
| `hosting_device_standby` | Physical chassis for standby vFW | `DC3-FW-02` |
| `l3_vni` | L3 VNI for customer VRF (auto from `GLOBAL-L3VNI` pool if omitted) | `50014` |
| `segments` | VXLAN segments to create (see format below) | see below |

### Segment definition format

```yaml
segments:
  - customer_name: web-frontend
    prefix_cidr: 10.X.10.0/24
    gateway_ip: 10.X.10.1/24
    arp_suppression: true

  - customer_name: app-backend
    prefix_cidr: 10.X.20.0/24
    gateway_ip: 10.X.20.1/24

  - customer_name: database
    prefix_cidr: 10.X.30.0/24
    gateway_ip: 10.X.30.1/24
```

The `X` in the third octet is the customer index (C001 → 1, C002 → 2, etc.) — confirm with user.

## Existing customers in the demo (infrahub-demo/data/demos/100_universal_topology/05_customer_boarding/)

All three are boarded on DC3 with Check Point VSX hosted on DC3-FW-01/02.

| ID | Name | DC | Namespace | vFW active | vFW standby | L3 VNI |
| --- | --- | --- | --- | --- | --- | --- |
| C001 | Nordix Ltd. | DC3 | C001-DC3-PROD | DC3-C001-VSX-01 | DC3-C001-VSX-02 | 50011 |
| C002 | SwiftGo GmbH | DC3 | C002-DC3-PROD | DC3-C002-VSX-01 | DC3-C002-VSX-02 | 50012 |
| C003 | Vaultex Inc. | DC3 | C003-DC3-PROD | DC3-C003-VSX-01 | DC3-C003-VSX-02 | 50013 |

### Existing segments per customer

**C001 — Nordix Ltd.**

| Segment | CIDR | Gateway | Namespace |
| --- | --- | --- | --- |
| web-frontend | 10.1.10.0/24 | 10.1.10.1/24 | C001-DC3-PROD |
| app-backend | 10.1.20.0/24 | 10.1.20.1/24 | C001-DC3-PROD |
| database | 10.1.30.0/24 | 10.1.30.1/24 | C001-DC3-PROD |

**C002 — SwiftGo GmbH**

| Segment | CIDR | Gateway | Namespace |
| --- | --- | --- | --- |
| web-frontend | 10.2.10.0/24 | 10.2.10.1/24 | C002-DC3-PROD |
| app-backend | 10.2.20.0/24 | 10.2.20.1/24 | C002-DC3-PROD |
| database | 10.2.30.0/24 | 10.2.30.1/24 | C002-DC3-PROD |

**C003 — Vaultex Inc.** — check `05_customer_boarding/C003/03_segments.yml` for exact values.

## Interaction script

1. **Discover** — call `discover_customer_options()`. Report available DCs, existing customers, FW types.
2. **Collect** — ask for all required inputs. For DC3, suggest `CloudGuard / checkpoint_gaia` + `DC3-FW-01/02`.
   Propose three standard segments (web-frontend / app-backend / database) as default; let user adjust.
   Next available `10.X.*` prefix block: check existing customers above, suggest the next free index.
3. **Confirm** — echo the full plan (namespace name, vFW pair, segments with CIDRs). No writes without confirmation.
4. **Execute** — call `create_customer_boarding(...)`. Report branch, namespace, FW names, segment names.
5. **Validate** — call `validate_customer_boarding(branch, customer_id)`. Flag missing objects.
6. **Summary** — one-line status. Next steps: merge branch → run generator → validate routing.

## Guardrails

- Never invent L3 VNIs, CIDRs, or gateway IPs — ask the user or state auto-allocation.
- Segment CIDRs must be non-overlapping. Cross-check against existing customer prefixes above.
- Hosting device names must match real `DcimDevice` names. Check with `get_topology_devices(datacenter=..., role='firewall')` if unsure.
- If the customer already exists, warn and offer to add segments only.
