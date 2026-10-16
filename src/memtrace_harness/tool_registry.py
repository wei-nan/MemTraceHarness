"""Registry of the primary agent's tools and their authority tiers.

read runs immediately; propose records a request; change creates a single-use
confirmation and runs its handler only after that tap. An unregistered tool or
unknown tier is refused. change is the default. Commands forbidden by the
command policy remain forbidden regardless of confirmation.

Rule/always_note metadata remains readable for historical audit and migration;
the runtime never treats it or a stored allowlist as permission to execute.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

class OpsError(RuntimeError):
    """A tool call that is refused or fails; the model is shown the text."""


READ, PROPOSE, CHANGE = "read", "propose", "change"
TIERS = (READ, PROPOSE, CHANGE)


@dataclass(frozen=True)
class ToolSpec:
    name: str
    schema: dict[str, Any]  # the MCP tool entry: name, description, inputSchema
    handler: Callable[[Any, dict[str, Any]], str]  # (OpsContext, arguments) -> text
    tier: str = CHANGE
    available: Callable[[Any], bool] | None = None  # (OpsContext) -> is it offered here; None = always
    validate: Callable[[Any, dict[str, Any]], dict[str, Any]] | None = None  # change tier: normalise or raise ValueError
    summarize: Callable[[dict[str, Any]], str] | None = None  # change tier: one line for the confirm card
    rule: Callable[[dict[str, Any]], tuple[str, str] | None] | None = None  # change tier: what "always allow" covers
    always_note: str | None = None  # change tier: the card's words for what "always allow" covers (default: same arguments)


_REGISTRY: dict[str, ToolSpec] = {}


def register(spec: ToolSpec) -> ToolSpec:
    if spec.tier not in TIERS:
        raise ValueError(f"tool {spec.name!r} has unknown tier {spec.tier!r} (use one of {', '.join(TIERS)})")
    if spec.schema.get("name") != spec.name:
        raise ValueError(f"tool {spec.name!r}: schema name {spec.schema.get('name')!r} does not match")
    if spec.name in _REGISTRY:
        raise ValueError(f"tool {spec.name!r} is already registered")
    _REGISTRY[spec.name] = spec
    return spec


def get(name: str) -> ToolSpec | None:
    return _REGISTRY.get(name)


def all_specs() -> list[ToolSpec]:
    return list(_REGISTRY.values())


def unregister(name: str) -> None:
    """For tests that register a throw-away tool."""
    _REGISTRY.pop(name, None)
