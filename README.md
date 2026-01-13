
# Infrahub MCP LLM/AI Agent Context

[![Ruff][ruff-badge]][ruff-link]
[![Python][python-badge]][python-link]
[![Actions status][github-badge]][github-link]
[![Coverage Status][coverage-badge]][coverage-link]
[![GitHub Discussion][github-discussions-badge]][github-discussions-link]

## Project Overview

This project provides an MCP (Model Context Protocol) server for Infrahub, enabling LLM/AI agents to discover schemas, query objects, manage branches, and orchestrate data-center deployments. It includes schema-driven discovery, datacenter deployment helpers, and test automation with robust mocking.

### Key Features

- **Schema discovery & validation:** Tools to list schema nodes, retrieve filters, and validate required fields before mutations.
- **Datacenter deployment orchestration:** Discover design/strategy/provider options, then create full deployments with `create_datacenter_deployment`.
- **Branch management:** Create and list branches for isolated deployments and experiments.
- **Node retrieval:** Query nodes by kind/filters, fetch related nodes, and navigate object graphs.
- **Test automation:** Pytest fixtures with file-based mocks for deterministic, isolated unit tests.
- **Agent guidance:** Shared best practices in `AGENTS.md` and `.github/agents/infrahub.agent.md` for consistent agent behavior.

## Available Tools

### Schema & Discovery

- `get_schema_mapping`: List all schema node kinds and generics.
- `get_schema`: Retrieve full schema (attributes, relationships) for a specific kind.
- `get_schemas`: Retrieve all schemas (optionally exclude Profiles/Templates).
- `get_node_filters`: List valid filter keys for a kind.
- `get_required_fields`: List required attribute fields for object creation.

### Node Retrieval

- `get_nodes`: Retrieve all objects of a specific kind with optional filters.
- `get_related_nodes`: Fetch related nodes for a specific object and relationship.

### Branch Management

- `branch_create`: Create a new branch with optional git sync.
- `get_branches`: List all branches.

### Datacenter Deployment

- `discover_datacenter_options`: List available metros, designs, strategies, and providers.
- `create_datacenter_deployment`: Create a data-center topology on an isolated branch.
- `validate_datacenter_deployment`: Validate that a deployment exists on a branch.

### GraphQL

- `get_graphql_schema`: Retrieve the complete GraphQL schema.
- `query_graphql`: Execute arbitrary GraphQL queries.

## Sample Workflow: Datacenter Deployment

```text

User: "Deploy a new datacenter in Berlin."

Agent:
1. discover_datacenter_options
   → returns metros: ["BERLIN", "MUNICH", ...],
            designs: ["S-Standard", "M-Standard", "L-Hierarchical", ...],
            strategies: ["ebgp-evpn", "isis-ibgp", ...],
            providers: ["Internal", "Technology Partner", ...]

2. Collect inputs from user:
   - site_name: "DC-BER-1"
   - metro_location: "BERLIN"
   - design: "M-Standard"
   - strategy: "ebgp-evpn"
   - provider: "Internal"

3. create_datacenter_deployment(
     site_name="DC-BER-1",
     metro_location="BERLIN",
     design="M-Standard",
     strategy="ebgp-evpn",
     provider="Internal"
   )
   → returns branch name, topology summary, status

4. validate_datacenter_deployment(branch="dc-deploy-dc-ber-1-...", site_name="DC-BER-1")
   → confirms topology exists with expected attributes
```

## Best Practices

For detailed agent guidance, see [AGENTS.md](AGENTS.md) and [.github/agents/infrahub.agent.md](.github/agents/infrahub.agent.md).

**Quick summary:**

- **Discover first:** Use `get_schema_mapping`/`get_node_filters`/`discover_datacenter_options` before prompting users.
- **Validate inputs:** Check required fields with `get_required_fields` before mutations.
- **Fail fast:** When queries return empty, restate kind/branch/filters and suggest remediation.
- **Concise summaries:** Return counts and status in bullets/tables; avoid full object dumps unless requested.
- **Datacenter flow:** Discover options → collect all inputs → call `create_datacenter_deployment` once.
- **Branch naming:** Auto-generate branch names when not provided; avoid guessing values.

[ruff-badge]:
<https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json>
[ruff-link]:
(https://github.com/astral-sh/ruff)
[github-discussions-link]:
<https://github.com/t0m3kz/franc-mcp/discussions/>
[github-discussions-badge]:
<https://img.shields.io/static/v1?label=Discussions&message=Ask&color=blue&logo=github>
[github-badge]:
<https://github.com/t0m3kz/franc-mcp/actions/workflows/main.yml/badge.svg?branch=main>
[github-link]:
<https://github.com/t0m3kz/franc-mcp/actions/workflows/main.yml>
[coverage-badge]:
https://img.shields.io/codecov/c/github/t0m3kz/franc?label=coverage
[coverage-link]:
https://codecov.io/gh/t0m3kz/franc-mcp
[python-badge]:
<https://img.shields.io/badge/python-3.10%7C3.11%7C3.12%7C3.13-000000?logo=python>
[python-link]:
<https://www.python.org>
