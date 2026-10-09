"""Titles and bookkeeping for the hourly archive nodes ("drafts") in MemTrace.

A draft is titled by its subject (a cheap model names it, see duties.make_topic_namer),
followed by when and which turns; the opener of the conversation stands in when no model
answers. The history below explains the earlier, content-blind titles.

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
# What follows "Draft: <project> " in a title that has no topic yet.
_STAMP_TITLE = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}（")

# MemTrace rate-limits per API key, and the live Harness shares ours: bulk calls from a
# command stay well under it (a burst of ~60 deletes drew an HTTP 429 once).
CALL_INTERVAL_SECONDS = 1.5
BACKOFF_SECONDS = 10.0
MAX_ATTEMPTS = 6


def _head(content: str) -> str:
    text = _QUOTE_SUFFIX.sub("", content or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= _HEAD_CHARS else text[:_HEAD_CHARS] + "…"


_TOPIC_MAX_CHARS = 30
_TOPIC_LABEL = re.compile(r"^(?:主題|标题|標題|title|topic)\s*[:：]\s*", re.IGNORECASE)
_TOPIC_JUNK = "`*#>-「」『』\"'“”（）()[]【】 "
_TOPIC_TURNS = 14
_TOPIC_TURN_CHARS = 220


def clean_topic(raw: str | None) -> str:
    """One short line out of whatever a model answered, or "" when nothing usable came
    back (the caller then falls back to the opener-based title)."""
    for line in (raw or "").splitlines():
        text = re.sub(r"\s+", " ", line).strip().strip(_TOPIC_JUNK)
        text = _TOPIC_LABEL.sub("", text).strip().strip(_TOPIC_JUNK)
        if text:
            return text if len(text) <= _TOPIC_MAX_CHARS else text[:_TOPIC_MAX_CHARS].rstrip() + "…"
    return ""


def topic_prompt(project: str, turns: Iterable[tuple[int, str, str, str]]) -> str:
    ordered = sorted(turns, key=lambda t: t[0])
    shown = ordered[:_TOPIC_TURNS]
    lines = []
    for seq, _, speaker, content in shown:
        text = re.sub(r"\s+", " ", _QUOTE_SUFFIX.sub("", content or "")).strip()
        if len(text) > _TOPIC_TURN_CHARS:
            text = text[:_TOPIC_TURN_CHARS] + "…"
        lines.append(f"#{seq} [{speaker}] {text}")
    omitted = len(ordered) - len(shown)
    if omitted > 0:
        lines.append(f"（另有 {omitted} 則較後面的對話未列出）")
    return (
        f"以下是專案「{project}」對話紀錄的一段。請替這一段取一個之後用來搜尋與辨認的主題標題。\n"
        "要求：繁體中文，8 到 25 個字，說出『談了什麼、決定了什麼』（例如：sendPhoto 圖表推送測試與驗收），"
        "用具體的名詞與動作，不要寫日期、編號、專案名，也不要寫『討論』『對話』『紀錄』這類空泛字。\n"
        "只回覆標題那一行，不要引號、不要說明。\n\n" + "\n".join(lines)
    )


def draft_title(
    project: str,
    turns: Iterable[tuple[int, str, str, str]],
    tz: tzinfo,
    topic: str | None = None,
) -> str:
    """turns: (turn_seq, created_at ISO, speaker, content) for the turns the draft holds.

    With a topic: `Draft: Beri 修正登入逾時並補上重試（2026-09-30 14:05 · turns #412–#419）` —
    the words a search or a link decision can use come first; the time and turn range
    keep two drafts on the same subject apart. Without one (no model answered), the
    opener of the conversation stands in:
    `Draft: Beri 2026-09-30 14:05（turns #412–#419）· 請接續上次的任務`."""
    ordered = sorted(turns, key=lambda t: t[0])
    first_seq, first_at, _, _ = ordered[0]
    last_seq = ordered[-1][0]
    stamp = datetime.fromisoformat(first_at).astimezone(tz).strftime("%Y-%m-%d %H:%M")
    span = f"turn #{first_seq}" if first_seq == last_seq else f"turns #{first_seq}–#{last_seq}"
    subject = clean_topic(topic)
    if subject:
        return f"{DRAFT_TITLE_PREFIX} {project} {subject}（{stamp} · {span}）"
    opener = next((t for t in ordered if t[2] == "user"), ordered[0])
    head = _head(opener[3])
    return f"{DRAFT_TITLE_PREFIX} {project} {stamp}（{span}）" + (f"· {head}" if head else "")


def name_topic(
    project: str,
    turns: list[tuple[int, str, str, str]],
    topic_fn: Callable[[str], str] | None,
) -> str | None:
    """Ask the topic model about these turns; None on any failure (naming is a
    convenience and must never stop a draft from being written or renamed)."""
    if topic_fn is None or not turns:
        return None
    try:
        return clean_topic(topic_fn(topic_prompt(project, turns))) or None
    except Exception:
        return None


def has_topic_title(project: str, title: str) -> bool:
    """True for a draft that was already given a topic title."""
    return title.startswith(f"{DRAFT_TITLE_PREFIX} {project} ") and not _STAMP_TITLE.match(
        title[len(f"{DRAFT_TITLE_PREFIX} {project} "):]
    )


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
    topic_fn: Callable[[str], str] | None = None,
) -> tuple[list[RetitlePlan], list[SkippedDraft]]:
    """Read-only (apart from the topic model calls): work out the new title of every
    draft of this project in its memory workspace that has no topic title yet — the
    identical legacy titles and the time-and-opener ones. The turn numbers come from the
    node body and the timestamps and text from the local hot log (the node itself may be
    a copy made later). A draft the topic model could not name keeps the opener-based
    title; a node whose title would not change is left out, so a re-run only retries
    those."""
    old_title = f"{LEGACY_DRAFT_TITLE}: {project}"
    stamped_prefix = f"{DRAFT_TITLE_PREFIX} {project} "
    by_seq = {
        int(t["turn_seq"]): (int(t["turn_seq"]), t["created_at"], t["speaker"], t["content"])
        for t in trace_store.get_primary_session_turns(f"psess_{project}")
    }
    plans: list[RetitlePlan] = []
    skipped: list[SkippedDraft] = []
    for node in _list_all_nodes(client, workspace_id):
        title = str(node.get("title") or "")
        if not (title == old_title or (title.startswith(stamped_prefix) and _STAMP_TITLE.match(title[len(stamped_prefix):]))):
            continue
        seqs = parse_turn_seqs(str(node.get("body") or ""))
        if not seqs:
            skipped.append(SkippedDraft(str(node["id"]), "no 'Turn #n' lines in the body"))
            continue
        missing = [s for s in seqs if s not in by_seq]
        if missing:
            skipped.append(SkippedDraft(str(node["id"]), f"turns not in the local log: {missing[:3]}"))
            continue
        turns = [by_seq[s] for s in seqs]
        new_title = draft_title(project, turns, tz, name_topic(project, turns, topic_fn))
        if new_title == title:
            continue
        plans.append(
            RetitlePlan(
                node_id=str(node["id"]),
                old_title=title,
                new_title=new_title,
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
