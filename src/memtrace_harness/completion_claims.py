"""The Controller maintains the knowledge base; the operator accepts what it claims is done.

At the end of a governed run the converge-stage Controller may *propose* updates to the
knowledge base (`kb_updates` in its output). The harness validates and applies them — the model
never calls MemTrace's write tools itself — so every write is checked, logged and tied to the
run that justified it, and works the same whichever vendor the Controller runs on.

Two kinds of proposal:

* `claim_done` — declare that the task a node stands for is finished. The harness records a
  separate *completion claim* node (evidence, run id) linked to the original, and moves the
  original from `status:open` to `status:claimed-done`. The original node's body is never edited:
  task nodes carry machine-read JSON.
* `note` — record a piece of knowledge (a fact, a procedure, a decision, a lesson), optionally
  linked to existing nodes.

A claim is not an acceptance. It stays `claimed` until the operator answers: accepting moves the
node to `status:done` and resolves it; rejecting reopens it. The rejection is what the
Controller learns from — it is kept as a decision (see decision_records.py), listed in the
precedent the Controller reads next time, marked as a completion that was sent back. Silence
accepts nothing, and a claim has no deadline.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from memtrace_harness.memtrace_client import MemTraceClient

logger = logging.getLogger(__name__)

MAX_UPDATES = 3
MAX_CONSIDERED = 10
OPS = ("claim_done", "note")
NOTE_CONTENT_TYPES = ("factual", "procedural", "context", "inquiry", "preference")
LINK_RELATIONS = (
    "related_to", "extends", "extracted_from", "depends_on", "contradicts", "superseded_by", "answered_by",
)

STATUS_OPEN = "status:open"
STATUS_CLAIMED = "status:claimed-done"
STATUS_DONE = "status:done"
_STATUS_TAGS = {STATUS_OPEN, STATUS_CLAIMED, STATUS_DONE}
CLAIM_NODE_TAGS = ["harness", "controller", "completion-claim"]
NOTE_NODE_TAGS = ["harness", "controller", "note"]

CLAIM_OK = "claim_ok"
CLAIM_NO = "claim_no"

KB_UPDATES_SCHEMA: dict[str, Any] = {
    "type": ["array", "null"],
    "description": "Updates to the knowledge base the harness should apply; null when none.",
    "maxItems": MAX_UPDATES,
    "items": {
        "type": "object",
        "properties": {
            "op": {"type": "string", "enum": list(OPS)},
            "node_id": {"type": "string", "maxLength": 64},
            "title": {"type": "string", "maxLength": 120},
            "body": {"type": "string", "maxLength": 4000},
            "content_type": {"type": "string", "enum": list(NOTE_CONTENT_TYPES)},
            "links": {
                "type": "array",
                "maxItems": 5,
                "items": {
                    "type": "object",
                    "properties": {
                        "to_id": {"type": "string", "maxLength": 64},
                        "relation": {"type": "string", "enum": list(LINK_RELATIONS)},
                    },
                    "required": ["to_id", "relation"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["op", "node_id", "title", "body", "content_type", "links"],
        "additionalProperties": False,
    },
}

KB_UPDATES_INSTRUCTION = (
    "kb_updates: this is the converge stage, and you maintain this project's knowledge base. "
    "You do not call any write tool; list what should be recorded and the harness applies it. "
    "Use op \"claim_done\" (node_id = the node of the task this run finished, taken from the "
    "context — never invented; body = one or two sentences on what was done and the evidence) to "
    "declare the task finished: the operator still has to accept it, and if they send it back "
    "that is recorded and you will see it next time, so claim only what the evidence supports. "
    "Use op \"note\" (title, body, content_type; links to existing node ids) to record knowledge "
    "this run produced that is worth keeping: a decision and why, a fact, a procedure, or a "
    "lesson about how work in this project goes. For fields an op does not use, give \"\" "
    "(content_type: \"factual\") and []. At most 3 items; null when there is nothing worth "
    "recording. At the start stage kb_updates must be null."
)


def normalize_kb_updates(raw: Any) -> list[dict[str, Any]]:
    """The usable proposals out of a Controller's `kb_updates`; anything malformed is dropped
    individually, never raised."""
    if not isinstance(raw, list):
        return []
    ops: list[dict[str, Any]] = []
    # Malformed items do not use up the allowance: only usable proposals count toward it.
    for item in raw[:MAX_CONSIDERED]:
        if len(ops) >= MAX_UPDATES:
            break
        if not isinstance(item, dict) or item.get("op") not in OPS:
            continue
        body = str(item.get("body") or "").strip()
        if not body:
            continue
        links = []
        for link in item.get("links") if isinstance(item.get("links"), list) else []:
            if (
                isinstance(link, dict)
                and link.get("relation") in LINK_RELATIONS
                and str(link.get("to_id") or "").startswith("mem_")
            ):
                links.append({"to_id": str(link["to_id"]), "relation": link["relation"]})
        if item["op"] == "claim_done":
            node_id = str(item.get("node_id") or "").strip()
            if not node_id.startswith("mem_"):
                continue
            ops.append({"op": "claim_done", "node_id": node_id, "body": body[:4000], "links": links})
        else:
            title = str(item.get("title") or "").strip()
            content_type = item.get("content_type")
            if not title:
                continue
            ops.append(
                {
                    "op": "note",
                    "title": title[:120],
                    "body": body[:4000],
                    "content_type": content_type if content_type in NOTE_CONTENT_TYPES else "factual",
                    "links": links,
                }
            )
    return ops


CHAT_NOTE_MARKER = "HARNESS_KB_NOTE::"
CHAT_NOTE_TAGS = ["harness", "chat", "note"]


def chat_notes_to_ops(payloads: list[str]) -> list[dict[str, Any]]:
    """`HARNESS_KB_NOTE::<content_type>::<title>::<body>` lines from a chat reply, as `note`
    ops. The chat model holds no write tool; like the Controller it proposes and the Harness
    validates and writes (same cap, same content types)."""
    raw = []
    for payload in payloads:
        parts = payload.split("::", 2)
        if len(parts) == 3:
            raw.append({"op": "note", "content_type": parts[0].strip(), "title": parts[1], "body": parts[2], "links": []})
    return normalize_kb_updates(raw)


def kb_updates_from_artifact(artifact: dict[str, Any] | None) -> list[dict[str, Any]]:
    return normalize_kb_updates((artifact or {}).get("kb_updates"))


def _with_status(tags: list[str], status: str) -> list[str]:
    return [t for t in tags if t not in _STATUS_TAGS] + [status]


def _set_status(
    client: MemTraceClient, workspace_id: str, node_id: str, status: str,
    *, resolution: str | None, stage: str, run_id: str | None,
) -> bool:
    """Move a node to a status tag. Tags are only rewritten when the node's current tags could
    be read: replacing a list we could not see would wipe it."""
    node = client.get_node(workspace_id=workspace_id, node_id=node_id, run_id=run_id, stage=stage)
    tags = node.get("tags")
    if not isinstance(tags, list):
        return False
    client.update_node(
        workspace_id=workspace_id,
        node_id=node_id,
        tags=_with_status([str(t) for t in tags], status),
        resolution_status=resolution,
        run_id=run_id,
        stage=stage,
    )
    return True


def apply_kb_updates(
    client: MemTraceClient,
    *,
    workspace_id: str,
    ops: list[dict[str, Any]],
    claim_id: str,
    conversation_id: str,
    claim_summary: str,
    origin: str = "Controller",
    note_tags: list[str] | None = None,
) -> dict[str, Any]:
    """Apply validated proposals. Returns {"lines": [...human-readable results...], "node_id":
    the node claimed done or None, "claim_node_id": the completion node or None}. A failed op is
    reported and skipped; it never stops the others or raises."""
    result: dict[str, Any] = {"lines": [], "node_id": None, "claim_node_id": None}
    for op in ops:
        try:
            if op["op"] == "claim_done":
                origin = client.get_node(
                    workspace_id=workspace_id, node_id=op["node_id"], run_id=conversation_id,
                    stage="controller_kb_update",
                )
                title = str(origin.get("title") or op["node_id"])
                claim_node = client.create_node(
                    workspace_id=workspace_id,
                    title=f"完成宣告：{title}"[:120],
                    body=(
                        f"{op['body']}\n\n---\n宣告編號 {claim_id}；對話 {conversation_id}。\n"
                        f"任務摘要：{claim_summary}\n狀態：待驗收（尚未被接受）。"
                    ),
                    content_type="factual",
                    tags=[*CLAIM_NODE_TAGS, STATUS_CLAIMED],
                    force_create=True,
                    run_id=conversation_id,
                    stage="controller_kb_update",
                )
                client.create_edge(
                    workspace_id=workspace_id, from_id=claim_node, to_id=op["node_id"],
                    relation="extends", run_id=conversation_id, stage="controller_kb_update",
                )
                moved = _set_status(
                    client, workspace_id, op["node_id"], STATUS_CLAIMED, resolution=None,
                    stage="controller_kb_update", run_id=conversation_id,
                )
                result["node_id"], result["claim_node_id"] = op["node_id"], claim_node
                result["lines"].append(
                    f"已在知識庫記下完成宣告（{claim_node}），"
                    + (f"「{title}」改為待驗收" if moved else f"「{title}」的標籤讀不到，狀態未改")
                )
            else:
                node = client.create_node(
                    workspace_id=workspace_id,
                    title=op["title"],
                    body=f"{op['body']}\n\n---\n來源：對話 {conversation_id}（{origin} 整理）。",
                    content_type=op["content_type"],
                    tags=list(note_tags or NOTE_NODE_TAGS),
                    run_id=conversation_id,
                    stage="controller_kb_update",
                )
                for link in op["links"]:
                    client.create_edge(
                        workspace_id=workspace_id, from_id=node, to_id=link["to_id"],
                        relation=link["relation"], run_id=conversation_id, stage="controller_kb_update",
                    )
                result["lines"].append(f"已記下知識「{op['title']}」（{node}）")
        except Exception as exc:
            logger.exception("applying a Controller knowledge-base update failed")
            what = op.get("node_id") or op.get("title") or op["op"]
            result["lines"].append(f"知識庫更新沒有完成（{op['op']} {what}）：{exc}")
    return result


def accept_claim(client: MemTraceClient, claim: dict[str, Any]) -> str | None:
    """The operator accepted: the node is done and resolved. Returns a problem description, or
    None. Never raises."""
    return _transition(client, claim, STATUS_DONE, resolution="resolved", claim_tag="status:accepted")


def reject_claim(client: MemTraceClient, claim: dict[str, Any]) -> str | None:
    """The operator sent it back: the node is open again."""
    return _transition(client, claim, STATUS_OPEN, resolution="open", claim_tag="status:rejected")


def _transition(
    client: MemTraceClient, claim: dict[str, Any], status: str, *, resolution: str, claim_tag: str
) -> str | None:
    workspace_id = claim.get("workspace_id")
    problems: list[str] = []
    if not workspace_id:
        return None
    try:
        if claim.get("node_id"):
            _set_status(
                client, workspace_id, claim["node_id"], status, resolution=resolution,
                stage="claim_resolution", run_id=claim.get("conversation_id"),
            )
        if claim.get("claim_node_id"):
            node = client.get_node(
                workspace_id=workspace_id, node_id=claim["claim_node_id"], stage="claim_resolution"
            )
            tags = node.get("tags")
            if isinstance(tags, list):
                kept = [str(t) for t in tags if t != STATUS_CLAIMED and not str(t).startswith("status:")]
                client.update_node(
                    workspace_id=workspace_id, node_id=claim["claim_node_id"],
                    tags=[*kept, claim_tag], stage="claim_resolution",
                )
    except Exception as exc:
        logger.exception("moving a claimed node after the operator's answer failed")
        problems.append(str(exc))
    return "；".join(problems) or None


def claim_keyboard(claim_id: str) -> list[list[dict[str, str]]]:
    return [
        [
            {"text": "✅ 驗收通過", "callback_data": f"{CLAIM_OK}:{claim_id}"},
            {"text": "❌ 還沒完成", "callback_data": f"{CLAIM_NO}:{claim_id}"},
        ]
    ]
