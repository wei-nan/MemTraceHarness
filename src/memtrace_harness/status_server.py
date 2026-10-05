from __future__ import annotations

import json
import logging
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

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
# search recent-conversation content. Approve/reject/trigger-a-scan style actions still
# stay deferred until there's an auth story matching Telegram's allowed_chat_ids gate
# (see the status-web-dashboard memory note) — those are IRREVERSIBLE remote actions.
# The one exception (2026-09-17, explicit user request): editing a role's provider/
# model is a local config change, not a remote action, so it's exposed via POST
# /api/role-profile (see _StatusRequestHandler.do_POST()) even without that auth
# story — its only real gate is the dashboard's own bind address (127.0.0.1 unless
# the operator widens it).
# Preferences (2026-10-01/02, explicit user request): the Harness adopts and retires
# the operator's standing preferences ITSELF (nightly digest, or a correction made in
# chat); this page is the after-the-fact view and override. POST /api/preference adopts a
# rule that was left waiting (over the per-day auto-adopt cap), dismisses it, retires an
# adopted one, or restores a retired one. Same local-only gate as the other writes.
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
  .turn .content.full { white-space: pre-wrap; }
  details.turn-full summary { cursor: pointer; list-style: none; }
  details.turn-full[open] summary { display: none; }
  .turn.match .content { background: #3a3410; }
  .empty { color: var(--muted); font-style: italic; }
  .approval { padding: 0.25rem 0; }
  .approval .id { color: var(--accent); }
  .approval-actions { display: flex; align-items: center; gap: 0.4rem; margin: 0.2rem 0 0.4rem; }
  .approval-btn {
    border: none; border-radius: 0.3rem; padding: 0.18rem 0.6rem; font-size: 0.75rem;
    cursor: pointer; font-weight: 600;
  }
  .approval-btn.approve { background: var(--live); color: #16181d; }
  .approval-btn.reject { background: #e05555; color: #fff; }
  .approval-btn:hover { opacity: 0.85; }
  .approval-status { font-size: 0.72rem; }
  .approval-status.ok { color: var(--live); }
  .approval-status.err { color: #e05555; }
  .rp-row { display: flex; align-items: center; gap: 0.3rem; margin-top: 0.2rem; flex-wrap: wrap; }
  .rp-row select, .rp-row input {
    background: var(--bg); border: 1px solid var(--border); color: var(--text);
    border-radius: 0.3rem; padding: 0.15rem 0.35rem; font-size: 0.75rem;
  }
  .rp-row input { width: 9rem; }
  .rp-row button {
    background: var(--accent); color: #16181d; border: none; border-radius: 0.3rem;
    padding: 0.18rem 0.6rem; font-size: 0.75rem; cursor: pointer; font-weight: 600;
  }
  .rp-row button:hover { opacity: 0.85; }
  .rp-status { font-size: 0.72rem; }
  .rp-status.ok { color: var(--live); }
  .rp-status.err { color: #e05555; }
  .rp-disabled-note { color: var(--muted); font-size: 0.75rem; margin-bottom: 0.4rem; }
  .schedule { padding: 0.25rem 0; border-bottom: 1px solid var(--border); font-size: 0.8rem; }
  .schedule:last-child { border-bottom: none; }
  .schedule .id { color: var(--accent); }
  .schedule .meta { color: var(--muted); font-size: 0.72rem; }
  .memory-card { background: var(--card); border: 1px solid var(--border); border-radius: 0.6rem; padding: 0.8rem 1rem; margin-bottom: 1rem; }
  .memory-card h2 { font-size: 1rem; margin: 0 0 0.3rem; }
  .pref { border-top: 1px solid var(--border); padding: 0.5rem 0; }
  .pref:first-of-type { border-top: none; }
  .pref .meta { color: var(--muted); font-size: 0.72rem; display: flex; gap: 0.4rem; flex-wrap: wrap; align-items: center; }
  .pref textarea {
    width: 100%; min-height: 2.6rem; margin: 0.3rem 0; resize: vertical;
    background: var(--bg); border: 1px solid var(--border); color: var(--text);
    border-radius: 0.3rem; padding: 0.3rem 0.4rem; font: inherit; font-size: 0.85rem;
  }
  .pref-actions { display: flex; align-items: center; gap: 0.4rem; flex-wrap: wrap; }
  .pref-actions select {
    background: var(--bg); border: 1px solid var(--border); color: var(--text);
    border-radius: 0.3rem; padding: 0.15rem 0.35rem; font-size: 0.75rem;
  }
  .pref-btn { border: none; border-radius: 0.3rem; padding: 0.2rem 0.7rem; font-size: 0.78rem; cursor: pointer; font-weight: 600; }
  .pref-btn.adopt { background: var(--live); color: #16181d; }
  .pref-btn.dismiss { background: var(--chip); color: var(--text); }
  .pref-btn.retire { background: #e05555; color: #fff; }
  .pref-btn:hover { opacity: 0.85; }
  .pref-btn:disabled { opacity: 0.5; cursor: default; }
  .pref-status { font-size: 0.72rem; }
  .pref-status.ok { color: var(--live); }
  .pref-status.err { color: #e05555; }
  .tag-auto { color: var(--accent); }
  .pref-btn.restore { background: var(--chip); color: var(--text); }
  .tag-explicit { color: var(--live); }
  .tag-inferred { color: var(--warn); }
  details.evidence summary, details.digest summary, details.models summary { cursor: pointer; color: var(--accent); font-size: 0.78rem; }
  .evidence-item { font-size: 0.75rem; color: var(--muted); padding: 0.15rem 0 0.15rem 0.6rem; border-left: 2px solid var(--border); margin: 0.2rem 0; white-space: pre-wrap; }
  details.models { margin: 0.4rem 0; }
  details.digest { border-bottom: 1px solid var(--border); padding: 0.3rem 0; }
  details.digest:last-child { border-bottom: none; }
  .digest-body { font-size: 0.8rem; padding: 0.3rem 0 0.2rem 0.6rem; }
  .digest-body .summary { white-space: pre-wrap; margin-bottom: 0.3rem; }
  .digest-body ul { margin: 0.1rem 0 0.4rem; padding-left: 1.2rem; }
  .digest-body .sub { color: var(--muted); font-size: 0.72rem; margin-top: 0.3rem; }
  .refs { color: var(--muted); font-size: 0.7rem; }
</style>
</head>
<body>
<header>
  <h1>MemTrace Harness — 運行狀態</h1>
  <div class="hint"><span id="dot"></span><span id="conn">連線中…</span> · 即時串流更新 · 可編輯：Agent Loop 角色模型、聊天模型、每日摘要模型、背景回想模型、待核准、偏好確認</div>
</header>
<div class="toolbar">
  <input id="search" type="text" placeholder="搜尋專案名稱或對話內容…">
</div>
<div id="daemon" class="daemon-bar"></div>
<div id="memory"></div>
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
var state = { data: null, query: "", collapsed: {}, prefDrafts: {}, openDetails: {}, renderDeferred: false };

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

var PROVIDERS = ["claude", "codex", "antigravity"];

function providerOptions(selected) {
  return PROVIDERS.map(function (p) {
    return '<option value="' + p + '"' + (p === selected ? " selected" : "") + ">" + p + "</option>";
  }).join("");
}

var CUSTOM_MODEL_VALUE = "__custom__";

function modelOptions(provider, currentModel) {
  var known = (state.data.known_models && state.data.known_models[provider]) || [];
  var opts = known.slice();
  if (currentModel && opts.indexOf(currentModel) === -1) opts.unshift(currentModel);
  var html = opts.map(function (m) {
    return '<option value="' + esc(m) + '"' + (m === currentModel ? " selected" : "") + ">" + esc(m) + "</option>";
  }).join("");
  return html + '<option value="' + CUSTOM_MODEL_VALUE + '">— 自訂 —</option>';
}

// Shared by every "provider + model" edit row (role-profile rows, the chat-model
// row): a model <select> populated from known_models for the currently-chosen
// provider, plus a "自訂" option that reveals a free-text fallback — model catalogs
// aren't tracked anywhere in this codebase (names change too often, see
// profiles/beri.toml's own "verified against `agy models`" comments), so the
// dropdown only ever offers what's already configured somewhere in this harness.
function modelEditRowHtml(provider, model) {
  return '<select class="rp-provider" onchange="onProviderSelectChange(this)">' + providerOptions(provider) + "</select>" +
    '<select class="rp-model-select" onchange="onModelSelectChange(this)">' + modelOptions(provider, model) + "</select>" +
    '<input class="rp-model-custom" type="text" value="' + esc(model || "") + '" placeholder="模型名稱" style="display:none">';
}

function onProviderSelectChange(providerSelect) {
  var row = providerSelect.closest(".rp-row");
  var modelSelect = row.querySelector(".rp-model-select");
  modelSelect.innerHTML = modelOptions(providerSelect.value, "");
  syncCustomModelVisibility(row);
}

function onModelSelectChange(modelSelect) {
  syncCustomModelVisibility(modelSelect.closest(".rp-row"));
}

function syncCustomModelVisibility(row) {
  var modelSelect = row.querySelector(".rp-model-select");
  row.querySelector(".rp-model-custom").style.display =
    modelSelect.value === CUSTOM_MODEL_VALUE ? "inline-block" : "none";
}

function readModelEditRow(row) {
  var provider = row.querySelector(".rp-provider").value;
  var modelSelect = row.querySelector(".rp-model-select");
  var model = modelSelect.value === CUSTOM_MODEL_VALUE
    ? row.querySelector(".rp-model-custom").value
    : modelSelect.value;
  return { provider: provider, model: model };
}

function renderModelCandidates(project, list, saveClass, emptyText, note) {
  var display = (!list || !list.length)
    ? '<div class="empty">' + emptyText + "</div>"
    : '<div class="chips">' + list.map(function (c) {
        return '<span class="chip">' + esc(c.provider) + "/" + esc(c.model || "(預設)") + "</span>";
      }).join(" -> ") + "</div>";
  var current = list && list.length ? list[0] : { provider: "claude", model: "" };
  var editRow = '<div class="rp-row">' +
    modelEditRowHtml(current.provider, current.model) +
    '<button class="' + saveClass + '" data-project="' + esc(project.name) + '">儲存</button>' +
    '<span class="rp-status"></span>' +
    "</div>";
  return display + (note ? '<div class="hint">' + note + "</div>" : "") + editRow;
}

function renderChatCandidates(project) {
  return renderModelCandidates(project, project.chat_candidates, "chat-model-save",
    "（未設定 HARNESS_CHAT_PROVIDER）", "");
}

function renderFallbackRow(project, candidates, inputClass, saveClass) {
  var rest = (candidates || []).slice(1).map(function (c) {
    return c.provider + "/" + (c.model || "");
  }).join(",");
  return '<div class="rp-row"><span class="hint">備援（provider/model，逗號分隔，留空＝無）</span> ' +
    '<input class="fallbacks-input ' + inputClass + '" type="text" size="38" value="' + esc(rest) + '" placeholder="codex/gpt-5.6-sol">' +
    '<button class="' + saveClass + '" data-project="' + esc(project.name) + '">儲存</button>' +
    '<span class="rp-status"></span></div>';
}

function renderDigestFallbackRow(project) {
  return renderFallbackRow(project, project.digest_candidates, "digest-fallbacks", "digest-fallbacks-save");
}

function renderRecallCandidates(project) {
  var note = project.recall_enabled === false ? "背景回想已停用（HARNESS_RECALL_ENABLED=false）。"
    : (project.recall_is_own ? "" : "尚未獨立設定，目前沿用每日摘要模型。");
  return renderModelCandidates(project, project.recall_candidates, "recall-model-save", "（未設定）", note) +
    renderFallbackRow(project, project.recall_candidates, "recall-fallbacks", "recall-fallbacks-save");
}

function renderTopicBriefs(project) {
  var list = project.topic_briefs || [];
  var st = project.recall_state || {};
  var head = '<div class="section-label">主題簡報（短效期，' + list.length + ' 份有效' +
    (st.pending ? "，整理中 " + st.pending : "") + ")</div>";
  var err = st.last_error ? '<div class="warn">⚠️ 上次背景整理失敗：' + esc(st.last_error) + "</div>" : "";
  if (!list.length) return head + err + '<div class="empty">（目前沒有有效的主題簡報）</div>';
  return head + err + list.map(function (b) {
    var key = "brief:" + project.name + ":" + b.id;
    var related = (b.related || []).map(function (r) {
      return '<div class="evidence-item">' + esc((r.date ? r.date + " " : "") + (r.title || r.node_id)) +
        "（" + esc(r.node_id) + "）：" + esc(r.why) + "</div>";
    }).join("");
    return '<details class="digest" data-key="' + esc(key) + '"' + detailsOpen(key) + "><summary>" +
      esc(b.title) + (b.fresh ? "（全新主題）" : "") + (b.pushed ? " · 已推送" : "") +
      " · 到期 " + esc((b.expires_at || "").slice(0, 16).replace("T", " ")) + "</summary>" +
      '<div class="digest-body">' + esc(b.summary) + related + "</div></details>";
  }).join("");
}

function renderDigestCandidates(project) {
  return renderModelCandidates(project, project.digest_candidates, "digest-model-save",
    "（未設定）",
    project.digest_is_own ? "" : "尚未獨立設定，目前沿用聊天模型；儲存後摘要才會改用自己的模型。") +
    renderDigestFallbackRow(project);
}

function renderRoleProfiles(project) {
  if (project.role_profiles_error) {
    return '<div class="warn">⚠️ role-profiles 讀取失敗：' + esc(project.role_profiles_error) + "</div>";
  }
  var editable = !!(project.dedicated && project.dedicated.role_profiles);
  var note = editable ? "" : '<div class="rp-disabled-note">此專案共用全域預設 role-profiles，' +
    "要先給它一份專屬設定檔才能在這裡調整。" +
    '<div class="rp-row"><button class="rp-independent" data-project="' + esc(project.name) + '">獨立出去</button>' +
    '<span class="rp-status"></span></div></div>';
  var rows = (project.role_profiles || []).map(function (p) {
    var fb = p.fallbacks.length ? p.fallbacks.map(function (f) { return f.provider + "/" + f.model; }).join(", ") : "";
    var current = "<tr><td>" + esc(p.id) + "</td><td>" + esc(p.provider + "/" + p.model) +
      (fb ? " <span class='chip'>fallback: " + esc(fb) + "</span>" : "");
    if (editable) {
      current += '<div class="rp-row">' +
        modelEditRowHtml(p.provider, p.model) +
        '<button class="rp-save" data-project="' + esc(project.name) + '" data-profile="' + esc(p.id) + '">儲存</button>' +
        '<span class="rp-status"></span>' +
        "</div>";
    }
    return current + "</td></tr>";
  }).join("");
  return note + "<table>" + rows + "</table>";
}

function renderSchedules(project) {
  var list = project.schedules || [];
  if (!list.length) return "";
  var rows = list.map(function (s) {
    var window = s.end_time_of_day ? s.time_of_day + "~" + s.end_time_of_day : s.time_of_day;
    var freq = s.kind === "interval"
      ? "每 " + s.interval_seconds + " 秒"
      : (s.kind === "daily" ? "每天 " : "每個工作日 ") + window;
    return '<div class="schedule"><span class="id">' + esc(s.id) + "</span> — " + esc(freq) +
      '<div class="meta">下次 ' + esc(s.next_run_at) + " · " + esc(s.goal) + "</div></div>";
  }).join("");
  return '<div class="section-label">排程 <span class="badge">' + list.length + "</span></div>" + rows;
}

document.addEventListener("click", function (e) {
  if (!e.target.classList.contains("rp-save")) return;
  var btn = e.target;
  var row = btn.closest(".rp-row");
  var project = btn.getAttribute("data-project");
  var profileId = btn.getAttribute("data-profile");
  var picked = readModelEditRow(row);
  var statusEl = row.querySelector(".rp-status");
  statusEl.className = "rp-status";
  statusEl.textContent = "儲存中…";
  btn.disabled = true;
  fetch("/api/role-profile", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      project: project, profile_id: profileId, provider: picked.provider, model: picked.model
    })
  })
    .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, body: d }; }); })
    .then(function (res) {
      btn.disabled = false;
      if (res.ok) {
        statusEl.className = "rp-status ok";
        statusEl.textContent = "已儲存，下次執行生效";
      } else {
        statusEl.className = "rp-status err";
        statusEl.textContent = res.body.error || "儲存失敗";
      }
    })
    .catch(function () {
      btn.disabled = false;
      statusEl.className = "rp-status err";
      statusEl.textContent = "儲存失敗（連線問題）";
    });
});

document.addEventListener("click", function (e) {
  var fbEndpoint = e.target.classList.contains("digest-fallbacks-save") ? "/api/digest-fallbacks"
    : e.target.classList.contains("recall-fallbacks-save") ? "/api/recall-fallbacks" : null;
  if (!fbEndpoint) return;
  var btn = e.target;
  var row = btn.closest(".rp-row");
  var statusEl = row.querySelector(".rp-status");
  statusEl.className = "rp-status";
  statusEl.textContent = "儲存中…";
  btn.disabled = true;
  fetch(fbEndpoint, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ project: btn.getAttribute("data-project"), fallbacks: row.querySelector(".fallbacks-input").value })
  })
    .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, body: d }; }); })
    .then(function (res) {
      btn.disabled = false;
      statusEl.className = "rp-status " + (res.ok ? "ok" : "err");
      statusEl.textContent = res.ok ? "已儲存，立即生效" : (res.body.error || "儲存失敗");
    })
    .catch(function () {
      btn.disabled = false;
      statusEl.className = "rp-status err";
      statusEl.textContent = "儲存失敗（連線問題）";
    });
});

document.addEventListener("click", function (e) {
  var endpoint = e.target.classList.contains("chat-model-save") ? "/api/chat-model"
    : e.target.classList.contains("digest-model-save") ? "/api/digest-model"
    : e.target.classList.contains("recall-model-save") ? "/api/recall-model" : null;
  if (!endpoint) return;
  var btn = e.target;
  var row = btn.closest(".rp-row");
  var project = btn.getAttribute("data-project");
  var picked = readModelEditRow(row);
  var statusEl = row.querySelector(".rp-status");
  statusEl.className = "rp-status";
  statusEl.textContent = "儲存中…";
  btn.disabled = true;
  fetch(endpoint, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ project: project, provider: picked.provider, model: picked.model })
  })
    .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, body: d }; }); })
    .then(function (res) {
      btn.disabled = false;
      if (res.ok) {
        statusEl.className = "rp-status ok";
        statusEl.textContent = "已儲存，立即生效";
      } else {
        statusEl.className = "rp-status err";
        statusEl.textContent = res.body.error || "儲存失敗";
      }
    })
    .catch(function () {
      btn.disabled = false;
      statusEl.className = "rp-status err";
      statusEl.textContent = "儲存失敗（連線問題）";
    });
});

document.addEventListener("click", function (e) {
  if (!e.target.classList.contains("rp-independent")) return;
  var btn = e.target;
  var project = btn.getAttribute("data-project");
  var statusEl = btn.parentElement.querySelector(".rp-status");
  statusEl.className = "rp-status";
  statusEl.textContent = "建立中…";
  btn.disabled = true;
  fetch("/api/role-profile/independent", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ project: project })
  })
    .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, body: d }; }); })
    .then(function (res) {
      btn.disabled = false;
      if (res.ok) {
        statusEl.className = "rp-status ok";
        statusEl.textContent = "已建立 " + res.body.path + "，重啟 gateway 後才會生效並可在此編輯";
      } else {
        statusEl.className = "rp-status err";
        statusEl.textContent = res.body.error || "建立失敗";
      }
    })
    .catch(function () {
      btn.disabled = false;
      statusEl.className = "rp-status err";
      statusEl.textContent = "建立失敗（連線問題）";
    });
});

document.addEventListener("click", function (e) {
  if (!e.target.classList.contains("approval-btn")) return;
  var btn = e.target;
  var project = btn.getAttribute("data-project");
  var requestId = btn.getAttribute("data-request");
  var action = btn.getAttribute("data-action");
  var group = btn.closest(".approval-actions");
  var statusEl = group.querySelector(".approval-status");
  var buttons = group.querySelectorAll(".approval-btn");
  statusEl.className = "approval-status";
  statusEl.textContent = (action === "approve" ? "核准中…" : "拒絕中…");
  buttons.forEach(function (b) { b.disabled = true; });
  fetch("/api/approval", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ project: project, request_id: requestId, action: action })
  })
    .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, body: d }; }); })
    .then(function (res) {
      if (res.ok) {
        statusEl.className = "approval-status ok";
        statusEl.textContent = res.body.message || "已更新";
      } else {
        buttons.forEach(function (b) { b.disabled = false; });
        statusEl.className = "approval-status err";
        statusEl.textContent = res.body.error || "更新失敗";
      }
    })
    .catch(function () {
      buttons.forEach(function (b) { b.disabled = false; });
      statusEl.className = "approval-status err";
      statusEl.textContent = "更新失敗（連線問題）";
    });
});

var TURN_PREVIEW_CHARS = 200;

function renderTurns(turns, q, projectName) {
  if (!turns || !turns.length) return '<div class="empty">尚無對話紀錄</div>';
  q = (q || "").toLowerCase();
  return turns.map(function (t) {
    var isMatch = q && t.content.toLowerCase().indexOf(q) !== -1;
    var body;
    if (t.content.length <= TURN_PREVIEW_CHARS) {
      body = '<div class="content">' + esc(t.content) + "</div>";
    } else {
      // Long turns show a preview with an ellipsis and open to the full text (the
      // preview used to cut at 200 characters with no sign anything was missing).
      var key = "turn:" + projectName + ":" + t.created_at;
      body = '<details class="turn-full" data-key="' + esc(key) + '"' + detailsOpen(key) + ">" +
        '<summary class="content">' + esc(t.content.slice(0, TURN_PREVIEW_CHARS)) + "…（展開全文）</summary>" +
        '<div class="content full">' + esc(t.content) + "</div></details>";
    }
    return '<div class="turn' + (isMatch ? " match" : "") + '">' +
      '<div class="meta">[' + esc(t.created_at) + "] " + esc(t.speaker) + "</div>" + body + "</div>";
  }).join("");
}

function renderApprovals(list, projectName) {
  if (!list || !list.length) return "";
  return '<div class="section-label">待核准 <span class="badge">' + list.length + "</span></div>" +
    list.map(function (a) {
      return '<div class="approval">' +
        '<div><span class="id">' + esc(a.id) + "</span> — " + esc(a.reason) + " — " + esc(a.proposed_action) + "</div>" +
        '<div class="approval-actions">' +
        '<button class="approval-btn approve" data-project="' + esc(projectName) + '" data-request="' + esc(a.id) + '" data-action="approve">✅ 核准</button>' +
        '<button class="approval-btn reject" data-project="' + esc(projectName) + '" data-request="' + esc(a.id) + '" data-action="reject">❌ 拒絕</button>' +
        '<span class="approval-status"></span>' +
        "</div></div>";
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

function renderLock(project) {
  var locks = project.locks || [];
  var html = locks.map(function (lock) {
    return '<div class="warn">⚠️ 執行中，conversation_id=' + esc(lock.conversation_id) +
      (lock.schedule_id ? "（排程 " + esc(lock.schedule_id) + "）" : "") +
      "，開始於 " + esc(lock.locked_at) + '</div>';
  }).join("");
  var queue = project.queue || [];
  if (queue.length) {
    html += '<div class="lock-progress">⏳ 佇列中 ' + queue.length + " 個：" +
      queue.map(function (q) { return esc(q.goal); }).join("、") + "</div>";
  }
  return html;
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

function categoryLabel(cat) {
  var labels = (state.data.preferences && state.data.preferences.category_labels) || {};
  return labels[cat] || cat;
}

function detailsOpen(key) {
  return state.openDetails[key] ? " open" : "";
}

function renderEvidence(rule) {
  var key = "evidence:" + rule.id;
  var items = (rule.evidence || []).map(function (e) {
    var at = e.turn_seq === null || e.turn_seq === undefined ? "" : " #" + e.turn_seq;
    return '<div class="evidence-item">' + esc(e.date) + esc(at) + "：" + esc(e.quote) + "</div>";
  }).join("");
  return '<details class="evidence" data-key="' + key + '"' + detailsOpen(key) + "><summary>你當時說的原話（" +
    (rule.evidence || []).length + "）</summary>" + items + "</details>";
}

function renderPendingPreference(rule) {
  var draft = state.prefDrafts[rule.id] || {};
  var text = draft.text !== undefined ? draft.text : rule.text;
  var scope = draft.scope || rule.scope;
  return '<div class="pref" data-id="' + rule.id + '">' +
    '<div class="meta"><span class="chip">' + esc(categoryLabel(rule.category)) + "</span>" +
    (rule.explicit
      ? '<span class="tag-explicit">● 你明確說過的規則</span>'
      : '<span class="tag-inferred">● 模型推測，請仔細確認</span>') +
    "<span>來源：" + esc(rule.project) + " · " + esc(rule.source_digest_date || "") + "</span></div>" +
    '<textarea class="pref-text" data-id="' + rule.id + '">' + esc(text) + "</textarea>" +
    '<div class="pref-actions">' +
    '<select class="pref-scope" data-id="' + rule.id + '">' +
    '<option value="global"' + (scope === "global" ? " selected" : "") + ">全域（所有專案）</option>" +
    '<option value="project"' + (scope === "project" ? " selected" : "") + ">僅 " + esc(rule.project) + "</option>" +
    "</select>" +
    '<button class="pref-btn adopt" data-id="' + rule.id + '" data-action="adopt">✅ 採用</button>' +
    '<button class="pref-btn dismiss" data-id="' + rule.id + '" data-action="dismiss">略過</button>' +
    '<span class="pref-status"></span></div>' +
    renderEvidence(rule) + "</div>";
}

var ADOPTED_BY_LABELS = {
  "harness-digest": "Harness 每晚整理時自動採用",
  "harness-chat": "Harness 依你的聊天更正",
  "operator": "你在網頁採用"
};

function renderAdoptedPreference(rule) {
  return '<div class="pref" data-id="' + rule.id + '">' +
    '<div class="meta"><span class="chip">#' + rule.id + " " + esc(categoryLabel(rule.category)) + "</span>" +
    '<span class="tag-auto">' + esc(ADOPTED_BY_LABELS[rule.adopted_by] || "已採用") + "</span>" +
    "<span>" + (rule.scope === "global" ? "全域" : "僅 " + esc(rule.project)) + " · " + esc((rule.resolved_at || "").slice(0, 10)) + "</span></div>" +
    '<div class="content">' + esc(rule.text) + "</div>" +
    '<div class="pref-actions"><button class="pref-btn retire" data-id="' + rule.id + '" data-action="retire">撤銷</button>' +
    '<span class="pref-status"></span></div>' +
    renderEvidence(rule) + "</div>";
}

function renderRetiredPreference(rule) {
  return '<div class="pref" data-id="' + rule.id + '">' +
    '<div class="meta"><span class="chip">#' + rule.id + " " + esc(categoryLabel(rule.category)) + "</span>" +
    "<span>" + esc((rule.resolved_at || "").slice(0, 10)) + " 撤回</span></div>" +
    '<div class="content">' + esc(rule.text) + "</div>" +
    '<div class="meta">' + esc(rule.retire_reason || "") + "</div>" +
    '<div class="pref-actions"><button class="pref-btn restore" data-id="' + rule.id + '" data-action="restore">恢復</button>' +
    '<span class="pref-status"></span></div></div>';
}

function renderMemory() {
  var prefs = state.data.preferences || { pending: [], adopted: [], retired: [] };
  var pending = prefs.pending || [];
  var adopted = (prefs.adopted || []).slice().sort(function (a, b) { return b.id - a.id; });
  var retired = prefs.retired || [];
  var html = '<div class="memory-card"><h2>🧠 記憶整理 · 偏好</h2>' +
    '<div class="hint">Harness 會自己從你的對話歸納偏好，直接採用並套用到之後的聊天，不需要先經過你；' +
    "不同意可以直接在聊天裡說（例如「那個不用了」），或在這裡撤銷。每條都附你當時說的原話。</div>" +
    '<div class="section-label">已採用 (' + adopted.length + ")</div>" +
    (adopted.length ? adopted.map(renderAdoptedPreference).join("") : '<div class="empty">尚未採用任何偏好</div>') +
    (pending.length
      ? '<div class="section-label">等你確認 <span class="badge">' + pending.length + "</span></div>" +
        '<div class="hint">超過每日自動採用上限，或舊版留下來的候選；採用前可以先修改。</div>' +
        pending.map(renderPendingPreference).join("")
      : "") +
    (retired.length
      ? '<div class="section-label">最近撤回 (' + retired.length + ")</div>" + retired.map(renderRetiredPreference).join("")
      : "") +
    "</div>";
  document.getElementById("memory").innerHTML = html;
}

function renderItemList(title, items, withSince) {
  if (!items || !items.length) return "";
  return '<div class="sub">' + title + "</div><ul>" + items.map(function (it) {
    var refs = it.turns || it.resolved_turns || [];
    return "<li>" + esc(it.text) +
      (withSince && it.since ? ' <span class="refs">（自 ' + esc(it.since) + "）</span>" : "") +
      (refs.length ? ' <span class="refs">#' + refs.map(esc).join(", #") + "</span>" : "") + "</li>";
  }).join("") + "</ul>";
}

function renderDigests(project) {
  var list = project.digests || [];
  var label = '<div class="section-label">每日摘要（最近 ' + list.length + " 天 · 寫入 " + esc(project.memory_workspace_id || "") + "）</div>";
  if (!list.length) return label + '<div class="empty">尚無摘要（每天 02:00 整理前一天）</div>';
  return label + list.map(function (d) {
    var g = d.digest || {};
    var key = "digest:" + project.name + ":" + d.digest_date;
    var meta = d.turn_count + " 則對話 · " + (g.open_items || []).length + " 項未完成" +
      ((g.preferences_adopted || 0) ? " · 自動採用 " + g.preferences_adopted + " 條偏好" : "") +
      ((g.preferences_retired || 0) ? " · 撤回 " + g.preferences_retired + " 條" : "") +
      (d.synced ? "" : " · 尚未寫入 MemTrace");
    return '<details class="digest" data-key="' + esc(key) + '"' + detailsOpen(key) + "><summary>" +
      esc(d.digest_date) + ' <span class="refs">' + esc(meta) + "</span></summary>" +
      '<div class="digest-body"><div class="summary">' + esc(g.summary || "（無摘要）") + "</div>" +
      renderItemList("決策", g.decisions) +
      renderItemList("知識與事實", g.facts) +
      renderItemList("未完成事項", g.open_items, true) +
      renderItemList("當天解決", g.resolved_items, true) +
      renderItemList("過期未處理（超過 14 天）", g.expired_items, true) +
      renderItemList("流程教訓", g.process_lessons) +
      '<div class="sub">模型：' + esc((d.provider || "?") + "/" + (d.model || "?")) +
      " · 無出處而捨棄：" + esc(g.discarded_ungrounded || 0) + "</div>" +
      "</div></details>";
  }).join("");
}

// All model settings live in one collapsed block; opening it is the "I want to edit"
// gesture. Open state is remembered per project across the live re-renders.
function renderModelSettings(project) {
  var key = "models:" + project.name;
  return '<details class="models" data-key="' + esc(key) + '"' + detailsOpen(key) + ">" +
    "<summary>模型設定（聊天 / 每日摘要 / 背景回想 / Agent Loop 角色）</summary>" +
    '<div class="section-label">聊天模型</div>' + renderChatCandidates(project) +
    '<div class="section-label">每日摘要模型</div>' + renderDigestCandidates(project) +
    '<div class="section-label">背景回想模型</div>' + renderRecallCandidates(project) +
    '<div class="section-label">Agent Loop 角色模型</div>' + renderRoleProfiles(project) +
    "</details>";
}

function renderProject(project, q) {
  var collapsed = state.collapsed[project.name] ? " collapsed" : "";
  var visible = matchesQuery(project, q) ? "" : " hidden";
  var d = project.dedicated;
  var running = (project.locks || []).length;
  var runningBadge = running
    ? '<span class="badge running">執行中 ' + running + "/" + project.max_workers + "</span>"
    : "";
  var lock = running || (project.queue || []).length ? renderLock(project) : "";
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
    '<span class="chip' + ((project.schedules || []).length ? " on" : "") + '">排程: ' + (project.schedules || []).length + "</span>" +
    "</div>" +
    renderModelSettings(project) +
    renderTopicBriefs(project) +
    renderSchedules(project) +
    '<div class="section-label">對話記錄 (' + project.turn_count + ' 則，目標待處理 ' + project.pending_goal + ')</div>' +
    renderTurns(project.recent_turns, q, project.name) +
    renderDigests(project) +
    lock + pipelineSection + renderApprovals(project.pending_approvals, project.name) +
    "</div></div>";
}

function isEditingPreference() {
  var el = document.activeElement;
  return !!(el && el.classList && (el.classList.contains("pref-text") || el.classList.contains("pref-scope")));
}

function render() {
  if (!state.data) return;
  // An SSE push re-renders everything; don't yank the textarea out from under
  // someone mid-edit — render once they leave it.
  if (isEditingPreference()) { state.renderDeferred = true; return; }
  state.renderDeferred = false;
  renderDaemon(state.data.daemon);
  renderMemory();
  document.getElementById("projects").innerHTML = state.data.projects.length
    ? state.data.projects.map(function (p) { return renderProject(p, state.query); }).join("")
    : '<div class="empty">（沒有已註冊的專案）</div>';
}

function toggleCard(name) {
  state.collapsed[name] = !state.collapsed[name];
  render();
}

document.addEventListener("toggle", function (e) {
  var key = e.target && e.target.getAttribute && e.target.getAttribute("data-key");
  if (key) state.openDetails[key] = e.target.open;
}, true);

document.addEventListener("input", function (e) {
  if (!e.target.classList.contains("pref-text")) return;
  var id = e.target.getAttribute("data-id");
  state.prefDrafts[id] = Object.assign(state.prefDrafts[id] || {}, { text: e.target.value });
});

document.addEventListener("change", function (e) {
  if (!e.target.classList.contains("pref-scope")) return;
  var id = e.target.getAttribute("data-id");
  state.prefDrafts[id] = Object.assign(state.prefDrafts[id] || {}, { scope: e.target.value });
});

document.addEventListener("focusout", function (e) {
  if (state.renderDeferred && e.target.classList && (e.target.classList.contains("pref-text") || e.target.classList.contains("pref-scope"))) {
    setTimeout(function () { if (!isEditingPreference()) render(); }, 0);
  }
});

document.addEventListener("click", function (e) {
  if (!e.target.classList.contains("pref-btn")) return;
  var btn = e.target;
  var id = btn.getAttribute("data-id");
  var action = btn.getAttribute("data-action");
  var box = btn.closest(".pref");
  var statusEl = box.querySelector(".pref-status");
  var body = { id: Number(id), action: action };
  if (action === "adopt") {
    body.text = box.querySelector(".pref-text").value;
    body.scope = box.querySelector(".pref-scope").value;
  }
  if (action === "retire" && !confirm("撤銷後這條偏好就不再影響聊天（可以在「最近撤回」恢復），確定嗎？")) return;
  var buttons = box.querySelectorAll(".pref-btn");
  buttons.forEach(function (b) { b.disabled = true; });
  statusEl.className = "pref-status";
  statusEl.textContent = "處理中…";
  fetch("/api/preference", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body)
  })
    .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, body: d }; }); })
    .then(function (res) {
      if (res.ok) {
        delete state.prefDrafts[id];
        statusEl.className = "pref-status ok";
        statusEl.textContent = res.body.memtrace_synced === false ? "已完成（MemTrace 同步失敗，下次再試）" : "已完成";
      } else {
        buttons.forEach(function (b) { b.disabled = false; });
        statusEl.className = "pref-status err";
        statusEl.textContent = res.body.error || "失敗";
      }
    })
    .catch(function () {
      buttons.forEach(function (b) { b.disabled = false; });
      statusEl.className = "pref-status err";
      statusEl.textContent = "連線失敗";
    });
});

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
    gateway_for_project: dict[str, Any]

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

    def do_POST(self) -> None:  # noqa: N802 (stdlib method name)
        # The write actions this dashboard offers (2026-09-17, explicit user
        # request) — everything else stays read-only by design (see the module
        # docstring above). Still only reachable at all when the dashboard itself is:
        # bound to config.status_server_host (127.0.0.1 by default, never exposed off
        # the machine unless the operator explicitly widens it, e.g. via Tailscale —
        # there is no separate auth on any of these endpoints, so widening exposure
        # widens who can change a project's models or approve/reject its pending work
        # too). approve/reject in particular can trigger a real governed Agent Loop
        # run — same consequence as an approval from Telegram, just a different door.
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path == "/api/role-profile":
            self._handle_update_role_profile()
        elif parsed.path == "/api/role-profile/independent":
            self._handle_make_role_profiles_independent()
        elif parsed.path == "/api/chat-model":
            self._handle_update_chat_model()
        elif parsed.path == "/api/recall-model":
            self._handle_update_stage_model("recall-model")
        elif parsed.path == "/api/recall-fallbacks":
            self._handle_update_stage_model("recall-fallbacks")
        elif parsed.path == "/api/digest-fallbacks":
            self._handle_update_digest_fallbacks()
        elif parsed.path == "/api/digest-model":
            self._handle_update_digest_model()
        elif parsed.path == "/api/approval":
            self._handle_approval_action()
        elif parsed.path == "/api/preference":
            self._handle_preference_action()
        else:
            self.send_response(404)
            self.end_headers()

    def _read_json_body(self) -> dict | None:
        length = int(self.headers.get("Content-Length") or "0")
        raw = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(raw.decode("utf-8"))
            return payload if isinstance(payload, dict) else None
        except Exception:
            return None

    def _handle_update_role_profile(self) -> None:
        from memtrace_harness.cli import update_role_profile_for_project

        payload = self._read_json_body()
        try:
            project = str(payload["project"])
            profile_id = str(payload["profile_id"])
            provider = str(payload["provider"])
            model = str(payload["model"])
        except (TypeError, KeyError):
            self._send_json(400, {"error": "malformed request body"})
            return
        try:
            update_role_profile_for_project(self.config, project, profile_id, provider, model)
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
            return
        except Exception:
            logger.exception("status dashboard failed to update a role profile")
            self._send_json(500, {"error": "internal error updating the role profile"})
            return
        self.bus.publish()
        self._send_json(200, {"ok": True})

    def _handle_make_role_profiles_independent(self) -> None:
        from memtrace_harness.cli import create_dedicated_role_profiles_file

        payload = self._read_json_body()
        try:
            project = str(payload["project"])
        except (TypeError, KeyError):
            self._send_json(400, {"error": "malformed request body"})
            return
        try:
            dest = create_dedicated_role_profiles_file(self.config, project)
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
            return
        except Exception:
            logger.exception("status dashboard failed to create a dedicated role-profiles file")
            self._send_json(500, {"error": "internal error creating the dedicated file"})
            return
        self.bus.publish()
        self._send_json(200, {"ok": True, "path": str(dest)})

    def _handle_update_chat_model(self) -> None:
        from memtrace_harness.cli import update_chat_model_for_project

        payload = self._read_json_body()
        try:
            project = str(payload["project"])
            provider = str(payload["provider"])
            model = str(payload["model"])
        except (TypeError, KeyError):
            self._send_json(400, {"error": "malformed request body"})
            return
        try:
            update_chat_model_for_project(self.config, project, provider, model)
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
            return
        except Exception:
            logger.exception("status dashboard failed to update a chat model")
            self._send_json(500, {"error": "internal error updating the chat model"})
            return
        self.bus.publish()
        self._send_json(200, {"ok": True})

    def _handle_update_digest_model(self) -> None:
        from memtrace_harness.cli import update_digest_model_for_project

        payload = self._read_json_body()
        try:
            project = str(payload["project"])
            provider = str(payload["provider"])
            model = str(payload["model"])
        except (TypeError, KeyError):
            self._send_json(400, {"error": "malformed request body"})
            return
        try:
            update_digest_model_for_project(self.config, project, provider, model)
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
            return
        except Exception:
            logger.exception("status dashboard failed to update a digest model")
            self._send_json(500, {"error": "internal error updating the digest model"})
            return
        self.bus.publish()
        self._send_json(200, {"ok": True})

    def _handle_update_stage_model(self, kind: str) -> None:
        """Recall model / fallbacks: same body shapes as the digest endpoints."""
        from memtrace_harness.cli import (
            update_recall_fallbacks_for_project,
            update_recall_model_for_project,
        )

        payload = self._read_json_body()
        try:
            project = str(payload["project"])
            if kind == "recall-model":
                args = (str(payload["provider"]), str(payload["model"]))
                update = update_recall_model_for_project
            else:
                args = (str(payload["fallbacks"]),)
                update = update_recall_fallbacks_for_project
        except (TypeError, KeyError):
            self._send_json(400, {"error": "malformed request body"})
            return
        try:
            update(self.config, project, *args)
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
            return
        except Exception:
            logger.exception("status dashboard failed to update the recall model settings")
            self._send_json(500, {"error": "internal error updating the recall model"})
            return
        self.bus.publish()
        self._send_json(200, {"ok": True})

    def _handle_update_digest_fallbacks(self) -> None:
        from memtrace_harness.cli import update_digest_fallbacks_for_project

        payload = self._read_json_body()
        try:
            project = str(payload["project"])
            fallbacks = str(payload["fallbacks"])
        except (TypeError, KeyError):
            self._send_json(400, {"error": "malformed request body"})
            return
        try:
            update_digest_fallbacks_for_project(self.config, project, fallbacks)
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
            return
        except Exception:
            logger.exception("status dashboard failed to update digest fallbacks")
            self._send_json(500, {"error": "internal error updating the digest fallbacks"})
            return
        self.bus.publish()
        self._send_json(200, {"ok": True})

    def _handle_approval_action(self) -> None:
        payload = self._read_json_body()
        try:
            project = str(payload["project"])
            request_id = str(payload["request_id"])
            action = str(payload["action"]).lower()
        except (TypeError, KeyError):
            self._send_json(400, {"error": "malformed request body"})
            return
        if action not in ("approve", "reject"):
            self._send_json(
                400, {"error": f"unsupported action {action!r} (expected approve or reject)"}
            )
            return
        gateway = self.gateway_for_project.get(project)
        if gateway is None:
            self._send_json(400, {"error": f"no gateway registered for project {project!r}"})
            return
        req = gateway.approval_manager.get_request(request_id)
        if req is None:
            self._send_json(400, {"error": f"approval request {request_id!r} not found"})
            return
        if req.telegram_chat_id is None:
            self._send_json(
                400, {"error": "this request was never posted to a chat, cannot resolve it here"}
            )
            return
        try:
            # Same code path a Telegram text command, inline button, or swipe-reply
            # would go through — one place resolves an approval, this is just
            # another door to it. Runs any resulting Agent Loop resumption in a
            # background thread (see _resume_approved_conversation()), so this call
            # returns immediately rather than blocking the request on a full run.
            msg = gateway._resolve_approval_action(request_id, action, req.telegram_chat_id, None)
        except Exception:
            logger.exception("status dashboard failed to resolve an approval")
            self._send_json(500, {"error": "internal error resolving the approval"})
            return
        self.bus.publish()
        self._send_json(200, {"ok": True, "message": msg})

    def _handle_preference_action(self) -> None:
        from memtrace_harness.memory_digest import resolve_preference, sync_operator_profile
        from memtrace_harness.memtrace_client import MemTraceClient
        from memtrace_harness.trace_store import TraceStore

        payload = self._read_json_body()
        try:
            rule_id = int(payload["id"])
            action = str(payload["action"]).lower()
        except (TypeError, KeyError, ValueError):
            self._send_json(400, {"error": "malformed request body"})
            return
        text = payload.get("text")
        scope = payload.get("scope")
        if (text is not None and not isinstance(text, str)) or (scope is not None and not isinstance(scope, str)):
            self._send_json(400, {"error": "malformed request body"})
            return
        trace_store = TraceStore(self.config.trace_db_path)
        try:
            rule = resolve_preference(trace_store, rule_id, action, text=text, scope=scope)
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
            return
        except Exception:
            logger.exception("status dashboard failed to resolve a preference")
            self._send_json(500, {"error": "internal error resolving the preference"})
            return
        # Local SQLite is what chat reads, so the change is already in effect here.
        # Rewriting the MemTrace profile node is a best-effort mirror.
        synced: bool | None = None
        if action in ("adopt", "retire", "restore") and self.config.memtrace_mcp_url and self.config.operator_preference_workspace_id:
            try:
                client = MemTraceClient(self.config.memtrace_mcp_url, self.config.memtrace_api_token)
                sync_operator_profile(trace_store, client, self.config.operator_preference_workspace_id)
                synced = True
            except Exception:
                logger.exception("mirroring the operator profile to MemTrace failed")
                synced = False
        self.bus.publish()
        self._send_json(200, {"ok": True, "rule": rule, "memtrace_synced": synced})

    def _send_json(self, status: int, data: dict) -> None:
        payload = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _collect_data(self) -> dict:
        from memtrace_harness.cli import _collect_status_data

        try:
            return _collect_status_data(self.config)
        except Exception as exc:  # keep the dashboard itself from crashing on a bad read
            logger.exception("status dashboard failed to collect status")
            return {
                "daemon": {"pids": [], "multiple_warning": False, "pgrep_error": str(exc), "launchd": None, "launchd_error": None},
                "project_index_path": None,
                "projects": [],
                "known_models": {},
            }

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
    gateway_for_project: dict[str, Any] | None = None,
) -> tuple[ThreadingHTTPServer, StatusEventBus] | tuple[None, None]:
    """Start the web status dashboard in a background thread, bound to
    config.status_server_host (default 127.0.0.1 — not exposed off the machine unless
    the operator explicitly widens it, e.g. via Tailscale). Returns (None, None) if
    disabled or if the port can't be bound (e.g. already running from a previous
    process), so this never blocks the gateway's actual job. The returned bus's
    publish() should be called by the caller whenever something worth showing changed
    (a Telegram update was processed, a scan/consolidation pass did something) so
    connected browsers update immediately instead of waiting for the heartbeat.

    gateway_for_project (project name -> TelegramGateway, same mapping
    _serve_gateway_loop already builds) is what lets the dashboard's approve/reject
    buttons resolve an approval through the exact same TelegramGateway._resolve_
    approval_action() every other entry point (text command, inline button, swipe-
    reply) already goes through — one place, no drift. None (the CLI `gateway`
    one-shot path, or tests) just means approve/reject aren't available; every
    read-only view still works."""
    if not config.status_server_enabled:
        return None, None

    bus = StatusEventBus()
    handler_cls = type(
        "StatusRequestHandler",
        (_StatusRequestHandler,),
        {"config": config, "bus": bus, "gateway_for_project": gateway_for_project or {}},
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
