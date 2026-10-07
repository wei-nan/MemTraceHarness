"""Turn what was found into knowledge: conclusion notes and an overview of the directions tried.

Most work in a project is done by chats and operational runs, and neither ends in a stage where the
Controller could record what it learned. The results survive only as raw conversation and as the
nightly digests in the cold-memory workspace, which are drafts. So a project's specification
workspace can hold one carefully written strategy while thirty backtests of others exist nowhere but
in transcripts. This pass promotes the digests' findings into the specification workspace:

* *conclusion notes* — what was tried, what it showed, how far to trust it, each as one node;
* an *overview* — one node listing every direction investigated with its status (in use, being
  researched, dropped, shelved) and the node that holds its detail.

The Controller writes the content, the harness decides what is admissible. Every note and every
overview row must carry verbatim quotes from the digests, and the harness checks that each quote
really occurs in the node it is attributed to; one that does not is thrown away together with the
claim it was meant to support. The provenance section of every note is written by the harness from
the verified quotes, never taken from the model. A rerun updates its own notes by title instead of
adding copies, and a whole pass can be undone.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable

from memtrace_harness.completion_claims import LINK_RELATIONS
from memtrace_harness.kb_gardening import fetch_nodes

if TYPE_CHECKING:
    from memtrace_harness.memtrace_client import MemTraceClient
    from memtrace_harness.trace_store import TraceStore

logger = logging.getLogger(__name__)

PROMOTION_TIMEOUT_SECONDS = 900
INDEX_KIND = "directions"
CONCLUSION_TAGS = ["harness", "controller", "conclusion"]
INDEX_TAGS = ["harness", "controller", "directions-index"]
STATUSES = ("採用", "研究中", "淘汰", "擱置", "未定")

MAX_SOURCES = 40
MAX_SOURCE_CHARS = 3500
MAX_SPEC_LISTED = 80
MAX_NOTES = 8
MAX_DIRECTIONS = 15
MAX_EVIDENCE_PER_ITEM = 4
MIN_QUOTE_CHARS = 12
MAX_QUOTE_CHARS = 400
MIN_BODY_CHARS = 40
MAX_BODY_CHARS = 3000
MAX_STALE = 8

_LOOP_DRAFT_PREFIX = "Harness loop draft"


@dataclass
class PromotionOutcome:
    run_id: int | None
    project: str
    workspace_id: str
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: int = 0
    index: str | None = None  # "created" | "updated" | None
    index_rows: int = 0
    dropped: list[str] = field(default_factory=list)
    stale: list[tuple[str, str]] = field(default_factory=list)
    notes: str = ""
    dry_run: bool = False
    # What a dry run would write, for inspection.
    preview: dict[str, Any] = field(default_factory=dict)


# ---- grounding: quotes must really occur --------------------------------------------------


def _norm(text: Any) -> str:
    """Whitespace and markdown decoration collapsed, so a quote survives re-wrapping but nothing
    else: the words themselves must match."""
    return re.sub(r"[\s*`_#>|]+", " ", str(text or "")).strip()


def _compact(text: Any) -> str:
    """Every space and markdown mark removed, for matching a quote: a line wrapped in the middle
    of a Chinese sentence adds a space the source does not have, and must not void the quote.
    (Number support uses _norm instead, so two figures are never run together.)"""
    return re.sub(r"[\s*`_#>|]+", "", str(text or ""))


def verify_evidence(
    raw: Any, sources: dict[str, dict[str, Any]]
) -> list[dict[str, str]]:
    """The evidence items whose quote occurs verbatim in the node it names. Items naming an
    unknown node, quoting too little, or quoting text that is not there are dropped."""
    verified: list[dict[str, str]] = []
    if not isinstance(raw, list):
        return verified
    for item in raw[:MAX_EVIDENCE_PER_ITEM * 2]:
        if len(verified) >= MAX_EVIDENCE_PER_ITEM:
            break
        if not isinstance(item, dict):
            continue
        node = sources.get(str(item.get("node_id") or ""))
        quote = _norm(item.get("quote"))
        if node is None or not (MIN_QUOTE_CHARS <= len(quote) <= MAX_QUOTE_CHARS):
            continue
        if _compact(quote) not in _compact(node.get("body")):
            continue
        verified.append({"node_id": node["id"], "quote": quote})
    return verified


_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


def unsupported_numbers(text: str, evidence: list[dict[str, str]], sources: dict[str, dict[str, Any]]) -> list[str]:
    """Numbers in `text` (two digits or more) that occur nowhere in the nodes its evidence cites.
    A quote only proves the quoted words exist; this is what stops a figure that is not in the
    sources — a recomputed average, a misremembered count — from becoming knowledge."""
    cited = " ".join(_norm(sources[e["node_id"]].get("body")) for e in evidence).replace(",", "")
    missing = []
    for match in _NUMBER.finditer(text):
        token = match.group().replace(",", "")
        if len(token.replace(".", "")) >= 2 and token not in cited and token not in missing:
            missing.append(token)
    return missing


def _provenance(evidence: list[dict[str, str]], sources: dict[str, dict[str, Any]], workspace_id: str) -> str:
    lines = ["## 出處（由 Harness 核對過原文）"]
    for item in evidence:
        node = sources[item["node_id"]]
        lines.append(f"- {node.get('title')}（{workspace_id}/{node['id']}）：「{item['quote']}」")
    return "\n".join(lines)


# ---- the model's side ---------------------------------------------------------------------


def _is_digest(node: dict[str, Any]) -> bool:
    return "daily-digest" in {str(t) for t in (node.get("tags") or [])}


def source_digests(memory_nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    digests = [n for n in memory_nodes if _is_digest(n)]
    digests.sort(key=lambda n: str(n.get("title") or ""))
    return digests[-MAX_SOURCES:]


def spec_knowledge(spec_nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The specification workspace's real nodes, as the model needs to see them to avoid
    repeating what is already there: not run drafts, not the harness's own map and overview."""
    managed = {"charter", "directions-index"}
    kept = [
        n for n in spec_nodes
        if not str(n.get("title") or "").startswith(_LOOP_DRAFT_PREFIX)
        and not ({str(t) for t in (n.get("tags") or [])} & managed)
    ]
    kept.sort(key=lambda n: str(n.get("created_at") or ""))
    return kept[-MAX_SPEC_LISTED:]


def build_prompt(
    *,
    project: str,
    purpose: str,
    digests: list[dict[str, Any]],
    spec: list[dict[str, Any]],
    existing_notes: list[dict[str, Any]],
    previous_index: str | None,
) -> str:
    digest_text = "\n\n".join(
        f"=== {d['id']} | {d.get('title')} ===\n{str(d.get('body') or '')[:MAX_SOURCE_CHARS]}" for d in digests
    )
    spec_text = "\n".join(
        f"{n['id']} | {n.get('content_type')} | {n.get('resolution_status')} | {str(n.get('created_at') or '')[:10]} | "
        f"{n.get('title')} :: {' '.join(str(n.get('body') or '').split())[:160]}"
        for n in spec
    ) or "(none)"
    mine = "\n".join(f"{n['node_id']} | {n['key']}" for n in existing_notes) or "(none yet)"
    return (
        "You are the Controller of an agent harness. Over the project's life, work was done in chats and "
        "runs whose findings survive only in the nightly digests below (drafts in the cold-memory workspace). "
        "The project's specification workspace holds only some of it. Promote what was found into knowledge. "
        "You have no tools and write nothing yourself: judge only from the text below; the harness checks "
        "and applies your answer.\n\n"
        f"Project: {project}\nWhat the project is:\n{purpose[:2500] or '(not described)'}\n\n"
        f"Nodes already in the specification workspace (id | type | status | date | title :: start of body):\n"
        f"{spec_text}\n\n"
        f"Conclusion notes you already wrote earlier (node id | title) — reuse the exact title to update one:\n{mine}\n\n"
        f"Overview you wrote earlier:\n{previous_index or '(none yet)'}\n\n"
        f"Daily digests, oldest first (each starts with its node id):\n{digest_text}\n\n"
        "Answer with ONLY one JSON object:\n"
        '{"notes": [{"title": "...", "body": "...", "tags": ["..."], "links": [{"to_id": "mem_…", "relation": "...'
        '"}], "evidence": [{"node_id": "mem_…", "quote": "..."}]}],\n'
        ' "directions": [{"name": "...", "status": "採用|研究中|淘汰|擱置|未定", "summary": "<one sentence>", '
        '"conclusion": "<what it showed, and how far to trust that>", "note_titles": ["..."], '
        '"existing_node_id": "mem_…" or null, "evidence": [{"node_id": "mem_…", "quote": "..."}]}],\n'
        ' "stale_candidates": [{"node_id": "mem_…", "reason": "..."}],\n'
        ' "summary": "<one or two sentences in Traditional Chinese: what you promoted and what is still missing>"}\n\n'
        "Rules:\n"
        f"- A NOTE is one finding worth keeping as knowledge: what was tried, the setup, the result with its "
        f"numbers, and how far to trust it (sample, period, caveats the digests mention). At most {MAX_NOTES}, "
        f"each {MIN_BODY_CHARS}-{MAX_BODY_CHARS} characters, Traditional Chinese, one subject each. Do not "
        "restate what an existing node already says; do not note anything that is only a request or a plan with "
        "no result; do not record a position or price as if it were permanent.\n"
        f"- A DIRECTION is a strategy or approach that was investigated or used (at most {MAX_DIRECTIONS}), with "
        "its status as the digests show it now. `note_titles` lists the notes above that hold its detail; "
        "`existing_node_id` names a node already in the specification workspace that holds it.\n"
        f"- EVERY note and direction needs `evidence`: verbatim quotes ({MIN_QUOTE_CHARS}-{MAX_QUOTE_CHARS} "
        "characters, copied exactly, not paraphrased) taken from a digest, with that digest's node id. The "
        "harness checks each quote occurs in that digest; one that does not is discarded with its claim, so "
        "quote the sentences that carry the numbers and the conclusion.\n"
        "- Use only numbers that appear in the digests you cite for that item; never recompute, round or combine "
        "figures into new ones. The harness rejects an item containing a number its cited digests do not contain.\n"
        "- Prefer fewer, accurate items to many. If the digests disagree, or a result was later corrected, say so "
        "in the note and quote both.\n"
        "- stale_candidates: existing specification nodes that the digests show are no longer true (a changed "
        "rule, a position that was closed). They are only reported to the operator; you cannot change them. "
        f"At most {MAX_STALE}."
    )


def _json(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    from memtrace_harness.loop import parse_json_object

    return parse_json_object(raw)


# ---- validation ---------------------------------------------------------------------------


def _norm_key(title: str) -> str:
    return re.sub(r"\s+", " ", title).strip().lower()


def validate_notes(
    raw: Any, sources: dict[str, dict[str, Any]], spec_ids: set[str]
) -> tuple[list[dict[str, Any]], list[str]]:
    notes: list[dict[str, Any]] = []
    dropped: list[str] = []
    seen: set[str] = set()
    for item in (raw if isinstance(raw, list) else [])[: MAX_NOTES * 2]:
        if len(notes) >= MAX_NOTES:
            break
        if not isinstance(item, dict):
            dropped.append("筆記：格式不對")
            continue
        title = " ".join(str(item.get("title") or "").split())[:120]
        body = str(item.get("body") or "").strip()
        if not title or not MIN_BODY_CHARS <= len(body) <= MAX_BODY_CHARS:
            dropped.append(f"筆記「{title or '?'}」：標題空白或內容長度不合")
            continue
        if _norm_key(title) in seen:
            dropped.append(f"筆記「{title}」：同一輪重複的標題")
            continue
        evidence = verify_evidence(item.get("evidence"), sources)
        if not evidence:
            dropped.append(f"筆記「{title}」：沒有任何一段引文能在摘要原文中核對到")
            continue
        missing = unsupported_numbers(f"{title} {body}", evidence, sources)
        if missing:
            dropped.append(f"筆記「{title}」：數字 {', '.join(missing[:6])} 在引用的摘要裡找不到")
            continue
        links = [
            {"to_id": str(link["to_id"]), "relation": link["relation"]}
            for link in (item.get("links") if isinstance(item.get("links"), list) else [])
            if isinstance(link, dict) and link.get("relation") in LINK_RELATIONS
            and str(link.get("to_id")) in spec_ids
        ][:5]
        tags = [str(t).strip() for t in (item.get("tags") if isinstance(item.get("tags"), list) else []) if str(t).strip()][:6]
        seen.add(_norm_key(title))
        notes.append({"title": title, "body": body, "tags": tags, "links": links, "evidence": evidence})
    return notes, dropped


def validate_directions(
    raw: Any, sources: dict[str, dict[str, Any]], spec_ids: set[str], note_titles: set[str]
) -> tuple[list[dict[str, Any]], list[str]]:
    rows: list[dict[str, Any]] = []
    dropped: list[str] = []
    for item in (raw if isinstance(raw, list) else [])[: MAX_DIRECTIONS * 2]:
        if len(rows) >= MAX_DIRECTIONS:
            break
        if not isinstance(item, dict):
            dropped.append("方向：格式不對")
            continue
        name = " ".join(str(item.get("name") or "").split())[:80]
        summary = " ".join(str(item.get("summary") or "").split())[:300]
        conclusion = " ".join(str(item.get("conclusion") or "").split())[:500]
        if not name or not conclusion or item.get("status") not in STATUSES:
            dropped.append(f"方向「{name or '?'}」：缺名稱、結論或狀態不合法")
            continue
        existing = str(item.get("existing_node_id") or "")
        existing = existing if existing in spec_ids else ""
        evidence = verify_evidence(item.get("evidence"), sources)
        if not evidence and not existing:
            dropped.append(f"方向「{name}」：沒有可核對的引文，也沒有指向既有節點")
            continue
        missing = unsupported_numbers(f"{summary} {conclusion}", evidence, sources) if evidence else []
        if missing:
            dropped.append(f"方向「{name}」：數字 {', '.join(missing[:6])} 在引用的摘要裡找不到")
            continue
        titles = [
            t for t in (item.get("note_titles") if isinstance(item.get("note_titles"), list) else [])
            if isinstance(t, str) and _norm_key(t) in note_titles
        ]
        rows.append(
            {"name": name, "status": item["status"], "summary": summary, "conclusion": conclusion,
             "note_titles": titles, "existing_node_id": existing, "evidence": evidence}
        )
    return rows, dropped


def validate_stale(raw: Any, spec_ids: set[str]) -> list[tuple[str, str]]:
    result = []
    for item in (raw if isinstance(raw, list) else [])[:MAX_STALE]:
        if isinstance(item, dict) and str(item.get("node_id")) in spec_ids and str(item.get("reason") or "").strip():
            result.append((str(item["node_id"]), " ".join(str(item["reason"]).split())[:200]))
    return result


# ---- writing ------------------------------------------------------------------------------


def render_index(
    project: str, rows: list[dict[str, Any]], titles_to_ids: dict[str, str],
    sources: dict[str, dict[str, Any]], memory_workspace_id: str, when: str,
) -> str:
    lines = [
        f"# 研究與策略總覽：{project}",
        f"更新 {when}。由 Controller 依冷記憶的每日摘要整理；每一列的依據都附有經 Harness 核對過原文的引文。"
        "狀態以摘要最後的記載為準，不是永久結論。",
        "",
        "| 方向 | 狀態 | 結論 | 詳細 |",
        "| --- | --- | --- | --- |",
    ]
    for row in rows:
        refs = [f"{titles_to_ids[_norm_key(t)]}" for t in row["note_titles"] if _norm_key(t) in titles_to_ids]
        if row["existing_node_id"]:
            refs.append(row["existing_node_id"])
        detail = "、".join(dict.fromkeys(refs)) or "—"
        cell = lambda s: s.replace("|", "／")  # noqa: E731
        lines.append(f"| {cell(row['name'])} | {row['status']} | {cell(row['conclusion'])} | {detail} |")
    lines += ["", "## 各列的依據"]
    for row in rows:
        lines.append(f"### {row['name']}")
        lines.append(row["summary"] or "")
        for item in row["evidence"]:
            node = sources[item["node_id"]]
            lines.append(f"- {node.get('title')}（{memory_workspace_id}/{node['id']}）：「{item['quote']}」")
    return "\n".join(lines)


def _changed_after(node: dict[str, Any], cutoff: datetime) -> bool:
    for key in ("created_at", "updated_at"):
        raw = node.get(key)
        if not raw:
            continue
        try:
            stamp = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            continue
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        if stamp > cutoff:
            return True
    return False


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def run_promotion_pass(
    *,
    client: MemTraceClient,
    trace_store: TraceStore,
    project: str,
    spec_workspace_id: str,
    memory_workspace_id: str,
    purpose: str,
    caller: Callable[[str], str | None],
    dry_run: bool = False,
    only_if_new: bool = False,
) -> PromotionOutcome | None:
    """One promotion pass for a project. None when there is nothing to promote from or the model
    gave nothing usable. With dry_run nothing is written: the validated proposal is returned in
    `preview` so it can be read first. With only_if_new (the scheduled pass) a project whose
    digests have not changed since the last successful pass is skipped without a model call.
    Every real pass leaves a record, so a failure or a skip is not retried on the next tick."""
    run_id = None if dry_run else trace_store.start_gardening_run(spec_workspace_id, project, kind="promote")

    def finish(status: str, summary: dict | None = None) -> None:
        if run_id is not None:
            trace_store.finish_gardening_run(run_id, status, summary)

    if memory_workspace_id == spec_workspace_id:
        finish("ok", {"skipped": "no separate memory workspace"})
        return None
    memory_nodes = fetch_nodes(client, memory_workspace_id)
    digests = source_digests(memory_nodes)
    if not digests:
        finish("ok", {"skipped": "no digests"})
        return None
    if only_if_new:
        since = trace_store.last_gardening_success(spec_workspace_id, "promote")
        if since is not None:
            cutoff = datetime.fromisoformat(since)
            if not any(_changed_after(d, cutoff) for d in digests):
                finish("ok", {"skipped": "no new digests"})
                return None
    sources = {d["id"]: d for d in digests}
    spec_nodes = fetch_nodes(client, spec_workspace_id)
    spec = spec_knowledge(spec_nodes)
    spec_ids = {n["id"] for n in spec_nodes}
    existing = trace_store.list_promotions(spec_workspace_id)
    previous = trace_store.get_workspace_index(spec_workspace_id, INDEX_KIND)
    try:
        reply = _json(
            caller(
                build_prompt(
                    project=project, purpose=purpose, digests=digests, spec=spec,
                    existing_notes=existing, previous_index=previous["body"] if previous else None,
                )
            )
        )
    except Exception:
        logger.exception(f"[{project}] the promotion model call failed")
        reply = None
    if not isinstance(reply, dict):
        finish("failed")
        return None

    notes, dropped = validate_notes(reply.get("notes"), sources, spec_ids)
    note_keys = {_norm_key(n["title"]) for n in notes} | {e["key"] for e in existing}
    rows, dropped_rows = validate_directions(reply.get("directions"), sources, spec_ids, note_keys)
    dropped += dropped_rows
    outcome = PromotionOutcome(
        run_id=None, project=project, workspace_id=spec_workspace_id, dropped=dropped,
        stale=validate_stale(reply.get("stale_candidates"), spec_ids),
        notes=str(reply.get("summary") or "").strip()[:400], dry_run=dry_run,
    )
    outcome.preview = {"notes": notes, "directions": rows}
    if dry_run:
        return outcome

    outcome.run_id = run_id
    titles_to_ids = {e["key"]: e["node_id"] for e in existing}
    for note in notes:
        key = _norm_key(note["title"])
        body = note["body"] + "\n\n" + _provenance(note["evidence"], sources, memory_workspace_id)
        record = trace_store.get_promotion(spec_workspace_id, key)
        try:
            if record and record["body_hash"] == _hash(body):
                outcome.unchanged += 1
                titles_to_ids[key] = record["node_id"]
                continue
            if record:
                client.update_node(
                    workspace_id=spec_workspace_id, node_id=record["node_id"], body=body,
                    title=note["title"], stage="kb_promotion",
                )
                outcome.updated.append(note["title"])
                node_id = record["node_id"]
            else:
                node_id = client.create_node(
                    workspace_id=spec_workspace_id, title=note["title"], body=body, content_type="factual",
                    tags=[*CONCLUSION_TAGS, *note["tags"]], force_create=True, stage="kb_promotion",
                )
                trace_store.add_gardening_action(run_id, op="create", node_id=node_id)
                outcome.created.append(note["title"])
            trace_store.set_promotion(spec_workspace_id, key, node_id, _hash(body))
            titles_to_ids[key] = node_id
            for link in note["links"]:
                client.create_edge(
                    workspace_id=spec_workspace_id, from_id=node_id, to_id=link["to_id"],
                    relation=link["relation"], stage="kb_promotion",
                )
        except Exception as exc:
            logger.exception(f"[{project}] writing the note {note['title']!r} failed")
            outcome.dropped.append(f"筆記「{note['title']}」：寫入失敗（{exc}）")

    if rows:
        body = render_index(
            project, rows, titles_to_ids, sources, memory_workspace_id,
            datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        )
        try:
            if previous and previous["body"].strip() == body.strip():
                pass
            elif previous:
                client.update_node(
                    workspace_id=spec_workspace_id, node_id=previous["node_id"], body=body, stage="kb_promotion"
                )
                trace_store.set_workspace_index(spec_workspace_id, INDEX_KIND, previous["node_id"], body)
                outcome.index = "updated"
            else:
                node_id = client.create_node(
                    workspace_id=spec_workspace_id, title=f"研究與策略總覽：{project}", body=body,
                    content_type="document", tags=list(INDEX_TAGS), force_create=True, stage="kb_promotion",
                )
                client.update_node(workspace_id=spec_workspace_id, node_id=node_id, pinned=True, stage="kb_promotion")
                trace_store.add_gardening_action(run_id, op="create", node_id=node_id)
                trace_store.set_workspace_index(spec_workspace_id, INDEX_KIND, node_id, body)
                outcome.index = "created"
            outcome.index_rows = len(rows)
        except Exception as exc:
            logger.exception(f"[{project}] writing the overview failed")
            outcome.dropped.append(f"總覽：寫入失敗（{exc}）")
    trace_store.finish_gardening_run(
        run_id, "ok",
        {"kind": "promote", "created": outcome.created, "updated": outcome.updated, "index": outcome.index,
         "dropped": outcome.dropped[:20], "stale": outcome.stale},
    )
    return outcome


def describe_outcome(outcome: PromotionOutcome) -> str:
    head = f"📚 [{outcome.project}] 把冷記憶的研究結論整理進規格工作區（{outcome.workspace_id}）"
    if outcome.dry_run:
        head += "（試跑，沒有寫入）"
    parts = []
    if outcome.created:
        parts.append(f"新增 {len(outcome.created)} 則結論")
    if outcome.updated:
        parts.append(f"更新 {len(outcome.updated)} 則")
    if outcome.unchanged:
        parts.append(f"{outcome.unchanged} 則沒變")
    if outcome.index:
        parts.append("策略總覽已" + ("建立" if outcome.index == "created" else "更新") + f"（{outcome.index_rows} 個方向）")
    text = head + "：" + ("、".join(parts) if parts else "這次沒有可寫入的") + "。"
    if outcome.created or outcome.updated:
        titles = [*outcome.created, *outcome.updated]
        text += "\n" + "\n".join(f"- {t}" for t in titles[:8])
    if outcome.dropped:
        text += f"\n有 {len(outcome.dropped)} 項被擋下（引文對不上原文、格式不合等），沒有寫入。"
    if outcome.stale:
        text += "\n可能已過期的既有節點（只回報，沒動）：\n" + "\n".join(f"- {n}：{r}" for n, r in outcome.stale)
    if outcome.notes:
        text += f"\n{outcome.notes}"
    if outcome.created:
        text += "\n新增的節點都可以用下面的按鈕整批撤銷。"
    return text
