"""The nightly archive: a day's conversation split into matters, one MemTrace node per matter.

By day the conversation lives only in the local hot log. At night the Harness reads the whole
day and splits it by matter *before* anything enters MemTrace (operator decision, 2026-10-10,
"Memory archive" in docs/operating-contract.md): a node's title is the matter itself, its body is
organised (current state, decisions, facts, open items, each citing the turns it came from), and
a matter that already has a node is updated rather than duplicated. No transcript goes in.

Local SQLite is the source of truth (`memory_topics`); MemTrace sync is best-effort and retried.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, tzinfo
from typing import Any

from memtrace_harness.memory_digest import (
    DigestError,
    ModelCaller,
    _cited_turns,
    _extract_json_object,
    _format_log,
    split_schedule_activity,
    turns_by_local_date,
)
from memtrace_harness.calibration import calibration_block
from memtrace_harness.trace_store import TraceStore

logger = logging.getLogger(__name__)

TOPIC_TAGS = ["harness", "draft", "topic"]
MAX_TOPICS_PER_DAY = 20
# The nightly pass archives every finished day that has no matters yet (a Harness that was down, or
# history from before this existed), oldest first, but at most this many per project per night: each
# day is one model call, so a long backlog is worked off over a few nights instead of one long run.
MAX_DAYS_PER_PASS = 7
EXISTING_TOPICS_SHOWN = 60
TITLE_MAX_CHARS = 40
STATE_MAX_CHARS = 300
ITEM_MAX_CHARS = 400
ITEMS_PER_KIND = 12
BODY_ITEMS_PER_KIND = 40
_KINDS = (("decisions", "決定"), ("facts", "事實"), ("open_items", "未決"))
_TITLE_LABEL = re.compile(r"^(?:主題|標題|title)\s*[:：]\s*", re.IGNORECASE)
_TITLE_JUNK = "`*#>-「」『』\"'“”（）()[]【】 "


def clean_title(raw: Any) -> str:
    text = re.sub(r"\s+", " ", str(raw or "")).strip().strip(_TITLE_JUNK)
    text = _TITLE_LABEL.sub("", text).strip().strip(_TITLE_JUNK)
    return text if len(text) <= TITLE_MAX_CHARS else text[:TITLE_MAX_CHARS].rstrip() + "…"


def _clip(text: Any, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def _normalize(title: str) -> str:
    return re.sub(r"[\s\W_]+", "", title).lower()


# --- Prompt --------------------------------------------------------------------


def _existing_block(existing: list[dict]) -> str:
    if not existing:
        return "(none yet)"
    lines = []
    for topic in existing[:EXISTING_TOPICS_SHOWN]:
        latest = topic["entries"][-1]
        open_items = "；".join(i["text"] for i in latest.get("open_items", [])[:4])
        lines.append(
            f"[{topic['id']}] {topic['title']}（最近 {topic['last_date']}"
            + ("，已標記解決，若今天又談到就沿用它" if topic.get("status") == "resolved" else "")
            + f"）現況：{latest.get('state', '')}"
            + (f" 未決：{open_items}" if open_items else "")
        )
    return "\n".join(lines)


def build_topics_prompt(
    project: str, digest_date: str, turns: list[dict], existing: list[dict], schedule_note: str = "", calibration: str = ""
) -> str:
    schedule_block = (
        f"\nScheduled runs that day (their firings are not matters; only the last result of each is in the log):\n{schedule_note}\n"
        if schedule_note
        else ""
    )
    return f"""You are the memory archivist of the project "{project}". Below is the complete conversation log of {digest_date} (turn numbers are #n). Turn it into MATTERS for the project's long-term memory: one matter per subject, however the messages interleaved. Each matter becomes one searchable note, so a person (or a model) searching later must find it by what it is about.

Matters already in memory (id, title, last touched, current state, open items):
{_existing_block(existing)}
{schedule_block}{calibration}
Rules:
1. One matter = one subject (a strategy rule, a bug, a decision, a question being worked out). Group all turns about it together, even when other subjects were discussed in between. Never put the whole day in one matter; a day with five subjects gives five matters. At most {MAX_TOPICS_PER_DAY}.
2. If a matter continues one listed above, set "match" to its id and DO NOT invent a new matter; its "state" replaces the old one and its lists add to what is known. Otherwise "match" is null and you give a "title".
3. "title": the matter itself, in the words someone would search for (e.g. "移動停利：+8% 啟動、6×ATR% 回撤出場"). 8-30 characters, Traditional Chinese unless the subject is naturally English. No date, no turn numbers, no project name, no words like 討論/對話/紀錄/摘要.
4. "state": one or two sentences saying where the matter stands NOW (after this day). Write the conclusion, not the story.
5. "decisions", "facts", "open_items": organised statements, not quotes. decisions = things the operator decided or agreed; facts = things learned or verified; open_items = what is still unresolved, to do or waiting (for a continued matter, list the COMPLETE current open list: carry over those still open, drop those resolved today). Each item is {{"text": "...", "turns": [n, ...]}} and MUST cite the turn numbers it comes from, only turns that appear in the log. An item without valid turns is discarded.
6. Skip greetings, small talk, status pings and anything with no lasting value. If nothing in the day is worth keeping, return {{"topics": []}}.
7. Write in Traditional Chinese. Do not copy long passages; numbers, names, parameters and file paths must be exact.

Reply with ONE JSON object and nothing else:
{{"topics": [{{"match": <id or null>, "title": "<only when match is null>", "state": "...", "decisions": [{{"text": "...", "turns": [1]}}], "facts": [], "open_items": []}}]}}

=== Conversation log of {digest_date} ===
{_format_log(turns)}
"""


# --- Grounding -----------------------------------------------------------------


def _ground_list(raw: Any, valid_seqs: set[int]) -> tuple[list[dict], int]:
    items, dropped = [], 0
    for item in raw if isinstance(raw, list) else []:
        text = _clip(item.get("text"), ITEM_MAX_CHARS) if isinstance(item, dict) else ""
        turns = sorted({t for t in _cited_turns(item.get("turns")) if t in valid_seqs}) if text else []
        if not turns:
            dropped += 1
            continue
        if any(i["text"] == text for i in items):
            continue
        items.append({"text": text, "turns": turns})
    return items[:ITEMS_PER_KIND], dropped


def ground_topics(raw: dict[str, Any], *, digest_date: str, valid_seqs: set[int], existing: list[dict]) -> tuple[list[dict], int]:
    """Check the model's answer against the log and the matters already known. Returns
    ([{"topic_id": id|None, "title": str, "entry": {...}}], number of items dropped).
    A matter that names a matter we do not have becomes a new one (if it has a title);
    one that repeats an existing title joins it; two answers for one matter are merged;
    one with no state or no cited turn at all is dropped with its items."""
    by_id = {t["id"]: t for t in existing}
    by_title = {_normalize(t["title"]): t["id"] for t in existing}
    grounded: list[dict] = []
    seen_new: dict[str, dict] = {}
    seen_matched: dict[int, dict] = {}
    dropped = 0
    topics = raw.get("topics")
    for item in topics if isinstance(topics, list) else []:
        if not isinstance(item, dict):
            continue
        state = _clip(item.get("state"), STATE_MAX_CHARS)
        title = clean_title(item.get("title"))
        match = item.get("match")
        try:
            match_id = int(match) if match is not None else None
        except (TypeError, ValueError):
            match_id = None
        if match_id not in by_id:
            match_id = by_title.get(_normalize(title)) if title else None
        lists: dict[str, list[dict]] = {}
        for kind, _ in _KINDS:
            lists[kind], lost = _ground_list(item.get(kind), valid_seqs)
            dropped += lost
        cited = sorted({t for kind in lists for i in lists[kind] for t in i["turns"]})
        if not state or not cited or (match_id is None and not title):
            dropped += sum(len(v) for v in lists.values())
            continue
        entry = {"date": digest_date, "state": state, "turns": cited, **lists}
        target = seen_matched.get(match_id) if match_id is not None else seen_new.get(_normalize(title))
        if target is not None:
            _merge_entry(target["entry"], entry)
            continue
        record = {"topic_id": match_id, "title": by_id[match_id]["title"] if match_id is not None else title, "entry": entry}
        (seen_matched.__setitem__(match_id, record) if match_id is not None else seen_new.__setitem__(_normalize(title), record))
        grounded.append(record)
    return grounded[:MAX_TOPICS_PER_DAY], dropped


def _merge_entry(into: dict, other: dict) -> None:
    into["state"] = f"{into['state']} {other['state']}"[:STATE_MAX_CHARS]
    into["turns"] = sorted(set(into["turns"]) | set(other["turns"]))
    for kind, _ in _KINDS:
        for item in other[kind]:
            if all(i["text"] != item["text"] for i in into[kind]):
                into[kind].append(item)
        into[kind] = into[kind][:ITEMS_PER_KIND]


# --- Rendering -----------------------------------------------------------------


def _turn_refs(turns: list[int]) -> str:
    ranges, start, prev = [], None, None
    for t in sorted(set(turns)):
        if start is None:
            start = prev = t
        elif t == prev + 1:
            prev = t
        else:
            ranges.append((start, prev))
            start = prev = t
    if start is not None:
        ranges.append((start, prev))
    return "、".join(f"#{a}" if a == b else f"#{a}–#{b}" for a, b in ranges)


def render_topic_markdown(topic: dict) -> str:
    """What the node holds: the matter's state now, what is still open, then the decisions
    and facts accumulated over the days it was discussed (dated, with the turns they come
    from). Organised statements only; the words themselves stay in the local log."""
    entries = topic["entries"]
    latest = entries[-1]
    lines = [f"**現況（{latest['date']}）**：{latest['state']}", ""]
    if topic.get("status") == "resolved":
        lines.insert(0, f"**狀態：已解決**（{topic.get('resolved_reason') or '無待辦'}）\n")
    open_items = latest.get("open_items") or []
    if open_items:
        lines.append("## 未決")
        lines += [f"- {i['text']}（turn {_turn_refs(i['turns'])}）" for i in open_items]
        lines.append("")
    for kind, label in _KINDS[:2]:
        accumulated = [(e["date"], i) for e in entries for i in e.get(kind, [])][-BODY_ITEMS_PER_KIND:]
        if accumulated:
            lines.append(f"## {label}")
            lines += [f"- {date[5:]}｜{i['text']}（turn {_turn_refs(i['turns'])}）" for date, i in accumulated]
            lines.append("")
    if len(entries) > 1:
        lines.append("## 歷程")
        lines += [f"- {e['date']}：{e['state']}" for e in reversed(entries[-10:])]
        lines.append("")
    lines.append(f"（整理自 {topic['first_date']} 起的對話；原文在 Harness 本機紀錄，依 turn 編號查閱。）")
    return "\n".join(lines).strip() + "\n"


# --- Running -------------------------------------------------------------------


def due_topic_dates(
    trace_store: TraceStore, project: str, session_id: str, tz: tzinfo, now: datetime, *, max_days_back: int | None
) -> list[str]:
    """Finished local days (before today) that have turns but no topic run yet, oldest
    first so a matter's history stays chronological."""
    today = now.astimezone(tz).date()
    grouped = turns_by_local_date(trace_store.get_primary_session_turns(session_id), tz)
    done = trace_store.topic_run_dates(project)
    dates = sorted(d for d in grouped if d < today.isoformat() and d not in done)
    if max_days_back is not None:
        earliest = (today - timedelta(days=max_days_back)).isoformat()
        dates = [d for d in dates if d >= earliest]
    return dates


def run_topics_for_date(
    trace_store: TraceStore,
    project: str,
    session_id: str,
    workspace_id: str,
    digest_date: str,
    tz: tzinfo,
    call_model: ModelCaller,
) -> int:
    """Split that day into matters and record them locally. Returns the number of
    matters touched. A day with nothing worth keeping is still recorded as run."""
    turns = turns_by_local_date(trace_store.get_primary_session_turns(session_id), tz).get(digest_date, [])
    if not turns:
        raise DigestError(f"no turns for {project} on {digest_date}")
    shown, conversation_turns, schedule_note = split_schedule_activity(turns)
    existing = trace_store.list_memory_topics(project, workspace_id, limit=EXISTING_TOPICS_SHOWN)
    provider = model = None
    topics: list[dict] = []
    dropped = 0
    if conversation_turns:
        stdout, provider, model = call_model(build_topics_prompt(project, digest_date, shown, existing, schedule_note, calibration_block(trace_store, project, "archive")))
        topics, dropped = ground_topics(
            _extract_json_object(stdout),
            digest_date=digest_date,
            valid_seqs={int(t["turn_seq"]) for t in shown},
            existing=existing,
        )
    touched = trace_store.save_topic_day(
        project=project, workspace_id=workspace_id, digest_date=digest_date, topics=topics, provider=provider, model=model
    )
    if dropped:
        logger.info(f"[{project}] {digest_date}: {dropped} ungrounded topic item(s) dropped")
    return len(touched)


def sync_topics_to_memtrace(trace_store: TraceStore, memtrace_client, project: str, workspace_id: str) -> int:
    """Write matters MemTrace does not have yet, and refresh the ones changed since, in
    place. A failure stops the pass and is retried next time (the rows stay dirty)."""
    written = 0
    for topic in trace_store.topics_needing_sync(project, workspace_id):
        body = render_topic_markdown(topic)
        if topic["node_id"]:
            memtrace_client.update_node(
                workspace_id=workspace_id, node_id=topic["node_id"], title=topic["title"], body=body, stage="memory_topic"
            )
            trace_store.mark_topic_synced(topic["id"], topic["node_id"])
        else:
            node_id = memtrace_client.create_node(
                workspace_id=workspace_id,
                title=topic["title"],
                body=body,
                content_type="context",
                tags=TOPIC_TAGS,
                force_create=True,
                stage="memory_topic",
            )
            trace_store.mark_topic_synced(topic["id"], str(node_id))
        written += 1
    return written


def finish_archived_days(trace_store: TraceStore, project: str, workspace_id: str, tz: tzinfo) -> list[str]:
    """Mark a day's turns archived (so the status page stops counting them as waiting) once
    every matter of that day is in MemTrace; re-link that day's digest to its matters."""
    done: list[str] = []
    for digest_date in trace_store.unarchived_topic_days(project, workspace_id):
        day = turns_by_local_date(trace_store.get_primary_session_turns(f"psess_{project}"), tz).get(digest_date, [])
        trace_store.mark_turns_consolidated([int(t["id"]) for t in day])
        trace_store.mark_topic_day_archived(project, digest_date)
        trace_store.mark_digest_edges_unsynced(project, digest_date)
        done.append(digest_date)
    return done
