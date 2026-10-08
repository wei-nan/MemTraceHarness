"""Looking back at how the harness's own work went (復盤 of the work, not of trades).

The harness leaves a detailed record of everything it does: which runs ended well, which stopped
for a human or failed, which models ran out of quota and what took their place, what the gates
rejected, which questions it kept asking, which completions the operator sent back. Nobody reads
it. This turns it into a review in two parts, kept strictly apart:

* a *report* the harness computes from its own records — counts, shares, the worst cases — with no
  model involved, so every figure in it is a fact about what happened;
* an *interpretation* the Controller writes from that report: what the pattern is, what is going
  well, and what to change. Every finding must quote the report verbatim and every number it uses
  must occur in the report, so the model can say what the numbers mean but cannot bring its own.

The period of a review runs from where the last successful one ended; the first covers everything.
What the Controller proposes to change (a prompt, a model order, a routing rule) reaches the operator
as a decision card, and their answer is kept as precedent: the harness does not adopt a change to
its own working method by itself. Lessons about how work goes are written to the project's memory
workspace as notes, with the same quote and number checks as everything else the Controller records.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Callable

from memtrace_harness.decision_card import CARD_INSTRUCTION, normalize_card
from memtrace_harness.kb_promotion import MAX_QUOTE_CHARS, MIN_QUOTE_CHARS, _compact, _norm, unsupported_numbers

if TYPE_CHECKING:
    from memtrace_harness.trace_store import TraceStore

logger = logging.getLogger(__name__)

REVIEW_TIMEOUT_SECONDS = 600
MIN_RUNS_FOR_AUTO_REVIEW = 5
REVIEW_INTERVAL = timedelta(days=7)
RETRY_AFTER_FAILURE = timedelta(hours=6)
MAX_FINDINGS = 6
MAX_PROPOSALS = 3
MAX_LESSONS = 3
MAX_CASES = 8
INFO_NEEDED = ("ambiguous_requirement", "reasoning_gap", "gate_reject_twice")
REPORT_EVIDENCE_ID = "report"
PROPOSAL_REASON = "review_proposal"
LESSON_TAGS = ["harness", "controller", "work-lesson"]


def _pct(part: int, whole: int) -> str:
    return f"{round(100 * part / whole)}%" if whole else "0%"


def _status(summary_json: str | None) -> tuple[str, str]:
    try:
        data = json.loads(summary_json or "{}")
    except ValueError:
        return "unknown", ""
    return str(data.get("status") or "unknown"), str(data.get("recommendation") or "")


def _outcome_line(runs: list[tuple]) -> tuple[str, int, int, int]:
    counts = {"succeeded": 0, "needs_human": 0, "failed": 0}
    other = 0
    for run in runs:
        status, _ = _status(run[3])
        if status in counts:
            counts[status] += 1
        else:
            other += 1
    total = len(runs)
    text = (
        f"成功 {counts['succeeded']}、需要人 {counts['needs_human']}（{_pct(counts['needs_human'], total)}）、"
        f"失敗 {counts['failed']}（{_pct(counts['failed'], total)}）"
    )
    if other:
        text += f"、其他 {other}"
    return text, counts["succeeded"], counts["needs_human"], counts["failed"]


@dataclass
class Report:
    text: str
    runs: int
    period_start: str | None
    period_end: str
    case_ids: set[str] = field(default_factory=set)


def collect_report(
    trace_store: TraceStore,
    *,
    workspace_id: str,
    project: str,
    project_names: list[str],
    since: str | None,
    until: str,
) -> Report:
    data = trace_store.work_review_data(workspace_id, project_names, since, until)
    runs = data["runs"]
    lines = [
        f"# 工作報告：{project}（{workspace_id}）",
        f"期間：{since[:16] if since else '最早的紀錄'} 到 {until[:16]}；期間內共 {len(runs)} 次執行。",
        "",
        "## 一、執行結果",
    ]
    outcome, _, _, _ = _outcome_line(runs)
    lines.append(f"執行結果：{outcome}。")
    if since:
        try:
            span = datetime.fromisoformat(until) - datetime.fromisoformat(since)
            prev_start = (datetime.fromisoformat(since) - span).isoformat()
            prev = trace_store.work_review_data(workspace_id, project_names, prev_start, since)["runs"]
            if prev:
                lines.append(f"上一期（同樣長度）：共 {len(prev)} 次，{_outcome_line(prev)[0]}。")
        except ValueError:
            pass
    actions = {a or "?": n for a, n in data["controller_actions"]}
    if actions:
        total_actions = sum(actions.values())
        lines.append(
            "Controller 起始判斷：" + "、".join(f"{a} {n}" for a, n in sorted(actions.items(), key=lambda x: -x[1]))
            + f"（run_operational_action 占 {_pct(actions.get('run_operational_action', 0), total_actions)}）。"
        )
    stage_by_role: dict[str, dict[str, int]] = {}
    for profile, state, n in data["stages"]:
        stage_by_role.setdefault(profile, {})[state] = n
    lines += ["", "## 二、各角色的階段結果"]
    for profile, states in sorted(stage_by_role.items()):
        lines.append(f"{profile}：" + "、".join(f"{s} {n}" for s, n in sorted(states.items(), key=lambda x: -x[1])) + "。")

    cli: dict[tuple[str, str], dict[str, Any]] = {}
    fallbacks: dict[tuple[str, str], int] = {}
    for profile, provider, status, category, fallback_index, duration in data["cli"]:
        entry = cli.setdefault((profile, provider), {"calls": 0, "quota": 0, "other_fail": 0, "secs": []})
        entry["calls"] += 1
        if category == "quota_exhausted":
            entry["quota"] += 1
        elif status != "succeeded":
            entry["other_fail"] += 1
        elif duration:
            entry["secs"].append(duration / 1000)
        if fallback_index and fallback_index > 0:
            fallbacks[(profile, provider)] = fallbacks.get((profile, provider), 0) + 1
    lines += ["", "## 三、模型呼叫"]
    for (profile, provider), e in sorted(cli.items(), key=lambda x: -x[1]["calls"]):
        avg = f"，成功時平均 {round(sum(e['secs']) / len(e['secs']))} 秒" if e["secs"] else ""
        lines.append(
            f"{profile} / {provider}：共 {e['calls']} 次，額度用盡 {e['quota']}（{_pct(e['quota'], e['calls'])}），"
            f"其他失敗 {e['other_fail']}{avg}。"
        )
    if fallbacks:
        lines.append(
            "改由備援模型接手的呼叫：" + "、".join(f"{p} 的 {v} 備援呼叫 {n} 次" for (p, v), n in sorted(fallbacks.items(), key=lambda x: -x[1])) + "。"
        )

    gates = data["gates"]
    if gates:
        verdicts: dict[str, int] = {}
        for verdict, _, n in gates:
            verdicts[str(verdict)] = verdicts.get(str(verdict), 0) + n
        total_gates = sum(verdicts.values())
        mismatched = sum(n for v, code, n in gates if v == "PASS" and code not in (None, "none"))
        lines += [
            "", "## 四、Red Team 關卡",
            f"判決共 {total_gates} 個：" + "、".join(f"{v} {n}" for v, n in sorted(verdicts.items(), key=lambda x: -x[1]))
            + f"（PASS 占 {_pct(verdicts.get('PASS', 0), total_gates)}）。",
            "原因碼：" + "、".join(f"{v}/{c} {n}" for v, c, n in sorted(gates, key=lambda x: -x[2])) + "。",
        ]
        if mismatched:
            lines.append(f"判決是 PASS 但原因碼不是 none 的有 {mismatched} 個。")

    approvals = data["approvals"]
    lines += ["", "## 五、核准與提問"]
    if approvals:
        by: dict[tuple[str, str], int] = {}
        asked: dict[str, int] = {}
        last_asked: dict[str, str] = {}
        for conv, reason, status, created in approvals:
            by[(reason, status)] = by.get((reason, status), 0) + 1
            if reason in INFO_NEEDED:
                asked[conv] = asked.get(conv, 0) + 1
                last_asked[conv] = max(last_asked.get(conv, ""), str(created)[:10])
        lines.append("共 " + str(len(approvals)) + " 筆：" + "、".join(f"{r}/{s} {n}" for (r, s), n in sorted(by.items(), key=lambda x: -x[1])) + "。")
        goals = {r[1]: r[2] for r in runs}
        repeated = sorted(((c, n) for c, n in asked.items() if n >= 3), key=lambda x: -x[1])[:5]
        for conv, n in repeated:
            lines.append(
                f"同一個對話被問了 {n} 次：{conv}，最後一次在 {last_asked[conv]}"
                f"（{' '.join(str(goals.get(conv, '?')).split())[:60]}）。"
            )
    else:
        lines.append("這段期間沒有核准或提問。")

    decisions = data["decisions"]
    claims = {s: n for s, n in data["claims"]}
    lines += ["", "## 六、你的決定與驗收"]
    if decisions:
        by_kind: dict[str, list[int]] = {}
        for kind, outcome_name, followed in decisions:
            entry = by_kind.setdefault(kind, [0, 0])
            entry[0] += 1
            if followed is not None and not followed:
                entry[1] += 1
        lines.append("決定：" + "、".join(f"{k} 共 {a} 筆（沒照建議或被退回 {b}）" for k, (a, b) in sorted(by_kind.items())) + "。")
    else:
        lines.append("這段期間沒有記錄到決定。")
    if claims:
        lines.append("完成宣告：" + "、".join(f"{s} {n}" for s, n in claims.items()) + "。")

    held, reports = data["schedule_reports"][0] if data["schedule_reports"] else (0, 0)
    held = held or 0
    if reports:
        lines += ["", "## 七、排程", f"排程結果共 {reports} 則，其中 {held} 則由 Controller 留在摘要沒有推送（{_pct(held, reports)}）。"]

    cases = []
    case_ids: set[str] = set()
    for _, conv, goal, summary_json, created in reversed(runs):
        status, recommendation = _status(summary_json)
        if status in ("needs_human", "failed") and conv:
            cases.append(f"- {conv}（{created[:10]}，{status}）：{' '.join(goal.split())[:50]} → {' '.join(recommendation.split())[:90]}")
            case_ids.add(conv)
        if len(cases) >= MAX_CASES:
            break
    if cases:
        lines += ["", "## 八、停下來或失敗的案例（最近的在前）", *cases]
    return Report("\n".join(lines), len(runs), since, until, case_ids)


# ---- the Controller's interpretation ---------------------------------------------------------


# What the report cannot show and a reader would otherwise misjudge. Kept short and literal: each is
# something the harness does on purpose, or the meaning of a number that looks like a defect.
HARNESS_FACTS = (
    "How this harness works (facts the report does not show; do not call any of these a defect):\n"
    "- A stage tries its role's primary model. On quota exhaustion the harness records a cooldown for that "
    "model's quota bucket and goes to the role's next fallback model; while the cooldown lasts, a later stage "
    "of that role goes straight to the fallback and the primary's CLI is NOT invoked. So `quota_cooldown` "
    "stages are free skips, not failures; the calls that really ran out of quota are the 額度用盡 counts. "
    "A role with no fallback left stops the loop.\n"
    "- run_operational_action (do something once, no plan or review) is the normal route for monitoring, "
    "lookups and scripts. A high share of it in a project built around schedules is expected.\n"
    "- A schedule result the Controller held back for the nightly digest is deliberate: it repeated an earlier "
    "result. The report cannot tell whether a particular hold-back was right; do not call the count a problem.\n"
    "- A needs_human stop is the harness asking for a decision, not a failure. A Red Team REJECT sends the work "
    "back for one revision.\n"
    "- Records of the operator's decisions and of completion claims only began on 2026-10-07; an empty "
    "section before that is expected, not a gap.\n"
    "- The history includes problems that were fixed since; the dates tell you which cases are recent. "
    "Weigh recent cases more than old ones, and say when a pattern is only in old cases.\n\n"
)


def build_prompt(
    *, project: str, report: Report, previous: dict[str, Any] | None, precedent: str
) -> str:
    last = ""
    if previous and previous.get("findings"):
        last = "上一次復盤你的發現（看看這期有沒有改善或惡化）：\n" + "\n".join(
            f"- {f['title']}：{f['statement']}" for f in previous["findings"]
        ) + "\n\n"
    return (
        "You are the Controller of an agent harness, reviewing how the harness's OWN work went — not any "
        "trading or product result. Below is a report the harness computed from its records. You have no "
        "tools: judge only from the report. The harness checks your answer against it.\n\n"
        f"{HARNESS_FACTS}Project: {project}\n\n{report.text}\n\n{last}"
        + (f"{precedent}\n\n" if precedent else "")
        + "Answer with ONLY one JSON object:\n"
        '{"findings": [{"title": "...", "kind": "problem|working|unclear", "statement": "<what the numbers show and '
        'what it likely means, Traditional Chinese>", "evidence": [{"quote": "..."}]}],\n'
        ' "proposals": [{"title": "<a change to how the harness works>", "evidence": [{"quote": "..."}], '
        '"decision_card": { ... }}],\n'
        ' "lessons": [{"title": "...", "body": "<durable lesson about how work in this project goes>", '
        '"evidence": [{"quote": "..."}]}],\n'
        ' "summary": "<two sentences in Traditional Chinese for the operator>"}\n\n'
        "Rules:\n"
        f"- At most {MAX_FINDINGS} findings, {MAX_PROPOSALS} proposals, {MAX_LESSONS} lessons. Prefer the few that "
        "matter: the biggest cost, the clearest pattern, something that got worse, something that works.\n"
        f"- EVERY item needs `evidence`: verbatim quotes ({MIN_QUOTE_CHARS}-{MAX_QUOTE_CHARS} characters) copied "
        "exactly from the report. Use only numbers that appear in the report; never recompute or round them. An item "
        "whose quote is not in the report, or that uses a number the report does not contain, is discarded.\n"
        "- A pattern in a few cases is not a cause. Say 'unclear' and what would settle it rather than guess.\n"
        "- A lesson must be specific enough to change a decision (what to do, or not do, in which situation). "
        "Do not write platitudes such as 'quality gates matter'. If you have no such lesson, return none.\n"
        "- Do not propose what the harness already does (see the facts above).\n"
        "- A proposal changes how the harness works (a prompt, the order of fallback models, how a kind of request is "
        "routed, a threshold). Its decision_card is how the operator will be asked: "
        f"{CARD_INSTRUCTION}\n"
        "- Never propose something that approves, loosens or removes a gate or a permission.\n"
    )


def _json(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    from memtrace_harness.loop import parse_json_object

    return parse_json_object(raw)


def verify_report_evidence(raw: Any, report_text: str) -> list[str]:
    """Quotes that occur verbatim in the report (whitespace and markdown ignored)."""
    compact_report = _compact(report_text)
    quotes = []
    for item in (raw if isinstance(raw, list) else [])[:8]:
        quote = _norm(item.get("quote") if isinstance(item, dict) else None)
        if MIN_QUOTE_CHARS <= len(quote) <= MAX_QUOTE_CHARS and _compact(quote) in compact_report:
            quotes.append(quote)
        if len(quotes) >= 4:
            break
    return quotes


def _numbers_ok(text: str, quotes: list[str], report: Report) -> list[str]:
    source = {REPORT_EVIDENCE_ID: {"id": REPORT_EVIDENCE_ID, "body": report.text}}
    return unsupported_numbers(text, [{"node_id": REPORT_EVIDENCE_ID, "quote": q} for q in quotes], source)


def validate_review(reply: dict[str, Any], report: Report) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
    findings: list[dict[str, Any]] = []
    proposals: list[dict[str, Any]] = []
    lessons: list[dict[str, Any]] = []
    dropped: list[str] = []

    for item in (reply.get("findings") if isinstance(reply.get("findings"), list) else [])[: MAX_FINDINGS * 2]:
        if len(findings) >= MAX_FINDINGS:
            break
        if not isinstance(item, dict):
            dropped.append("發現：格式不對")
            continue
        title = " ".join(str(item.get("title") or "").split())[:100]
        statement = " ".join(str(item.get("statement") or "").split())[:500]
        quotes = verify_report_evidence(item.get("evidence"), report.text)
        if not title or not statement or not quotes:
            dropped.append(f"發現「{title or '?'}」：缺內容，或引文在報告裡核對不到")
            continue
        missing = _numbers_ok(f"{title} {statement}", quotes, report)
        if missing:
            dropped.append(f"發現「{title}」：數字 {', '.join(missing[:5])} 不在報告裡")
            continue
        kind = item.get("kind") if item.get("kind") in ("problem", "working", "unclear") else "unclear"
        findings.append({"title": title, "kind": kind, "statement": statement, "evidence": quotes})

    for item in (reply.get("proposals") if isinstance(reply.get("proposals"), list) else [])[: MAX_PROPOSALS * 2]:
        if len(proposals) >= MAX_PROPOSALS:
            break
        if not isinstance(item, dict):
            dropped.append("建議：格式不對")
            continue
        title = " ".join(str(item.get("title") or "").split())[:100]
        card = normalize_card(item.get("decision_card"))
        quotes = verify_report_evidence(item.get("evidence"), report.text)
        if not title or card is None or not quotes:
            dropped.append(f"建議「{title or '?'}」：缺標題、決策卡格式不對，或引文在報告裡核對不到")
            continue
        card_text = " ".join([title, card["situation"], card["reason"], *[o["action"] + o["tradeoff"] for o in card["options"]]])
        missing = _numbers_ok(card_text, quotes, report)
        if missing:
            dropped.append(f"建議「{title}」：數字 {', '.join(missing[:5])} 不在報告裡")
            continue
        proposals.append({"title": title, "card": {**card, "basis": []}, "evidence": quotes})

    for item in (reply.get("lessons") if isinstance(reply.get("lessons"), list) else [])[: MAX_LESSONS * 2]:
        if len(lessons) >= MAX_LESSONS:
            break
        if not isinstance(item, dict):
            dropped.append("教訓：格式不對")
            continue
        title = " ".join(str(item.get("title") or "").split())[:100]
        body = str(item.get("body") or "").strip()
        quotes = verify_report_evidence(item.get("evidence"), report.text)
        if not title or not 30 <= len(body) <= 1500 or not quotes:
            dropped.append(f"教訓「{title or '?'}」：內容長度不合，或引文在報告裡核對不到")
            continue
        missing = _numbers_ok(f"{title} {body}", quotes, report)
        if missing:
            dropped.append(f"教訓「{title}」：數字 {', '.join(missing[:5])} 不在報告裡")
            continue
        lessons.append({"title": title, "body": body, "evidence": quotes})
    return {"findings": findings, "proposals": proposals, "lessons": lessons}, dropped


def write_lessons(
    client: Any, trace_store: TraceStore, outcome: ReviewOutcome, memory_workspace_id: str
) -> list[str]:
    """Record the review's lessons in the project's memory workspace, keyed by title so a later
    review updates rather than repeats them, with the quotes that support them. Returns the titles
    written. A failure to write one never stops the others or the review."""
    if outcome.dry_run or not outcome.lessons:
        return []
    import hashlib

    run_id = trace_store.start_gardening_run(memory_workspace_id, outcome.project, kind="review")
    written: list[str] = []
    for lesson in outcome.lessons:
        key = "lesson:" + " ".join(lesson["title"].lower().split())
        quotes = "\n".join(f"- 「{q}」" for q in lesson["evidence"])
        body = (
            f"{lesson['body']}\n\n## 出處（工作報告，期間 {outcome.report.period_start or '最早'} 到 "
            f"{outcome.report.period_end[:10]}，復盤 #{outcome.review_id}）\n{quotes}"
        )
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]
        try:
            record = trace_store.get_promotion(memory_workspace_id, key)
            if record and record["body_hash"] == digest:
                continue
            if record:
                client.update_node(
                    workspace_id=memory_workspace_id, node_id=record["node_id"], body=body,
                    title=lesson["title"], stage="work_review",
                )
                node_id = record["node_id"]
            else:
                node_id = client.create_node(
                    workspace_id=memory_workspace_id, title=lesson["title"], body=body, content_type="procedural",
                    tags=list(LESSON_TAGS), force_create=True, stage="work_review",
                )
                trace_store.add_gardening_action(run_id, op="create", node_id=node_id)
            trace_store.set_promotion(memory_workspace_id, key, node_id, digest)
            written.append(lesson["title"])
        except Exception:
            logger.exception(f"[{outcome.project}] writing the lesson {lesson['title']!r} failed")
    trace_store.finish_gardening_run(run_id, "ok", {"kind": "review", "lessons": written})
    outcome.lesson_run_id = run_id
    return written


# ---- the review record node ------------------------------------------------------------------

REVIEW_INDEX_KIND = "work-review"
REVIEW_INDEX_TAGS = ["harness", "controller", "work-review-index"]
MAX_LESSONS_IN_INDEX = 8
MAX_HISTORY_LINES = 6


def render_review_index(project: str, reviews: list[dict[str, Any]]) -> str:
    """The record of the harness's own reviews, newest first: the latest findings in full, the lessons
    kept so far, and a line per earlier review. Rendered by the harness from what the reviews already
    passed the checks for. It describes the past: it says so, and a later review replaces it."""
    latest = reviews[0]
    result = latest.get("result") or {}
    start = (latest.get("period_start") or "最早的紀錄")[:10]
    lines = [
        f"# 工作復盤紀錄：{project}",
        "由 Controller 依 Harness 自己的執行紀錄整理，每則都經過對照報告的核對。這描述的是過去：問題可能已經"
        "修好或改變，引用前先確認它現在是否還存在；下一次復盤會取代這份內容。",
        "",
        f"## 最近一次復盤（復盤 #{latest['id']}，{start} 到 {latest['period_end'][:10]}，共 {result.get('runs', '?')} 次執行）",
        str(result.get("summary") or ""),
    ]
    marks = {"problem": "⚠️", "working": "✅", "unclear": "❔"}
    for finding in result.get("findings") or []:
        lines.append(f"- {marks.get(finding.get('kind'), '❔')} {finding['title']}：{finding['statement']}")
    lessons: list[tuple[str, str]] = []
    seen: set[str] = set()
    for review in reviews:
        res = review.get("result") or {}
        full = res.get("lessons_full") or [{"title": t, "body": ""} for t in res.get("lessons") or []]
        for lesson in full:
            key = " ".join(lesson["title"].lower().split())
            if key not in seen and len(lessons) < MAX_LESSONS_IN_INDEX:
                seen.add(key)
                lessons.append((lesson["title"], lesson.get("body") or ""))
    if lessons:
        lines += ["", "## 目前記下的教訓"]
        lines += [f"- **{t}**" + (f"：{' '.join(b.split())[:300]}" if b else "") for t, b in lessons]
    older = reviews[1:MAX_HISTORY_LINES]
    if older:
        lines += ["", "## 之前的復盤"]
        lines += [
            f"- 復盤 #{r['id']}（到 {r['period_end'][:10]}）：{str((r.get('result') or {}).get('summary') or '')[:160]}"
            for r in older
        ]
    return "\n".join(lines)


def write_review_index(
    client: Any, trace_store: TraceStore, outcome: ReviewOutcome, memory_workspace_id: str
) -> str | None:
    """Create or update the pinned record node in the memory workspace and link it to the lesson
    nodes it lists. Returns "created", "updated" or None (nothing to write, or it failed — never
    raises: the review has already been delivered)."""
    if outcome.dry_run:
        return None
    reviews = trace_store.list_work_reviews(outcome.workspace_id, MAX_HISTORY_LINES)
    if not reviews:
        return None
    body = render_review_index(outcome.project, reviews)
    previous = trace_store.get_workspace_index(memory_workspace_id, REVIEW_INDEX_KIND)
    try:
        if previous and previous["body"].strip() == body.strip():
            return None
        if previous:
            client.update_node(
                workspace_id=memory_workspace_id, node_id=previous["node_id"], body=body, stage="work_review"
            )
            node_id, result = previous["node_id"], "updated"
        else:
            node_id = client.create_node(
                workspace_id=memory_workspace_id, title=f"工作復盤紀錄：{outcome.project}", body=body,
                content_type="context", tags=list(REVIEW_INDEX_TAGS), force_create=True, stage="work_review",
            )
            client.update_node(workspace_id=memory_workspace_id, node_id=node_id, pinned=True, stage="work_review")
            result = "created"
        trace_store.set_workspace_index(memory_workspace_id, REVIEW_INDEX_KIND, node_id, body)
        # Connect it to the lesson nodes, so the record is reachable from them and they from it.
        for review in reviews:
            for title in (review.get("result") or {}).get("lessons") or []:
                record = trace_store.get_promotion(memory_workspace_id, "lesson:" + " ".join(title.lower().split()))
                if record:
                    try:
                        client.create_edge(
                            workspace_id=memory_workspace_id, from_id=node_id, to_id=record["node_id"],
                            relation="related_to", stage="work_review",
                        )
                    except Exception:
                        logger.exception("linking the review record to a lesson failed")
        return result
    except Exception:
        logger.exception(f"[{outcome.project}] writing the work-review record failed")
        return None


# ---- one review -----------------------------------------------------------------------------


@dataclass
class ReviewOutcome:
    review_id: int | None
    project: str
    workspace_id: str
    report: Report
    findings: list[dict[str, Any]] = field(default_factory=list)
    proposals: list[dict[str, Any]] = field(default_factory=list)
    lessons: list[dict[str, Any]] = field(default_factory=list)
    lesson_run_id: int | None = None
    dropped: list[str] = field(default_factory=list)
    summary: str = ""
    dry_run: bool = False


def review_due(trace_store: TraceStore, workspace_id: str, now: datetime | None = None) -> bool:
    """Whether the weekly automatic review is due: a week since the last successful one (the
    first review is always asked for by the operator), and not within six hours of a failed try."""
    now = now or datetime.now(timezone.utc)
    last_any = trace_store.last_work_review(workspace_id, ok_only=False)
    if last_any and last_any["status"] != "ok" and now - datetime.fromisoformat(last_any["started_at"]) < RETRY_AFTER_FAILURE:
        return False
    last_ok = trace_store.last_work_review(workspace_id)
    return last_ok is not None and now - datetime.fromisoformat(last_ok["period_end"]) >= REVIEW_INTERVAL


def run_work_review(
    *,
    trace_store: TraceStore,
    workspace_id: str,
    project: str,
    project_names: list[str],
    caller: Callable[[str], str | None],
    precedent: str = "",
    min_runs: int = 0,
    dry_run: bool = False,
    now: datetime | None = None,
) -> ReviewOutcome | None:
    """Review the work since the last successful review. None when the period holds fewer than
    `min_runs` runs or the model gave nothing usable. Nothing about the period is lost on a
    failure: the next attempt starts from the same point."""
    until = (now or datetime.now(timezone.utc)).isoformat()
    previous_row = trace_store.last_work_review(workspace_id)
    since = previous_row["period_end"] if previous_row else None
    report = collect_report(
        trace_store, workspace_id=workspace_id, project=project, project_names=project_names,
        since=since, until=until,
    )
    if report.runs < max(min_runs, 1):
        return None
    review_id = None if dry_run else trace_store.start_work_review(workspace_id, project, since, until)
    try:
        reply = _json(
            caller(build_prompt(
                project=project, report=report,
                previous=previous_row["result"] if previous_row else None, precedent=precedent,
            ))
        )
    except Exception:
        logger.exception(f"[{project}] the work-review model call failed")
        reply = None
    if not isinstance(reply, dict):
        if review_id is not None:
            trace_store.finish_work_review(review_id, "failed", report=report.text)
        return None
    checked, dropped = validate_review(reply, report)
    outcome = ReviewOutcome(
        review_id=review_id, project=project, workspace_id=workspace_id, report=report,
        findings=checked["findings"], proposals=checked["proposals"], lessons=checked["lessons"],
        dropped=dropped, summary=str(reply.get("summary") or "").strip()[:400], dry_run=dry_run,
    )
    if review_id is not None:
        trace_store.finish_work_review(
            review_id, "ok", report=report.text,
            result={"findings": outcome.findings, "proposals": [p["title"] for p in outcome.proposals],
                    "lessons": [l["title"] for l in outcome.lessons],
                    "lessons_full": [{"title": l["title"], "body": l["body"]} for l in outcome.lessons],
                    "runs": report.runs, "dropped": dropped[:20],
                    "summary": outcome.summary},
        )
    return outcome


def describe_review(outcome: ReviewOutcome) -> str:
    report = outcome.report
    start = report.period_start[:10] if report.period_start else "最早的紀錄"
    head = f"🔍 [{outcome.project}] 工作復盤：{start} 到 {report.period_end[:10]}，共 {report.runs} 次執行"
    if outcome.dry_run:
        head += "（試跑，沒有寫入）"
    lines = [head + "。"]
    if outcome.summary:
        lines.append(outcome.summary)
    marks = {"problem": "⚠️", "working": "✅", "unclear": "❔"}
    for finding in outcome.findings:
        lines.append(f"{marks[finding['kind']]} {finding['title']}：{finding['statement']}")
    if outcome.proposals:
        lines.append(f"我有 {len(outcome.proposals)} 項改進建議，下面逐項請你決定。")
    if outcome.lessons:
        lines.append("記下的教訓：" + "、".join(l["title"] for l in outcome.lessons) + "。")
    if outcome.dropped:
        lines.append(f"另有 {len(outcome.dropped)} 項因為對不上報告而被擋下，沒有採用。")
    if not outcome.findings:
        lines.append("這次沒有得出經過核對的發現。")
    return "\n".join(lines)
