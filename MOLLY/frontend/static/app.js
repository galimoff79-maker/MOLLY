/* МОЛЛИ — запуск приложения, каркас, навигация, опрос состояния. */
(function () {
  "use strict";
  var M = window.Molly, S = M.S, h = M.h;

  var VIEW_TITLES = { chat: "Чат", mail: "Почта", settings: "Настройки" };

  /* ---------- состояние сервисов ---------- */

  M.checkOllama = async function (force) {
    if (!S.cfg || !S.cfg.is_local) return;
    try {
      S.ollama = await M.api("/api/ollama/status");
    } catch (e) {
      S.ollama = { ok: false, error: e.message, models: [] };
    }
    M.renderTopbar();
    M.refreshBanners();
  };

  M.refreshIndexStatus = async function () {
    var hadProject = !!(S.project && S.project.path);
    try {
      var r = await M.api("/api/index/status");
      S.index = r;
      S.project = { path: r.project };
    } catch (e) {
      if (e.status === 400 || e.status === 404) { S.index = null; S.project = null; }
      else return;
    }
    if (M.hooks.onIndex) M.hooks.onIndex();
    M.renderTopbar();
    if (hadProject !== !!(S.project && S.project.path)) {
      M.refreshBanners();
      if (M.updateComposer) M.updateComposer();
    }
  };

  /* ---------- каркас ---------- */

  M.closeSidebar = function () { var a = M.$(".app"); if (a) a.classList.remove("side-open"); };

  function buildShell() {
    var root = document.getElementById("root");
    M.clear(root);
    var local = S.cfg.is_local;

    var navButtons = [["chat", "Чат"]];
    if (local) navButtons.push(["mail", "Почта"], ["settings", "Настройки"]);

    var nav = h("div", { class: "nav", id: "nav" }, navButtons.map(function (b) {
      return h("button", { dataset: { view: b[0] }, text: b[1], onclick: function () { M.showView(b[0]); M.closeSidebar(); } });
    }));

    var themeBtn = h("button", { class: "btn ghost sm", title: "Сменить тему", "aria-label": "Сменить тему", text: "◐", onclick: function () {
      var cur = document.documentElement.dataset.theme || "system";
      var next = cur === "system" ? "light" : cur === "light" ? "dark" : "system";
      M.applyTheme(next);
      M.toast("Тема: " + { system: "как в Windows", light: "светлая", dark: "тёмная" }[next]);
      if (local && S.settings) {
        M.api("/api/settings", { method: "PUT", body: { data: { theme: next } } }).then(function (s) { S.settings = s; }).catch(function () {});
      }
    } });

    var foot = h("div", { class: "side-foot" },
      h("span", { class: "who", id: "who", text: S.cfg.user || (local ? "Этот компьютер" : "") }),
      themeBtn,
      local ? null : h("button", { class: "btn ghost sm", text: "Выйти", onclick: async function () {
        try { await M.api("/api/auth/logout", { method: "POST" }); } catch (e) { /* уже вышли */ }
        location.reload();
      } }));

    var sidebar = h("aside", { class: "sidebar" },
      h("div", { class: "brand" }, h("div", { class: "logo", text: "М" }),
        h("div", null, h("div", { class: "brand-title", text: "МОЛЛИ" }), h("div", { class: "brand-sub", text: "AI-помощник ПТО" }))),
      h("button", { class: "btn new", text: "＋  Новый чат", onclick: function () { M.showView("chat"); M.newChat(); M.closeSidebar(); } }),
      nav,
      h("div", { class: "chat-list", id: "chat-list" }),
      foot);

    var main = h("main", { class: "main" },
      h("div", { class: "topbar", id: "topbar" }),
      h("div", { class: "view", id: "view" }));

    var scrim = h("div", { style: { position: "fixed", inset: "0", zIndex: "30", background: "rgba(0,0,0,.35)", display: "none" }, id: "scrim", onclick: M.closeSidebar });

    root.appendChild(h("div", { class: "app", id: "app" }, sidebar, main, scrim));

    // Затемнение под выдвижной панелью (узкие окна).
    var app = document.getElementById("app");
    new MutationObserver(function () {
      scrim.style.display = app.classList.contains("side-open") && window.innerWidth <= 860 ? "block" : "none";
    }).observe(app, { attributes: true, attributeFilter: ["class"] });
  }

  M.renderTopbar = function () {
    var bar = document.getElementById("topbar");
    if (!bar || !S.cfg) return;
    M.clear(bar);
    var local = S.cfg.is_local;

    bar.appendChild(h("button", { class: "btn ghost icon menu-btn", "aria-label": "Меню", text: "☰", onclick: function () {
      document.getElementById("app").classList.toggle("side-open");
    } }));
    bar.appendChild(h("strong", { text: VIEW_TITLES[S.view] || "МОЛЛИ" }));

    if (S.view === "chat") {
      if (local) {
        var models = (S.ollama && S.ollama.models) || [];
        var current = (S.settings && S.settings.model) || S.cfg.model || "";
        var sel = h("select", { class: "input model-select", "aria-label": "Модель", title: "Модель" });
        var names = models.map(function (m) { return m.name; });
        if (current && names.indexOf(current) < 0) sel.appendChild(h("option", { value: current, text: current + (S.ollama && S.ollama.ok ? " (не найдена)" : "") }));
        if (!current && !names.length) sel.appendChild(h("option", { value: "", text: "Нет моделей" }));
        names.forEach(function (n) { sel.appendChild(h("option", { value: n, text: n })); });
        sel.value = current || (names[0] || "");
        sel.addEventListener("change", async function () {
          try {
            S.settings = await M.api("/api/settings", { method: "PUT", body: { data: { model: sel.value } } });
            S.cfg.model = sel.value;
            M.checkOllama(true);
          } catch (e) { M.toast(e.message, true); }
        });
        bar.appendChild(sel);
      } else if (S.cfg.model) {
        bar.appendChild(h("span", { class: "chip", text: "Модель: " + S.cfg.model }));
      }
    }

    bar.appendChild(h("div", { class: "grow" }));

    if (local) {
      var o = S.ollama;
      bar.appendChild(h("button", { class: "chip " + (o ? (o.ok ? "ok" : "bad") : ""), style: { cursor: "pointer" }, title: o && !o.ok ? o.error : "Подключение к Ollama",
        onclick: function () { M.openSettings("model"); } },
        h("span", { class: "dot" }), o ? (o.ok ? "Ollama" : "Ollama не запущена") : "Ollama…"));
    }

    var idx = S.index;
    if (idx) {
      var running = idx.running;
      bar.appendChild(h("button", { class: "chip" + (running ? " warn" : ""), style: { cursor: local ? "pointer" : "default" },
        title: S.project && S.project.path, onclick: function () { if (local) M.openSettings("docs"); } },
        running ? h("span", { class: "spinner" }) : "📄",
        running ? "Индексация: " + M.fmtNum(idx.status && idx.status.scanned) : "Документов: " + M.fmtNum(idx.documents)));
    } else if (local) {
      bar.appendChild(h("button", { class: "chip warn", style: { cursor: "pointer" }, text: "Папка не выбрана", onclick: function () { M.openSettings("docs"); } }));
    }
  };

  M.showView = function (view) {
    if (!M.views[view]) view = "chat";
    M.clearTimers();
    M.hooks.onIndex = null;
    S.view = view;
    Array.prototype.forEach.call(document.querySelectorAll("#nav button"), function (b) {
      b.classList.toggle("active", b.dataset.view === view);
    });
    M.renderTopbar();
    var container = document.getElementById("view");
    M.clear(container);
    M.views[view].render(container);
    M.renderChatList();
  };

  /* ---------- запуск ---------- */

  function fatal(message) {
    var root = document.getElementById("root");
    M.clear(root);
    root.appendChild(h("div", { class: "login" }, h("div", { class: "card" },
      h("h2", { text: "МОЛЛИ не отвечает" }),
      h("p", { text: message }),
      h("button", { class: "btn primary", text: "Повторить", onclick: function () { location.reload(); } }))));
  }

  M.hooks.loginRequired = function () { M.showLogin(); };

  async function boot() {
    var auth;
    try {
      auth = await M.api("/api/auth/status");
    } catch (e) {
      fatal(e.message + " Проверьте, что окно МОЛЛИ (start.bat) открыто.");
      return;
    }

    if (!auth.authenticated) { M.showLogin(); return; }

    try {
      S.cfg = await M.api("/api/ui-config");
    } catch (e) {
      if (e.status === 401) return;
      fatal(e.message);
      return;
    }

    S.useDocs = S.cfg.use_documents;
    if (S.cfg.is_local) M.applyTheme(S.cfg.theme);

    buildShell();

    if (S.cfg.is_local) {
      try { S.settings = await M.api("/api/settings"); } catch (e) { M.toast(e.message, true); }
    }

    await Promise.all([M.loadChats(), M.refreshIndexStatus(), M.checkOllama(true)]);

    M.showView("chat");

    if (S.cfg.is_local && !S.cfg.setup_done) M.runWizard();

    // Опрос состояния: чаще, пока идёт индексация.
    var n = 0;
    setInterval(function () {
      if (document.hidden) return;
      n++;
      if ((S.index && S.index.running) || n % 3 === 0) M.refreshIndexStatus();
      if (n % 10 === 0) M.checkOllama();
    }, 3000);

    document.addEventListener("visibilitychange", function () {
      if (!document.hidden) { M.refreshIndexStatus(); M.checkOllama(); }
    });
  }

  boot();
})();
