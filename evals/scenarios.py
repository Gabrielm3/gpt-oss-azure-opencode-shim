"""Scenarios for the forced tool_choice evaluation.

Each scenario is one Chat Completions request that forces a tool call, plus the
tool the answer must call. ``expect_tool=None`` accepts any declared tool.
The mix covers the three situations seen with OpenCode and other clients:

- final-answer steps: the conversation already holds tool results and the
  client requires a structured answer (OpenCode structured output);
- single-turn extraction into a required schema;
- a forced function on the first turn.

``expect_args`` holds golden values for the fields with exactly one right
answer (see ``evals/golden.py``). Triage severity, ticket priority and
free-text fields are left out on purpose.
"""

from __future__ import annotations

import json
from typing import Any


def _tool(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required},
        },
    }


def _structured(properties: dict[str, Any], required: list[str]) -> dict:
    return _tool(
        "StructuredOutput",
        "Use this tool to return your final response in the requested structured format.",
        properties,
        required,
    )


GLOB = _tool(
    "glob",
    "Find files by glob pattern.",
    {"pattern": {"type": "string"}, "path": {"type": "string"}},
    ["pattern"],
)
READ = _tool("read", "Read a file.", {"filePath": {"type": "string"}}, ["filePath"])
BASH = _tool(
    "bash",
    "Run a shell command.",
    {"command": {"type": "string"}, "description": {"type": "string"}},
    ["command"],
)

SYSTEM = "You are a coding agent. Use the provided tools."


def _after_tool(user: str, call: str, arguments: dict, result: str) -> list[dict]:
    """Messages for a final-answer step: user asked, a tool ran, its result came back."""
    call_id = "call_prev"
    return [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": user},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": call, "arguments": json.dumps(arguments)},
                }
            ],
        },
        {"role": "tool", "tool_call_id": call_id, "content": result},
    ]


def _single(user: str) -> list[dict]:
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]


FILES = _structured({"files": {"type": "array", "items": {"type": "string"}}}, ["files"])
SENTIMENT = _structured(
    {
        "label": {"type": "string", "enum": ["positive", "negative", "neutral"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    ["label", "confidence"],
)
PERSON = _structured(
    {"name": {"type": "string"}, "age": {"type": "integer"}, "city": {"type": "string"}},
    ["name", "age"],
)
TRIAGE = _structured(
    {
        "severity": {"type": "string", "enum": ["low", "medium", "high", "critical"]},
        "component": {"type": "string"},
        "summary": {"type": "string"},
    },
    ["severity", "component", "summary"],
)
DEPS = _structured(
    {
        "dependencies": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"name": {"type": "string"}, "version": {"type": "string"}},
                "required": ["name"],
            },
        }
    },
    ["dependencies"],
)
TODO = _structured(
    {"count": {"type": "integer"}, "files": {"type": "array", "items": {"type": "string"}}},
    ["count", "files"],
)

WEATHER = _tool(
    "get_weather",
    "Get the current weather for a city.",
    {"city": {"type": "string"}, "unit": {"type": "string", "enum": ["celsius", "fahrenheit"]}},
    ["city"],
)
TICKET = _tool(
    "create_ticket",
    "Create an issue ticket.",
    {
        "title": {"type": "string"},
        "priority": {"type": "string", "enum": ["p0", "p1", "p2", "p3"]},
    },
    ["title", "priority"],
)
SEARCH = _tool("search_docs", "Search the documentation.", {"query": {"type": "string"}}, ["query"])
CURRENCY = _tool(
    "convert_currency",
    "Convert an amount between currencies.",
    {"amount": {"type": "number"}, "from": {"type": "string"}, "to": {"type": "string"}},
    ["amount", "from", "to"],
)


def _forced(name: str) -> dict:
    return {"type": "function", "function": {"name": name}}


SCENARIOS: list[dict[str, Any]] = [
    # Final-answer steps, as OpenCode sends them for structured output.
    {
        "id": "final-files",
        "messages": _after_tool(
            "Which markdown files are in this directory?",
            "glob",
            {"pattern": "**/*.md"},
            "/repo/README.md\n/repo/docs/PROBLEM.md",
        ),
        "tools": [GLOB, READ, FILES],
        "tool_choice": "required",
        "expect_tool": "StructuredOutput",
        "expect_args": {"files": ("files", ["README.md", "PROBLEM.md"])},
    },
    {
        "id": "final-todo-count",
        "messages": _after_tool(
            "How many TODO comments are in the source tree, and in which files?",
            "bash",
            {"command": "grep -rn TODO src"},
            "src/app.py:12: # TODO retry\nsrc/app.py:40: # TODO cache\nsrc/db.py:7: # TODO index",
        ),
        "tools": [GLOB, READ, BASH, TODO],
        "tool_choice": "required",
        "expect_tool": "StructuredOutput",
        "expect_args": {"count": ("exact", 3), "files": ("files", ["app.py", "db.py"])},
        "stream": True,
    },
    {
        "id": "final-deps",
        "messages": _after_tool(
            "List the runtime dependencies of this project.",
            "read",
            {"filePath": "pyproject.toml"},
            'dependencies = [\n  "fastapi>=0.110",\n  "httpx>=0.27",\n  "jsonschema>=4.21",\n]',
        ),
        "tools": [GLOB, READ, DEPS],
        "tool_choice": "required",
        "expect_tool": "StructuredOutput",
        "expect_args": {"dependencies": ("names", ["fastapi", "httpx", "jsonschema"])},
    },
    {
        "id": "final-triage",
        "messages": _after_tool(
            "Triage the failing test from the CI log.",
            "read",
            {"filePath": "ci.log"},
            "FAILED tests/test_auth.py::test_login - KeyError: 'session' (all logins fail)",
        ),
        "tools": [READ, BASH, TRIAGE],
        "tool_choice": "required",
        "expect_tool": "StructuredOutput",
        "stream": True,
    },
    # Single-turn extraction into a required schema.
    {
        "id": "extract-sentiment",
        "messages": _single(
            "Classify the sentiment of this review: 'The update broke my build twice. Not happy.'"
        ),
        "tools": [READ, SENTIMENT],
        "tool_choice": "required",
        "expect_tool": "StructuredOutput",
        "expect_args": {"label": ("exact", "negative")},
    },
    {
        "id": "extract-person",
        "messages": _single("Extract the person: 'Maria Souza, 34, moved to Recife last year.'"),
        "tools": [READ, PERSON],
        "tool_choice": "required",
        "expect_tool": "StructuredOutput",
        "expect_args": {
            "name": ("text", "Maria Souza"),
            "age": ("exact", 34),
            "city": ("text", "Recife"),
        },
        "stream": True,
    },
    {
        "id": "extract-triage",
        "messages": _single(
            "Triage this bug report: 'Checkout page returns 500 for every user since the last deploy.'"
        ),
        "tools": [BASH, TRIAGE],
        "tool_choice": "required",
        "expect_tool": "StructuredOutput",
    },
    {
        "id": "extract-files-inline",
        "messages": _single(
            "Return the Python files from this listing: README.md, app.py, db.py, notes.txt"
        ),
        "tools": [GLOB, FILES],
        "tool_choice": "required",
        "expect_tool": "StructuredOutput",
        "expect_args": {"files": ("files", ["app.py", "db.py"])},
        "stream": True,
    },
    # Forced function on the first turn.
    {
        "id": "forced-weather",
        "messages": _single("What's the weather like in Lisbon?"),
        "tools": [WEATHER, SEARCH],
        "tool_choice": _forced("get_weather"),
        "expect_tool": "get_weather",
        "expect_args": {"city": ("text", "Lisbon")},
    },
    {
        "id": "forced-ticket",
        "messages": _single("Open a high-priority ticket: login is broken for all users."),
        "tools": [TICKET, SEARCH],
        "tool_choice": _forced("create_ticket"),
        "expect_tool": "create_ticket",
        "stream": True,
    },
    {
        "id": "forced-search",
        "messages": _single("How do I configure the read timeout?"),
        "tools": [SEARCH, TICKET],
        "tool_choice": _forced("search_docs"),
        "expect_tool": "search_docs",
    },
    {
        "id": "forced-currency",
        "messages": _single("How much is 250 US dollars in euros?"),
        "tools": [CURRENCY, SEARCH],
        "tool_choice": _forced("convert_currency"),
        "expect_tool": "convert_currency",
        "expect_args": {
            "amount": ("exact", 250),
            "from": ("oneof", ["USD", "US dollars", "US dollar"]),
            "to": ("oneof", ["EUR", "euros", "euro"]),
        },
        "stream": True,
    },
    # "required" where any action tool is a valid answer.
    {
        "id": "required-action-files",
        "messages": _single("Find all YAML files in the repository."),
        "tools": [GLOB, READ, BASH],
        "tool_choice": "required",
        "expect_tool": None,
    },
    {
        "id": "required-action-read",
        "messages": _single("Show me the contents of setup.cfg."),
        "tools": [GLOB, READ, BASH],
        "tool_choice": "required",
        "expect_tool": None,
        "stream": True,
    },
    {
        "id": "required-answer-known",
        "messages": _single("What is 17 * 23? Answer through the structured output tool."),
        "tools": [BASH, _structured({"result": {"type": "integer"}}, ["result"])],
        "tool_choice": "required",
        "expect_tool": "StructuredOutput",
        "expect_args": {"result": ("exact", 391)},
    },
]
