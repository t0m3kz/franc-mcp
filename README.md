
# Infrahub MCP LLM/AI Agent Context (Optimized)

## Project Overview

This project provides an optimized context and toolkit for integrating LLM/AI agents with the Infrahub Model Context Protocol (MCP) platform. It enables schema-driven extraction, robust mocking, and flexible test automation for Infrahub MCP APIs and data models.

### Key Features
- **Schema-driven extraction:** Utilities to extract attributes, relationships, and required fields from Infrahub schemas.
- **Flexible mocking:** Pytest fixtures and file-based mocks for HTTP endpoints, supporting robust and isolated unit tests.
- **Async and sync support:** Works with both async and sync InfrahubClient usage patterns.
- **Test automation:** Example tests for MCP tool calls, filter extraction, and object retrieval.
- **LLM/AI agent context:** Optimized prompt and context management for LLM-based automation and validation.

### Usage
- Use the provided fixtures and mocks to test MCP tool integrations.
- Extract and validate schema fields and relationships for dynamic UI or LLM-driven workflows.
- Run the `.dev/a.py` script for schema and object extraction examples.

### Technologies
- Python, pytest, pytest-asyncio, pytest-httpx
- Infrahub MCP, InfrahubClient SDK
- FastMCP, LLM/AI agent integration

### Sample prompt
**LLM-Optimized Sample Prompt**

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
- `create_object`: Create a new object (all required fields needed).
- `update_object`: Update an object (only provided fields updated).
- `delete_object`: Delete an object (permanent).

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



