from __future__ import annotations

import json
import logging
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from memtrace_harness.config import HarnessConfig

logger = logging.getLogger(__name__)

# Fallback refresh even with no activity, so the page never looks stalled if a browser
# tab sat open past the last real event (e.g. nothing happened for an hour).
HEARTBEAT_SECONDS = 15.0


class StatusEventBus:
    """Version counter + condition variable, not a per-client queue: every SSE
    connection just waits for the version to change, then recomputes the latest status
    data itself. Cheap to publish from (a single notify_all()), and a burst of
    publishes right after each other (e.g. poll + scan + consolidation all in one
    cycle) still only costs each client one recomputation, not one per publish."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._version = 0

    def publish(self) -> None:
        with self._condition:
            self._version += 1
            self._condition.notify_all()

    def wait_for_update(self, last_seen: int, timeout: float) -> int:
        with self._condition:
            self._condition.wait_for(lambda: self._version != last_seen, timeout=timeout)
            return self._version


# Frontend-only interactivity — everything below runs against data already pushed over
# SSE, no new endpoints: expand/collapse per-project cards, filter by project name, and
# search recent-conversation content. Nothing here writes anything back to the harness;
# see the status-web-dashboard memory note for why write actions (approve/reject,
# trigger a scan) are deliberately deferred until there's an auth story matching
# Telegram's allowed_chat_ids gate.
_PAGE_TEMPLATE = """<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<title>MemTrace Harness Status</title>
<style>
  :root {
    --bg: #111317; --card: #1a1d23; --border: #2a2e37; --text: #ddd; --muted: #888;
    --accent: #8ab4f8; --warn: #f0b84c; --live: #5cb85c; --chip: #262a33;
  }
  * { box-sizing: border-box; }
  body {
    background: var(--bg); color: var(--text); margin: 0; padding: 1.5rem;
    font-family: -apple-system, "Segoe UI", sans-serif;
  }
  header { display: flex; align-items: baseline; justify-content: space-between; flex-wrap: wrap; gap: 0.5rem; margin-bottom: 1rem; }
  h1 { font-size: 1.2rem; color: var(--accent); margin: 0; }
  .hint { color: var(--muted); font-size: 0.8rem; }
  #dot { display: inline-block; width: 0.5rem; height: 0.5rem; border-radius: 50%; background: #666; margin-right: 0.4rem; }
  #dot.live { background: var(--live); }
  .toolbar { display: flex; gap: 0.5rem; flex-wrap: wrap; margin-bottom: 1rem; }
  .toolbar input {
    background: var(--card); border: 1px solid var(--border); color: var(--text);
    border-radius: 0.4rem; padding: 0.4rem 0.6rem; font-size: 0.85rem;
  }
  #search { flex: 1; min-width: 12rem; }
  .daemon-bar {
    background: var(--card); border: 1px solid var(--border); border-radius: 0.5rem;
    padding: 0.6rem 0.9rem; margin-bottom: 1rem; font-size: 0.85rem; color: var(--muted);
  }
  .daemon-bar .warn { color: var(--warn); }
  #projects { display: grid; grid-template-columns: repeat(auto-fill, minmax(22rem, 1fr)); gap: 0.9rem; }
  .card {
    background: var(--card); border: 1px solid var(--border); border-radius: 0.6rem;
    padding: 0.9rem 1rem; font-size: 0.85rem;
  }
  .card.hidden { display: none; }
  .card h2 { font-size: 1rem; margin: 0 0 0.3rem 0; color: var(--accent); cursor: pointer; user-select: none; }
  .card h2 .caret { display: inline-block; transition: transform 0.15s; margin-right: 0.3rem; }
  .card.collapsed h2 .caret { transform: rotate(-90deg); }
  .card .body { display: block; }
  .card.collapsed .body { display: none; }
  .path { color: var(--muted); font-size: 0.78rem; word-break: break-all; margin-bottom: 0.5rem; }
  .chips { display: flex; flex-wrap: wrap; gap: 0.3rem; margin-bottom: 0.6rem; }
  .chip { background: var(--chip); border-radius: 1rem; padding: 0.15rem 0.55rem; font-size: 0.75rem; color: var(--muted); }
  .chip.on { color: var(--accent); }
  table { width: 100%; border-collapse: collapse; font-size: 0.8rem; margin-bottom: 0.6rem; }
  table td { padding: 0.15rem 0.3rem; border-bottom: 1px solid var(--border); vertical-align: top; }
  table td:first-child { color: var(--muted); white-space: nowrap; }
  .badge { background: var(--warn); color: #222; border-radius: 0.3rem; padding: 0.05rem 0.4rem; font-size: 0.75rem; font-weight: 600; }
  .badge.running { background: var(--live); margin-left: 0.4rem; vertical-align: middle; }
  .lock-progress { color: var(--muted); font-size: 0.78rem; margin: -0.2rem 0 0.4rem; }
  .pipeline { display: flex; align-items: center; flex-wrap: wrap; gap: 0; margin-bottom: 0.6rem; }
  .step {
    background: var(--chip); border: 1px solid var(--border); border-radius: 0.4rem;
    padding: 0.35rem 0.5rem; cursor: pointer; min-width: 5.5rem; text-align: center;
  }
  .step:hover { border-color: var(--accent); }
  .step.ok { border-color: var(--live); }
  .step.bad { border-color: #e05555; }
  .step.mid { border-color: var(--warn); }
  .step-icon { font-size: 0.9rem; font-weight: 700; }
  .step.ok .step-icon { color: var(--live); }
  .step.bad .step-icon { color: #e05555; }
  .step.mid .step-icon { color: var(--warn); }
  .step-label { font-size: 0.72rem; margin-top: 0.1rem; }
  .step-meta { font-size: 0.65rem; color: var(--muted); margin-top: 0.1rem; }
  .step-connector { width: 0.8rem; height: 1px; background: var(--border); }
  .stage-detail {
    position: fixed; top: 0; right: 0; width: min(32rem, 92vw); height: 100vh;
    background: var(--card); border-left: 1px solid var(--border);
    transform: translateX(100%); transition: transform 0.2s ease;
    z-index: 10; display: flex; flex-direction: column;
  }
  .stage-detail.open { transform: translateX(0); }
  .stage-detail-header {
    display: flex; justify-content: space-between; align-items: center;
    padding: 0.8rem 1rem; border-bottom: 1px solid var(--border); font-weight: 600;
  }
  .stage-detail-close { cursor: pointer; color: var(--muted); }
  .stage-detail-close:hover { color: var(--text); }
  .stage-detail-body { padding: 0.8rem 1rem; overflow-y: auto; font-size: 0.85rem; }
  .detail-row { margin-bottom: 0.4rem; }
  .detail-row.meta { color: var(--muted); font-size: 0.78rem; }
  .detail-pre {
    white-space: pre-wrap; word-break: break-word; background: var(--bg);
    border: 1px solid var(--border); border-radius: 0.4rem; padding: 0.6rem;
    font-size: 0.78rem; max-height: 28rem; overflow-y: auto;
  }
  .section-label { color: var(--muted); font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.03em; margin: 0.6rem 0 0.3rem; }
  .turn { padding: 0.3rem 0; border-bottom: 1px solid var(--border); }
  .turn:last-child { border-bottom: none; }
  .turn .meta { color: var(--muted); font-size: 0.72rem; }
  .turn .content { margin-top: 0.1rem; }
  .turn.match .content { background: #3a3410; }
  .empty { color: var(--muted); font-style: italic; }
  .approval { padding: 0.25rem 0; }
  .approval .id { color: var(--accent); }
</style>
</head>
<body>
<header>
  <h1>MemTrace Harness — 運行狀態</h1>
  <div class="hint"><span id="dot"></span><span id="conn">連線中…</span> · 即時串流更新 · 純檢視，不會修改任何狀態</div>
</header>
<div class="toolbar">
  <input id="search" type="text" placeholder="搜尋專案名稱或對話內容…">
</div>
<div id="daemon" class="daemon-bar"></div>
<div id="projects"></div>
<div id="stage-detail" class="stage-detail">
  <div class="stage-detail-panel">
    <div class="stage-detail-header">
      <span>關卡詳情</span>
      <span class="stage-detail-close" onclick="closeStageDetail()">✕</span>
    </div>
    <div id="stage-detail-body" class="stage-detail-body"></div>
  </div>
</div>
<script>
var state = { data: null, query: "", collapsed: {} };

function esc(s) {
  return String(s).replace(/[&<>"']/g, function (c) {
    return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
  });
}

function renderDaemon(d) {
  var el = document.getElementById("daemon");
  var pids = d.pids && d.pids.length ? d.pids.map(esc).join("<br>") : "沒有正在跑的 gateway --serve 行程";
  var warn = d.multiple_warning ? '<div class="warn">⚠️ 找到不只一個 --serve 行程，同一個 bot 會搶 getUpdates 連線（409 Conflict）</div>' : "";
  var launchd = d.launchd ? esc(d.launchd) : "沒有註冊 com.memtraceharness.gateway";
  el.innerHTML = pids + warn + "<div>launchd：" + launchd + "</div>";
}

function matchesQuery(project, q) {
  if (!q) return true;
  q = q.toLowerCase();
  if (project.name.toLowerCase().indexOf(q) !== -1) return true;
  return (project.recent_turns || []).some(function (t) {
    return t.content.toLowerCase().indexOf(q) !== -1;
  });
}

function renderChatCandidates(list) {
  if (!list || !list.length) return '<div class="empty">（未設定 HARNESS_CHAT_PROVIDER）</div>';
  return '<div class="chips">' + list.map(function (c) {
    return '<span class="chip">' + esc(c.provider) + "/" + esc(c.model || "(預設)") + "</span>";
  }).join(" -> ") + "</div>";
}

function renderRoleProfiles(project) {
  if (project.role_profiles_error) {
    return '<div class="warn">⚠️ role-profiles 讀取失敗：' + esc(project.role_profiles_error) + "</div>";
  }
  var rows = (project.role_profiles || []).map(function (p) {
    var fb = p.fallbacks.length ? p.fallbacks.map(function (f) { return f.provider + "/" + f.model; }).join(", ") : "";
    return "<tr><td>" + esc(p.id) + "</td><td>" + esc(p.provider + "/" + p.model) + (fb ? " <span class='chip'>fallback: " + esc(fb) + "</span>" : "") + "</td></tr>";
  }).join("");
  return "<table>" + rows + "</table>";
}

function renderTurns(turns, q) {
  if (!turns || !turns.length) return '<div class="empty">尚無對話紀錄</div>';
  q = (q || "").toLowerCase();
  return turns.map(function (t) {
    var isMatch = q && t.content.toLowerCase().indexOf(q) !== -1;
    return '<div class="turn' + (isMatch ? " match" : "") + '">' +
      '<div class="meta">[' + esc(t.created_at) + "] " + esc(t.speaker) + "</div>" +
      '<div class="content">' + esc(t.content.slice(0, 200)) + "</div></div>";
  }).join("");
}

function renderApprovals(list) {
  if (!list || !list.length) return "";
  return '<div class="section-label">待核准 <span class="badge">' + list.length + "</span></div>" +
    list.map(function (a) {
      return '<div class="approval"><span class="id">' + esc(a.id) + "</span> — " + esc(a.reason) + " — " + esc(a.proposed_action.slice(0, 60)) + "</div>";
    }).join("");
}

var STAGE_LABELS = {
  "control-start": "Controller",
  "plan": "Planner",
  "plan-escalation": "Planner (升級)",
  "plan-revision": "Planner (修正)",
  "g1": "Red Team G1",
  "g1-recheck": "Red Team G1 (複查)",
  "develop": "Developer",
  "develop-revision": "Developer (修正)",
  "g2": "Red Team G2",
  "g2-recheck": "Red Team G2 (複查)",
  "converge": "Converge"
};

var STATE_ICON = {
  "succeeded": "✓",
  "invalid_output": "!",
  "failed": "✕",
  "unavailable": "✕"
};

function stageLabel(stage) {
  return STAGE_LABELS[stage] || stage;
}

function renderPipeline(pipeline) {
  if (!pipeline || !pipeline.length) {
    return '<div class="empty">尚未有階段完成（可能剛啟動）</div>';
  }
  var steps = pipeline.map(function (t) {
    var stateClass = t.state === "succeeded" ? "ok" : (t.state === "invalid_output" || t.state === "failed" || t.state === "unavailable" ? "bad" : "mid");
    var icon = STATE_ICON[t.state] || "•";
    var retryTag = t.attempt_index > 0 ? '<span class="chip">重試 #' + t.attempt_index + '</span>' : "";
    return '<div class="step ' + stateClass + '" onclick="openStageDetail(' + t.turn_id + ')" title="' + esc(t.provider + "/" + (t.model || "?")) + '">' +
      '<div class="step-icon">' + icon + '</div>' +
      '<div class="step-label">' + esc(stageLabel(t.stage)) + '</div>' +
      '<div class="step-meta">' + esc(t.provider + "/" + (t.model || "?")) + " " + retryTag + '</div>' +
      '</div>';
  });
  return '<div class="pipeline">' + steps.join('<div class="step-connector"></div>') + '</div>';
}

function renderLock(lock) {
  return '<div class="warn">⚠️ workspace 鎖定中，conversation_id=' + esc(lock.conversation_id) +
    "，開始於 " + esc(lock.locked_at) + '</div>';
}

function openStageDetail(turnId) {
  var panel = document.getElementById("stage-detail");
  var body = document.getElementById("stage-detail-body");
  panel.classList.add("open");
  body.innerHTML = '<div class="empty">載入中…</div>';
  fetch("/api/turn?turn_id=" + turnId)
    .then(function (r) { return r.ok ? r.json() : null; })
    .then(function (d) {
      if (!d) { body.innerHTML = '<div class="empty">找不到這個階段的紀錄</div>'; return; }
      var html = "";
      html += '<div class="detail-row"><b>' + esc(stageLabel(d.stage)) + '</b> — ' + esc(d.profile_id) + '</div>';
      html += '<div class="detail-row meta">' + esc(d.provider + "/" + (d.model || "?")) + " — " + esc(d.state) + " — " + esc(d.created_at) + '</div>';
      if (d.artifact) {
        html += '<div class="section-label">結構化輸出</div><pre class="detail-pre">' + esc(JSON.stringify(d.artifact, null, 2)) + '</pre>';
      }
      if (d.final_text) {
        html += '<div class="section-label">模型原始回覆</div><pre class="detail-pre">' + esc(d.final_text) + '</pre>';
      }
      if (!d.artifact && !d.final_text) {
        html += '<div class="empty">這個階段沒有可顯示的內容</div>';
      }
      body.innerHTML = html;
    })
    .catch(function () { body.innerHTML = '<div class="empty">載入失敗</div>'; });
}

function closeStageDetail() {
  document.getElementById("stage-detail").classList.remove("open");
}

function renderProject(project, q) {
  var collapsed = state.collapsed[project.name] ? " collapsed" : "";
  var visible = matchesQuery(project, q) ? "" : " hidden";
  var d = project.dedicated;
  var runningBadge = project.lock ? '<span class="badge running">執行中</span>' : "";
  var lock = project.lock ? renderLock(project.lock) : "";
  var pipelineSection = "";
  if (project.pipeline_conversation_id) {
    var pipelineHint = project.lock
      ? ""
      : '<div class="lock-progress">（等待核准中，非鎖定狀態 — conversation_id=' + esc(project.pipeline_conversation_id) + '）</div>';
    pipelineSection = '<div class="section-label">Agent Loop 進度</div>' + pipelineHint + renderPipeline(project.pipeline);
  }
  return '<div class="card' + collapsed + visible + '" data-name="' + esc(project.name) + '">' +
    '<h2 onclick="toggleCard(\\'' + esc(project.name) + '\\')"><span class="caret">▾</span>' + esc(project.name) + runningBadge + "</h2>" +
    '<div class="body">' +
    '<div class="path">' + esc(project.working_directory) + "</div>" +
    '<div class="chips">' +
    '<span class="chip' + (d.bot ? " on" : "") + '">bot: ' + (d.bot ? "專屬" : "共用") + "</span>" +
    '<span class="chip' + (d.chat_model ? " on" : "") + '">chat: ' + (d.chat_model ? "專屬" : "共用") + "</span>" +
    '<span class="chip' + (d.memory ? " on" : "") + '">memory: ' + (d.memory ? "專屬" : "共用") + "</span>" +
    '<span class="chip' + (d.role_profiles ? " on" : "") + '">role-profiles: ' + (d.role_profiles ? "專屬" : "預設") + "</span>" +
    "</div>" +
    '<div class="section-label">聊天模型</div>' + renderChatCandidates(project.chat_candidates) +
    '<div class="section-label">Agent Loop 角色模型</div>' + renderRoleProfiles(project) +
    '<div class="section-label">對話記錄 (' + project.turn_count + ' 則，目標待處理 ' + project.pending_goal + '、偏好待處理 ' + project.pending_pref + ')</div>' +
    renderTurns(project.recent_turns, q) +
    lock + pipelineSection + renderApprovals(project.pending_approvals) +
    "</div></div>";
}

function render() {
  if (!state.data) return;
  renderDaemon(state.data.daemon);
  document.getElementById("projects").innerHTML = state.data.projects.length
    ? state.data.projects.map(function (p) { return renderProject(p, state.query); }).join("")
    : '<div class="empty">（沒有已註冊的專案）</div>';
}

function toggleCard(name) {
  state.collapsed[name] = !state.collapsed[name];
  render();
}

document.getElementById("search").addEventListener("input", function (e) {
  state.query = e.target.value;
  render();
});

state.data = __INITIAL_DATA__;
render();

var dot = document.getElementById("dot");
var conn = document.getElementById("conn");
var es = new EventSource("/events");
es.onopen = function () { dot.className = "live"; conn.textContent = "即時連線中"; };
es.onerror = function () { dot.className = ""; conn.textContent = "連線中斷，重試中…"; };
es.onmessage = function (evt) { state.data = JSON.parse(evt.data); render(); };
</script>
</body>
</html>"""


class _StatusRequestHandler(BaseHTTPRequestHandler):
    config: HarnessConfig
    bus: StatusEventBus

    def do_GET(self) -> None:  # noqa: N802 (stdlib method name)
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path in ("/", ""):
            self._serve_page()
        elif parsed.path == "/events":
            self._serve_events()
        elif parsed.path == "/api/turn":
            self._serve_turn_detail(urllib.parse.parse_qs(parsed.query))
        else:
            self.send_response(404)
            self.end_headers()

    def _collect_data(self) -> dict:
        from memtrace_harness.cli import _collect_status_data

        try:
            return _collect_status_data(self.config)
        except Exception as exc:  # keep the dashboard itself from crashing on a bad read
            logger.exception("status dashboard failed to collect status")
            return {"daemon": {"pids": [], "multiple_warning": False, "pgrep_error": str(exc), "launchd": None, "launchd_error": None}, "project_index_path": None, "projects": []}

    def _serve_page(self) -> None:
        data_json = json.dumps(self._collect_data())
        page = _PAGE_TEMPLATE.replace("__INITIAL_DATA__", data_json)
        payload = page.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _serve_events(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        last_seen = 0
        try:
            while True:
                self._write_event(self._collect_data())
                last_seen = self.bus.wait_for_update(last_seen, timeout=HEARTBEAT_SECONDS)
        except (BrokenPipeError, ConnectionResetError):
            pass  # client navigated away or closed the tab — not an error

    def _serve_turn_detail(self, query: dict[str, list[str]]) -> None:
        # On-demand only — deliberately not part of _collect_data()/the SSE push,
        # since artifact/final_text bodies can be long and most pipeline stages a
        # viewer sees are never clicked. Read-only: turn_id must parse as int and the
        # lookup itself never touches process/file state, just SQLite reads.
        raw_id = (query.get("turn_id") or [""])[0]
        try:
            turn_id = int(raw_id)
        except ValueError:
            self.send_response(400)
            self.end_headers()
            return
        from memtrace_harness.cli import _collect_turn_detail

        try:
            detail = _collect_turn_detail(self.config, turn_id)
        except Exception:
            logger.exception(f"status dashboard failed to collect turn detail for turn_id={turn_id}")
            detail = None
        payload = json.dumps(detail).encode("utf-8")
        self.send_response(200 if detail is not None else 404)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _write_event(self, data: dict) -> None:
        frame = f"data: {json.dumps(data)}\n\n".encode("utf-8")
        self.wfile.write(frame)
        self.wfile.flush()

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        logger.debug("status server: " + format, *args)


def start_status_server(
    config: HarnessConfig,
) -> tuple[ThreadingHTTPServer, StatusEventBus] | tuple[None, None]:
    """Start the read-only web status dashboard in a background thread, bound to
    config.status_server_host (default 127.0.0.1 — not exposed off the machine unless
    the operator explicitly widens it, e.g. via Tailscale). Returns (None, None) if
    disabled or if the port can't be bound (e.g. already running from a previous
    process), so this never blocks the gateway's actual job. The returned bus's
    publish() should be called by the caller whenever something worth showing changed
    (a Telegram update was processed, a scan/consolidation pass did something) so
    connected browsers update immediately instead of waiting for the heartbeat."""
    if not config.status_server_enabled:
        return None, None

    bus = StatusEventBus()
    handler_cls = type(
        "StatusRequestHandler",
        (_StatusRequestHandler,),
        {"config": config, "bus": bus},
    )
    try:
        server = ThreadingHTTPServer(
            (config.status_server_host, config.status_server_port), handler_cls
        )
    except OSError:
        logger.exception(
            f"could not bind status server on {config.status_server_host}:"
            f"{config.status_server_port}; is another gateway process already running?"
        )
        return None, None

    # Long-lived SSE connections must not keep the process alive after shutdown().
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, name="status-server", daemon=True)
    thread.start()
    logger.info(f"status dashboard listening on http://{config.status_server_host}:{config.status_server_port}")
    return server, bus
