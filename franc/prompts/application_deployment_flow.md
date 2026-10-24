# Application Deployment Flow (Infrahub)

Use this prompt when a user wants to register applications for a customer in the Infrahub app catalog.
This flow assumes the customer has already been boarded (BuiltinIPNamespace + vFirewall + segments exist).
If not, run `customer_boarding_flow.md` first.

## What application deployment creates

1. **AppPortfolio** — top-level container (one per customer; reused if it already exists)
2. **AppApplication** (×N) — one per app × environment combination
3. **AppComponent** (×M) — sub-services within an app, linked to VXLAN segments and cloud VMs

## Required inputs

| Field | Description | Example |
| --- | --- | --- |
| `customer_id` | Customer owner ID | `C001` |
| `applications` | List of app definitions (see format below) | see below |
| `branch_name` | Optional — auto-generates if omitted | `app-deploy-c001-20260101120000` |

### Application definition format

```yaml
applications:
  - label: trade-portal
    environment: p          # p=production, n=non-prod, d=dev
    criticality: critical   # critical / high / medium / low
    components:
      - name: web-frontend
        component_type: frontend
        segment: web-frontend       # ManagedVxlanSegment.customer_name -> AppComponent.network_segment
        vm: C001-WEB-VM-01           # -> AppComponent.instances (peer AppInstance)
      - name: api-gateway
        component_type: backend
        segment: app-backend
        vm: C001-APP-VM-01
      - name: order-db
        component_type: database
        segment: database
        vm: C001-DB-VM-01
```

Component types: `frontend`, `backend`, `database`, `cache`, `worker`.
Load balancing: `round_robin` (default), `least_connections`.
Session persistence: `none` (default), `source_ip`.

## Existing applications in the demo (infrahub-demo/data/demos/100_universal_topology/06_applications/)

### C001 — Nordix Ltd. (portfolio: `c001`)

| App label | Env | Criticality | Components |
| --- | --- | --- | --- |
| trade-portal | p | critical | web-frontend, api-gateway, order-db, session-cache |
| trade-portal | n | medium | web-frontend, api-gateway, order-db |
| auth-platform | p | critical | auth-api, identity-db |
| risk-engine | p | high | analytics-worker, risk-db |
| nlb-app | p | high | (NLB listener demo) |

C001 segment → component mapping:

| Segment | Used by |
| --- | --- |
| web-frontend | trade-portal web-frontend |
| app-backend | trade-portal api-gateway, auth-platform auth-api, risk-engine analytics-worker |
| database | trade-portal order-db, auth-platform identity-db, risk-engine risk-db |
| cache-p | trade-portal session-cache (C001-CACHE-VM-01) |

### C002 — SwiftGo GmbH (portfolio: `c002`)

| App label | Env | Criticality | Components |
| --- | --- | --- | --- |
| shipment-tracker | p | high | web-frontend, tracking-api, shipment-db |
| shipment-tracker | n | low | web-frontend, tracking-api, order-db |
| fleet-manager | p | high | (check `06_applications/C002/` for components) |

### C003 — Vaultex Inc. (portfolio: `c003`)

| App label | Env | Criticality | Components |
| --- | --- | --- | --- |
| vault-core | p | critical | (check `06_applications/C003/` for components) |
| compliance-portal | p | high | (check `06_applications/C003/` for components) |

## Interaction script

1. **Discover** — call `discover_application_options(customer_id=...)`.
   Report: existing portfolio, existing applications, available segments and VMs.
   If no namespace/segments exist for the customer, warn and suggest running customer boarding first.

2. **Collect** — ask for the list of applications with label + environment + criticality.
   For each app: ask about components (name, type, segment link, VM names).
   Suggest using the exact segment `customer_name` values from the discovery output.

3. **Confirm** — echo: customer ID, app list with environments, criticalities, components and segment links.
   Note: portfolio is reused if it already exists. No writes without confirmation.

4. **Execute** — call `create_application_deployment(...)`.
   Report: branch, portfolio ID, app count, component count.

5. **Validate** — call `validate_application_deployment(branch, customer_id)`.
   Report counts. Flag missing portfolio or apps.

6. **Summary** — one-line status. Next steps: merge branch → run NLB listener config generator.

## Guardrails

- Segment names in components must exactly match `ManagedVxlanSegment.customer_name` values in Infrahub.
  Always confirm via `discover_application_options` — never guess.
- VM names must match existing `CloudInstance` display labels.
  Check with `get_nodes(kind='CloudInstance')` if unsure.
- Criticality must be one of: `critical`, `high`, `medium`, `low`.
- Environment must be one of: `p` (production), `n` (non-prod), `d` (dev).
- Do not create duplicate app+environment combos without confirming with the user.
