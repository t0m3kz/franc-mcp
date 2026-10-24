# Agent Guide

This guide keeps all automation agents aligned on workflow, quality gates, and prompting practices for this repo.

## Who is Franc?

Franc is a brilliantly sarcastic MCP server — competent, dry, and mildly judgmental about your life choices.
In English: witty and sharp. In Polish: switches to "synu" (son) and dispenses fatherly wisdom
("Synu, zawsze sprawdzaj branch zanim coś zepsujesz.").
Tool output is always clean and factual. The personality lives in conversational messages and `ctx.info` logs.

---

## Supported Workflows

### 1. Schema & Node Discovery

Use these tools first. Every other workflow depends on them.

- `get_schema_mapping` → list all kinds
- `get_schema(kind, expand?)` → full schema with attributes and relationships. `expand=true` inlines one
  level of each relationship's peer schema, and for generic peers lists `used_by` — the concrete kinds a
  relationship may point at. Use it to answer "what can this relationship be connected to?"
- `get_node_filters(kind)` → available filter keys
- `get_nodes(kind, filters?, limit?, offset?, include_attributes?)` → one page (default 50). Read `has_more`
  and `total_count`; page with `offset` instead of asking for everything (`limit=-1` is the escape hatch)
- `search_nodes(query, kind)` → substring match on `name` only; use `get_nodes(partial_match=true)` otherwise
- `get_required_fields(kind)` → required fields before creating

### 2. Data Center Deployment

See `franc/prompts/datacenter_flow.md` for the full script.

Short path:

1. `discover_datacenter_options()` — get metros, designs, strategies, providers
2. Collect: `site_name`, `metro_location`, `design`, `strategy`, `provider`
3. `create_datacenter_deployment(...)` — creates branch + TopologyDataCenter
4. `validate_datacenter_deployment(branch, site_name)` — verify

### 3. Topology Inspection

Use after a DC exists to understand its structure:

- `get_topology_summary(datacenter?)` — DC/pod/device/namespace/segment counts
- `get_topology_devices(datacenter, role?, pod_index?)` — device list with roles and platforms
- `get_bgp_sessions(datacenter?, device_name?, session_type?)` — BGP peering inventory
- `get_tenant_namespaces()` — IPAM namespace list with L3 VNI (customer deployments share the built-in "default" namespace)

### 4. Customer Boarding

See `franc/prompts/customer_boarding_flow.md` for the full script.

Short path:

1. `discover_customer_options()` — existing customers, DCs, FW types
2. Collect: `customer_id`, `customer_name`, `datacenter`, `environment`, `firewall_type`, `segments`
3. `create_customer_boarding(...)` — creates branch + namespace + vFW pair + VXLAN segments
4. `validate_customer_boarding(branch, customer_id)` — verify counts

### 5. Application Deployment

See `franc/prompts/application_deployment_flow.md` for the full script.

**Prerequisites**: customer must be boarded (namespace + segments must exist).

Short path:

1. `discover_application_options(customer_id?)` — portfolios, apps, segments, VMs
2. Collect: `customer_id`, app list with labels/environments/criticalities/components
3. `create_application_deployment(...)` — creates branch + portfolio + apps + components
4. `validate_application_deployment(branch, customer_id)` — verify

### 6. Graph Traversal (Neo4j)

Path and impact tools use the SDK's server-side traversal (`InfrahubClient.traverse_paths()` /
`reachable_nodes()`, **Infrahub ≥ 1.10**). Only `execute_cypher` talks **directly to Neo4j** (HTTP port 7474);
it needs the docker-compose stack and env vars `NEO4J_HTTP_URL`, `NEO4J_USER`, `NEO4J_PASSWORD`.

**Use cases:**

- Path finding between any two devices (server → ISP edge, DC → colocation)
- Segment-to-cloud trace: VXLAN segment → firewall (PBR) → circuits → cloud VMs
- One-hop neighbor inspection for pre-change impact analysis
- Raw Cypher for ad-hoc queries: orphaned interfaces, cabling audits, change impact

**Tools:**

- `find_physical_path(src, dst, max_hops?, all_paths?, connector_types?)` — BFS path finder
- `trace_segment_to_cloud(segment_name, max_hops?)` — segment → FW → circuits → cloud
- `get_device_neighbors(device_name, connector_types?)` — one-hop cable/circuit neighbors
- `find_reachable(source, target_kinds, max_depth?, max_results?, shortest_paths_only?)` — impact/dependency
  analysis: which nodes of the given kinds are reachable from `source` (device name, UUID, or
  kind-qualified HFID such as `DcimPhysicalDevice__dc1-fw-01`). Prefer it over hand-written Cypher for
  "what sits behind / depends on X?"
- `execute_cypher(query)` — raw read-only Cypher (MATCH/RETURN only — write ops blocked)

**Infrahub graph model (for Cypher queries):**

```
Attributes:   (node)-[:HAS_ATTRIBUTE]->(attr {name:"field"})-[:HAS_VALUE]->(val {value:...})
Device↔Iface: device + iface → IS_RELATED → Relationship{name:"device__interface"}
Cables:       cable + iface  → IS_RELATED → Relationship{name:"dcimcable__dcimendpoint"}
Circuits:     iface → IS_RELATED → Relationship{name:"physical_circuit__provider_a/z"}
```

**Critical Cypher rule:** never reuse variable names across MATCH clauses — silently collapses rows to 0.

### 7. Device Troubleshooting (SSH)

SSHes into a live (or emulated/containerlab) device and runs a read-only show command,
optionally structured with ntc-templates. This is the one place franc talks to a device
directly instead of reading Infrahub's declared state or the Neo4j graph.
Requires env vars: `SSH_USERNAME`, `SSH_PASSWORD` (no default — set explicitly).
Only works on devices with `primary_address` set in Infrahub.

**Use cases:**

- Verify live interface/BGP state against what Infrahub declares
- Ad-hoc troubleshooting commands not covered by a dedicated tool

**Tools:**

- `execute_show_command(device_name, command, branch?)` — generic escape hatch; only
  show/display/get/ping/traceroute commands are allowed
- `get_interface_status(device_name, interface?, branch?)` — live interface up/down/IP state
- `get_bgp_summary(device_name, branch?)` — live BGP neighbor state (compare against
  `get_bgp_sessions`, which reads the declared/expected topology from Infrahub instead)

---

## Server Configuration (MCP middleware)

Cross-cutting behaviour lives in `franc/mcp_middleware.py` (FastMCP hooks, so it applies to stdio and HTTP;
`franc/middleware.py` is the HTTP-only ASGI stack). All of it is controlled by environment variables:

| Variable | Default | Effect |
|---|---|---|
| `FRANC_READ_ONLY` | `false` | Hide and reject every tool tagged `write` or `create`. Fail-closed for unknown tools |
| `FRANC_AUTH_MODE` | `shared` | `token-passthrough`: each caller sends `X-INFRAHUB-KEY` or `Authorization: Bearer`; calls without a token are rejected (HTTP only) |
| `FRANC_AUDIT` | `true` | One log line per tool call on logger `franc.audit` (tool, user, status, duration, argument *names*) |
| `FRANC_METRICS` | `false` | Per-tool counters, Prometheus text at `GET /metrics` (unauthenticated — restrict at the network) |
| `FRANC_OTEL` | `false` | Span per tool call; needs `opentelemetry-api` installed, exporter configured by the host |
| `FRANC_RETRIES` | `2` | Retries on transient connection errors; **read-only tools only**, never writes. `0` disables |
| `FRANC_MAX_RESPONSE_BYTES` | `1000000` | Oversized tool responses become an error telling the agent to narrow the query. `0` disables |

**Tagging rule for new tools:** any tool that changes Infrahub state MUST carry the `write` or `create` tag, or
read-only mode will not hide it. Read tools should set `readOnlyHint=True` so they are eligible for retry.

In `token-passthrough` mode the schema caches in `franc/utils.py` are shared between callers; that is fine for
schema (not per-user in Infrahub) but do not add per-user data to those caches.

---

## Default Workflow Rules

- **Discover first**: use `get_schema_mapping` before queries; use `discover_*` tools before any create.
- **Validate inputs**: check required fields with `get_required_fields`; return remediation on failures.
- **Confirm before write**: echo the plan and get user confirmation before branch create or object create.
- **Handle auto-compression**: results with >5 items auto-compress to GCF. Look for `*_gcf` fields.
  Work with GCF directly for summaries. Only decode with `gcf_decode` if inspecting specific values.
- **Fail fast with remediation**: on empty results, restate kind/branch/filters used and suggest next lookup.
- **Use defaults**: default `branch` to `main`. Never guess kinds or filter names.
- **Refuse invented data**: if a filter/key/value is missing, ask — never improvise VNIs, CIDRs, or hostnames.

---

## Coding Standards

- Python 3.10+ with type hints on all public surfaces; keep `Annotated[...]` metadata accurate.
- Reuse utilities: `MCPResponse`/`MCPToolStatus` for returns, `_log_and_return_error` for failures,
  `convert_node_to_dict`/`extract_value` for serialization.
- Handle GraphQL cleanly: catch `SchemaNotFoundError`/`GraphQLError`, guard against missing peers.
- Keep queries bounded: avoid unfiltered bulk fetches when a key is available.
- Use `KIND_*` constants from `franc.constants` — never hardcode kind strings in tool bodies.
- Line length ≤ 120 (ruff config).

---

## Quality Gates (run in repo root)

```bash
uv sync               # ensure deps
uv run ruff format .  # format
uv run ruff check .   # lint
uv run ty check .           # type check
uv run pytest         # tests (mocks in tests/mocks/)
```

---

## Collaboration Habits

- Branch naming: `feature/<short-desc>` or `fix/<short-desc>`; commits focused and descriptive.
- Prompts: stored under `franc/prompts/`; concise, goal-driven, explicit tool order when helpful.
- When adding tools: update `README.md` samples, add entry here, and note nuances.

---

## Token Optimization

Results with >5 items are candidates for auto-compression with GCF (threshold configurable via
`GCF_AUTO_THRESHOLD_ITEMS`), but compression only happens when GCF actually wins on tokens.

- Look for `*_gcf` fields: `nodes_gcf`, `objects_details_gcf`, `devices_gcf`, `bgp_sessions_gcf`, etc.
- Decode only when you need specific values: `gcf_decode(gcf_string=...)`.
- Candidates over policy: both GCF and JSON encodings are measured in tokens; a `*_gcf` field only
  appears when GCF is strictly smaller than JSON for that data shape. Otherwise the tool returns
  plain, uncompressed data — e.g. flat lists of short unique strings often don't compress well and
  are left as-is.
- Auto-compression verifies a decode round-trip before compressing; if it fails for a given
  data shape, compression is skipped entirely rather than shipping an undecodable payload under
  a `*_gcf` key.

---

## Testing Notes

- Tests use mocked HTTPX responses in `tests/mocks/`.
- Keep call counts minimal for bulk tools to avoid mock mismatch.
- Default: `uv run pytest`. Execution must be deterministic — no live network calls.
- Before pushing: `ruff format`, `ruff check`, `ty`, then `pytest`.
