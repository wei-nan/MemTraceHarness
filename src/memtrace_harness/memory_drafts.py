"""Titles and bookkeeping for the hourly archive nodes ("drafts") in MemTrace.

Every hourly draft used to be titled "Draft primary session consolidation: <project>":
55 identical titles for one project, indistinguishable in the MemTrace UI and in search
results, and the node id create_node returned was thrown away, so nothing could ever be
linked to a draft. A draft is now titled by when it happened, which turns it holds and
what it started with, and its node id is recorded locally (trace_store.memory_drafts) so
the nightly digest can link to it.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime, tzinfo
from typing import Callable, Iterable

from memtrace_harness.memtrace_client import MemTraceClient, MemTraceClientError
from memtrace_harness.trace_store import TraceStore

DRAFT_TITLE_PREFIX = "Draft:"
LEGACY_DRAFT_TITLE = "Draft primary session consolidation"
_HEAD_CHARS = 24
_QUOTE_SUFFIX = re.compile(r"\n?（回覆的訊息：「.*」）\s*$", re.DOTALL)
_TURN_LINE = re.compile(r"^Turn #(\d+) \[", re.MULTILINE)

# MemTrace rate-limits per API key, and the live gateway shares ours: bulk calls from a
# command stay well under it (a burst of ~60 deletes drew an HTTP 429 once).
CALL_INTERVAL_SECONDS = 1.5
BACKOFF_SECONDS = 10.0
MAX_ATTEMPTS = 6


def _head(content: str) -> str:
    text = _QUOTE_SUFFIX.sub("", content or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= _HEAD_CHARS else text[:_HEAD_CHARS] + "…"


def draft_title(project: str, turns: Iterable[tuple[int, str, str, str]], tz: tzinfo) -> str:
    """turns: (turn_seq, created_at ISO, speaker, content) for the turns the draft
    holds. Reads like `Draft: Beri 2026-09-30 14:05（turns #412–#419）· 請接續上次的任務`."""
    ordered = sorted(turns, key=lambda t: t[0])
    first_seq, first_at, _, _ = ordered[0]
    last_seq = ordered[-1][0]
    stamp = datetime.fromisoformat(first_at).astimezone(tz).strftime("%Y-%m-%d %H:%M")
    span = f"turn #{first_seq}" if first_seq == last_seq else f"turns #{first_seq}–#{last_seq}"
    opener = next((t for t in ordered if t[2] == "user"), ordered[0])
    head = _head(opener[3])
    return f"{DRAFT_TITLE_PREFIX} {project} {stamp}（{span}）" + (f"· {head}" if head else "")


def parse_turn_seqs(body: str) -> list[int]:
    return sorted({int(m) for m in _TURN_LINE.findall(body or "")})


@dataclass
class RetitlePlan:
    node_id: str
    old_title: str
    new_title: str
    first_turn_seq: int
    last_turn_seq: int


@dataclass
class SkippedDraft:
    node_id: str
    reason: str


def _list_all_nodes(client: MemTraceClient, workspace_id: str) -> list[dict]:
    nodes: list[dict] = []
    offset = 0
    while True:
        page = _throttled(client.list_nodes, workspace_id=workspace_id, limit=200, offset=offset)
        nodes += page
        if len(page) < 200:
            return nodes
        offset += len(page)


def _throttled(fn: Callable, **kwargs):
    for attempt in range(MAX_ATTEMPTS):
        try:
            result = fn(**kwargs)
            time.sleep(CALL_INTERVAL_SECONDS)
            return result
        except MemTraceClientError as exc:
            if "429" not in str(exc) or attempt == MAX_ATTEMPTS - 1:
                raise
            time.sleep(BACKOFF_SECONDS * (attempt + 1))


def plan_draft_retitle(
    client: MemTraceClient,
    trace_store: TraceStore,
    project: str,
    workspace_id: str,
    tz: tzinfo,
) -> tuple[list[RetitlePlan], list[SkippedDraft]]:
    """Read-only: work out the new title of every old-style draft of this project in
    its memory workspace. The turn numbers come from the node body and the timestamps
    from the local hot log (the node itself may be a copy made later)."""
    old_title = f"{LEGACY_DRAFT_TITLE}: {project}"
    by_seq = {
        int(t["turn_seq"]): (int(t["turn_seq"]), t["created_at"], t["speaker"], t["content"])
        for t in trace_store.get_primary_session_turns(f"psess_{project}")
    }
    plans: list[RetitlePlan] = []
    skipped: list[SkippedDraft] = []
    for node in _list_all_nodes(client, workspace_id):
        if node.get("title") != old_title:
            continue
        seqs = parse_turn_seqs(str(node.get("body") or ""))
        if not seqs:
            skipped.append(SkippedDraft(str(node["id"]), "no 'Turn #n' lines in the body"))
            continue
        missing = [s for s in seqs if s not in by_seq]
        if missing:
            skipped.append(SkippedDraft(str(node["id"]), f"turns not in the local log: {missing[:3]}"))
            continue
        plans.append(
            RetitlePlan(
                node_id=str(node["id"]),
                old_title=old_title,
                new_title=draft_title(project, [by_seq[s] for s in seqs], tz),
                first_turn_seq=seqs[0],
                last_turn_seq=seqs[-1],
            )
        )
    plans.sort(key=lambda p: p.first_turn_seq)
    return plans, skipped


def apply_draft_retitle(
    client: MemTraceClient,
    trace_store: TraceStore,
    project: str,
    workspace_id: str,
    plans: list[RetitlePlan],
    log: Callable[[str], None] = print,
) -> int:
    """Rename each node, then remember which turns it holds. Safe to re-run: a node that
    already has its new title is no longer matched by plan_draft_retitle()."""
    done = 0
    for plan in plans:
        _throttled(
            client.update_node, workspace_id=workspace_id, node_id=plan.node_id, title=plan.new_title,
            stage="draft_retitle",
        )
        trace_store.record_memory_draft(
            project=project,
            workspace_id=workspace_id,
            node_id=plan.node_id,
            first_turn_seq=plan.first_turn_seq,
            last_turn_seq=plan.last_turn_seq,
        )
        done += 1
        if done % 10 == 0:
            log(f"[{project}] renamed {done}/{len(plans)}")
    return done
