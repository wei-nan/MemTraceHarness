"""The Controller keeps each knowledge-base workspace in order, and keeps a map of it.

Workspaces are organised differently — a product's workspace is built around decisions and
versions, a trading project's around strategies and risk — and none of that is written down, so
a model sent to explore one pages through hundreds of nodes. This pass gives the Controller two
jobs per workspace:

* a *map* (charter): one pinned node it writes and keeps current — what the workspace is for,
  what lives where, what its tags mean, how to find things, what it has tidied. The Controller
  reads the map instead of exploring, and the map is what it improves each pass.
* *tidying*: it proposes changes — remove duplicates and wrong nodes, mark what is resolved or
  superseded, fix tags and titles, link and pin — as ops that the harness validates and applies.

The model never calls a write tool. The harness shows it a survey it computed (so it need not read
four hundred bodies), checks every op against the nodes it was actually shown, protects what it
manages or a person confirmed, caps how much one pass may do, and records what it changed. Nothing
is destroyed: a delete is MemTrace's soft delete (restorable for 30 days) and its content is also
kept locally, so one tap in Telegram undoes a whole pass's deletions.
"""

from __future__ import annotations

import collections
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Callable

from memtrace_harness.completion_claims import LINK_RELATIONS

if TYPE_CHECKING:
    from memtrace_harness.memtrace_client import MemTraceClient
    from memtrace_harness.scope import ProjectScope
    from memtrace_harness.trace_store import TraceStore

logger = logging.getLogger(__name__)

GARDEN_INTERVAL = timedelta(days=7)
# A pass that failed (no usable answer from the model) is not retried sooner than this, so a
# model outage costs one call every few hours, not one every housekeeping tick.
RETRY_AFTER_FAILURE = timedelta(hours=6)
GARDEN_TIMEOUT_SECONDS = 600
PAGE_SIZE = 200
MAX_FETCH = 600
MAX_LISTED = 300
MIN_NODES = 3
MAX_DELETES = 100
MAX_DELETE_IDS_PER_OP = 20
MAX_OTHER_OPS = 40
MAX_TAGS_PER_OP = 5
CHARTER_MAX_CHARS = 5000

CHARTER_TAGS = ["harness", "controller", "charter"]
# Nodes the harness itself manages (digests and consolidation notes it syncs, task nodes it
# scans, the map): a model's tidying must not delete, retitle or retag them.
PROTECTED_TAGS = frozenset({"daily-digest", "primary-session", "charter", "task"})
DELETE_REASONS = ("hallucination", "wrong_direction", "duplicate", "pii", "orphaned", "other")
RESOLUTIONS = ("open", "resolved", "superseded")
OPS = ("dedupe", "delete", "retag", "set_resolution", "supersede", "link", "retitle", "pin")
_DESTRUCTIVE = frozenset({"dedupe", "delete"})


@dataclass
class GardenOutcome:
    run_id: int
    workspace_id: str
    project: str
    role_label: str
    charter: str | None = None  # "created" | "updated" | None
    applied: dict[str, int] = field(default_factory=dict)
    deleted: int = 0
    skipped: list[str] = field(default_factory=list)
    notes: str = ""


# ---- reading the workspace --------------------------------------------------------------


def fetch_nodes(client: MemTraceClient, workspace_id: str) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    offset = 0
    while len(nodes) < MAX_FETCH:
        page = client.list_nodes(
            workspace_id=workspace_id, limit=PAGE_SIZE, offset=offset, stage="kb_gardening"
        )
        nodes.extend(n for n in page if isinstance(n, dict) and n.get("id"))
        if len(page) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return nodes


def _title_key(title: Any) -> str:
    return re.sub(r"\s+", " ", str(title or "")).strip().lower()


def title_groups(nodes: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Nodes sharing a title, newest first, keyed G1, G2 … (largest group first). A model is
    shown a group as one line and refers to it by key, instead of being sent every id."""
    by_title: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for node in nodes:
        by_title[_title_key(node.get("title"))].append(node)
    groups = [
        sorted(members, key=lambda n: str(n.get("created_at") or ""), reverse=True)
        for members in by_title.values()
        if len(members) >= 2 and _title_key(members[0].get("title"))
    ]
    groups.sort(key=len, reverse=True)
    return {f"G{i}": members for i, members in enumerate(groups, start=1)}


def is_protected(node: dict[str, Any], charter_node_id: str | None = None) -> bool:
    tags = {str(t) for t in (node.get("tags") or [])}
    return bool(
        node.get("pinned")
        or node.get("validity_confirmed_by")
        or node.get("validity_confirmed_at")
        or (node.get("ask_count") or 0) > 0
        or tags & PROTECTED_TAGS
        or (charter_node_id is not None and node.get("id") == charter_node_id)
    )


def survey_text(nodes: list[dict[str, Any]], groups: dict[str, list[dict[str, Any]]]) -> str:
    def count(key: str) -> collections.Counter:
        return collections.Counter(str(n.get(key)) for n in nodes)

    tags = collections.Counter(str(t) for n in nodes for t in (n.get("tags") or []))
    created = sorted(str(n.get("created_at") or "")[:10] for n in nodes if n.get("created_at"))
    grouped = sum(len(m) for m in groups.values())
    lines = [
        f"Nodes: {len(nodes)} (created {created[0] if created else '?'} .. {created[-1] if created else '?'})",
        f"By content_type: {dict(count('content_type'))}",
        f"By resolution_status: {dict(count('resolution_status'))}",
        f"Pinned: {sum(1 for n in nodes if n.get('pinned'))}; never traversed: "
        f"{sum(1 for n in nodes if not n.get('traversal_count'))}; ever explicitly asked for: "
        f"{sum(1 for n in nodes if (n.get('ask_count') or 0) > 0)}; without tags: "
        f"{sum(1 for n in nodes if not n.get('tags'))}",
        f"Most used tags: {tags.most_common(15)}",
        f"Nodes sharing an exact title with another: {grouped} in {len(groups)} group(s)",
    ]
    return "\n".join(lines)


def node_lines(nodes: list[dict[str, Any]], groups: dict[str, list[dict[str, Any]]]) -> str:
    in_group = {n["id"] for members in groups.values() for n in members}
    lines = []
    for key, members in groups.items():
        lines.append(
            f"{key} | x{len(members)} identical titles | newest {members[0]['id']} "
            f"({str(members[0].get('created_at') or '')[:10]}), oldest {members[-1]['id']} "
            f"({str(members[-1].get('created_at') or '')[:10]}) | type {members[0].get('content_type')} | "
            f"tags {members[0].get('tags')} | {str(members[0].get('title') or '')[:90]}"
        )
    singles = sorted(
        (n for n in nodes if n["id"] not in in_group),
        key=lambda n: str(n.get("created_at") or ""),
        reverse=True,
    )[:MAX_LISTED]
    for n in singles:
        body = " ".join(str(n.get("body") or "").split())[:80]
        lines.append(
            f"{n['id']} | {n.get('content_type')} | {n.get('resolution_status')} | tags {n.get('tags')} | "
            f"{'PINNED ' if n.get('pinned') else ''}asked {n.get('ask_count') or 0} traversed "
            f"{n.get('traversal_count') or 0} | {str(n.get('created_at') or '')[:10]} | "
            f"{str(n.get('title') or '')[:70]} :: {body}"
        )
    return "\n".join(lines)


# ---- the model's side -------------------------------------------------------------------


def build_prompt(
    *,
    project: str,
    workspace_id: str,
    role_label: str,
    purpose: str,
    charter: str | None,
    nodes: list[dict[str, Any]],
    groups: dict[str, list[dict[str, Any]]],
) -> str:
    return (
        "You are the Controller of an agent harness, and you maintain this project's knowledge-base "
        "workspace. Other models (Planner, Developer) and you yourself search it later; it has become "
        "hard to explore. You do two things: keep a MAP of the workspace, and tidy it. You have no "
        "tools: judge only from what is below. You do not write anything yourself — you list ops and "
        "the harness checks and applies them.\n\n"
        f"Project: {project}. Workspace {workspace_id} ({role_label}).\n"
        f"What the project is:\n{purpose[:3000] or '(not described)'}\n\n"
        f"Survey (computed by the harness):\n{survey_text(nodes, groups)}\n\n"
        f"Nodes (newest first; a G<n> line stands for several nodes with an identical title):\n"
        f"{node_lines(nodes, groups)}\n\n"
        f"Current map of this workspace:\n{charter or '(none yet — write the first one)'}\n\n"
        "Answer with ONLY one JSON object:\n"
        '{"charter": "<markdown map, or null if the current one is still right>", '
        '"ops": [ ... ], "notes": "<what you learned about organising THIS workspace, one or two '
        'sentences in Traditional Chinese>"}\n\n'
        f"The map (at most {CHARTER_MAX_CHARS} characters, Traditional Chinese) is read at the start "
        "of every later run, so write what lets a reader find things without paging through nodes: "
        "what the workspace is for; what kinds of nodes it holds and where; the tags actually used and "
        "what they mean; naming conventions you can see; how to search for the common questions; what "
        "does NOT belong here; and what you tidied last. Base it on the survey and nodes above, never "
        "on guesses, and keep what is still right in the current map.\n\n"
        "Ops (each a JSON object with an `op`):\n"
        '- {"op":"dedupe","group":"G1","keep_id":"mem_…","reason":"…"} remove every other node of an '
        "identical-title group, keeping one (usually the newest).\n"
        '- {"op":"delete","node_ids":["mem_…"],"reason_category":'
        f'"{"|".join(DELETE_REASONS)}","reason":"…"}} remove nodes that are wrong, never knowledge, or '
        f"stray (at most {MAX_DELETE_IDS_PER_OP} ids per op).\n"
        '- {"op":"retag","node_id":"mem_…","add":["…"],"remove":["…"]}\n'
        '- {"op":"set_resolution","node_id":"mem_…","status":"open|resolved|superseded"}\n'
        '- {"op":"supersede","old_id":"mem_…","new_id":"mem_…"} the old one is replaced by the new one.\n'
        f'- {{"op":"link","from_id":"mem_…","to_id":"mem_…","relation":"{"|".join(LINK_RELATIONS)}"}}\n'
        '- {"op":"retitle","node_id":"mem_…","title":"…"}\n'
        '- {"op":"pin","node_id":"mem_…"} for an overview node worth keeping at hand.\n'
        f"At most {MAX_DELETES} nodes removed and {MAX_OTHER_OPS} other ops per pass; do the most "
        "valuable ones first. Removed nodes can be restored by the operator for 30 days, but do not rely "
        "on that: remove only what is clearly wrong or clearly redundant. Use only ids shown above. "
        "Nodes that are pinned, confirmed by a person, ever explicitly asked for, tagged "
        "daily-digest/primary-session/task, or the map itself are protected: the harness will refuse to "
        "remove or rewrite them. When unsure, leave a node alone."
    )


def _json(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    from memtrace_harness.loop import parse_json_object

    return parse_json_object(raw)


# ---- applying ops -----------------------------------------------------------------------


def _snapshot(node: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": node.get("title"),
        "body": node.get("body"),
        "content_type": node.get("content_type"),
        "tags": node.get("tags"),
        "resolution_status": node.get("resolution_status"),
        "created_at": node.get("created_at"),
    }


def _strs(value: Any, limit: int) -> list[str]:
    return [str(v).strip() for v in value if isinstance(v, str) and v.strip()][:limit] if isinstance(value, list) else []


def apply_ops(
    client: MemTraceClient,
    trace_store: TraceStore,
    *,
    run_id: int,
    workspace_id: str,
    ops: list[Any],
    nodes: list[dict[str, Any]],
    groups: dict[str, list[dict[str, Any]]],
    charter_node_id: str | None,
) -> tuple[dict[str, int], int, list[str]]:
    """Validate and apply the proposed ops. Returns (applied counts by op, nodes deleted,
    reasons something was skipped). A refused or failing op never stops the others."""
    by_id = {n["id"]: n for n in nodes}
    applied: collections.Counter = collections.Counter()
    skipped: list[str] = []
    deleted_ids: set[str] = set()
    other_ops = 0

    def refuse(op: Any, why: str) -> None:
        skipped.append(f"{op.get('op') if isinstance(op, dict) else '?'}：{why}")

    def remove(node_id: str, category: str, reason: str, op_name: str) -> bool:
        node = by_id.get(node_id)
        if node is None:
            refuse({"op": op_name}, f"{node_id} 不在這次看到的節點裡")
            return False
        if node_id in deleted_ids:
            return False
        if is_protected(node, charter_node_id):
            refuse({"op": op_name}, f"{node_id} 受保護，不刪")
            return False
        if len(deleted_ids) >= MAX_DELETES:
            refuse({"op": op_name}, f"超過每次最多刪 {MAX_DELETES} 個的上限")
            return False
        trace_store.add_gardening_action(run_id, op="delete", node_id=node_id, snapshot=_snapshot(node))
        client.delete_node(
            workspace_id=workspace_id, node_id=node_id,
            reason_category=category if category in DELETE_REASONS else "other",
            reason_note=reason[:300] or None, stage="kb_gardening",
        )
        deleted_ids.add(node_id)
        return True

    for op in ops:
        if not isinstance(op, dict) or op.get("op") not in OPS:
            refuse(op, "看不懂的動作")
            continue
        name = op["op"]
        if name not in _DESTRUCTIVE:
            if other_ops >= MAX_OTHER_OPS:
                refuse(op, f"超過每次最多 {MAX_OTHER_OPS} 個其他動作的上限")
                continue
        try:
            if name == "dedupe":
                members = groups.get(str(op.get("group")))
                keep = str(op.get("keep_id") or "")
                if not members or keep not in {m["id"] for m in members}:
                    refuse(op, "群組或要保留的節點不存在")
                    continue
                reason = str(op.get("reason") or "重複節點")
                count = sum(
                    remove(m["id"], "duplicate", reason, "dedupe") for m in members if m["id"] != keep
                )
                if count:
                    applied["dedupe"] += count
            elif name == "delete":
                ids = _strs(op.get("node_ids"), MAX_DELETE_IDS_PER_OP)
                category = str(op.get("reason_category") or "other")
                count = sum(remove(i, category, str(op.get("reason") or ""), "delete") for i in ids)
                if count:
                    applied["delete"] += count
            else:
                node = by_id.get(str(op.get("node_id") or op.get("old_id") or op.get("from_id") or ""))
                if node is None:
                    refuse(op, "節點不在這次看到的節點裡")
                    continue
                node_id = node["id"]
                if node_id in deleted_ids:
                    refuse(op, f"{node_id} 這次已被刪除")
                    continue
                if name in {"retag", "retitle", "supersede", "set_resolution"} and is_protected(
                    node, charter_node_id
                ):
                    refuse(op, f"{node_id} 受保護，不改")
                    continue
                if name == "retag":
                    add, remove_tags = _strs(op.get("add"), MAX_TAGS_PER_OP), set(_strs(op.get("remove"), MAX_TAGS_PER_OP))
                    tags = [str(t) for t in (node.get("tags") or [])]
                    new_tags = [t for t in tags if t not in remove_tags] + [t for t in add if t not in tags]
                    if new_tags == tags or not isinstance(node.get("tags"), list):
                        refuse(op, f"{node_id} 標籤沒有變化或讀不到")
                        continue
                    client.update_node(workspace_id=workspace_id, node_id=node_id, tags=new_tags, stage="kb_gardening")
                    node["tags"] = new_tags
                elif name == "set_resolution":
                    if op.get("status") not in RESOLUTIONS:
                        refuse(op, "狀態不合法")
                        continue
                    client.update_node(
                        workspace_id=workspace_id, node_id=node_id, resolution_status=op["status"], stage="kb_gardening"
                    )
                elif name == "supersede":
                    new = by_id.get(str(op.get("new_id") or ""))
                    if new is None or new["id"] == node_id or new["id"] in deleted_ids:
                        refuse(op, "取代它的節點不存在")
                        continue
                    client.update_node(
                        workspace_id=workspace_id, node_id=node_id, resolution_status="superseded", stage="kb_gardening"
                    )
                    client.create_edge(
                        workspace_id=workspace_id, from_id=node_id, to_id=new["id"],
                        relation="superseded_by", stage="kb_gardening",
                    )
                elif name == "link":
                    to = by_id.get(str(op.get("to_id") or ""))
                    if to is None or to["id"] in deleted_ids or op.get("relation") not in LINK_RELATIONS:
                        refuse(op, "連線的目標不存在或關係不合法")
                        continue
                    client.create_edge(
                        workspace_id=workspace_id, from_id=node_id, to_id=to["id"],
                        relation=op["relation"], stage="kb_gardening",
                    )
                elif name == "retitle":
                    title = str(op.get("title") or "").strip()[:120]
                    if not title or title == node.get("title"):
                        refuse(op, "標題沒有變化")
                        continue
                    client.update_node(workspace_id=workspace_id, node_id=node_id, title=title, stage="kb_gardening")
                    node["title"] = title
                elif name == "pin":
                    client.update_node(workspace_id=workspace_id, node_id=node_id, pinned=True, stage="kb_gardening")
                    node["pinned"] = True
                applied[name] += 1
                other_ops += 1
        except Exception as exc:
            logger.exception(f"applying a knowledge-base tidying op failed: {op}")
            refuse(op, f"執行失敗：{exc}")
    return dict(applied), len(deleted_ids), skipped


# ---- the map ----------------------------------------------------------------------------


def write_charter(
    client: MemTraceClient,
    trace_store: TraceStore,
    *,
    workspace_id: str,
    project: str,
    role_label: str,
    text: str,
) -> str | None:
    """Create or update the workspace's pinned map node. Returns "created", "updated" or None
    (unchanged, or refused). Never raises."""
    text = text.strip()
    if not text or len(text) > CHARTER_MAX_CHARS:
        return None
    existing = trace_store.get_workspace_charter(workspace_id)
    if existing and existing["body"].strip() == text:
        return None
    try:
        if existing:
            try:
                client.update_node(
                    workspace_id=workspace_id, node_id=existing["node_id"], body=text, pinned=True,
                    stage="kb_gardening",
                )
                trace_store.set_workspace_charter(workspace_id, existing["node_id"], text)
                return "updated"
            except Exception:
                logger.warning(f"the map node {existing['node_id']} could not be updated; creating a new one")
        node_id = client.create_node(
            workspace_id=workspace_id,
            title=f"知識庫地圖：{project}（{role_label}）",
            body=text,
            content_type="context",
            tags=list(CHARTER_TAGS),
            force_create=True,
            stage="kb_gardening",
        )
        client.update_node(workspace_id=workspace_id, node_id=node_id, pinned=True, stage="kb_gardening")
        trace_store.set_workspace_charter(workspace_id, node_id, text)
        return "created"
    except Exception:
        logger.exception(f"writing the map for {workspace_id} failed")
        return None


# ---- one pass ---------------------------------------------------------------------------


def garden_due(trace_store: TraceStore, workspace_id: str, now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    attempt = trace_store.last_gardening_attempt(workspace_id)
    if attempt is not None and now - datetime.fromisoformat(attempt) < RETRY_AFTER_FAILURE:
        return False
    last = trace_store.last_gardening_success(workspace_id)
    return last is None or now - datetime.fromisoformat(last) >= GARDEN_INTERVAL


def run_gardening_pass(
    *,
    client: MemTraceClient,
    trace_store: TraceStore,
    workspace_id: str,
    project: str,
    role_label: str,
    purpose: str,
    caller: Callable[[str], str | None],
) -> GardenOutcome | None:
    """One tidying-and-mapping pass over a workspace. None when there is nothing to do or the
    model gave nothing usable (recorded as a failed run, so it is retried on the next pass)."""
    nodes = fetch_nodes(client, workspace_id)
    if len(nodes) < MIN_NODES:
        return None
    groups = title_groups(nodes)
    charter = trace_store.get_workspace_charter(workspace_id)
    run_id = trace_store.start_gardening_run(workspace_id, project)
    try:
        reply = _json(
            caller(
                build_prompt(
                    project=project, workspace_id=workspace_id, role_label=role_label, purpose=purpose,
                    charter=charter["body"] if charter else None, nodes=nodes, groups=groups,
                )
            )
        )
    except Exception:
        logger.exception(f"[{project}] the tidying model call for {workspace_id} failed")
        reply = None
    if not isinstance(reply, dict):
        trace_store.finish_gardening_run(run_id, "failed")
        return None
    outcome = GardenOutcome(run_id=run_id, workspace_id=workspace_id, project=project, role_label=role_label)
    ops = reply.get("ops") if isinstance(reply.get("ops"), list) else []
    outcome.applied, outcome.deleted, outcome.skipped = apply_ops(
        client, trace_store, run_id=run_id, workspace_id=workspace_id, ops=ops, nodes=nodes,
        groups=groups, charter_node_id=charter["node_id"] if charter else None,
    )
    if isinstance(reply.get("charter"), str):
        outcome.charter = write_charter(
            client, trace_store, workspace_id=workspace_id, project=project, role_label=role_label,
            text=reply["charter"],
        )
    outcome.notes = str(reply.get("notes") or "").strip()[:400]
    trace_store.finish_gardening_run(
        run_id, "ok",
        {"charter": outcome.charter, "applied": outcome.applied, "deleted": outcome.deleted,
         "skipped": outcome.skipped[:20], "notes": outcome.notes},
    )
    return outcome


def describe_outcome(outcome: GardenOutcome) -> str:
    parts = []
    if outcome.charter:
        parts.append("知識庫地圖已" + ("建立" if outcome.charter == "created" else "更新"))
    labels = {"dedupe": "合併重複", "delete": "刪除", "retag": "調整標籤", "set_resolution": "更新狀態",
              "supersede": "標記被取代", "link": "補連線", "retitle": "改標題", "pin": "釘選"}
    parts += [f"{labels[op]} {n}" for op, n in outcome.applied.items() if n]
    text = f"🌱 [{outcome.project}] 整理了知識庫「{outcome.role_label}」（{outcome.workspace_id}）：" + (
        "、".join(parts) if parts else "這次沒有需要改的"
    ) + "。"
    if outcome.deleted:
        text += f"\n刪除的 {outcome.deleted} 個都進了垃圾桶，內容也留在本機，按下面的按鈕可以整批還原。"
    if outcome.skipped:
        text += f"\n另外有 {len(outcome.skipped)} 個動作被擋下（受保護、超過上限或對不上節點）。"
    if outcome.notes:
        text += f"\n我學到：{outcome.notes}"
    return text


def undo_gardening_run(
    client: MemTraceClient, trace_store: TraceStore, run_id: int
) -> tuple[int, int, int]:
    """Bring back what a pass deleted: MemTrace's own restore first, and if the trash no longer
    has it, recreate the node from the copy kept locally. Returns (restored, recreated, failed)."""
    run = trace_store.get_gardening_run(run_id)
    if run is None:
        return 0, 0, 0
    restored = recreated = failed = 0
    for action in trace_store.list_gardening_actions(run_id, op="delete"):
        if action["undone"]:
            continue
        try:
            client.restore_node(workspace_id=run["workspace_id"], node_id=action["node_id"], stage="kb_gardening_undo")
            restored += 1
        except Exception:
            snap = action["snapshot"]
            try:
                if not snap or not snap.get("title") or not snap.get("body"):
                    raise ValueError("no local copy")
                client.create_node(
                    workspace_id=run["workspace_id"], title=snap["title"], body=snap["body"],
                    content_type=snap.get("content_type") or "factual",
                    tags=snap.get("tags") or ["harness", "restored"], force_create=True,
                    stage="kb_gardening_undo",
                )
                recreated += 1
            except Exception:
                logger.exception(f"could not restore node {action['node_id']}")
                failed += 1
                continue
        trace_store.mark_gardening_action_undone(action["id"])
    return restored, recreated, failed


# ---- which workspaces --------------------------------------------------------------------


def workspaces_to_garden(config: Any, scopes: list[ProjectScope]) -> list[tuple[str, str, str, str]]:
    """(workspace_id, project, role label, what it is for) for every workspace the harness's
    projects use — their specification workspace and their cold-memory workspace — once each. A
    workspace some other project shares keeps the first project's name."""
    seen: set[str] = set()
    result: list[tuple[str, str, str, str]] = []
    for scope in scopes:
        memory_ws = config.memory_workspace_id_for(scope.name, scope.workspace_id)
        for workspace_id, label, purpose in (
            (scope.workspace_id, "規格", scope.raw_markdown),
            (
                memory_ws, "記憶",
                f"Cold memory for {scope.name}: nightly digests and archived conversation notes the "
                "harness writes, plus anything the Controller records about how work here goes.",
            ),
        ):
            if workspace_id and workspace_id not in seen:
                seen.add(workspace_id)
                result.append((workspace_id, scope.name, label, purpose))
    return result
