var state = { data: null, query: "", prefDrafts: {}, sharedDrafts: {}, sourceDrafts: {}, sourceAccessDrafts: {}, openDetails: {}, renderDeferred: false, project: null, tab: "overview", invite: null };
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

var PROVIDERS = ["claude", "codex", "antigravity", "cursor"];

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
  antigravity: ["low", "medium", "high"],
  cursor: ["low", "medium", "high", "xhigh", "max"]
};

function effortOptions(provider, selected) {
  return (EFFORTS[provider] || []).map(function (e) {
    return '<option value="' + e + '"' + (e === selected ? " selected" : "") + ">" + e + "</option>";
  }).join("");
}

function sourceStoreFor(project) {
  return (state.data.execution_sources || []).filter(function (store) { return store.store_id === project.execution_sources_store_id; })[0];
}

function executionSourceOptions(provider, current, projectName) {
  var project = (state.data.projects || []).filter(function (p) { return p.name === projectName; })[0] || {};
  var store = sourceStoreFor(project) || {};
  var allowed = project.execution_sources_allowed || [];
  var entries = (store.entries || []).filter(function (entry) { return entry.provider === provider && allowed.indexOf(entry.id) !== -1; });
  var html = '<option value="">原本指令</option>';
  if (current && !entries.some(function (entry) { return entry.id === current; })) html += '<option selected value="' + esc(current) + '">' + esc(current) + '（未授權或來源不存在）</option>';
  return html + entries.map(function (entry) {
    return '<option value="' + esc(entry.id) + '"' + (entry.id === current ? ' selected' : '') + '>' + esc(entry.label) + (entry.enabled ? '' : '（停用）') + '</option>';
  }).join('');
}

function chainRowHtml(c, withEffort, index, projectName) {
  var label = index === 0 ? "主模型" : "備援 " + index;
  return '<div class="rp-row chain-item">' +
    '<span class="hint chain-label">' + label + "</span>" +
    '<select class="rp-provider" onchange="onProviderSelectChange(this)">' + providerOptions(c.provider) + "</select>" +
    '<select class="rp-source" aria-label="執行來源">' + executionSourceOptions(c.provider, c.source, projectName) + "</select>" +
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
    '<div class="chain-items">' + list.map(function (c, i) { return chainRowHtml(c, withEffort, i, project.name); }).join("") + "</div>" +
    '<div class="rp-row"><button class="chain-add">＋ 新增備援</button>' +
    '<button class="chain-save">儲存</button><button class="chain-reset">還原</button>' +
    (kind === "role" ? '<button class="chain-apply">套用備援到其他角色…</button>' : "") +
    '<span class="rp-status"></span></div>' +
    (kind === "role" ? applyPanelHtml(project, profileId) : "") + "</div>";
}

// "Apply to other roles": copies this editor's fallbacks (not its primary) onto the chosen roles,
// each of which keeps its own primary model.
function applyPanelHtml(project, profileId) {
  var others = (project.role_profiles || []).filter(function (p) { return p.id !== profileId && p.id !== "controller"; });
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
      var primary = { provider: role.provider, source: role.source, model: role.model, reasoning_effort: role.reasoning_effort };
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
  var source = row.querySelector(".rp-source");
  if (source) source.innerHTML = executionSourceOptions(providerSelect.value, null, row.closest(".chain").getAttribute("data-project"));
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
    source: row.querySelector(".rp-source").value || null,
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
  return (list || []).map(function (c) { return { provider: c.provider, source: c.source, model: c.model, reasoning_effort: c.reasoning_effort || "medium" }; });
}

function chainDisplay(list, emptyText) {
  if (!list || !list.length) return '<div class="empty">' + emptyText + "</div>";
  return '<div class="chips">' + list.map(function (c) {
    return '<span class="chip">' + esc(c.provider + (c.source ? "@" + c.source : "")) + "/" + esc(c.model || "(預設)") + "</span>";
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
  var note = project.recall_is_own ? "" : "尚未獨立設定，目前沿用每日摘要模型。";
  return renderChain(project, "recall", project.recall_candidates, "（未設定）", note);
}

function renderTopicBriefs(project) {
  var list = project.topic_briefs || [];
  var st = project.recall_state || {};
  var sum = list.length + " 份有效" + (st.pending ? " · 整理中 " + st.pending : "");
  var err = st.last_error ? '<div class="warn">⚠️ 上次背景整理失敗：' + esc(st.last_error) + "</div>" : "";
  var wrap = function (body) { return fold("brief-all:" + project.name, "主題簡報（短效期）", sum, body); };
  if (!list.length) return wrap(err + '<div class="empty">（目前沒有有效的主題簡報）</div>');
  return wrap(err + list.map(function (b) {
    var key = "brief:" + project.name + ":" + b.id;
    var related = (b.related || []).map(function (r) {
      return '<div class="evidence-item">' + esc((r.date ? r.date + " " : "") + (r.title || r.node_id)) +
        "（" + esc(r.node_id) + "）：" + esc(r.why) + "</div>";
    }).join("");
    return '<details class="digest" data-key="' + esc(key) + '"' + detailsOpen(key) + "><summary>" +
      esc(b.title) + (b.fresh ? "（全新主題）" : "") + (b.pushed ? " · 已推送" : "") +
      " · 到期 " + esc((b.expires_at || "").slice(0, 16).replace("T", " ")) + "</summary>" +
      '<div class="digest-body">' + esc(b.summary) + related + "</div></details>";
  }).join(""));
}

function renderDigestCandidates(project) {
  return renderChain(project, "digest", project.digest_candidates, "（未設定）",
    project.digest_is_own ? "" : "尚未獨立設定，目前沿用聊天模型；儲存後摘要才會改用自己的模型。");
}

function renderArchiveCandidates(project) {
  return renderChain(project, "archive", project.archive_candidates, "（未設定）",
    project.archive_is_own ? "" : "尚未獨立設定，目前沿用每日摘要模型；儲存後記憶整理才會改用自己的模型。");
}

function renderAuditCandidates(project) {
  return renderChain(project, "audit", project.audit_candidates, "（未設定）",
    project.audit_is_own ? "" : "尚未獨立設定，目前沿用每日摘要模型；建議改用與摘要不同的供應商，稽核才更獨立。");
}

function renderMemoryFeature(project, feature, title, purpose, enabled, chain) {
  var brief = { archive: project.archive_candidates, audit: project.audit_candidates, recall: project.recall_candidates }[feature];
  return fold("model:" + feature + ":" + project.name, esc(title), (enabled ? "啟用 · " : "停用 · ") + chainBrief(brief),
    '<div class="memory-feature"><div class="memory-feature-head"><div>' +
    '<div class="hint">主要用途：' + esc(purpose) + '</div></div>' +
    '<label class="memory-feature-toggle"><input type="checkbox" class="memory-feature-enabled" data-project="' +
    esc(project.name) + '" data-feature="' + esc(feature) + '"' + (enabled ? " checked" : "") +
    '> 啟用</label><span class="memory-feature-status rp-status"></span></div>' + chain + '</div>');
}

function renderAudit(project) {
  var a = project.audit || {};
  var trend = (a.trend || []).map(function (t) {
    return esc(t.date.slice(5)) + " " + (t.friction == null ? "–" : t.friction);
  }).join(" · ");
  var notes = (a.notes || []).map(function (n) {
    return '<div class="evidence-item">[' + esc(n.target) + "] " + esc(n.text) + "</div>";
  }).join("") || '<div class="empty">（目前沒有生效中的校正筆記）</div>';
  var props = (a.open_proposals || []).map(function (p) {
    return '<div class="evidence-item">💡 ' + esc(p.date) + " " + esc(p.text) + "</div>";
  }).join("");
  return fold("audit:" + project.name, "夜間稽核", (props ? "有建議等你決定 · " : "") + (a.notes || []).length + " 則校正筆記",
    '<div class="hint">北極星：' + esc(a.north_star || "") + "</div>" +
    (a.latest_assessment ? '<div class="hint">' + esc(a.latest_assessment) + "</div>" : "") +
    '<div class="hint">摩擦指數（越低越好）：' + (trend || "尚無資料") + "</div>" +
    '<div class="section-label">生效中的校正筆記</div>' + notes +
    (props ? '<div class="section-label">等你決定的建議</div>' + props : ""), !!props);
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
  var rows = (project.role_profiles || []).filter(function (p) { return p.id !== "controller"; }).map(function (p) {
    var chain = [{ provider: p.provider, source: p.source, model: p.model, reasoning_effort: p.reasoning_effort }].concat(p.fallbacks);
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
    holder.innerHTML = chainRowHtml({ provider: last.provider, source: last.source, model: "", reasoning_effort: last.reasoning_effort || "medium" }, withEffort, items.children.length, chainEl.getAttribute("data-project"));
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
  if (e.target.classList && e.target.classList.contains("memory-feature-enabled")) {
    saveMemoryFeature(e.target);
  }
});

function saveMemoryFeature(input) {
  var enabled = input.checked;
  var status = input.closest(".memory-feature").querySelector(".memory-feature-status");
  input.disabled = true;
  status.className = "memory-feature-status rp-status";
  status.textContent = "儲存中…";
  fetch("/api/memory-feature-enabled", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      project: input.getAttribute("data-project"),
      feature: input.getAttribute("data-feature"),
      enabled: enabled
    })
  })
    .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, body: d }; }); })
    .then(function (res) {
      input.disabled = false;
      if (res.ok) {
        status.className = "memory-feature-status rp-status ok";
        status.textContent = enabled ? "已啟用" : "已停用";
      } else {
        input.checked = !enabled;
        status.className = "memory-feature-status rp-status err";
        status.textContent = res.body.error || "儲存失敗";
      }
    })
    .catch(function () {
      input.disabled = false;
      input.checked = !enabled;
      status.className = "memory-feature-status rp-status err";
      status.textContent = "連線失敗，設定未變更";
    });
}

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

document.addEventListener("input", function (e) {
  if (e.target.classList && e.target.classList.contains("peer-name-text")) state.chainDirty = true;
});

document.addEventListener("click", function (e) {
  if (!e.target.classList || !e.target.classList.contains("peer-name-save")) return;
  var btn = e.target;
  var box = btn.closest(".peer-name");
  var statusEl = box.querySelector(".peer-name-status");
  var value = box.querySelector(".peer-name-text").value.trim();
  if (!confirm("把這個 Harness 的名稱改成「" + value + "」？已建立的連線不會斷，但重新配對會多出一筆新連線。")) return;
  statusEl.className = "peer-name-status rp-status";
  statusEl.textContent = "儲存中…";
  btn.disabled = true;
  fetch("/api/peer-name", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ project: box.getAttribute("data-project"), name: value })
  })
    .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, body: d }; }); })
    .then(function (res) {
      btn.disabled = false;
      statusEl.className = "peer-name-status rp-status " + (res.ok ? "ok" : "err");
      if (res.ok) {
        state.chainDirty = false;
        if (state.renderDeferred) setTimeout(render, 1500);
        statusEl.textContent = "已儲存";
      } else {
        statusEl.textContent = res.body.error || "儲存失敗";
      }
    })
    .catch(function () {
      btn.disabled = false;
      statusEl.className = "peer-name-status rp-status err";
      statusEl.textContent = "儲存失敗（連線問題）";
    });
});

document.addEventListener("input", function (e) {
  if (!e.target.classList || !e.target.classList.contains("peer-desc-text")) return;
  state.chainDirty = true;  // same rule as the model chains: no redraw under the cursor
  var box = e.target.closest(".peer-desc");
  box.querySelector(".peer-desc-count").textContent = e.target.value.length + " / 600";
  box.querySelector(".peer-desc-status").textContent = "";
});

document.addEventListener("click", function (e) {
  if (!e.target.classList || !e.target.classList.contains("peer-desc-save")) return;
  var btn = e.target;
  var box = btn.closest(".peer-desc");
  var statusEl = box.querySelector(".peer-desc-status");
  statusEl.className = "peer-desc-status rp-status";
  statusEl.textContent = "儲存中…";
  btn.disabled = true;
  fetch("/api/peer-description", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ project: box.getAttribute("data-project"), description: box.querySelector(".peer-desc-text").value })
  })
    .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, body: d }; }); })
    .then(function (res) {
      btn.disabled = false;
      statusEl.className = "peer-desc-status rp-status " + (res.ok ? "ok" : "err");
      if (res.ok) {
        state.chainDirty = false;
        if (state.renderDeferred) setTimeout(render, 1500);
        statusEl.textContent = "已儲存。其他 Harness 同步時會讀到（最多約五分鐘）";
      } else {
        statusEl.textContent = res.body.error || "儲存失敗";
      }
    })
    .catch(function () {
      btn.disabled = false;
      statusEl.className = "peer-desc-status rp-status err";
      statusEl.textContent = "儲存失敗（連線問題）";
    });
});

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

var WAITING_ACTION_TEXT = { pick: "送出中…", dismiss: "略過中…", approve: "核准中…", reject: "處理中…", choose: "送出中…", clarify: "送出中…", abandon: "放棄中…" };

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
  } else if (action === "pick" || action === "dismiss") {
    url = "/api/chat-question";
    body = { project: project, question_id: btn.getAttribute("data-question"), action: action };
    if (action === "pick") body.choice = parseInt(btn.getAttribute("data-choice"), 10);
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
  } else if (it.kind === "question") {
    var qopts = (it.options || []).map(function (o, i) {
      return '<button class="approval-btn choice" data-project="' + esc(projectName) + '" data-question="' + esc(it.id) +
        '" data-action="pick" data-choice="' + i + '" title="' + esc(o.action) + '">' + esc(o.label) + "</button>";
    }).join("");
    var qdetail = (it.options || []).map(function (o) {
      return '<div class="muted">・' + esc(o.label) + (o.action && o.action !== o.label ? "：" + esc(o.action) : "") + "</div>";
    }).join("");
    actions = qdetail + '<div class="approval-actions">' + qopts +
      '<button class="approval-btn reject" data-project="' + esc(projectName) + '" data-question="' + esc(it.id) +
      '" data-action="dismiss">略過</button><span class="approval-status"></span></div>';
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
  var b = w.backlog && w.backlog.open != null ? w.backlog : null;  // an old snapshot with no counts is no snapshot
  var holders = w.lock_holders || [];
  var line = function (c, why) {
    return '<div class="muted">' + esc(c.id) + " " + esc(c.title) + (why ? "（" + esc(why) + "）" : "") + "</div>";
  };
  var held = holders.map(function (h) {
    return h.alive
      ? '<div class="muted">工作區目前被 ' + esc(h.conversation_id) + " 使用中（正在執行，" + esc(sinceText(h.locked_at)) + "開始）；掃描會等它結束。</div>"
      : '<div class="approval-status err">工作區被 ' + esc(h.conversation_id) + " 佔著（" + esc(sinceText(h.locked_at)) +
        "），但它沒有在執行：掃描會一直略過，不會提出新任務。請在上方「等你處理」放棄這個任務。</div>";
  }).join("");
  if (!b) {
    return '<div class="section-label">Issue 清單</div>' + held +
      '<div class="muted">還沒有掃描結果（掃描每 30 分鐘一次，重啟後第一輪完成前是空的）。</div>';
  }
  var queued = b.queued || [], heldIssues = b.held || [];
  return '<div class="section-label">Issue 清單 <span class="badge">開啟 ' + b.open + "</span> " +
    '<span class="badge">排隊 ' + queued.length + '</span> <span class="badge">擱置 ' + heldIssues.length + "</span></div>" + held +
    '<div class="muted">上次檢查 ' + esc(sinceText(b.checked_at)) + "；一次只提一張，等你核准後才開工</div>" +
    (queued.length ? fold("backlog-queued:" + project.name, "排隊（最舊的先提）", queued.length + " 張", queued.map(function (c) { return line(c); }).join("")) : "") +
    (heldIssues.length ? fold("backlog-held:" + project.name, "擱置", heldIssues.length + " 張", heldIssues.map(function (c) { return line(c, c.why); }).join("")) : "");
}

var STAGE_LABELS = {
  "control-start": "主代理規劃",
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
    return '<div class="step ' + stateClass + '" onclick="openStageDetail(' + t.turn_id + ',' + JSON.stringify(projectName).replace(/"/g, '&quot;') + ')" title="' + esc(t.provider + (t.execution_source_id ? "@" + t.execution_source_id : "") + "/" + (t.model || "?")) + '">' +
      '<div class="step-icon">' + icon + '</div>' +
      '<div class="step-label">' + esc(stageLabel(t.stage)) + '</div>' +
      '<div class="step-meta">' + esc(t.provider + (t.execution_source_id ? "@" + t.execution_source_id : "") + "/" + (t.model || "?")) + " " + retryTag + '</div>' +
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
      html += '<div class="detail-row meta">' + esc(d.provider + (d.execution_source_id ? "@" + d.execution_source_id : "") + "/" + (d.model || "?")) + " — " + esc(d.state) + " — " + esc(d.created_at) + '</div>';
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

// One section, collapsed to a single readable line (title + short status) until opened, so a page
// shows what exists first and the detail on demand. `title` and `summary` are already-escaped HTML.
function fold(key, title, summary, body, defaultOpen) {
  var open = Object.prototype.hasOwnProperty.call(state.openDetails, key) ? state.openDetails[key] : !!defaultOpen;
  return '<details class="fold" data-key="' + esc(key) + '"' + (open ? " open" : "") + '><summary>' +
    '<span class="fold-title">' + title + "</span>" +
    (summary ? '<span class="fold-sum">' + summary + "</span>" : "") +
    '</summary><div class="fold-body">' + body + "</div></details>";
}

// A ready-made `.memory-card` (with its own <h2>) turned into a fold titled by that <h2>.
function cardFold(key, html, summary, defaultOpen) {
  var m = /<h2>(.*?)<\/h2>/.exec(html);
  return fold(key, m ? m[1] : "", summary, m ? html.replace(m[0], "") : html, defaultOpen);
}

function chainBrief(list) {
  if (!list || !list.length) return "未設定";
  var c = list[0];
  return esc(c.provider + "/" + (c.model || "(預設)")) + (list.length > 1 ? " ＋" + (list.length - 1) + " 備援" : "");
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

function renderExecutionSources() {
  var stores = state.data.execution_sources || [];
  var html = '<div class="memory-card"><h2>執行來源</h2><div class="hint">同一供應商可以登錄多個帳號指令。登入與帳號隔離由各 CLI wrapper 管理，這裡不儲存憑證。wrapper 須以 exec 呼叫真正 CLI，完整轉送參數、輸出與結束碼，不可在背景啟動後提早結束。同一帳號／額度池的來源請填相同額度池名稱，會共用併發上限與冷卻狀態。新增後須在各 Harness「設定」授權，再選入模型鏈。「原本指令」沿用舊設定，不納入具名來源的全域併發限制。</div>';
  if (!stores.length) return cardFold("sources-card", html + '<div class="empty">目前沒有支援來源管理的 Harness 連線。</div></div>', "無");
  stores.forEach(function (store) {
    function editor(entry) {
      var key = store.store_id + ':' + (entry.id || 'new');
      var value = Object.assign({ provider: 'codex', max_concurrency: 1 }, entry, state.sourceDrafts[key] || {});
      function field(name, label, type) {
        return '<label>' + label + '<input class="execution-source-input pref-text" data-field="' + name + '" type="' + (type || 'text') + '" value="' + esc(value[name] === undefined ? '' : value[name]) + '"' + (name === 'id' && entry.id ? ' disabled' : '') + '></label>';
      }
      return '<details class="execution-source-editor" data-key="' + esc(key) + '"' + detailsOpen(key) + '><summary>' + esc(entry.label || '＋ 新增執行來源') + (entry.id ? ' · ' + esc(entry.provider + '@' + entry.id) + (entry.enabled ? ' · 啟用' : ' · 停用') + ' · 額度池占用 ' + entry.active + (entry.cooldown ? ' · 冷卻：' + esc(entry.cooldown.category) : '') : '') + '</summary>' +
        '<div class="execution-source-form" data-key="' + esc(key) + '" data-project="' + esc(store.harness) + '" data-id="' + esc(entry.id || '') + '" data-revision="' + (entry.revision || 0) + '" data-enabled="' + !!entry.enabled + '">' +
        field('id', '來源 ID') + field('label', '顯示名稱') + '<label>供應商<select class="execution-source-input" data-field="provider"' + (entry.id ? ' disabled' : '') + '>' + providerOptions(value.provider) + '</select></label>' +
        field('command', '執行檔／wrapper 絕對路徑') + field('quota_bucket', '額度池 ID（空白使用來源 ID）') + field('max_concurrency', '同額度池最大併發', 'number') +
        '<div class="hint">同額度池採已啟用來源中最小的併發上限。冷卻是 Harness 的暫避時間，不代表供應商剩餘額度或登入有效。</div><div class="pref-actions"><button class="execution-source-button" data-action="save">儲存</button>' +
        (entry.id ? '<button class="execution-source-button" data-action="toggle">' + (entry.enabled ? '停用' : '啟用') + '</button><button class="execution-source-button" data-action="reset">清除冷卻，下次重試</button>' : '') +
        '<span class="execution-source-status"></span></div></div></details>';
    }
    html += (store.entries || []).map(editor).join('') + editor({});
  });
  return cardFold("sources-card", html + '</div>', stores.reduce(function (n, st) { return n + (st.entries || []).length; }, 0) + " 個來源");
}

function renderExecutionSourceAccess(project) {
  var store = sourceStoreFor(project);
  if (!store || !(store.entries || []).length) return '';
  var allowed = state.sourceAccessDrafts[project.name] || project.execution_sources_allowed || [];
  return '<div class="memory-card source-access" data-project="' + esc(project.name) + '"><h2>允許使用的執行來源</h2>' +
    '<div class="hint">授權後才會出現在模型的來源選單。取消授權會阻止後續呼叫；正在執行的 CLI 會保留到結束。</div>' +
    store.entries.map(function (entry) { return '<label class="source-access-item"><input type="checkbox" class="source-allowed" value="' + esc(entry.id) + '"' + (allowed.indexOf(entry.id) !== -1 ? ' checked' : '') + '> ' + esc(entry.label + ' · ' + entry.provider + '@' + entry.id) + '</label>'; }).join('') +
    '<button class="source-access-save">儲存來源授權</button><span class="execution-source-status"></span></div>';
}

function renderSourceAccessFold(project) {
  var html = renderExecutionSourceAccess(project);
  if (!html) return "";
  var allowed = state.sourceAccessDrafts[project.name] || project.execution_sources_allowed || [];
  return cardFold("source-access:" + project.name, html, allowed.length + " 個已授權");
}

function renderSharedMemory() {
  var stores = state.data.shared_memory || [];
  var html = '<div class="memory-card"><h2>🌐 共享記憶</h2>' +
    '<div class="hint">由你明確設定，所有使用同一份共享記憶的 Harness 都會讀取。各專案的對話與偏好維持獨立；儲存後於下一次對話或任務載入。停用的記憶不再提供給模型。</div>';
  if (!stores.length) return cardFold("shared-card", html + '<div class="empty">目前沒有可管理共享記憶的 Harness 連線。</div></div>', "無");
  stores.forEach(function (store) {
    function editor(entry) {
      var key = store.store_id + ":" + (entry.id || "new");
      var draft = state.sharedDrafts[key] || {};
      var title = draft.title !== undefined ? draft.title : entry.title || "";
      var body = draft.body !== undefined ? draft.body : entry.body || "";
      return '<div class="shared-memory pref" data-project="' + esc(store.harness) + '" data-key="' + esc(key) +
        '" data-memory-id="' + esc(entry.id || "") + '" data-revision="' + (entry.revision || 0) + '" data-enabled="' + !!entry.enabled + '">' +
        '<div class="meta">' + (entry.id ? (entry.enabled ? "已啟用 · 所有 Harness 共用" : "已停用") : "新增共享記憶") + '</div>' +
        (entry.policy === "worktree-pr-ci-main" ? '<div class="hint">自動交付：worktree → PR → CI／衝突檢查 → main → 關單與清理。停用會暫停後續遠端操作；文字編輯不會改寫這套固定流程。</div>' : '') +
        '<input class="shared-memory-input pref-text" data-field="title" maxlength="160" placeholder="記憶標題" value="' + esc(title) + '">' +
        '<textarea class="shared-memory-input pref-text" data-field="body" maxlength="6000" rows="6" placeholder="所有 Harness 應記住的內容">' + esc(body) + '</textarea>' +
        '<div class="pref-actions"><button class="shared-memory-btn" data-action="' + (entry.id ? "update" : "create") + '">儲存</button>' +
        (entry.id ? '<button class="shared-memory-btn" data-action="toggle">' + (entry.enabled ? "停用" : "啟用") + '</button><button class="shared-memory-btn" data-action="remove">移除</button>' : "") +
        '<span class="shared-memory-status"></span></div></div>';
    }
    html += (store.entries || []).map(editor).join("") + editor({});
  });
  return cardFold("shared-card", html + '</div>', stores.reduce(function (n, st) { return n + (st.entries || []).length; }, 0) + " 則");
}

function renderMemory() {
  var prefs = state.data.preferences || { pending: [], adopted: [], retired: [] };
  var pending = prefs.pending || [];
  var adopted = (prefs.adopted || []).slice().sort(function (a, b) { return b.id - a.id; });
  var retired = prefs.retired || [];
  var html = '<div class="memory-card"><h2>🧠 記憶整理 · 偏好</h2>' +
    '<div class="hint">Harness 會自己從你的對話歸納偏好，直接採用並套用到之後的聊天，不需要先經過你；' +
    "不同意可以直接在聊天裡說（例如「那個不用了」），或在這裡撤銷。每條都附你當時說的原話。</div>" +
    (pending.length
      ? fold("prefs-pending", "等你確認", '<span class="badge">' + pending.length + "</span>",
        '<div class="hint">超過每日自動採用上限，或舊版留下來的候選；採用前可以先修改。</div>' +
        pending.map(renderPendingPreference).join(""), true)
      : "") +
    fold("prefs-adopted", "已採用", adopted.length + " 條",
      adopted.length ? adopted.map(renderAdoptedPreference).join("") : '<div class="empty">尚未採用任何偏好</div>') +
    (retired.length
      ? fold("prefs-retired", "最近撤回", retired.length + " 條", retired.map(renderRetiredPreference).join(""))
      : "") +
    "</div>";
  return cardFold("prefs-card", html, pending.length ? pending.length + " 條等你確認" : adopted.length + " 條已採用", pending.length > 0);
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
  var wrapDigests = function (body) {
    return fold("digests-all:" + project.name, "每日摘要", "最近 " + list.length + " 天 · 寫入 " + esc(project.memory_workspace_id || ""), body);
  };
  if (!list.length) return wrapDigests('<div class="empty">尚無摘要（每天 02:00 整理前一天）</div>');
  return wrapDigests(list.map(function (d) {
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
  }).join(""));
}

// The settings tab: each chain shows as chips; "編輯" opens its editor (open state is remembered
// across the live re-renders).
var TRUST_LABELS = { ask: "只能提問（唯讀）", collaborate: "可回報問題、請對方修復" };

function trustSelect(cls, value, name) {
  return '<select class="' + cls + '" data-name="' + esc(name || "") + '">' +
    Object.keys(TRUST_LABELS).map(function (k) {
      return '<option value="' + k + '"' + (k === value ? " selected" : "") + ">" + TRUST_LABELS[k] + "</option>";
    }).join("") + "</select>";
}

function renderPeerName(project) {
  return fold("peer-name:" + project.name, "Harness 名稱", esc(project.peer_name || project.name),
    '<div class="hint">其他 Harness 用這個名字稱呼這個 Harness（連線、稽核標示都用它），和專案或資料夾的名稱無關。小寫英數字、- 或 _，最多 32 字。'
    + "改名只改標示，已建立的連線不會斷；但改名後重新配對會多出一筆新連線，舊的要自己撤銷。</div>" +
    '<div class="peer-name" data-project="' + esc(project.name) + '">' +
    '<input class="peer-name-text" size="24" maxlength="32" value="' + esc(project.peer_name || "") + '"> ' +
    '<button class="peer-name-save">儲存</button> <span class="peer-name-status rp-status"></span></div>');
}

function renderPeerLinks(project) {
  var links = project.peer_links || { endpoint: null, inbound: [], outbound: [] };
  var others = state.data.projects.filter(function (p) { return p.name !== project.name; });
  var out = '<div class="peer-links" data-project="' + esc(project.name) + '">';
  out += '<div class="hint">決定哪些 Harness 可以互相提問、或回報問題請對方處理。連線後立刻生效，不用改設定檔、不用重啟。'
    + "這個頁面沒有登入，請只在本機使用。</div>";
  out += '<div class="hint">本 Harness 的名稱：<b>' + esc(project.peer_name || project.name) + "</b></div>";
  out += links.endpoint
    ? '<div class="hint">本 Harness 的 A2A 位址：<code>' + esc(links.endpoint) + "</code></div>"
    : '<div class="hint">A2A 服務還沒有回報位址：memtrace-a2a 沒有在跑，或工作區 .harness.env 沒有 HARNESS_PEER_API_TOKEN。連線要兩邊的 A2A 服務都在跑才做得成。</div>';

  out += '<div class="hint"><b>可以來問我的 Harness</b></div>';
  out += links.inbound.length ? links.inbound.map(function (l) {
    return '<div class="pl-row">' + esc(l.name) + " " + trustSelect("pl-trust", l.trust, l.name) +
      ' <button class="pl-revoke" data-name="' + esc(l.name) + '">撤銷</button></div>';
  }).join("") : '<div class="hint">（沒有）</div>';

  out += '<div class="hint"><b>我可以問的 Harness</b></div>';
  out += links.outbound.length ? links.outbound.map(function (l) {
    return '<div class="pl-row">' + esc(l.name) + ' <code>' + esc(l.url) + "</code> " +
      '<label><input type="checkbox" class="pl-flag" data-name="' + esc(l.name) + '"' + (l.collaborate ? " checked" : "") +
      "> 我的模型可以向它回報問題</label>" +
      ' <button class="pl-remove" data-name="' + esc(l.name) + '">移除</button></div>';
  }).join("") : '<div class="hint">（沒有）</div>';

  out += '<div class="hint"><b>連到這個頁面上的另一個 Harness</b></div>';
  if (others.length) {
    out += '<div class="pl-row">我可以問 <select class="pl-target">' +
      others.map(function (p) {
        return '<option value="' + esc(p.name) + '">' + esc(p.peer_name || p.name) + "</option>";
      }).join("") +
      "</select> 對方給我的權限：" + trustSelect("pl-newtrust", "ask") +
      ' <label><input type="checkbox" class="pl-both"> 反過來它也可以問我（只能提問）</label>' +
      ' <button class="pl-pair">連線</button></div>';
  } else {
    out += '<div class="hint">（這個頁面上沒有其他 Harness。跨機器請用下面的邀請碼。）</div>';
  }

  out += '<div class="hint"><b>跨機器：邀請碼</b>　在被問的那一邊產生，貼到發問的那一邊。邀請碼含一把密鑰，只顯示這一次。</div>';
  out += '<div class="pl-row">讓名為 <input class="pl-input pl-invite-name" size="14" placeholder="對方的名稱"> 來問我：' +
    trustSelect("pl-invite-trust", "ask") + ' <button class="pl-invite">產生邀請碼</button></div>';
  if (state.invite && state.invite.project === project.name) {
    out += '<div class="pl-row"><textarea class="pl-invite-text" rows="3" readonly>' + esc(state.invite.text) + "</textarea>" +
      ' <button class="pl-invite-done">我已複製，清除</button></div>';
  }
  out += '<div class="pl-row">貼上別人給我的邀請碼：<textarea class="pl-input pl-accept-text" rows="2" placeholder="mtinv1:…"></textarea> ' +
    '<button class="pl-accept">接受邀請</button></div>';
  out += '<div class="pl-status rp-status"></div></div>';
  return fold("peer-links:" + project.name, "Harness 連線",
    "可問我 " + links.inbound.length + " · 我可問 " + links.outbound.length, out);
}

function peerLinkPost(box, url, body, okText) {
  var statusEl = box.querySelector(".pl-status");
  statusEl.className = "pl-status rp-status";
  statusEl.textContent = "處理中…";
  body.project = body.project || box.getAttribute("data-project");
  return fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })
    .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, body: d }; }); })
    .then(function (res) {
      statusEl.className = "pl-status rp-status " + (res.ok ? "ok" : "err");
      statusEl.textContent = res.ok ? okText : (res.body.error || "失敗");
      if (res.ok) {
        state.chainDirty = false;
        if (state.renderDeferred) setTimeout(render, 800);
      }
      return res;
    })
    .catch(function () {
      statusEl.className = "pl-status rp-status err";
      statusEl.textContent = "連線問題";
      return { ok: false, body: {} };
    });
}

var COLLAB_WARNING = "「可回報問題、請對方修復」是最大的授權：對方的 Harness 可以請這個 Harness 診斷問題、並在你同意後開始修改程式。確定嗎？";

document.addEventListener("input", function (e) {
  if (e.target.classList && e.target.classList.contains("pl-input")) state.chainDirty = true;
});

document.addEventListener("change", function (e) {
  var t = e.target;
  if (!t.classList) return;
  var box = t.closest(".peer-links");
  if (!box) return;
  if (t.classList.contains("pl-trust")) {
    if (t.value === "collaborate" && !confirm(COLLAB_WARNING)) { render(); return; }
    peerLinkPost(box, "/api/peer-link-trust", { name: t.getAttribute("data-name"), trust: t.value }, "已更新");
  } else if (t.classList.contains("pl-flag")) {
    peerLinkPost(box, "/api/peer-link-outbound-flag", { name: t.getAttribute("data-name"), collaborate: t.checked }, "已更新");
  }
});

document.addEventListener("click", function (e) {
  var t = e.target;
  if (!t.classList) return;
  var box = t.closest && t.closest(".peer-links");
  if (!box) return;
  var project = box.getAttribute("data-project");
  if (t.classList.contains("pl-revoke")) {
    var who = t.getAttribute("data-name");
    if (confirm("撤銷後「" + who + "」就不能再來問這個 Harness（它手上的密鑰立刻失效）。確定嗎？"))
      peerLinkPost(box, "/api/peer-link-revoke", { name: who }, "已撤銷");
  } else if (t.classList.contains("pl-remove")) {
    var target = t.getAttribute("data-name");
    if (confirm("移除後這個 Harness 就不能再問「" + target + "」。對方那邊的授權不會跟著撤銷。確定嗎？"))
      peerLinkPost(box, "/api/peer-link-outbound-remove", { name: target }, "已移除");
  } else if (t.classList.contains("pl-pair")) {
    var row = t.closest(".pl-row");
    var trust = row.querySelector(".pl-newtrust").value;
    if (trust === "collaborate" && !confirm(COLLAB_WARNING)) return;
    peerLinkPost(box, "/api/peer-pair", {
      from: project, to: row.querySelector(".pl-target").value, trust: trust,
      both: row.querySelector(".pl-both").checked, reverse_trust: "ask"
    }, "已連線");
  } else if (t.classList.contains("pl-invite")) {
    var inviteRow = t.closest(".pl-row");
    var trustValue = inviteRow.querySelector(".pl-invite-trust").value;
    if (trustValue === "collaborate" && !confirm(COLLAB_WARNING)) return;
    peerLinkPost(box, "/api/peer-invite", {
      name: inviteRow.querySelector(".pl-invite-name").value, trust: trustValue
    }, "邀請碼已產生，請複製後貼到對方的頁面").then(function (res) {
      if (res.ok) { state.invite = { project: project, text: res.body.invite }; render(); }
    });
  } else if (t.classList.contains("pl-invite-done")) {
    state.invite = null;
    render();
  } else if (t.classList.contains("pl-accept")) {
    var text = box.querySelector(".pl-accept-text").value;
    peerLinkPost(box, "/api/peer-accept", { invite: text }, "已接受，現在可以問對方了");
  }
});

function renderPeerDescription(project) {
  var off = project.peer_enabled ? "" :
    '<div class="hint">這個 Harness 還沒有啟用 Harness 之間的通訊（工作區 .harness.env 沒有 HARNESS_PEER_API_TOKEN），先寫好也可以，啟用後才會被其他 Harness 看到。</div>';
  return fold("peer-desc:" + project.name, "給其他 Harness 的自我介紹",
    project.peer_description ? "已填 " + project.peer_description.length + " 字" : "未填寫",
    '<div class="hint">其他 Harness 的 list_peers 和這個 Harness 的 Agent Card 會顯示這段話，它們的模型靠它判斷什麼時候該找你。'
    + '寫：這是什麼、什麼情況該找它、要附哪些資訊。會被所有已配對的 Harness 看到，不要放金鑰、路徑或私人資料。最多 600 字。</div>' + off +
    '<div class="peer-desc" data-project="' + esc(project.name) + '">' +
    '<textarea class="peer-desc-text" rows="5" maxlength="600" placeholder="例如：Harness 平台本身。平台的錯誤、卡住的任務、行為不對，請用 report_issue 附上錯誤文字、任務編號與你已排除的原因。">'
    + esc(project.peer_description || "") + "</textarea>" +
    '<div class="pref-actions"><button class="peer-desc-save">儲存</button> <span class="peer-desc-count"></span> <span class="peer-desc-status rp-status"></span></div></div>');
}

function renderModelSettings(project) {
  return '<div class="hint">每一處都可設定主模型與任意數量的備援（依序嘗試）；按「編輯」展開。清單包含各 CLI 目前提供的模型；'
    + '沒列出的選「— 自訂 —」輸入名稱，之後所有選單都會記住。 <button class="catalog-refresh" onclick="refreshModelCatalog(this)">重新抓取模型清單</button></div>' +
    fold("model:chat:" + project.name, "主代理（對話、規劃、執行追蹤與驗收）", chainBrief(project.chat_candidates), renderChatCandidates(project)) +
    fold("model:digest:" + project.name, "每日摘要模型", chainBrief(project.digest_candidates), renderDigestCandidates(project)) +
    renderMemoryFeature(project, "archive", "記憶整理模型",
      "夜間依主題整理對話、每週檢視記憶；也供手動重建使用。停用會暫停自動夜間整理。",
      project.archive_enabled !== false, renderArchiveCandidates(project)) +
    renderMemoryFeature(project, "audit", "夜間稽核模型",
      "檢查重複溝通與操作摩擦，評估是否需要調整 Harness 的校正筆記。停用會暫停自動夜間稽核。",
      project.audit_enabled !== false, renderAuditCandidates(project)) +
    renderMemoryFeature(project, "recall", "背景回想模型",
      "收到新訊息時搜尋相關舊對話與專案記憶，提供短效主題摘要。停用後聊天不會啟動背景回想。",
      project.recall_enabled !== false, renderRecallCandidates(project)) +
    fold("model:roles:" + project.name, "執行能力模型", (project.role_profiles || []).filter(function (p) { return p.id !== "controller"; }).length + " 個角色", renderRoleProfiles(project));
}

function renderImprovement(project) {
  var o = project.improvement;
  if (!o) return '';
  var html = '<div class="memory-card improvement-settings" data-project="' + esc(project.name) + '"><h2>主動觀察與平台改善</h2>' +
    '<div class="hint">每 ' + Math.round(o.interval_seconds / 60) + ' 分鐘收集已授權紀錄，有新內容才分析。平台修正走既有開發與交付，同時僅一件、每 24 小時最多三件；其他專案的業務排程、偏好只提出建議。無新問題不發通知。</div>';
  [['share_enabled', '分享本 Harness 的對話、任務與排程紀錄供觀察'], ['enabled', '擔任改善協調者：觀察其他已授權 Harness'], ['auto_develop', '平台空閒時，自動開始有證據支持的修正']].forEach(function (row) {
    html += '<label class="source-access-item"><input type="checkbox" class="improvement-toggle" data-setting="' + row[0] + '"' + (o[row[0]] ? ' checked' : '') + '> ' + row[1] + '</label>';
  });
  html += '<div class="hint">觀察來源：' + esc((o.sources || []).join('、') || '尚未設定；在 models.toml 的 [improvement].sources 指定 Harness 名稱') + '。待分析紀錄：' + o.pending_evidence + '</div>';
  var coverage = { ok: '正常收集', sharing_disabled: '未開啟分享', unreachable: '目前無法連線', upgrade_required: '等待載入新版' };
  var findingStates = { new: '已記錄', suggested: '保留建議', queued: '等待開始', running: '確認與修正中', delivering: '交付中', needs_human: '等待你決定', succeeded: '已完成交付', failed: '修正未完成', failed_to_start: '尚未開始', paused: '已暫停', cancelled: '已取消', interrupted: '已中斷，保留現場' };
  (o.sources_state || []).forEach(function (source) { html += '<div class="meta">' + esc(source.source + ' · ' + (coverage[source.status] || source.status)) + '</div>'; });
  if (o.enabled && o.auto_develop && (o.auto_requirements || []).length) html += '<div class="hint">自動修正尚待：' + esc(o.auto_requirements.join('、')) + '。觀察仍會持續。</div>';
  if (o.last_error) html += '<div class="error">最近一次分析未完成，已保留紀錄等待重試。</div>';
  (o.findings || []).forEach(function (f) {
    html += '<details data-key="observation:' + esc(f.id) + '"' + detailsOpen('observation:' + f.id) + '><summary>' + esc(f.title + ' · ' + (f.state === 'historical' ? '歷史紀錄，不啟動修正' : findingStates[f.state] || f.state)) + '</summary><p>' + esc(f.summary || '') + '</p>';
    (f.evidence || []).forEach(function (ref) { html += '<div class="hint">' + esc(ref.source + ' · ' + (ref.at || '時間未提供') + ' · ' + ref.id) + '</div><blockquote>' + esc(ref.quote) + '</blockquote>'; });
    html += (f.conversation_id ? '<div class="meta">任務：' + esc(f.conversation_id) + '</div>' : '') + '</details>';
  });
  return cardFold("improvement:" + project.name, html + '<span class="improvement-status"></span></div>',
    [o.share_enabled, o.enabled, o.auto_develop].filter(Boolean).length + " / 3 項開啟");
}

function renderProject(project, q) {
  var d = project.dedicated;
  var running = (project.locks || []).length;
  var runningBadge = running
    ? '<span class="badge running">執行中 ' + running + "/" + project.max_workers + "</span>"
    : "";
  var content = "";
  if (state.tab === "memory") {
    content = renderAudit(project) + renderTopicBriefs(project) + renderDigests(project);
  } else if (state.tab === "schedules") {
    content = renderSchedules(project) || '<div class="empty">（沒有排程）</div>';
  } else if (state.tab === "settings") {
    content = '<div class="chips">' +
      '<span class="chip' + (d.chat_model ? " on" : "") + '">chat: ' + (d.chat_model ? "已設定" : "預設") + "</span>" +
      '<span class="chip' + (d.memory ? " on" : "") + '">memory: ' + (d.memory ? "專屬" : "同專案") + "</span>" +
      '<span class="chip' + (d.role_profiles ? " on" : "") + '">role-profiles: ' + (d.role_profiles ? "自己的檔案" : "預設") + "</span>" +
      "</div>" + renderImprovement(project) + renderPeerName(project) + renderPeerLinks(project) + renderPeerDescription(project) + renderSourceAccessFold(project) + renderModelSettings(project);
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
    renderExecutionSources() + renderSharedMemory() + renderMemory() + "</div></div>";
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
  if (e.target.classList.contains("shared-memory-input")) {
    var shared = e.target.closest(".shared-memory");
    var key = shared.getAttribute("data-key");
    var draft = state.sharedDrafts[key] || {};
    draft[e.target.getAttribute("data-field")] = e.target.value;
    state.sharedDrafts[key] = draft;
    return;
  }
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
  if (!e.target.classList.contains("shared-memory-btn")) return;
  var btn = e.target, box = btn.closest(".shared-memory");
  var status = box.querySelector(".shared-memory-status");
  var action = btn.getAttribute("data-action");
  var body = { project: box.getAttribute("data-project"), action: action,
    id: box.getAttribute("data-memory-id"), revision: Number(box.getAttribute("data-revision")) };
  if (action === "create" || action === "update") {
    body.title = box.querySelector('[data-field="title"]').value;
    body.body = box.querySelector('[data-field="body"]').value;
  }
  if (action === "toggle") body.enabled = box.getAttribute("data-enabled") !== "true";
  btn.disabled = true; status.textContent = "儲存中…";
  fetch("/api/shared-memory", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })
    .then(function (r) { return r.json().then(function (data) { return { ok: r.ok, data: data }; }); })
    .then(function (res) {
      if (res.ok) {
        delete state.sharedDrafts[box.getAttribute("data-key")];
        status.textContent = "已儲存，所有 Harness 共同使用";
      } else { status.textContent = res.data.error || "儲存失敗"; }
      btn.disabled = false;
    })
    .catch(function () { btn.disabled = false; status.textContent = "連線失敗"; });
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

function rememberSourceDraft(target) {
  var box = target.closest('.execution-source-form');
  if (!box) return;
  var key = box.getAttribute('data-key');
  var draft = state.sourceDrafts[key] || {};
  draft[target.getAttribute('data-field')] = target.value;
  state.sourceDrafts[key] = draft;
}
document.addEventListener('input', function (e) {
  if (e.target.classList.contains('execution-source-input')) rememberSourceDraft(e.target);
});
document.addEventListener('change', function (e) {
  if (e.target.classList.contains('improvement-toggle')) {
    var improvementBox = e.target.closest('.improvement-settings');
    var improvementBody = { project: improvementBox.getAttribute('data-project') };
    improvementBody[e.target.getAttribute('data-setting')] = e.target.checked;
    fetch('/api/improvement', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(improvementBody) })
      .then(function (response) { return response.json().then(function (data) { improvementBox.querySelector('.improvement-status').textContent = response.ok ? '已儲存' : data.error || '儲存失敗'; }); })
      .catch(function () { improvementBox.querySelector('.improvement-status').textContent = '連線失敗，請重新確認設定'; });
  }
  if (e.target.classList.contains('execution-source-input')) rememberSourceDraft(e.target);
  if (e.target.classList.contains('source-allowed')) {
    var box = e.target.closest('.source-access');
    state.sourceAccessDrafts[box.getAttribute('data-project')] = Array.prototype.map.call(box.querySelectorAll('.source-allowed:checked'), function (input) { return input.value; });
  }
});
document.addEventListener('click', function (e) {
  var btn = e.target;
  if (!btn.classList.contains('execution-source-button') && !btn.classList.contains('source-access-save')) return;
  var access = btn.classList.contains('source-access-save');
  var box = btn.closest(access ? '.source-access' : '.execution-source-form');
  var status = box.querySelector('.execution-source-status');
  var body = { project: box.getAttribute('data-project') };
  if (access) body.allowed = Array.prototype.map.call(box.querySelectorAll('.source-allowed:checked'), function (input) { return input.value; });
  else {
    body.action = btn.getAttribute('data-action'); body.id = box.getAttribute('data-id'); body.revision = Number(box.getAttribute('data-revision'));
    if (body.action === 'save') box.querySelectorAll('[data-field]').forEach(function (input) { body[input.getAttribute('data-field')] = input.getAttribute('data-field') === 'max_concurrency' ? Number(input.value) : input.value; });
    if (body.action === 'toggle') body.enabled = box.getAttribute('data-enabled') !== 'true';
  }
  btn.disabled = true; status.textContent = '儲存中…';
  fetch(access ? '/api/execution-source-allowlist' : '/api/execution-source', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
    .then(function (response) { return response.json().then(function (data) { return { ok: response.ok, data: data }; }); })
    .then(function (response) {
      btn.disabled = false; status.textContent = response.ok ? '已儲存，下次呼叫生效' : response.data.error || '儲存失敗';
      if (response.ok && !access) delete state.sourceDrafts[box.getAttribute('data-key')];
      if (response.ok && access) delete state.sourceAccessDrafts[box.getAttribute('data-project')];
    }).catch(function () { btn.disabled = false; status.textContent = '連線失敗'; });
});
