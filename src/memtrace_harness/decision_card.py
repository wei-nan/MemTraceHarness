"""The decision card: the one shape in which a stage asks the human to decide something.

A stage that stops for a human used to hand over its raw artifact (a plan, a gate's findings
list, a developer summary), and the human had to work out what was being asked. A card is the
stage's own distilled question: what is blocking, two or three concrete ways forward with their
cost, which one it would take and why, and what happens if nobody answers.

The card travels inside the stage's structured output (see output_schemas/*.json). It is
validated here rather than trusted: a missing or malformed card is dropped and the human gets
the old unstructured message, so a model that fails to produce one never blocks a stop.
"""

from __future__ import annotations

from typing import Any

MAX_OPTIONS = 3
MIN_OPTIONS = 2
_LETTERS = "ABC"

# Telegram callback_data is capped at 64 bytes; "pick:appr_<12 hex>:<n>" is 24.
PICK_CALLBACK_PREFIX = "pick"

# The instruction every stage that can stop for a human appends to its prompt. One shared
# text so the stages cannot drift apart on what a card is.
CARD_INSTRUCTION = (
    "decision_card: when you stop for a human (status needs_human, verdict NEEDS_HUMAN, "
    "action ask_human, or a REJECT that may end the loop), fill it in; otherwise null. "
    "It is the ONLY thing the human will be shown, so write it for someone who has not read "
    "your other fields: `situation` is one or two plain sentences on what is blocking; "
    "`options` is exactly 2 or 3 concrete, mutually exclusive ways forward, each with a short "
    "`label`, the `action` that would be taken if chosen, and its `tradeoff` (cost or risk); "
    "`recommended` is the 0-based index of the option you would pick and `reason` says why in "
    "one sentence; `default_if_silent` says what happens if the human does not answer; `basis` lists the "
    "ids (D<n> past decision, P<n> preference) of any operator precedent you actually relied on, "
    "[] if none or if you were shown none. Do not "
    "pad: no option that is just \"ask me more\" or \"do nothing\" unless that is a real choice."
)

CARD_SCHEMA: dict[str, Any] = {
    "type": ["object", "null"],
    "description": "How to ask the human to decide; null when not stopping for a human.",
    "properties": {
        "situation": {"type": "string", "maxLength": 600},
        "options": {
            "type": "array",
            "maxItems": MAX_OPTIONS,
            "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "string", "maxLength": 80},
                    "action": {"type": "string", "maxLength": 400},
                    "tradeoff": {"type": "string", "maxLength": 300},
                },
                "required": ["label", "action", "tradeoff"],
                "additionalProperties": False,
            },
        },
        "recommended": {"type": "integer", "minimum": 0, "maximum": MAX_OPTIONS - 1},
        "reason": {"type": "string", "maxLength": 300},
        "default_if_silent": {"type": "string", "maxLength": 200},
        "basis": {"type": "array", "maxItems": 3, "items": {"type": "string", "maxLength": 20}},
    },
    "required": ["situation", "options", "recommended", "reason", "default_if_silent", "basis"],
    "additionalProperties": False,
}


def _text(value: Any, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > limit:
        return None
    return text


def normalize_card(value: Any) -> dict[str, Any] | None:
    """The card in canonical form, or None when it is absent or not usable. Strict on shape
    (a card with one option, or a recommendation that points at nothing, is not a decision
    anyone can make) but never raises."""
    if not isinstance(value, dict):
        return None
    situation = _text(value.get("situation"), 600)
    reason = _text(value.get("reason"), 300)
    default = _text(value.get("default_if_silent"), 200)
    raw_options = value.get("options")
    if situation is None or reason is None or default is None or not isinstance(raw_options, list):
        return None
    if not MIN_OPTIONS <= len(raw_options) <= MAX_OPTIONS:
        return None
    options = []
    for raw in raw_options:
        if not isinstance(raw, dict):
            return None
        label = _text(raw.get("label"), 80)
        action = _text(raw.get("action"), 400)
        tradeoff = _text(raw.get("tradeoff"), 300)
        if label is None or action is None or tradeoff is None:
            return None
        options.append({"label": label, "action": action, "tradeoff": tradeoff})
    recommended = value.get("recommended")
    if isinstance(recommended, bool) or not isinstance(recommended, int):
        return None
    if not 0 <= recommended < len(options):
        return None
    raw_basis = value.get("basis")
    basis = (
        [b.strip() for b in raw_basis if isinstance(b, str) and b.strip()][:3]
        if isinstance(raw_basis, list)
        else []
    )
    return {
        "situation": situation,
        "options": options,
        "recommended": recommended,
        "reason": reason,
        "default_if_silent": default,
        "basis": basis,
    }


def _asks_human(artifact: dict[str, Any]) -> bool:
    """Whether the stage's own verdict says it is handing the decision to a human. A card on
    an artifact that says otherwise (a Controller that chose "merge" but filled one in anyway)
    is not about whatever stopped the loop afterwards, so it is not used."""
    return (
        artifact.get("status") == "needs_human"
        or str(artifact.get("verdict", "")).upper() in {"NEEDS_HUMAN", "REJECT"}
        or artifact.get("action") in {"ask_human", "stop"}
    )


def card_from_artifact(artifact: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(artifact, dict) or not _asks_human(artifact):
        return None
    return normalize_card(artifact.get("decision_card"))


def restrict_basis(card: dict[str, Any] | None, known_ids: set[str]) -> dict[str, Any] | None:
    """The card with `basis` cut down to ids that were really offered to the model; a card
    cannot claim a precedent it was never shown."""
    if card is None:
        return None
    return {**card, "basis": [b for b in card.get("basis", []) if b in known_ids]}


def option_letter(index: int) -> str:
    return _LETTERS[index]


def render_card(card: dict[str, Any]) -> str:
    """The human-facing body of the card, as plain text for Telegram."""
    lines = [card["situation"], ""]
    for index, option in enumerate(card["options"]):
        star = " ⭐建議" if index == card["recommended"] else ""
        lines.append(f"{option_letter(index)}. {option['label']}{star}")
        lines.append(f"   做法：{option['action']}")
        lines.append(f"   代價：{option['tradeoff']}")
    lines.append("")
    lines.append(f"建議 {option_letter(card['recommended'])}：{card['reason']}")
    if card.get("basis"):
        lines.append(f"參考：{'、'.join(card['basis'])}")
    lines.append(f"不回應的話：{card['default_if_silent']}")
    return "\n".join(lines)


def answer_for_choice(card: dict[str, Any], index: int) -> str | None:
    """The text a tapped option is resumed with: the same string an operator would have typed
    to /clarify, so the resume path needs no knowledge of cards."""
    if not 0 <= index < len(card["options"]):
        return None
    option = card["options"][index]
    return f"我選方案 {option_letter(index)}「{option['label']}」：{option['action']}"


def pick_keyboard_rows(request_id: str, card: dict[str, Any]) -> list[list[dict[str, str]]]:
    rows = []
    for index, option in enumerate(card["options"]):
        star = "⭐ " if index == card["recommended"] else ""
        rows.append(
            [
                {
                    "text": f"{star}{option_letter(index)}. {option['label']}"[:60],
                    "callback_data": f"{PICK_CALLBACK_PREFIX}:{request_id}:{index}",
                }
            ]
        )
    return rows


def parse_pick_callback(data: str) -> tuple[str, int] | None:
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != PICK_CALLBACK_PREFIX:
        return None
    try:
        return parts[1], int(parts[2])
    except ValueError:
        return None
