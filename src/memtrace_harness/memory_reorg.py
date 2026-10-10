"""The Harness goes back over what it wrote to MemTrace and reorganises it.

Three means (contract: "Reorganising the knowledge base" in docs/operating-contract.md), all touching
only the Harness's own notes (matters and old hourly drafts) and all removing nothing for good — a
removal is MemTrace's soft delete, restorable for 30 days, with the local row kept:

* a weekly *review* of the matters: a model proposes merging matters that are one subject,
  retitling ones whose title no longer names them, and resolving finished ones; the Harness
  validates every op against the matters it showed and applies it;
* *draft retirement*: an old hourly draft goes to the trash once every day it spans is archived
  into matters;
* a *rebuild*: derive all matters again from the complete local log.

Every pass is a run with its ops logged, so it can be undone (`undo_reorg_run`).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any

from memtrace_harness.memory_digest import ModelCaller, _extract_json_object, turns_by_local_date
from memtrace_harness.memory_topics import (
    EXISTING_TOPICS_SHOWN,
    _merge_entry,
    _normalize,
    clean_title,
    sync_topics_to_memtrace,
)
from memtrace_harness.trace_store import TraceStore

logger = logging.getLogger(__name__)

REVIEW_INTERVAL = timedelta(days=7)
MIN_TOPICS_TO_REVIEW = 6
MAX_OPS_PER_PASS = 10
RESOLVE_MIN_IDLE_DAYS = 7
MAX_DRAFTS_PER_PASS = 60
REVIEW_KIND, DRAFTS_KIND, REBUILD_KIND = "review", "drafts", "rebuild"


@dataclass
class ReorgOutcome:
    kind: str
    project: str
    run_id: int
    lines: list[str] = field(default_factory=list)
    count: int = 0
    failed: int = 0


# --- Weekly review ---------------------------------------------------------------


def build_review_prompt(project: str, topics: list[dict], today: str) -> str:
    lines = []
    for t in topics[:EXISTING_TOPICS_SHOWN * 2]:
        latest = t["entries"][-1]
        lines.append(
            f"[{t['id']}] {t['title']} | {t['first_date']}→{t['last_date']} | {len(t['entries'])} 天 | "
            f"{'已解決' if t['status'] == 'resolved' else '進行中'} | 現況：{latest.get('state', '')[:160]} | "
            f"未決 {len(latest.get('open_items') or [])} 項"
        )
    return f"""You keep the long-term memory of the project "{project}" in order. Today is {today}. Below are its MATTERS (each is one memory note); review the list as a whole.

{chr(10).join(lines)}

You may propose three kinds of change:
- "merge": two or more matters that are really ONE subject (the same rule, bug or decision discussed under different titles). Give the id to keep ("keep"), the ids folded into it ("absorb"), and optionally a better "title" for the merged matter.
- "retitle": a matter whose title no longer says what it is about now. Give the "id" and the new "title".
- "resolve": a matter with nothing left to do or decide (finished, retired, superseded). Give the "id" and a "reason". Only matters last touched at least {RESOLVE_MIN_IDLE_DAYS} days ago.

Rules: propose only what you are sure of; leaving the list alone is a good answer. A matter may appear in at most one change. Titles: the matter itself in searchable words, 8-30 characters, Traditional Chinese, no date, no project name. At most {MAX_OPS_PER_PASS} changes in all.

Reply with ONE JSON object and nothing else:
{{"merge": [{{"keep": 1, "absorb": [2], "title": "...", "reason": "..."}}], "retitle": [{{"id": 3, "title": "...", "reason": "..."}}], "resolve": [{{"id": 4, "reason": "..."}}]}}
"""


def _as_id(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def ground_review_ops(raw: dict[str, Any], topics: list[dict], today: str) -> dict[str, list[dict]]:
    """Keep only the changes that name live matters the model was shown, use each matter once,
    and respect the guards (a resolve needs a reason and an idle matter). Order of priority:
    merges, resolves, retitles; the total is capped."""
    by_id = {t["id"]: t for t in topics}
    titles = {_normalize(t["title"]) for t in topics}
    idle_before = (datetime.fromisoformat(today) - timedelta(days=RESOLVE_MIN_IDLE_DAYS)).date().isoformat()
    used: set[int] = set()
    ops: dict[str, list[dict]] = {"merge": [], "resolve": [], "retitle": []}
    budget = MAX_OPS_PER_PASS

    for item in raw.get("merge") if isinstance(raw.get("merge"), list) else []:
        if not isinstance(item, dict) or budget <= 0:
            continue
        keep = _as_id(item.get("keep"))
        absorb = [a for a in dict.fromkeys(_as_id(a) for a in (item.get("absorb") if isinstance(item.get("absorb"), list) else [])) if a is not None]
        ids = [keep, *absorb]
        if keep not in by_id or not absorb or any(i not in by_id for i in absorb) or keep in absorb or used & set(ids):
            continue
        used |= set(ids)
        title = clean_title(item.get("title")) or None
        ops["merge"].append({"keep": keep, "absorb": absorb, "title": title, "reason": str(item.get("reason") or "")[:200]})
        budget -= 1
    for item in raw.get("resolve") if isinstance(raw.get("resolve"), list) else []:
        if not isinstance(item, dict) or budget <= 0:
            continue
        topic_id, reason = _as_id(item.get("id")), str(item.get("reason") or "").strip()[:200]
        topic = by_id.get(topic_id)
        if topic is None or topic_id in used or not reason or topic["status"] != "active" or topic["last_date"] > idle_before:
            continue
        used.add(topic_id)
        ops["resolve"].append({"id": topic_id, "reason": reason})
        budget -= 1
    for item in raw.get("retitle") if isinstance(raw.get("retitle"), list) else []:
        if not isinstance(item, dict) or budget <= 0:
            continue
        topic_id, title = _as_id(item.get("id")), clean_title(item.get("title"))
        topic = by_id.get(topic_id)
        if topic is None or topic_id in used or not title or _normalize(title) in titles:
            continue
        used.add(topic_id)
        titles.add(_normalize(title))
        ops["retitle"].append({"id": topic_id, "title": title, "reason": str(item.get("reason") or "")[:200]})
        budget -= 1
    return ops


def _combine_entries(into: list[dict], extra: list[dict]) -> list[dict]:
    merged = {e["date"]: dict(e) for e in into}
    for entry in extra:
        if entry["date"] in merged:
            _merge_entry(merged[entry["date"]], entry)
        else:
            merged[entry["date"]] = dict(entry)
    return [merged[d] for d in sorted(merged)]


def apply_review_ops(trace_store: TraceStore, client, project: str, workspace_id: str, run_id: int, ops: dict[str, list[dict]]) -> ReorgOutcome:
    outcome = ReorgOutcome(REVIEW_KIND, project, run_id)
    for op in ops["merge"]:
        keep = trace_store.get_memory_topic(op["keep"])
        absorbed = [t for t in (trace_store.get_memory_topic(a) for a in op["absorb"]) if t]
        if keep is None or not absorbed:
            continue
        entries = keep["entries"]
        for other in absorbed:
            entries = _combine_entries(entries, other["entries"])
        before = {k: keep[k] for k in ("title", "entries", "first_date", "last_date", "status", "resolved_reason")}
        moved = {t["id"]: trace_store.topic_day_dates(t["id"]) for t in absorbed}
        trace_store.update_memory_topic(
            keep["id"], title=op["title"] or keep["title"], entries=entries, first_date=entries[0]["date"],
            last_date=entries[-1]["date"], node_dirty=1,
            **({"status": "active", "resolved_reason": None} if keep["status"] == "resolved" else {}),
        )
        for other in absorbed:
            trace_store.update_memory_topic(other["id"], status="merged", merged_into=keep["id"], node_dirty=0)
            trace_store.add_topic_days(keep["id"], project, moved[other["id"]])  # the digest links to the kept node
        trace_store.add_reorg_op(
            run_id, "merge",
            {"keep": keep["id"], "before": before, "absorbed": [{"id": t["id"], "node_id": t["node_id"], "days": moved[t["id"]]} for t in absorbed]},
        )
        for other in absorbed:
            if other["node_id"] and not _soft_delete(client, workspace_id, other["node_id"], "duplicate", "merged into a matter"):
                outcome.failed += 1
        titles = "、".join(f"「{t['title']}」" for t in absorbed)
        outcome.lines.append(f"合併：{titles} → 「{op['title'] or keep['title']}」")
        outcome.count += 1
    for op in ops["resolve"]:
        topic = trace_store.get_memory_topic(op["id"])
        if topic is None:
            continue
        trace_store.add_reorg_op(run_id, "resolve", {"id": topic["id"], "before": {"status": topic["status"], "resolved_reason": topic["resolved_reason"]}})
        trace_store.update_memory_topic(topic["id"], status="resolved", resolved_reason=op["reason"], node_dirty=1)
        outcome.lines.append(f"標記已解決：「{topic['title']}」（{op['reason']}）")
        outcome.count += 1
    for op in ops["retitle"]:
        topic = trace_store.get_memory_topic(op["id"])
        if topic is None:
            continue
        trace_store.add_reorg_op(run_id, "retitle", {"id": topic["id"], "before": {"title": topic["title"]}})
        trace_store.update_memory_topic(topic["id"], title=op["title"], node_dirty=1)
        outcome.lines.append(f"改名：「{topic['title']}」→「{op['title']}」")
        outcome.count += 1
    return outcome


def review_due(trace_store: TraceStore, project: str, workspace_id: str, now: datetime) -> bool:
    last = trace_store.last_reorg_run(project, workspace_id, REVIEW_KIND)
    if last and last["status"] == "done" and datetime.fromisoformat(last["created_at"]) > now - REVIEW_INTERVAL:
        return False
    return len(trace_store.list_memory_topics(project, workspace_id)) >= MIN_TOPICS_TO_REVIEW


def run_review_pass(
    trace_store: TraceStore, client, project: str, workspace_id: str, call_model: ModelCaller,
    *, now: datetime | None = None, force: bool = False, plan_only: bool = False,
) -> ReorgOutcome | None:
    """One review. None when it is not due (or there is too little to review). The model's
    call is made before the run is recorded, so a model outage records nothing."""
    now = now or datetime.now(timezone.utc)
    if not force and not review_due(trace_store, project, workspace_id, now):
        return None
    topics = trace_store.list_memory_topics(project, workspace_id)
    if len(topics) < 2:
        return None
    today = now.date().isoformat()
    stdout, _, _ = call_model(build_review_prompt(project, topics, today))
    ops = ground_review_ops(_extract_json_object(stdout), topics, today)
    if plan_only:
        outcome = ReorgOutcome(REVIEW_KIND, project, 0)
        by_id = {t["id"]: t["title"] for t in topics}
        outcome.lines = (
            [f"合併：{'、'.join(by_id[a] for a in o['absorb'])} → {by_id[o['keep']]}" for o in ops["merge"]]
            + [f"已解決：{by_id[o['id']]}（{o['reason']}）" for o in ops["resolve"]]
            + [f"改名：{by_id[o['id']]} → {o['title']}" for o in ops["retitle"]]
        )
        outcome.count = len(outcome.lines)
        return outcome
    run_id = trace_store.create_reorg_run(project=project, workspace_id=workspace_id, kind=REVIEW_KIND)
    outcome = apply_review_ops(trace_store, client, project, workspace_id, run_id, ops)
    if client is not None:
        try:
            sync_topics_to_memtrace(trace_store, client, project, workspace_id)
        except Exception:
            logger.exception(f"[{project}] writing the reviewed matters to MemTrace failed; will retry next pass")
    trace_store.finish_reorg_run(run_id, summary="; ".join(outcome.lines)[:2000] or "no change")
    return outcome


# --- Draft retirement ------------------------------------------------------------


def retire_covered_drafts(
    trace_store: TraceStore, client, project: str, workspace_id: str, tz: tzinfo, *, limit: int = MAX_DRAFTS_PER_PASS
) -> ReorgOutcome | None:
    """Soft-delete the old hourly drafts whose every day is archived into matters."""
    drafts = trace_store.list_unretired_drafts(project, workspace_id)
    if not drafts or client is None:
        return None
    archived = trace_store.archived_topic_days(project)
    if not archived:
        return None
    turns = trace_store.get_primary_session_turns(f"psess_{project}")
    day_of = {int(t["turn_seq"]): d for d, day in turns_by_local_date(turns, tz).items() for t in day}
    covered = []
    for draft in drafts:
        days = {day_of.get(seq) for seq in range(draft["first_turn_seq"], draft["last_turn_seq"] + 1)} - {None}
        if days and days <= archived:
            covered.append(draft)
    if not covered:
        return None
    run_id = trace_store.create_reorg_run(project=project, workspace_id=workspace_id, kind=DRAFTS_KIND)
    outcome = ReorgOutcome(DRAFTS_KIND, project, run_id)
    for draft in covered[:limit]:
        if not _soft_delete(client, workspace_id, draft["node_id"], "other", "superseded by the nightly matter notes"):
            outcome.failed += 1
            continue
        trace_store.set_draft_retired(workspace_id, draft["node_id"], True)
        trace_store.add_reorg_op(run_id, "retire_draft", {"node_id": draft["node_id"]})
        outcome.count += 1
    outcome.lines = [f"{outcome.count} 個舊的每小時草稿已移到 MemTrace 垃圾桶（內容已整理進事項節點）"]
    trace_store.finish_reorg_run(run_id, summary=outcome.lines[0])
    return outcome


def _soft_delete(client, workspace_id: str, node_id: str, category: str, note: str) -> bool:
    try:
        client.delete_node(workspace_id=workspace_id, node_id=node_id, reason_category=category, reason_note=note, stage="memory_reorg")
        return True
    except Exception:
        logger.exception(f"moving {node_id} to the trash failed; will retry")
        return False


# --- Rebuild ---------------------------------------------------------------------


def start_rebuild(trace_store: TraceStore, project: str, workspace_id: str) -> tuple[int, bool]:
    """Supersede every live matter and forget which days were archived, so every day is due
    again. Returns (run id, resumed): an interrupted rebuild is resumed, not started over."""
    running = trace_store.running_reorg_run(project, workspace_id, REBUILD_KIND)
    if running:
        return running["id"], True
    old = trace_store.list_memory_topics(project, workspace_id)
    run_id = trace_store.create_reorg_run(project=project, workspace_id=workspace_id, kind=REBUILD_KIND, status="running")
    trace_store.add_reorg_op(run_id, "rebuild", {"old": [{"id": t["id"], "node_id": t["node_id"]} for t in old]})
    for topic in old:
        trace_store.update_memory_topic(topic["id"], status="superseded", node_dirty=0)
    trace_store.clear_topic_runs(project)
    return run_id, False


def finish_rebuild(trace_store: TraceStore, client, project: str, workspace_id: str, run_id: int) -> ReorgOutcome:
    """The new matters are all written: now the superseded ones leave MemTrace (soft delete)."""
    outcome = ReorgOutcome(REBUILD_KIND, project, run_id)
    ops = trace_store.list_reorg_ops(run_id)
    old = next(o["payload"]["old"] for o in ops if o["op"] == "rebuild")
    for item in old:
        if item["node_id"] and not _soft_delete(client, workspace_id, item["node_id"], "duplicate", "replaced by a rebuilt matter"):
            outcome.failed += 1
    new = trace_store.list_memory_topics(project, workspace_id)
    trace_store.add_reorg_op(run_id, "rebuild_done", {"new": [t["id"] for t in new]})
    outcome.count = len(new)
    outcome.lines = [f"重建完成：{len(old)} 個舊事項換成 {len(new)} 個新事項（舊節點在垃圾桶，30 天內可還原）"]
    trace_store.finish_reorg_run(run_id, summary=outcome.lines[0])
    return outcome


# --- Undo ------------------------------------------------------------------------


def undo_reorg_run(trace_store: TraceStore, client, run_id: int) -> tuple[int, int]:
    """Reverse a run. Returns (undone ops, failures). A node missing from the trash is
    rebuilt from the local row where there is one (matters); an old draft has none."""
    run = trace_store.get_reorg_run(run_id)
    if run is None or run["undone_at"]:
        return 0, 0
    project, workspace_id = run["project"], run["workspace_id"]
    done = failed = 0

    def restore(node_id: str | None) -> bool:
        if not node_id:
            return True
        try:
            client.restore_node(workspace_id=workspace_id, node_id=node_id, stage="memory_reorg")
            return True
        except Exception:
            logger.exception(f"restoring {node_id} failed")
            return False

    ops = trace_store.list_reorg_ops(run_id)
    nodes_were_trashed = any(o["op"] == "rebuild_done" for o in ops)
    for entry in reversed(ops):
        op, p = entry["op"], entry["payload"]
        if op == "merge":
            trace_store.update_memory_topic(p["keep"], node_dirty=1, **p["before"])
            had = {e["date"] for e in p["before"]["entries"]}
            for item in p["absorbed"]:
                trace_store.update_memory_topic(item["id"], status="active", merged_into=None, node_dirty=0)
                trace_store.remove_topic_days(p["keep"], [d for d in item["days"] if d not in had])
                if not restore(item["node_id"]):
                    trace_store.update_memory_topic(item["id"], node_id=None, node_dirty=1)  # recreated from the local row
                    failed += 1
        elif op == "resolve":
            trace_store.update_memory_topic(p["id"], node_dirty=1, **p["before"])
        elif op == "retitle":
            trace_store.update_memory_topic(p["id"], node_dirty=1, **p["before"])
        elif op == "retire_draft":
            if restore(p["node_id"]):
                trace_store.set_draft_retired(workspace_id, p["node_id"], False)
            else:
                failed += 1
        elif op == "rebuild_done":
            for topic_id in p["new"]:
                topic = trace_store.get_memory_topic(topic_id)
                if topic:
                    if topic["node_id"]:
                        _soft_delete(client, workspace_id, topic["node_id"], "other", "rebuild undone")
                    trace_store.update_memory_topic(topic_id, status="superseded", node_dirty=0)
        elif op == "rebuild":
            for item in p["old"]:
                topic = trace_store.get_memory_topic(item["id"])
                if topic is None:
                    continue
                restored = restore(item["node_id"]) if nodes_were_trashed else True
                trace_store.update_memory_topic(
                    item["id"], status="active", node_dirty=0 if restored else 1, **({} if restored else {"node_id": None})
                )
                failed += 0 if restored else 1
        done += 1
    trace_store.mark_reorg_run_undone(run_id)
    if client is not None:
        try:
            sync_topics_to_memtrace(trace_store, client, project, workspace_id)
        except Exception:
            logger.exception(f"[{project}] refreshing matter notes after the undo failed; will retry next pass")
    return done, failed


def describe_reorg(outcome: ReorgOutcome) -> str:
    head = {
        REVIEW_KIND: f"🗂 [{outcome.project}] 每週整理記憶：調整了 {outcome.count} 個事項",
        DRAFTS_KIND: f"🗂 [{outcome.project}] 清理舊草稿",
        REBUILD_KIND: f"🗂 [{outcome.project}] 重建記憶",
    }[outcome.kind]
    lines = [head, *[f"- {line}" for line in outcome.lines[:12]]]
    if len(outcome.lines) > 12:
        lines.append(f"…另外 {len(outcome.lines) - 12} 項")
    if outcome.failed:
        lines.append(f"（{outcome.failed} 項失敗，下次會再試）")
    return "\n".join(lines)
