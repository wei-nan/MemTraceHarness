// The chat box: one floating panel, built once outside the re-rendered page. Talks to this service's
// /api/chat* routes, which forward to the Harness of the project currently shown.
(function () {
  var chat = { open: false, project: null, since: 0, messages: {}, typing: false, token: 0 };

  var fab = document.createElement("button");
  fab.id = "chat-fab";
  fab.textContent = "💬 對話";
  var panel = document.createElement("div");
  panel.id = "chat-panel";
  panel.innerHTML =
    '<div class="chat-head"><span id="chat-title">對話</span><span class="x" id="chat-close">✕</span></div>' +
    '<div id="chat-log"></div>' +
    '<form id="chat-form"><textarea id="chat-input" rows="2" placeholder="對 Harness 說話…（Enter 送出，Shift+Enter 換行）"></textarea>' +
    '<button type="submit">送出</button></form>';
  document.body.appendChild(fab);
  document.body.appendChild(panel);
  var log = document.getElementById("chat-log");
  var input = document.getElementById("chat-input");

  function currentProject() {
    var projects = (state.data && state.data.projects) || [];
    var names = projects.map(function (p) { return p.name; });
    if (state.project && names.indexOf(state.project) !== -1) return state.project;
    return names[0] || null;
  }

  function note(text) {
    var el = document.createElement("div");
    el.className = "chat-note";
    el.textContent = text;
    log.appendChild(el);
  }

  function draw() {
    var stick = log.scrollTop + log.clientHeight >= log.scrollHeight - 40;
    log.textContent = "";
    var ids = Object.keys(chat.messages).map(Number).sort(function (a, b) { return a - b; });
    if (!ids.length) note("還沒有對話。說點什麼吧。");
    ids.forEach(function (id) {
      var m = chat.messages[id];
      var el = document.createElement("div");
      el.className = "chat-msg " + m.role;
      var text = document.createElement("div");
      text.textContent = m.text;
      el.appendChild(text);
      if (m.actions && m.actions.length) {
        m.actions.forEach(function (row) {
          var wrap = document.createElement("div");
          wrap.className = "btns";
          row.forEach(function (b) {
            var btn = document.createElement("button");
            btn.type = "button";
            btn.textContent = b.label;
            btn.onclick = function () { act(b.id, m.ref, btn); };
            wrap.appendChild(btn);
          });
          el.appendChild(wrap);
        });
      }
      log.appendChild(el);
    });
    if (chat.typing) note("Harness 思考中…");
    if (stick) log.scrollTop = log.scrollHeight;
  }

  function post(path, body) {
    return fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })
      .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, body: j }; }); });
  }

  function act(id, ref, btn) {
    btn.disabled = true;
    post("/api/chat-action", { project: chat.project, id: id, ref: ref }).then(function (res) {
      showToast(res.ok ? (res.body.toast || "已送出") : (res.body.error || "失敗"), !res.ok);
      if (!res.ok) btn.disabled = false;
    }).catch(function () { btn.disabled = false; showToast("連不上服務", true); });
  }

  function send() {
    var text = input.value.trim();
    if (!text || !chat.project) return;
    input.value = "";
    post("/api/chat-send", { project: chat.project, text: text }).then(function (res) {
      if (!res.ok) { showToast(res.body.error || "送出失敗", true); input.value = text; }
    }).catch(function () { showToast("連不上服務", true); input.value = text; });
  }

  // Long-poll for what the Harness said; a project switch starts a new conversation view.
  function loop(token) {
    if (token !== chat.token) return;
    fetch("/api/chat?project=" + encodeURIComponent(chat.project) + "&since=" + chat.since + "&wait=20")
      .then(function (r) { if (!r.ok) throw new Error("http " + r.status); return r.json(); })
      .then(function (data) {
        if (token !== chat.token) return;
        data.messages.forEach(function (m) { chat.messages[m.id] = m; });
        chat.since = data.version;
        chat.typing = data.typing;
        draw();
        loop(token);
      })
      .catch(function () { setTimeout(function () { loop(token); }, 3000); });
  }

  function attach() {
    var project = currentProject();
    if (!project || project === chat.project) return;
    chat.project = project;
    chat.since = 0;
    chat.messages = {};
    chat.typing = false;
    chat.token += 1;
    document.getElementById("chat-title").textContent = "對話 · " + project;
    draw();
    loop(chat.token);
  }

  function setOpen(open) {
    chat.open = open;
    panel.classList.toggle("open", open);
    fab.style.display = open ? "none" : "";
    if (open) { attach(); input.focus(); log.scrollTop = log.scrollHeight; }
  }

  fab.onclick = function () { setOpen(true); };
  document.getElementById("chat-close").onclick = function () { setOpen(false); };
  document.getElementById("chat-form").onsubmit = function (e) { e.preventDefault(); send(); };
  input.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); }
  });
  // Follow the project the page is showing while the panel is open.
  setInterval(function () { if (chat.open) attach(); }, 1000);
})();
