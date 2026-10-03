"""Topic recall — the slow path behind chat.

Chat answers fast from a small, fixed window (recent transcript, a few daily digests).
What it cannot do is notice that today's topic was already discussed three weeks ago, or
how it connects to something decided elsewhere. This module does that in the background
with a stronger model, once per human message:

  1. judge the topic: does the message continue an active topic, start a new one, or is it
     small talk that needs no recall;
  2. for a new topic, explore the cold-memory and spec workspaces (the harness searches
     first and hands the hits over; the model may dig further with MemTrace's read tools);
  3. write a short-lived *topic brief* — summary plus the related nodes it found — to local
     SQLite, where the chat model reads it on its next turn;
  4. if it found something the human probably no longer has in mind, push one message.

A topic with no history is fine: the brief is marked fresh and nothing is pushed.

Grounding is enforced here, not trusted to the model: every related node the model cites
must be a search hit the harness handed over or be confirmed to exist in MemTrace;
anything else is dropped, and a push is only sent for related nodes that survived.
Briefs are short-lived knowledge — they expire unless the topic comes up again, and only
the nightly digest (not this module) can promote anything into long-term memory.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from memtrace_harness.memory_digest import DigestError, _extract_json_object

if TYPE_CHECKING:
    from memtrace_harness.config import HarnessConfig
    from memtrace_harness.memtrace_client import MemTraceClient
    from memtrace_harness.scope import ProjectScope
    from memtrace_harness.trace_store import TraceStore

logger = logging.getLogger(__name__)

RECENT_TURNS_FOR_RECALL = 10
SEARCH_HITS_PER_WORKSPACE = 6
MAX_RELATED = 6
BRIEF_SUMMARY_CHARS = 700
BRIEF_TITLE_CHARS = 60
PUSH_TEXT_CHARS = 600
HIT_EXCERPT_CHARS = 240
# Messages shorter than this ("ok", "好", "謝謝") never start a recall run.
MIN_MESSAGE_CHARS = 4
RECALL_TIMEOUT_SECONDS = 300

DECISIONS = ("continue", "new", "none")

# prompt -> (stdout, provider, model). Raises RecallError when no candidate answered.
ModelCaller = Callable[[str], tuple[str, str | None, str | None]]
# query -> raw search hits (dicts with id/title/...), across the project's workspaces.
SearchFn = Callable[[str], list[dict[str, Any]]]
# (workspace_id, node_id) -> {"title": ...} when the node exists, else None.
VerifyFn = Callable[[str, str], dict[str, Any] | None]


class RecallError(RuntimeError):
    pass


@dataclass
class RecallOutcome:
    action: str  # "none" | "continue" | "new"
    brief_id: int | None = None
    push_text: str | None = None
    provider: str | None = None
    model: str | None = None


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _clip(text: Any, limit: int) -> str:
    return str(text or "").strip()[:limit]


def normalize_hits(raw_hits: list[dict[str, Any]], workspace_id: str) -> list[dict[str, Any]]:
    """Trim MemTrace search hits to what the prompt needs, tagged with their workspace."""
    hits = []
    for hit in raw_hits:
        if not isinstance(hit, dict) or not hit.get("id"):
            continue
        hits.append(
            {
                "node_id": str(hit["id"]),
                "workspace_id": workspace_id,
                "title": _clip(hit.get("title"), 120),
                "created_at": _clip(hit.get("created_at"), 10),
                "excerpt": _clip(hit.get("body_excerpt_200") or hit.get("summary_1line"), HIT_EXCERPT_CHARS),
            }
        )
    return hits


def _render_briefs_for_prompt(briefs: list[dict]) -> str:
    if not briefs:
        return "（目前沒有有效的主題簡報）"
    return "\n".join(
        f"- brief_id={b['id']}｜{b['title']}｜{b['summary'][:200]}" for b in briefs
    )


def _render_hits_for_prompt(hits: list[dict[str, Any]]) -> str:
    if not hits:
        return "（搜尋沒有命中任何節點）"
    return "\n".join(
        f"- node_id={h['node_id']} workspace_id={h['workspace_id']} "
        f"[{h['created_at'] or '?'}] {h['title']}：{h['excerpt']}"
        for h in hits
    )


def build_recall_prompt(
    *,
    project: str,
    message: str,
    recent_transcript: str,
    briefs: list[dict],
    hits: list[dict[str, Any]],
    memory_workspace_id: str,
    spec_workspace_id: str,
) -> str:
    return (
        f"你是專案「{project}」的背景回想模型。你不直接跟使用者對話；你的輸出會被存成一份"
        "短效期的「主題簡報」，供稍後的聊天模型參考，必要時也會推送給使用者。一律使用繁體中文。\n\n"
        "工作：判斷使用者最新訊息的主題，並依需要整理簡報。\n"
        "decision 只能三選一：\n"
        '- "continue"：訊息延續「目前有效的主題簡報」中的某一個（填 brief_id）。只有出現新資訊、'
        "或發現新的關聯時才把 update 設為 true 並重寫 summary/related；否則 update=false。\n"
        '- "new"：這是不屬於任何現有簡報的新主題。請整理出標題、摘要與關聯。\n'
        '- "none"：寒暄、確認、很短的回應，沒有值得整理的主題。\n\n'
        "關聯（related）的規則：\n"
        "- 只能引用下方「搜尋命中」中出現的節點，或你用 MemTrace 唯讀工具"
        f"（search_nodes、get_node、list_nodes、traverse；冷記憶工作區 {memory_workspace_id}、"
        f"規格工作區 {spec_workspace_id}）自己查到且確定存在的節點。不得編造 node_id。\n"
        "- 每一條要說明為什麼跟這個主題有關（why），並附上節點日期（date，YYYY-MM-DD，不確定就留空）。\n"
        "- 這個主題過去沒討論過是正常的：related 給 []，summary 寫成這是新主題即可，不要硬湊關聯。\n"
        "- 偏好少而準：最多 6 條，寧可漏掉也不要放弱相關的。\n\n"
        "push：只有在「找到了使用者此刻可能已不記得的關聯」才設 true，並用 push_text 寫"
        f"一段給使用者看的話（{PUSH_TEXT_CHARS} 字內，直接講過去談過什麼、跟現在的關係，"
        "不要客套）。全新主題、沒有新發現、或只是延續剛剛正在談的內容時 push=false。\n\n"
        "只輸出一個 JSON 物件，格式：\n"
        '{"decision": "continue|new|none", "brief_id": <整數或 null>, "title": "<主題標題>", '
        '"update": <true|false>, "summary": "<簡報摘要，含目前討論到哪、已知的決定與未決事項>", '
        '"related": [{"node_id": "...", "workspace_id": "...", "title": "...", "why": "...", '
        '"date": "YYYY-MM-DD"}], "push": <true|false>, "push_text": "<要推給使用者的話或空字串>"}\n'
        '當 decision 是 "none" 時其他欄位可留空。\n\n'
        f"目前有效的主題簡報：\n{_render_briefs_for_prompt(briefs)}\n\n"
        f"最近對話（舊到新）：\n{recent_transcript or '（無）'}\n\n"
        f"使用者最新訊息：\n{message}\n\n"
        f"搜尋命中（harness 以最新訊息搜尋的結果）：\n{_render_hits_for_prompt(hits)}\n"
    )


def _validate_related(
    raw: Any,
    allowed: dict[str, dict[str, Any]],
    verify: VerifyFn | None,
    default_workspaces: tuple[str, ...],
) -> list[dict[str, Any]]:
    """Keep only related nodes that are real. A hit the harness supplied is trusted; a
    node the model found with its own tools must be confirmed to exist."""
    if not isinstance(raw, list):
        return []
    related: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        node_id = str(item.get("node_id") or "").strip()
        if not node_id or node_id in seen:
            continue
        why = _clip(item.get("why"), 200)
        if not why:
            continue
        title = _clip(item.get("title"), 120)
        workspace_id = str(item.get("workspace_id") or "").strip()
        if node_id in allowed:
            known = allowed[node_id]
            workspace_id = known["workspace_id"]
            title = title or known["title"]
        elif verify is not None:
            found = None
            for ws in ([workspace_id] if workspace_id else []) + [
                w for w in default_workspaces if w != workspace_id
            ]:
                try:
                    found = verify(ws, node_id)
                except Exception:
                    logger.exception("verifying recalled node %s failed", node_id)
                    found = None
                if found:
                    workspace_id = ws
                    title = title or _clip(found.get("title"), 120)
                    break
            if not found:
                continue
        else:
            continue
        seen.add(node_id)
        related.append(
            {
                "node_id": node_id,
                "workspace_id": workspace_id,
                "title": title,
                "why": why,
                "date": _clip(item.get("date"), 10),
            }
        )
        if len(related) >= MAX_RELATED:
            break
    return related


def _format_push(title: str, push_text: str, related: list[dict[str, Any]]) -> str:
    lines = [f"🧠 關聯提示：{title}", "", _clip(push_text, PUSH_TEXT_CHARS)]
    refs = [
        f"・{r['date'] + ' ' if r['date'] else ''}{r['title'] or r['node_id']}（{r['node_id']}）"
        for r in related[:4]
    ]
    if refs:
        lines += ["", "出處：", *refs]
    return "\n".join(lines)


def run_topic_recall(
    trace_store: TraceStore,
    project: str,
    *,
    message: str,
    recent_transcript: str,
    source_turns: list[int],
    search: SearchFn,
    verify: VerifyFn | None,
    call_model: ModelCaller,
    memory_workspace_id: str,
    spec_workspace_id: str,
    ttl_hours: int,
    now: datetime | None = None,
) -> RecallOutcome:
    """One recall pass for one human message. Returns what was done; any push text is
    for the caller to send. Raises RecallError when the model answer is unusable."""
    now = now or datetime.now(timezone.utc)
    now_iso = _iso(now)
    expires_at = _iso(now + timedelta(hours=ttl_hours))
    briefs = trace_store.list_active_topic_briefs(project, now_iso)

    try:
        hits = search(message)
    except Exception:
        logger.exception("[%s] recall pre-search failed; the model will have no hits", project)
        hits = []
    allowed = {h["node_id"]: h for h in hits}

    stdout, provider, model = call_model(
        build_recall_prompt(
            project=project,
            message=message,
            recent_transcript=recent_transcript,
            briefs=briefs,
            hits=hits,
            memory_workspace_id=memory_workspace_id,
            spec_workspace_id=spec_workspace_id,
        )
    )
    try:
        answer = _extract_json_object(stdout)
    except DigestError as exc:
        raise RecallError(str(exc)) from exc

    decision = str(answer.get("decision") or "").strip().lower()
    if decision not in DECISIONS:
        raise RecallError(f"unknown decision {decision!r}")
    if decision == "none":
        return RecallOutcome("none", provider=provider, model=model)

    by_id = {b["id"]: b for b in briefs}
    brief_id = answer.get("brief_id")
    if decision == "continue":
        try:
            brief_id = int(brief_id)
        except (TypeError, ValueError):
            brief_id = None
        if brief_id not in by_id:
            # The model named a brief that doesn't exist (or expired mid-run). Treating
            # it as new keeps the content instead of dropping it.
            logger.warning("[%s] recall named unknown brief_id %r; treating as new", project, brief_id)
            decision = "new"

    summary = _clip(answer.get("summary"), BRIEF_SUMMARY_CHARS)
    title = _clip(answer.get("title"), BRIEF_TITLE_CHARS)
    related = _validate_related(
        answer.get("related"), allowed, verify, (memory_workspace_id, spec_workspace_id)
    )

    if decision == "continue":
        brief = by_id[brief_id]
        if not answer.get("update") or not summary:
            trace_store.extend_topic_brief(brief_id, expires_at)
            return RecallOutcome("continue", brief_id, provider=provider, model=model)
        previously_known = {r["node_id"] for r in brief["related"]}
        # Keep what was already found even if this pass searched for something narrower.
        merged = list(related)
        merged_ids = {r["node_id"] for r in merged}
        merged += [r for r in brief["related"] if r["node_id"] not in merged_ids]
        trace_store.update_topic_brief(
            brief_id,
            summary=summary,
            related=merged[:MAX_RELATED],
            source_turns=sorted(set(brief["source_turns"]) | set(source_turns)),
            provider=provider,
            model=model,
            expires_at=expires_at,
        )
        newly_found = [r for r in related if r["node_id"] not in previously_known]
        push_text = None
        if answer.get("push") is True and newly_found and str(answer.get("push_text") or "").strip():
            push_text = _format_push(brief["title"], str(answer["push_text"]), newly_found)
        return RecallOutcome("continue", brief_id, push_text, provider, model)

    if not summary or not title:
        raise RecallError("a new topic needs a title and a summary")
    brief_id = trace_store.insert_topic_brief(
        project=project,
        title=title,
        summary=summary,
        fresh=not related,
        related=related,
        source_turns=sorted(set(source_turns)),
        provider=provider,
        model=model,
        expires_at=expires_at,
    )
    push_text = None
    if answer.get("push") is True and related and str(answer.get("push_text") or "").strip():
        push_text = _format_push(title, str(answer["push_text"]), related)
    return RecallOutcome("new", brief_id, push_text, provider, model)


# --- what the chat model sees ---------------------------------------------------------

BRIEFS_HEADER = (
    "Topic briefs — short-lived notes a slower background model wrote about topics in this "
    "conversation, including what the knowledge base already holds on them. They are "
    "drafts, not accepted spec. Use one only when the current message is actually about "
    "that topic; if it is not, ignore them. A brief marked 整理中 is still being written, so "
    "do not guess what it will say"
)


def render_briefs_context(
    trace_store: TraceStore, project: str, now: datetime | None = None
) -> str:
    """The briefs block for the chat prompt: the most recently touched brief in full, the
    rest as one-line pointers, plus whether the slow model is still working. Empty when
    there is nothing to say."""
    now = now or datetime.now(timezone.utc)
    briefs = trace_store.list_active_topic_briefs(project, _iso(now))
    state = trace_store.get_recall_state(project)
    if not briefs and not state["pending"]:
        return ""
    lines = []
    if state["pending"]:
        lines.append("狀態：整理中——背景模型還在處理你剛剛的訊息，簡報可能還沒更新。")
    if briefs:
        current = briefs[0]
        updated = datetime.fromisoformat(current["updated_at"]).astimezone(timezone.utc)
        age_min = max(0, int((now - updated).total_seconds() // 60))
        lines.append(
            f"【最近的主題】{current['title']}（{age_min} 分鐘前更新"
            + ("；這是全新主題，知識庫裡沒有過去的討論" if current["fresh"] else "")
            + f"）\n{current['summary']}"
        )
        for r in current["related"]:
            lines.append(
                f"  ・關聯 {r['date'] + ' ' if r['date'] else ''}{r['title']}"
                f"（node {r['node_id']}，工作區 {r['workspace_id']}）：{r['why']}"
            )
        for other in briefs[1:]:
            lines.append(f"【其他有效主題】{other['title']}：{other['summary'][:100]}")
    if state["last_error"] and not briefs:
        lines.append("（上一次背景整理失敗，沒有可用簡報。）")
    return "\n".join(lines)


# --- wiring ---------------------------------------------------------------------------


class TopicRecallService:
    """Owns the background threads: one recall run at a time per project (runs queue on a
    lock, so briefs are written in message order), and the pending counter the chat model
    reads as 整理中."""

    def __init__(
        self,
        config: HarnessConfig,
        trace_store: TraceStore,
        memtrace_client: MemTraceClient | None,
        *,
        call_model_factory: Callable[[str], ModelCaller],
        send: Callable[[int, str], Any],
        record_push: Callable[[str, str], Any],
    ) -> None:
        self.config = config
        self.trace_store = trace_store
        self.memtrace_client = memtrace_client
        self._call_model_factory = call_model_factory
        self._send = send
        self._record_push = record_push
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _lock_for(self, project: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(project, threading.Lock())

    def start(self, scope: ProjectScope, chat_id: int, message: str) -> bool:
        """Queue a recall run for this message. Returns False when it was skipped."""
        if not self.config.recall_enabled or len(message.strip()) < MIN_MESSAGE_CHARS:
            return False
        if not self.config.recall_candidates_for(scope.name):
            return False
        self.trace_store.add_recall_pending(scope.name, 1)
        threading.Thread(
            target=self._run,
            args=(scope, chat_id, message),
            name=f"topic-recall-{scope.name}",
            daemon=True,
        ).start()
        return True

    def _run(self, scope: ProjectScope, chat_id: int, message: str) -> None:
        error: str | None = None
        try:
            with self._lock_for(scope.name):
                outcome = self.run_once(scope, message)
            if outcome.push_text:
                if self._send(chat_id, outcome.push_text):
                    self.trace_store.mark_topic_brief_pushed(outcome.brief_id)
                self._record_push(scope.name, outcome.push_text)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"[:300]
            logger.exception("[%s] topic recall failed", scope.name)
        finally:
            self.trace_store.add_recall_pending(scope.name, -1)
            self.trace_store.finish_recall_run(scope.name, error)

    def run_once(self, scope: ProjectScope, message: str) -> RecallOutcome:
        memory_ws = self.config.memory_workspace_id_for(scope.name, scope.workspace_id)
        spec_ws = scope.workspace_id
        workspaces = tuple(dict.fromkeys((memory_ws, spec_ws)))
        client = self.memtrace_client

        def search(query: str) -> list[dict[str, Any]]:
            if client is None:
                return []
            hits: list[dict[str, Any]] = []
            for ws in workspaces:
                hits += normalize_hits(
                    client.search_nodes(
                        workspace_id=ws, query=query, limit=SEARCH_HITS_PER_WORKSPACE, stage="topic_recall"
                    ),
                    ws,
                )
            return hits

        def verify(workspace_id: str, node_id: str) -> dict[str, Any] | None:
            if client is None:
                return None
            try:
                node = client.get_node(
                    workspace_id=workspace_id, node_id=node_id, detail_level="summary", stage="topic_recall"
                )
            except Exception:
                return None
            return node if node.get("id") or node.get("title") else None

        turns = self.trace_store.get_recent_primary_session_turns(
            f"psess_{scope.name}", RECENT_TURNS_FOR_RECALL * 3
        )
        from memtrace_harness.primary_session import SCHEDULE_TURN_TYPES

        convo = [t for t in turns if t["turn_type"] not in SCHEDULE_TURN_TYPES][-RECENT_TURNS_FOR_RECALL:]
        transcript = "\n".join(f"[{t['speaker']}]: {t['content'][:400]}" for t in convo)
        source_turns = [t["turn_seq"] for t in convo[-1:]]
        return run_topic_recall(
            self.trace_store,
            scope.name,
            message=message,
            recent_transcript=transcript,
            source_turns=source_turns,
            search=search,
            verify=verify,
            call_model=self._call_model_factory(scope.name),
            memory_workspace_id=memory_ws,
            spec_workspace_id=spec_ws,
            ttl_hours=self.config.recall_ttl_hours,
        )
