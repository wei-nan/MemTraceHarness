from __future__ import annotations

from pathlib import Path


PROFILE_SCHEMAS = {
    "controller": "controller.json",
    "planner": "plan.json",
    "planner-escalation": "plan.json",
    "red-team": "gate.json",
    "developer": "development.json",
}


def output_schema_path(profile_id: str) -> Path:
    try:
        filename = PROFILE_SCHEMAS[profile_id]
    except KeyError as exc:
        raise ValueError(f"No output schema for role profile: {profile_id}") from exc
    path = Path(__file__).with_name("output_schemas") / filename
    if not path.is_file():
        raise RuntimeError(f"Packaged output schema is missing: {path}")
    return path
