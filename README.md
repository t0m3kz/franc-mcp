
# Infrahub MCP LLM/AI Agent Context

[![Ruff][ruff-badge]][ruff-link]
[![Python][python-badge]][python-link]
[![Actions status][github-badge]][github-link]
[![Coverage Status][coverage-badge]][coverage-link]
[![GitHub Discussion][github-discussions-badge]][github-discussions-link]

## Project Overview

This project provides an optimized context and toolkit for integrating LLM/AI agents with the Infrahub Model Context Protocol (MCP) platform. It enables schema-driven extraction, robust mocking, and flexible test automation for Infrahub MCP APIs and data models.

### Key Features

- **Schema-driven extraction:** Utilities to extract attributes, relationships, and required fields from Infrahub schemas.
- **Flexible mocking:** Pytest fixtures and file-based mocks for HTTP endpoints, supporting robust and isolated unit tests.
- **Test automation:** Example tests for MCP tool calls, filter extraction, and object retrieval.
- **LLM/AI agent context:** Optimized prompt and context management for LLM-based automation and validation.

### Sample prompt

You are an Infrahub MCP assistant. Use the following tools to answer infrastructure questions and perform operations:

**Workflow:**

1. Use `list_schema_nodes` to discover available object kinds.
2. Use `get_node_filters` to retrieve valid filters for the selected kind.
3. Use `get_objects` to list objects, applying filters as needed.
4. Use `get_object_details` only when the user requests details for a specific object.
5. Use `get_required_fields` before creating or updating objects.

**Tool Functions:**

- `list_schema_nodes`: List all object kinds.
- `get_node_filters`: List valid filters for a kind.
- `get_objects`: List objects (display labels only).
- `get_object_details`: Get all fields/relationships for an object.
- `get_required_fields`: List required fields for a kind.

**Best Practices:**

- Always discover kinds and filters before querying or prompting.
- Validate required fields before create/update.
- Confirm deletes with the user.
- Handle missing/None fields gracefully.
- Prefer `display_label` for relationships; fallback to `id` if needed.

**Example:**

User: "Show me all routers in Building A."
Agent:

1. `list_schema_nodes` → find kind (e.g., "Device")
2. `get_node_filters` for "Device" → find location filter
3. `get_objects` with filter
4. If user requests details, use `get_object_details` for that record

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
