# Agent Guide

This guide keeps all automation agents aligned on workflow, quality gates, and prompting practices for this repo.

## Default Workflow

- Clarify intent and safety: restate the user goal, confirm target branch and any destructive actions before proceeding.
- Discover before acting: use `get_schema_mapping` to list kinds, `get_node_filters` for filter keys, then `get_nodes`/`get_object_details`/`get_related_nodes` as needed.
- Validate inputs: ensure required filters (IDs/HFIDs/names) exist before tool calls; return remediation via `_log_and_return_error` when inputs are incomplete.
- Minimize calls: favor existing store data and include lists only when required; prefer display labels and fall back to IDs.
- **Handle auto-compressed results**: Results with >10 items are automatically compressed with TOON format. Look for `*_toon` fields (e.g., `nodes_toon`, `objects_toon`) in responses. These contain the full data in TOON format - you can work with them directly for summaries or pass to other tools. Only use `toon_decode` if you need to inspect specific values.
- Log clearly: use `ctx.info` for progress, `ctx.debug` for details, and include actionable remediation text on failures.
- Use defaults consistently: default `branch` to `main` when not provided; never guess kinds or filter names.
- Refuse to invent data: if a filter/key/value is missing, return remediation that asks for it instead of improvising.
- Keep results lean: prefer summaries with optional details sections; avoid returning unbounded lists unless the user asked for them.

## Coding Standards

- Python 3.10+ with type hints on all public surfaces; keep `Annotated[...]` parameter metadata accurate for tools.
- Reuse utilities: `MCPResponse`/`MCPToolStatus` for structured returns, `_log_and_return_error` for failures, `convert_node_to_dict`/`extract_value` for serialization.
- Handle GraphQL cleanly: catch `SchemaNotFoundError`/`GraphQLError`, guard against missing peers, and avoid broad exceptions unless returning standardized errors.
- Keep queries bounded: avoid unfiltered bulk fetches when a key is available; disable parallelism/prefetch only when tests require specific HTTP patterns.
- Prefer small, purposeful comments; keep line length ≤120 (ruff config).

## Quality Gates (run in repo root)

- `uv sync` (ensure deps present)
- `uv run ruff format .` (formatting)
- `uv run ruff check .` (lint)
- `uv run ty .` (type check)
- `uv run pytest` (tests; mocks live in tests/mocks)

## Collaboration Habits

- Branch naming: `feature/<short-desc>` or `fix/<short-desc>`; keep commits focused and descriptive.
- Touching prompts: store under `franc/prompts/`; keep wording concise, goal-driven, and include explicit tool order when helpful.
- Update docs: when adding tools or changing behaviors, refresh `README.md` samples and note nuances in this file.
- For data-center deployments, follow the prompt in `franc/prompts/datacenter_flow.md` to gather required inputs, propose defaults, and orchestrate branch + deployment tools without guessing values.

## Prompting Best Practices

- Instruct agents to: (1) discover schemas/filters before fetching objects, (2) confirm required fields before create/update, (3) ask before delete, (4) present remediation alongside errors.
- Encourage concise intermediate summaries and numbered plans so downstream agents can pick up state quickly.
- **Token optimization**: Results with >10 items are auto-compressed with TOON. Agents receive `*_toon` fields with compression stats. Work with TOON format directly for efficiency - decode only when inspecting specific values.
- Provide example inputs/outputs when adding new tools to reduce ambiguity for other agents.
- Add guardrails in prompts: “Use returned filter keys verbatim; do not invent fields; ask for missing filters.”
- When requests are overly broad (e.g., unfiltered `get_nodes`), warn and suggest a narrowing filter before proceeding.
- Echo executed parameters in `ctx.info` (kind, branch, filters) so users see what actually ran.
- Prefer short, explicit step lists in prompts for orchestrations (ask → confirm → act), especially for multi-step flows like data-center deployment.

## Infrahub MCP quick actions (shared)

- Discover options first: run `discover_datacenter_options` to list metros, designs, strategies, providers; reuse the results during the session instead of re-asking.
- Gather creation inputs upfront: always collect `site_name`, `metro_location`, `design`, `strategy`, `provider`; if the schema requires subnets, also collect `management_subnet`, `customer_subnet`, `technical_subnet`; keep `branch_name` and `allocate_prefix_nodes` optional.
- Use the DC helper: prefer a single `create_datacenter_deployment` call with the collected inputs; only fall back to piecemeal node creation if the helper fails.
- Retrieval rules: for lists, use `get_nodes`; for specific objects, use explicit filters with `get_related_nodes`/`get_node_filters`; remember naming may differ across kinds (e.g., `TopologyDataCenter` vs pods/deployments).
- Summaries: report concise counts (pods, devices per pod), pools, racks, and status using bullets or small tables; avoid full record dumps unless requested.
- Fail fast with remediation: when lookups return empty, restate kind/branch/filters used and suggest the next lookup (e.g., list via `get_nodes` or search by index).

## Testing Notes

- Tests rely on mocked HTTPX responses; keep call counts minimal for `get_objects`/`get_object_details` to avoid mismatch.
- When introducing new GraphQL queries, prefer parameterized filters and ensure they align with available mock fixtures.
- Default test command: `uv run pytest` (or `uv run pytest tests` for speed). Keep execution deterministic—avoid network calls in tests.
- Before pushing, run `uv run ruff format .`, `uv run ruff check .`, and `uv run ty .` in addition to pytest.
