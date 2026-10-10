var state = { data: null, query: "", prefDrafts: {}, openDetails: {}, renderDeferred: false, project: null, tab: "overview" };
var GLOBAL_TAB = "__global__";
var PROJECT_TABS = [["overview", "總覽"], ["memory", "記憶"], ["schedules", "排程"], ["settings", "設定"]];

try {
  var savedNav = JSON.parse(localStorage.getItem("harness-nav") || "{}");
  if (savedNav.project) state.project = savedNav.project;
  if (savedNav.tab) state.tab = savedNav.tab;
} catch (e) { /* storage unavailable: start on the first project */ }

function saveNav() {
  try { localStorage.setItem("harness-nav", JSON.stringify({ project: state.project, tab: state.tab })); } catch (e) { /* ignore */ }
}

function esc(s) {
  return String(s).replace(/[&<>"']/g, function (c) {
    return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
  });
}

function harnessLines(list) {
  return (list || []).map(function (h) {
    if (!h.up) {
      return '<div class="warn">⚠️ ' + esc(h.name) + "：連不上 Harness（" + esc(h.error || "沒有回應") + "）</div>";
    }
    var backlog = h.outbox_backlog ? ' · <span class="warn">' + h.outbox_backlog + " 則訊息等著送出（聊天服務沒在取？）</span>" : "";
    return "<div>" + esc(h.name) + "：pid " + esc(h.pid) + " · " + esc(h.workspace_dir || "") + backlog + "</div>";
  }).join("") || "（沒有設定任何 Harness）";
}

// The top bar stays one line unless something is wrong; the full list lives in the 全域 tab.
function renderHarnesses(list) {
  var el = document.getElementById("daemon");
  list = list || [];
  var problems = list.filter(function (h) { return !h.up || h.outbox_backlog; });
  el.style.display = problems.length ? "" : "none";
  el.innerHTML = problems.length ? harnessLines(problems) : "";
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

// Every place a model is chosen is a "chain": the primary model plus any number of
// fallbacks tried in order. One editor (provider + model [+ reasoning effort] per row,
// add / remove / reorder) serves chat, digest, recall and each Agent Loop role.
// The model <select> lists everything the provider CLIs offer plus models added by
// hand; "— 自訂 —" takes any other name and remembers it for every dropdown.
var EFFORTS = {
  claude: ["low", "medium", "high", "xhigh", "max"],
  codex: ["minimal", "low", "medium", "high", "xhigh"],
  antigravity: ["low", "medium", "high"]
};

function effortOptions(provider, selected) {
  return (EFFORTS[provider] || []).map(function (e) {
    return '<option value="' + e + '"' + (e === selected ? " selected" : "") + ">" + e + "</option>";
  }).join("");
}

function chainRowHtml(c, withEffort, index) {
  var label = index === 0 ? "主模型" : "備援 " + index;
  return '<div class="rp-row chain-item">' +
    '<span class="hint chain-label">' + label + "</span>" +
    '<select class="rp-provider" onchange="onProviderSelectChange(this)">' + providerOptions(c.provider) + "</select>" +
    '<select class="rp-model-select" onchange="onModelSelectChange(this)">' + modelOptions(c.provider, c.model) + "</select>" +
    '<input class="rp-model-custom" type="text" value="' + esc(c.model || "") + '" placeholder="模型名稱" style="display:none">' +
    (withEffort ? '<select class="rp-effort" title="reasoning effort">' + effortOptions(c.provider, c.reasoning_effort) + "</select>" : "") +
    '<button class="chain-up" title="上移">↑</button><button class="chain-down" title="下移">↓</button>' +
    '<button class="chain-del" title="移除">✕</button>' +
    "</div>";
}

function chainEditorHtml(project, kind, chain, withEffort, profileId) {
  var key = "chain:" + project.name + ":" + kind + ":" + (profileId || "");
  return '<details class="chain-edit" data-key="' + esc(key) + '"' + detailsOpen(key) + "><summary>編輯</summary>" +
    chainEditorInner(project, kind, chain, withEffort, profileId) + "</details>";
}

function chainEditorInner(project, kind, chain, withEffort, profileId) {
  var list = chain && chain.length ? chain : [{ provider: "claude", model: "", reasoning_effort: "medium" }];
  return '<div class="chain" data-kind="' + kind + '" data-project="' + esc(project.name) + '"' +
    (profileId ? ' data-profile="' + esc(profileId) + '"' : "") +
    (withEffort ? ' data-effort="1"' : "") + ">" +
    '<div class="chain-items">' + list.map(function (c, i) { return chainRowHtml(c, withEffort, i); }).join("") + "</div>" +
    '<div class="rp-row"><button class="chain-add">＋ 新增備援</button>' +
    '<button class="chain-save">儲存</button><button class="chain-reset">還原</button>' +
    (kind === "role" ? '<button class="chain-apply">套用備援到其他角色…</button>' : "") +
    '<span class="rp-status"></span></div>' +
    (kind === "role" ? applyPanelHtml(project, profileId) : "") + "</div>";
}

// "Apply to other roles": copies this editor's fallbacks (not its primary) onto the chosen roles,
// each of which keeps its own primary model.
function applyPanelHtml(project, profileId) {
  var others = (project.role_profiles || []).filter(function (p) { return p.id !== profileId; });
  return '<div class="rp-row chain-apply-panel" style="display:none"><span class="hint">把目前這份備援順序套用到（會取代它們原有的備援，主模型不變）：</span>' +
    others.map(function (p) {
      return '<label><input type="checkbox" class="apply-target" value="' + esc(p.id) + '"> ' + esc(p.id) + "</label>";
    }).join(" ") +
    '<button class="chain-apply-go">套用</button><span class="apply-status"></span></div>';
}

var toastTimer = null;
function showToast(text, isError) {
  var el = document.getElementById("toast");
  el.textContent = text;
  el.className = "toast" + (isError ? " err" : "");
  el.style.display = "block";
  clearTimeout(toastTimer);
  toastTimer = setTimeout(function () { el.style.display = "none"; }, 8000);
}

function applyFallbacksToRoles(chainEl, btn) {
  var panel = chainEl.querySelector(".chain-apply-panel");
  var targets = Array.prototype.map.call(panel.querySelectorAll(".apply-target:checked"), function (c) { return c.value; });
  var statusEl = panel.querySelector(".apply-status");
  var rows = Array.prototype.map.call(chainEl.querySelectorAll(".chain-item"), readChainRow);
  var fallbacks = rows.slice(1);
  statusEl.className = "apply-status rp-status err";
  if (!targets.length) { statusEl.textContent = "請先勾選要套用的角色"; return; }
  if (fallbacks.some(function (f) { return !f.model; })) { statusEl.textContent = "備援裡有未選擇的模型"; return; }
  var projectName = chainEl.getAttribute("data-project");
  var project = state.data.projects.filter(function (p) { return p.name === projectName; })[0];
  btn.disabled = true;
  statusEl.className = "apply-status rp-status";
  statusEl.textContent = "套用中…";
  var done = [], failed = [];
  targets.reduce(function (chain, id) {
    return chain.then(function () {
      var role = project.role_profiles.filter(function (p) { return p.id === id; })[0];
      var primary = { provider: role.provider, model: role.model, reasoning_effort: role.reasoning_effort };
      return fetch("/api/role-profile-chain", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ project: projectName, profile_id: id, chain: [primary].concat(fallbacks) })
      }).then(function (r) { return r.json().then(function (d) { return { ok: r.ok, body: d }; }); })
        .then(function (res) { (res.ok ? done : failed).push(res.ok ? id : id + "（" + (res.body.error || "失敗") + "）"); })
        .catch(function () { failed.push(id + "（連線問題）"); });
    });
  }, Promise.resolve()).then(function () {
    btn.disabled = false;
    statusEl.className = "apply-status rp-status " + (failed.length ? "err" : "ok");
    var message = (done.length ? "已套用備援到：" + done.join("、") + "，下次執行生效。" : "") +
      (failed.length ? " 失敗：" + failed.join("、") : "");
    statusEl.textContent = message;
    showToast(message, failed.length > 0);
  });
}

function onProviderSelectChange(providerSelect) {
  var row = providerSelect.closest(".rp-row");
  row.querySelector(".rp-model-select").innerHTML = modelOptions(providerSelect.value, "");
  var effort = row.querySelector(".rp-effort");
  if (effort) effort.innerHTML = effortOptions(providerSelect.value, "medium");
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

function readChainRow(row) {
  var modelSelect = row.querySelector(".rp-model-select");
  var effort = row.querySelector(".rp-effort");
  return {
    provider: row.querySelector(".rp-provider").value,
    model: (modelSelect.value === CUSTOM_MODEL_VALUE ? row.querySelector(".rp-model-custom").value : modelSelect.value).trim(),
    reasoning_effort: effort ? effort.value : ""
  };
}

function relabelChain(chainEl) {
  chainEl.querySelectorAll(".chain-item").forEach(function (row, i) {
    row.querySelector(".chain-label").textContent = i === 0 ? "主模型" : "備援 " + i;
  });
}

function chainCandidates(list) {
  return (list || []).map(function (c) { return { provider: c.provider, model: c.model, reasoning_effort: c.reasoning_effort || "medium" }; });
}

function chainDisplay(list, emptyText) {
  if (!list || !list.length) return '<div class="empty">' + emptyText + "</div>";
  return '<div class="chips">' + list.map(function (c) {
    return '<span class="chip">' + esc(c.provider) + "/" + esc(c.model || "(預設)") + "</span>";
  }).join(" → ") + "</div>";
}

function renderChain(project, kind, list, emptyText, note) {
  return chainDisplay(list, emptyText) + (note ? '<div class="hint">' + note + "</div>" : "") +
    chainEditorHtml(project, kind, chainCandidates(list), false);
}

function renderChatCandidates(project) {
  return renderChain(project, "chat", project.chat_candidates, "（未設定 HARNESS_CHAT_PROVIDER）", "");
}

function renderRecallCandidates(project) {
  var note = project.recall_enabled === false ? "背景回想已停用（HARNESS_RECALL_ENABLED=false）。"
    : (project.recall_is_own ? "" : "尚未獨立設定，目前沿用每日摘要模型。");
  return renderChain(project, "recall", project.recall_candidates, "（未設定）", note);
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
  return renderChain(project, "digest", project.digest_candidates, "（未設定）",
    project.digest_is_own ? "" : "尚未獨立設定，目前沿用聊天模型；儲存後摘要才會改用自己的模型。");
}

function renderArchiveCandidates(project) {
  return renderChain(project, "archive", project.archive_candidates, "（未設定）",
    project.archive_is_own ? "" : "尚未獨立設定，目前沿用每日摘要模型；儲存後記憶整理才會改用自己的模型。");
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
    var chain = [{ provider: p.provider, model: p.model, reasoning_effort: p.reasoning_effort }].concat(p.fallbacks);
    var cell = chainDisplay(chain, "") ;
    if (editable) cell += chainEditorHtml(project, "role", chainCandidates(chain), true, p.id);
    return "<tr><td>" + esc(p.id) + "</td><td>" + cell + "</td></tr>";
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

// A chain being edited must survive the live re-renders until it is saved or reset.
document.addEventListener("click", function (e) {
  var t = e.target;
  var chainEl = t.closest && t.closest(".chain");
  if (!chainEl || t.tagName !== "BUTTON") return;
  var items = chainEl.querySelector(".chain-items");
  var withEffort = !!chainEl.getAttribute("data-effort");
  var row = t.closest(".chain-item");
  if (t.classList.contains("chain-add")) {
    var last = items.lastElementChild ? readChainRow(items.lastElementChild) : { provider: "claude", model: "", reasoning_effort: "medium" };
    var holder = document.createElement("div");
    holder.innerHTML = chainRowHtml({ provider: last.provider, model: "", reasoning_effort: last.reasoning_effort || "medium" }, withEffort, items.children.length);
    var fresh = holder.firstChild;
    items.appendChild(fresh);
    fresh.querySelector(".rp-model-select").selectedIndex = 0;
    syncCustomModelVisibility(fresh);
    state.chainDirty = true;
  } else if (t.classList.contains("chain-del") && row) {
    if (items.children.length > 1) { row.remove(); state.chainDirty = true; }
  } else if (t.classList.contains("chain-up") && row && row.previousElementSibling) {
    items.insertBefore(row, row.previousElementSibling); state.chainDirty = true;
  } else if (t.classList.contains("chain-down") && row && row.nextElementSibling) {
    items.insertBefore(row.nextElementSibling, row); state.chainDirty = true;
  } else if (t.classList.contains("chain-apply")) {
    var applyPanel = chainEl.querySelector(".chain-apply-panel");
    applyPanel.style.display = applyPanel.style.display === "none" ? "flex" : "none";
    return;
  } else if (t.classList.contains("chain-apply-go")) {
    applyFallbacksToRoles(chainEl, t);
    return;
  } else if (t.classList.contains("chain-reset")) {
    state.chainDirty = false; render(); return;
  } else if (t.classList.contains("chain-save")) {
    saveChain(chainEl, t);
    return;
  } else {
    return;
  }
  relabelChain(chainEl);
});

document.addEventListener("change", function (e) {
  if (e.target.closest && e.target.closest(".chain")) state.chainDirty = true;
});

function saveChain(chainEl, btn) {
  var kind = chainEl.getAttribute("data-kind");
  var chain = Array.prototype.map.call(chainEl.querySelectorAll(".chain-item"), readChainRow);
  var body = { project: chainEl.getAttribute("data-project"), chain: chain };
  if (kind === "role") body.profile_id = chainEl.getAttribute("data-profile");
  var statusEl = chainEl.querySelector(".rp-status");
  statusEl.className = "rp-status";
  statusEl.textContent = "儲存中…";
  btn.disabled = true;
  fetch(kind === "role" ? "/api/role-profile-chain" : "/api/" + kind + "-chain", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body)
  })
    .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, body: d }; }); })
    .then(function (res) {
      btn.disabled = false;
      statusEl.className = "rp-status " + (res.ok ? "ok" : "err");
      if (res.ok) {
        state.chainDirty = false;
        if (state.renderDeferred) setTimeout(render, 1500);
        statusEl.textContent = kind === "role" ? "已儲存，下次執行生效" : "已儲存，立即生效";
      } else {
        statusEl.textContent = res.body.error || "儲存失敗";
      }
    })
    .catch(function () {
      btn.disabled = false;
      statusEl.className = "rp-status err";
      statusEl.textContent = "儲存失敗（連線問題）";
    });
}

function refreshModelCatalog(btn) {
  btn.disabled = true;
  var first = state.data.projects[0];
  fetch("/api/model-catalog-refresh", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ project: first ? first.name : "" })
  }).finally(function () { btn.disabled = false; });
}

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
        statusEl.textContent = "已建立 " + res.body.path + "，下次執行任務就會用它，現在可以在此編輯";
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

var WAITING_ACTION_TEXT = { approve: "核准中…", reject: "處理中…", choose: "送出中…", clarify: "送出中…", abandon: "放棄中…" };

document.addEventListener("click", function (e) {
  if (!e.target.classList.contains("approval-btn")) return;
  var btn = e.target;
  var project = btn.getAttribute("data-project");
  var action = btn.getAttribute("data-action");
  var item = btn.closest(".approval");
  var statusEl = item.querySelector(".approval-status");
  var buttons = item.querySelectorAll(".approval-btn");
  var url = "/api/approval";
  var body = { project: project, request_id: btn.getAttribute("data-request"), action: action };
  if (action === "choose") {
    body.choice = parseInt(btn.getAttribute("data-choice"), 10);
  } else if (action === "clarify") {
    var box = item.querySelector(".answer-box");
    body.answer = box ? box.value.trim() : "";
    if (!body.answer) {
      statusEl.className = "approval-status err";
      statusEl.textContent = "請先寫下答案";
      return;
    }
  } else if (action === "abandon") {
    if (!confirm("放棄這個任務？它會被標記為失敗，並釋放工作區。")) return;
    url = "/api/stuck";
    body = { project: project, conversation_id: btn.getAttribute("data-conversation"), action: "abandon" };
  }
  statusEl.className = "approval-status";
  statusEl.textContent = WAITING_ACTION_TEXT[action] || "處理中…";
  buttons.forEach(function (b) { b.disabled = true; });
  fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body)
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

function sinceText(iso) {
  var t = Date.parse(iso || "");
  if (isNaN(t)) return "";
  var m = Math.max(0, Math.round((Date.now() - t) / 60000));
  if (m < 60) return m + " 分鐘前";
  if (m < 1440) return Math.round(m / 60) + " 小時前";
  return Math.round(m / 1440) + " 天前";
}

function waitingButton(cls, project, id, action, text, extra) {
  return '<button class="approval-btn ' + cls + '" data-project="' + esc(project) + '" data-request="' + esc(id) +
    '" data-action="' + action + '"' + (extra || "") + ">" + text + "</button>";
}

function renderWaitingItem(it, projectName) {
  var head = '<div><span class="tag">' + esc(it.label) + '</span> <span class="id">' + esc(it.id) + "</span>" +
    (it.since ? ' <span class="muted">' + esc(sinceText(it.since)) + "</span>" : "") + "</div>" +
    '<div class="wait-summary">' + esc(it.summary) + "</div>";
  var actions = "";
  if (it.kind === "approval" && it.needs_answer) {
    var opts = (it.options || []).map(function (o, i) {
      return waitingButton("choice", projectName, it.id, "choose",
        (i === it.recommended ? "⭐ " : "") + esc(o.label), ' data-choice="' + i + '" title="' + esc(o.action + (o.tradeoff ? "（" + o.tradeoff + "）" : "")) + '"');
    }).join("");
    var detail = (it.options || []).map(function (o, i) {
      return '<div class="muted">' + String.fromCharCode(65 + i) + "．" + esc(o.label) + "：" + esc(o.action) + "</div>";
    }).join("");
    actions = (it.situation ? '<div class="muted">' + esc(it.situation) + "</div>" : "") + detail +
      '<div class="approval-actions">' + opts + "</div>" +
      '<textarea class="answer-box" rows="2" placeholder="或直接寫下具體答案…"></textarea>' +
      '<div class="approval-actions">' +
      waitingButton("approve", projectName, it.id, "clarify", "送出回答") +
      waitingButton("reject", projectName, it.id, "reject", "❌ 放棄這個任務") +
      '<span class="approval-status"></span></div>';
  } else if (it.kind === "approval") {
    actions = '<div class="approval-actions">' +
      waitingButton("approve", projectName, it.id, "approve", "✅ 核准") +
      waitingButton("reject", projectName, it.id, "reject", "❌ 拒絕") +
      '<span class="approval-status"></span></div>';
  } else if (it.kind === "confirm") {
    actions = '<div class="muted">請到 Telegram 按確認（' + "到期 " + esc((it.expires_at || "").slice(11, 16)) + " UTC，逾時自動取消）</div>" +
      '<div class="approval-actions"><span class="approval-status"></span></div>';
  } else if (it.kind === "interrupted") {
    actions = '<div class="approval-actions">' +
      '<button class="approval-btn reject" data-project="' + esc(projectName) + '" data-conversation="' + esc(it.id) +
      '" data-action="abandon">放棄這個任務</button><span class="approval-status"></span></div>' +
      '<div class="muted">想繼續做的話，改在 Telegram 對該任務用 /clarify 回覆。</div>';
  }
  return '<div class="approval">' + head + actions + "</div>";
}

function renderBacklog(project) {
  var w = project.waiting || {};
  var b = w.backlog;
  if (!b) return "";
  var queued = b.queued || [], held = b.held || [];
  var line = function (c, why) {
    return '<div class="muted">' + esc(c.id) + " " + esc(c.title) + (why ? "（" + esc(why) + "）" : "") + "</div>";
  };
  return '<div class="section-label">Issue 清單 <span class="badge">開啟 ' + b.open + "</span> " +
    '<span class="badge">排隊 ' + queued.length + '</span> <span class="badge">擱置 ' + held.length + "</span></div>" +
    (b.blocked === "locked"
      ? '<div class="approval-status err">掃描被工作區鎖擋住，現在不會提出新任務（上次檢查 ' + esc(sinceText(b.checked_at)) + "）</div>"
      : '<div class="muted">上次檢查 ' + esc(sinceText(b.checked_at)) + "；一次只提一張，等你核准後才開工</div>") +
    (queued.length ? '<div class="muted">排隊（最舊的先提）</div>' + queued.map(function (c) { return line(c); }).join("") : "") +
    (held.length ? '<div class="muted">擱置</div>' + held.map(function (c) { return line(c, c.why); }).join("") : "");
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

function renderPipeline(pipeline, projectName) {
  if (!pipeline || !pipeline.length) {
    return '<div class="empty">尚未有階段完成（可能剛啟動）</div>';
  }
  var steps = pipeline.map(function (t) {
    var stateClass = t.state === "succeeded" ? "ok" : (t.state === "invalid_output" || t.state === "failed" || t.state === "unavailable" ? "bad" : "mid");
    var icon = STATE_ICON[t.state] || "•";
    var retryTag = t.attempt_index > 0 ? '<span class="chip">重試 #' + t.attempt_index + '</span>' : "";
    return '<div class="step ' + stateClass + '" onclick="openStageDetail(' + t.turn_id + ',' + JSON.stringify(projectName).replace(/"/g, '&quot;') + ')" title="' + esc(t.provider + "/" + (t.model || "?")) + '">' +
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

function openStageDetail(turnId, project) {
  var panel = document.getElementById("stage-detail");
  var body = document.getElementById("stage-detail-body");
  panel.classList.add("open");
  body.innerHTML = '<div class="empty">載入中…</div>';
  fetch("/api/turn?turn_id=" + turnId + "&project=" + encodeURIComponent(project || ""))
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
  var key = "evidence:" + rule.uid;
  var items = (rule.evidence || []).map(function (e) {
    var at = e.turn_seq === null || e.turn_seq === undefined ? "" : " #" + e.turn_seq;
    return '<div class="evidence-item">' + esc(e.date) + esc(at) + "：" + esc(e.quote) + "</div>";
  }).join("");
  return '<details class="evidence" data-key="' + key + '"' + detailsOpen(key) + "><summary>你當時說的原話（" +
    (rule.evidence || []).length + "）</summary>" + items + "</details>";
}

function renderPendingPreference(rule) {
  var draft = state.prefDrafts[rule.uid] || {};
  var text = draft.text !== undefined ? draft.text : rule.text;
  var scope = draft.scope || rule.scope;
  return '<div class="pref" data-id="' + rule.uid + '">' +
    '<div class="meta"><span class="chip">' + esc(categoryLabel(rule.category)) + "</span>" +
    (rule.explicit
      ? '<span class="tag-explicit">● 你明確說過的規則</span>'
      : '<span class="tag-inferred">● 模型推測，請仔細確認</span>') +
    "<span>來源：" + esc(rule.project) + " · " + esc(rule.source_digest_date || "") + "</span></div>" +
    '<textarea class="pref-text" data-id="' + rule.uid + '">' + esc(text) + "</textarea>" +
    '<div class="pref-actions">' +
    '<select class="pref-scope" data-id="' + rule.uid + '">' +
    '<option value="global"' + (scope === "global" ? " selected" : "") + ">全域（所有專案）</option>" +
    '<option value="project"' + (scope === "project" ? " selected" : "") + ">僅 " + esc(rule.project) + "</option>" +
    "</select>" +
    '<button class="pref-btn adopt" data-id="' + rule.uid + '" data-action="adopt">✅ 採用</button>' +
    '<button class="pref-btn dismiss" data-id="' + rule.uid + '" data-action="dismiss">略過</button>' +
    '<span class="pref-status"></span></div>' +
    renderEvidence(rule) + "</div>";
}

var ADOPTED_BY_LABELS = {
  "harness-digest": "Harness 每晚整理時自動採用",
  "harness-chat": "Harness 依你的聊天更正",
  "operator": "你在網頁採用"
};

function renderAdoptedPreference(rule) {
  return '<div class="pref" data-id="' + rule.uid + '">' +
    '<div class="meta"><span class="chip">#' + rule.id + " " + esc(categoryLabel(rule.category)) + "</span>" +
    '<span class="tag-auto">' + esc(ADOPTED_BY_LABELS[rule.adopted_by] || "已採用") + "</span>" +
    "<span>" + (rule.scope === "global" ? "全域" : "僅 " + esc(rule.project)) + " · " + esc((rule.resolved_at || "").slice(0, 10)) + "</span></div>" +
    '<div class="content">' + esc(rule.text) + "</div>" +
    '<div class="pref-actions"><button class="pref-btn retire" data-id="' + rule.uid + '" data-action="retire">撤銷</button>' +
    '<span class="pref-status"></span></div>' +
    renderEvidence(rule) + "</div>";
}

function renderRetiredPreference(rule) {
  return '<div class="pref" data-id="' + rule.uid + '">' +
    '<div class="meta"><span class="chip">#' + rule.id + " " + esc(categoryLabel(rule.category)) + "</span>" +
    "<span>" + esc((rule.resolved_at || "").slice(0, 10)) + " 撤回</span></div>" +
    '<div class="content">' + esc(rule.text) + "</div>" +
    '<div class="meta">' + esc(rule.retire_reason || "") + "</div>" +
    '<div class="pref-actions"><button class="pref-btn restore" data-id="' + rule.uid + '" data-action="restore">恢復</button>' +
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
  return html;
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

// The settings tab: each chain shows as chips; "編輯" opens its editor (open state is remembered
// across the live re-renders).
function renderModelSettings(project) {
  return '<div class="hint">每一處都可設定主模型與任意數量的備援（依序嘗試）；按「編輯」展開。清單包含各 CLI 目前提供的模型；'
    + '沒列出的選「— 自訂 —」輸入名稱，之後所有選單都會記住。 <button class="catalog-refresh" onclick="refreshModelCatalog(this)">重新抓取模型清單</button></div>' +
    '<div class="section-label">聊天模型</div>' + renderChatCandidates(project) +
    '<div class="section-label">每日摘要模型</div>' + renderDigestCandidates(project) +
    '<div class="section-label">記憶整理模型（夜間分題、每週審查、重建）</div>' + renderArchiveCandidates(project) +
    '<div class="section-label">背景回想模型</div>' + renderRecallCandidates(project) +
    '<div class="section-label">Agent Loop 角色模型</div>' + renderRoleProfiles(project);
}

function renderProject(project, q) {
  var d = project.dedicated;
  var running = (project.locks || []).length;
  var runningBadge = running
    ? '<span class="badge running">執行中 ' + running + "/" + project.max_workers + "</span>"
    : "";
  var content = "";
  if (state.tab === "memory") {
    content = renderTopicBriefs(project) + renderDigests(project);
  } else if (state.tab === "schedules") {
    content = renderSchedules(project) || '<div class="empty">（沒有排程）</div>';
  } else if (state.tab === "settings") {
    content = '<div class="chips">' +
      '<span class="chip' + (d.chat_model ? " on" : "") + '">chat: ' + (d.chat_model ? "已設定" : "預設") + "</span>" +
      '<span class="chip' + (d.memory ? " on" : "") + '">memory: ' + (d.memory ? "專屬" : "同專案") + "</span>" +
      '<span class="chip' + (d.role_profiles ? " on" : "") + '">role-profiles: ' + (d.role_profiles ? "自己的檔案" : "預設") + "</span>" +
      "</div>" + renderModelSettings(project);
  } else {
    var lock = running || (project.queue || []).length ? renderLock(project) : "";
    var pipelineSection = "";
    if (project.pipeline_conversation_id) {
      var pipelineHint = project.lock
        ? ""
        : '<div class="lock-progress">（等待核准中，非鎖定狀態 — conversation_id=' + esc(project.pipeline_conversation_id) + '）</div>';
      pipelineSection = '<div class="section-label">Agent Loop 進度</div>' + pipelineHint + renderPipeline(project.pipeline, project.name);
    }
    content = (lock || pipelineSection ? lock + pipelineSection : '<div class="empty">目前沒有進行中的任務</div>') +
      renderBacklog(project) +
      '<div class="section-label">對話記錄 (' + project.turn_count + ' 則，目標待處理 ' + project.pending_goal + ')</div>' +
      renderTurns(project.recent_turns, q, project.name);
  }
  var subtabs = PROJECT_TABS.map(function (t) {
    var badge = t[0] === "schedules" && (project.schedules || []).length
      ? ' <span class="count">' + project.schedules.length + "</span>" : "";
    return '<button class="subtab' + (state.tab === t[0] ? " active" : "") + '" data-tab="' + t[0] + '">' + t[1] + badge + "</button>";
  }).join("");
  return '<div class="project">' +
    '<div class="project-head"><h2>' + esc(project.name) + runningBadge + "</h2>" +
    '<div class="path">' + esc(project.working_directory) +
    ((project.repos || []).length > 1
      ? " · repos：" + project.repos.map(function (r) {
          return esc(r.name) + (r.primary ? "（主要）" : "") + (r.exists ? "" : " ⚠️");
        }).join("、")
      : "") + "</div></div>" +
    '<div class="subtabs">' + subtabs + "</div>" +
    '<div class="panel">' + content + "</div></div>";
}

function renderGlobal() {
  return '<div class="project"><div class="project-head"><h2>全域</h2></div><div class="panel">' +
    '<div class="section-label">Harness 連線</div><div class="daemon-list">' + harnessLines(state.data.harnesses) + "</div>" +
    renderMemory() + "</div></div>";
}

function projectMatches(project) { return matchesQuery(project, state.query); }

function renderTabs() {
  var projects = state.data.projects;
  var html = projects.map(function (p) {
    var running = (p.locks || []).length;
    var approvals = ((p.waiting || {}).total) || 0;
    return '<button class="tab' + (state.project === p.name ? " active" : "") + (projectMatches(p) ? "" : " dim") +
      '" data-project="' + esc(p.name) + '">' + esc(p.name) +
      (running ? ' <span class="count live">▶ ' + running + "</span>" : "") +
      (approvals ? ' <span class="count warn">⚠ ' + approvals + "</span>" : "") + "</button>";
  }).join("");
  var pendingPrefs = ((state.data.preferences || {}).pending || []).length;
  html += '<button class="tab' + (state.project === GLOBAL_TAB ? " active" : "") + '" data-project="' + GLOBAL_TAB + '">全域' +
    (pendingPrefs ? ' <span class="count warn">⚠ ' + pendingPrefs + "</span>" : "") + "</button>";
  document.getElementById("project-tabs").innerHTML = html;
}

// Everything waiting on a person, across all projects, above the tabs: the thing most worth seeing first.
function renderApprovalsBanner() {
  var el = document.getElementById("approvals-banner");
  var total = 0, html = "";
  state.data.projects.forEach(function (p) {
    var w = p.waiting || { total: 0, items: [] };
    if (!w.total) return;
    total += w.total;
    html += '<div class="banner-project"><a class="goto" data-project="' + esc(p.name) + '">' + esc(p.name) + "</a> " +
      '<span class="badge">' + w.total + " 項</span>" +
      w.items.map(function (it) { return renderWaitingItem(it, p.name); }).join("") + "</div>";
  });
  el.innerHTML = html ? '<div class="banner"><div class="banner-title">有 ' + total + " 項在等你處理</div>" + html + "</div>" : "";
}

function isEditingPreference() {
  var el = document.activeElement;
  return !!(el && el.classList && (el.classList.contains("pref-text") || el.classList.contains("pref-scope")));
}

function render() {
  if (!state.data) return;
  // An SSE push re-renders everything; don't yank the textarea out from under
  // someone mid-edit — render once they leave it.
  if (isEditingPreference() || state.chainDirty) { state.renderDeferred = true; return; }
  state.renderDeferred = false;
  var names = state.data.projects.map(function (p) { return p.name; });
  if (state.project !== GLOBAL_TAB && names.indexOf(state.project) === -1) state.project = names[0] || GLOBAL_TAB;
  renderHarnesses(state.data.harnesses);
  renderApprovalsBanner();
  renderTabs();
  var main = document.getElementById("projects");
  if (state.project === GLOBAL_TAB) {
    main.innerHTML = renderGlobal();
  } else {
    var project = state.data.projects.filter(function (p) { return p.name === state.project; })[0];
    main.innerHTML = project ? renderProject(project, state.query) : '<div class="empty">（沒有已註冊的專案）</div>';
  }
}

document.addEventListener("click", function (e) {
  var tab = e.target.closest && e.target.closest(".tab, .goto");
  var sub = e.target.closest && e.target.closest(".subtab");
  if (tab) {
    if (state.chainDirty && !confirm("有尚未儲存的模型修改，切換會捨棄，確定嗎？")) return;
    state.chainDirty = false;
    state.project = tab.getAttribute("data-project");
    if (tab.classList.contains("goto")) state.tab = "overview";
  } else if (sub) {
    if (state.chainDirty && !confirm("有尚未儲存的模型修改，切換會捨棄，確定嗎？")) return;
    state.chainDirty = false;
    state.tab = sub.getAttribute("data-tab");
  } else {
    return;
  }
  saveNav();
  render();
});

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
  var uidParts = String(id).split(":");
  var body = { id: Number(uidParts.pop()), project: uidParts.join(":"), action: action };
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

state.data = window.__INITIAL_DATA__;
render();

var dot = document.getElementById("dot");
var conn = document.getElementById("conn");
var es = new EventSource("/events");
es.onopen = function () { dot.className = "live"; conn.textContent = "即時連線中"; };
es.onerror = function () { dot.className = ""; conn.textContent = "連線中斷，重試中…"; };
es.onmessage = function (evt) { state.data = JSON.parse(evt.data); render(); };
