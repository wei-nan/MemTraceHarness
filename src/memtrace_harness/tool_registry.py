"""The one place a chat-model tool declares how much trust it needs (operator decision, 2026-10-12).

Every tool the chat model can call through the ops server is a ``ToolSpec`` with a **tier**:

- ``read``    — looks, changes nothing; runs at once, no confirm tap (files, databases, launchd, a web page).
- ``propose`` — only records a request for something else (a job, a GitHub action, a setting); that request has its
  own confirm card (service.py) and may be put on the allowed list.
- ``change``  — would change something itself; the model's call is turned into a ``tool_call`` request, the user sees
  the exact tool and arguments on a confirm card, and the handler runs only after the tap. It may be put on the allowed
  list ("確認並之後都允許"), bound to ``rule`` (default: the exact tool and arguments).

**``change`` is the default tier**: a tool registered without saying anything about its tier is asked about, never run
silently. A tool that is not registered cannot be called at all. Commands the command policy forbids (sudo, deletion,
system or security settings) are not a tier — no tool, tap or allowlist entry can run them (command_policy.py).

Adding a tool is adding one ``register(ToolSpec(...))``; the confirm card, expiry, double-tap guard, allowed list,
notification and the contract's wording do not change.
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
