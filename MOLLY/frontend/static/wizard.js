/* МОЛЛИ — мастер первого запуска и экран входа для сети. */
(function () {
  "use strict";
  var M = window.Molly, S = M.S, h = M.h;

  /* ---------- вход (пользователи LAN) ---------- */

  M.showLogin = function () {
    var root = document.getElementById("root");
    M.clear(root);
    var name = M.input("", { placeholder: "Ваше имя", maxlength: 40, autocomplete: "nickname" });
    var token = M.input("", { placeholder: "XXXX-XXXX", autocomplete: "off", style: { textTransform: "uppercase", letterSpacing: ".1em" } });
    try { name.value = localStorage.getItem("molly_lan_name") || ""; } catch (e) { /* нет localStorage */ }
    var err = h("div", { class: "msg-error", hidden: true });
    var btn = h("button", { class: "btn primary", text: "Войти", style: { width: "100%", justifyContent: "center" }, onclick: go });

    async function go() {
      err.hidden = true;
      btn.disabled = true;
      try {
        await M.api("/api/auth/login", { method: "POST", body: { token: token.value, name: name.value } });
        try { localStorage.setItem("molly_lan_name", name.value.trim()); } catch (e) { /* нет localStorage */ }
        location.reload();
      } catch (e) {
        err.textContent = e.message; err.hidden = false;
      } finally { btn.disabled = false; }
    }
    [name, token].forEach(function (i) { i.addEventListener("keydown", function (e) { if (e.key === "Enter") go(); }); });

    root.appendChild(h("div", { class: "login" }, h("div", { class: "card" },
      h("div", { class: "brand", style: { padding: 0, marginBottom: "14px" } }, h("div", { class: "logo", text: "М" }),
        h("div", null, h("div", { class: "brand-title", text: "МОЛЛИ" }), h("div", { class: "brand-sub", text: "AI-помощник ПТО" }))),
      h("p", { class: "hint", style: { marginTop: 0 }, text: "Введите имя и токен доступа. Токен показан в настройках МОЛЛИ на основном компьютере." }),
      M.field("Имя", name), M.field("Токен доступа", token), err, h("div", { style: { marginTop: "12px" } }, btn))));
    name.focus();
  };

  /* ---------- мастер ---------- */

  var STEPS = 8;

  M.runWizard = async function () {
    if (!S.settings) { try { S.settings = await M.api("/api/settings"); } catch (e) { M.toast(e.message, true); return; } }

    var w = {
      step: 1,
      url: S.settings.ollama_url,
      model: S.settings.model,
      assistant: S.settings.assistant_name,
      user: S.settings.user_name,
      style: S.settings.style,
      verbosity: S.settings.verbosity,
      ollama: null,
    };

    var content = h("div");
    var dots = h("div", { class: "steps" });
    var nav = h("div", { class: "modal-foot", style: { padding: "14px 0 0", marginTop: "10px" } });
    var dlg = M.modal({ title: "Первоначальная настройка МОЛЛИ", body: h("div", null, content, dots, nav), closable: false, autofocus: false, wide: true });

    function paintDots() {
      M.clear(dots);
      for (var i = 1; i <= STEPS; i++) dots.appendChild(h("i", { class: i <= w.step ? "on" : "" }));
    }

    function navButtons(opts) {
      M.clear(nav);
      if (opts.back !== false && w.step > 1) nav.appendChild(h("button", { class: "btn", text: "← Назад", onclick: function () { go(w.step - 1); } }));
      (opts.extra || []).forEach(function (b) { nav.appendChild(b); });
      nav.appendChild(h("button", { class: "btn primary", text: opts.nextLabel || "Далее →", onclick: opts.onNext || function () { go(w.step + 1); } }));
    }

    function go(step) {
      if (S.index && M.hooks.onIndex === wizardIndexHook) M.hooks.onIndex = null;
      w.step = step;
      M.clear(content);
      paintDots();
      var render = [null, s1, s2, s3, s4, s5, s6, s7, s8][step];
      render();
    }

    function title(t, sub) {
      content.appendChild(h("h2", { style: { margin: "6px 0 4px" }, text: t }));
      if (sub) content.appendChild(h("p", { class: "lead", style: { color: "var(--muted)", margin: "0 0 14px" }, text: sub }));
    }

    /* 1. Добро пожаловать */
    function s1() {
      content.appendChild(h("div", { class: "wizard-hero" }, h("div", { class: "logo", text: "М" }),
        h("h2", { text: "Добро пожаловать в МОЛЛИ" }),
        h("p", { style: { color: "var(--muted)", maxWidth: "460px", margin: "0 auto" },
          text: "AI-помощник ПТО работает на вашем компьютере: документы остаются у вас и никуда не отправляются. Настройка займёт пару минут — всё потом можно изменить в настройках." })));
      navButtons({ back: false, nextLabel: "Начать" });
    }

    /* 2. Ollama */
    function s2() {
      title("Проверка Ollama", "МОЛЛИ использует локальную модель через Ollama.");
      var url = M.input(w.url);
      var result = h("div", { style: { margin: "10px 0" } });
      content.appendChild(M.field("Адрес Ollama", url));
      content.appendChild(result);
      async function check() {
        w.url = url.value.trim();
        M.clear(result); result.appendChild(h("span", { class: "spinner" }));
        try {
          w.ollama = await M.api("/api/ollama/status?url=" + encodeURIComponent(w.url));
        } catch (e) { w.ollama = { ok: false, error: e.message, models: [] }; }
        M.clear(result);
        if (w.ollama.ok) {
          result.appendChild(h("div", { class: "chip ok" }, h("span", { class: "dot" }), "Ollama " + (w.ollama.version || "") + " работает · моделей: " + w.ollama.models.length));
        } else {
          result.appendChild(h("div", { class: "msg-error", text: w.ollama.error }));
          result.appendChild(h("div", { class: "hint" }, "Нужно установить Ollama: ", h("a", { href: "https://ollama.com/download", target: "_blank", rel: "noopener" }, "ollama.com/download"),
            " (интернет нужен только для скачивания). Запустите её и нажмите «Проверить снова». Можно продолжить и настроить позже."));
        }
      }
      navButtons({ extra: [h("button", { class: "btn", text: "Проверить снова", onclick: check })], onNext: function () { w.url = url.value.trim(); go(3); } });
      check();
    }

    /* 3. Модель */
    function s3() {
      title("Выбор модели", "Модель, которая будет отвечать на вопросы.");
      var models = (w.ollama && w.ollama.models) || [];
      var input = M.input(w.model, { list: "wiz-models", autocomplete: "off" });
      var dl = h("datalist", { id: "wiz-models" }, models.map(function (m) { return h("option", { value: m.name }); }));
      var names = models.map(function (m) { return m.name; });
      if (!w.model || (names.length && names.indexOf(w.model) < 0 && names.indexOf(w.model + ":latest") < 0)) {
        var pref = names.filter(function (n) { return /^molly-pto/.test(n); })[0] || names[0];
        if (pref) input.value = pref;
      }
      content.appendChild(M.field("Модель", h("div", null, input, dl),
        models.length ? "Найдено моделей: " + models.length + (names.some(function (n) { return /^molly-pto/.test(n); }) ? " (включая molly-pto)." : ".")
          : "Список пуст (Ollama недоступна или моделей нет). Можно ввести имя вручную, например molly-pto."));
      navButtons({ onNext: function () { w.model = input.value.trim(); go(4); } });
    }

    /* 4. Папка */
    function s4() {
      title("Рабочая папка проекта", "Папка с документами: PDF, Word, Excel, текст. МОЛЛИ только читает файлы.");
      var shown = h("div", { style: { fontWeight: "600", overflowWrap: "anywhere", margin: "8px 0" }, text: S.project && S.project.path ? S.project.path : "Папка не выбрана" });
      var manual = M.input("", { placeholder: "или вставьте путь, например D:\\1. МДП Каркатеевы" });
      async function apply(p) {
        if (p && (await M.chooseProject(p))) shown.textContent = S.project.path;
      }
      content.appendChild(shown);
      content.appendChild(h("div", { class: "row" }, h("button", { class: "btn primary", text: "Выбрать папку…", onclick: async function () {
        var p = await M.pickFolder(S.project && S.project.path ? S.project.path : ""); if (p) apply(p);
      } })));
      content.appendChild(h("div", { class: "row", style: { marginTop: "10px" } }, h("div", { class: "grow" }, manual),
        h("button", { class: "btn", text: "Применить", onclick: function () { apply(manual.value.trim().replace(/^"|"$/g, "")); } })));
      navButtons({ nextLabel: "Далее →" });
    }

    /* 5. Индексация */
    function wizardIndexHook() { paintIndex(); }
    var indexBox = h("div");
    function paintIndex() {
      if (!indexBox.isConnected) return;
      M.clear(indexBox);
      var idx = S.index;
      if (!idx) { indexBox.appendChild(h("div", { class: "hint", text: "Рабочая папка не выбрана — этот шаг можно пропустить." })); return; }
      var st = idx.status || {};
      if (idx.running) {
        indexBox.appendChild(h("div", { class: "progress indet" }, h("div")));
        indexBox.appendChild(h("div", { class: "status-text", text: "Просмотрено файлов: " + M.fmtNum(st.scanned) + " · добавлено: " + M.fmtNum(st.indexed) }));
        if (st.current_file) indexBox.appendChild(h("div", { class: "hint", style: { overflowWrap: "anywhere" }, text: st.current_file }));
      } else {
        indexBox.appendChild(h("div", { class: "chip ok" }, h("span", { class: "dot" }), "Документов в индексе: " + M.fmtNum(idx.documents)));
        if (st.errors) indexBox.appendChild(h("div", { class: "hint", text: "Файлов, которые не удалось прочитать: " + M.fmtNum(st.errors) }));
      }
    }
    function s5() {
      title("Индексация документов", "МОЛЛИ читает документы папки и готовит быстрый поиск. После этого новые и изменённые файлы подхватываются автоматически.");
      content.appendChild(indexBox);
      M.hooks.onIndex = wizardIndexHook;
      if (S.project && S.project.path) {
        M.startIndexing().then(function () { paintIndex(); });
        content.appendChild(h("div", { class: "hint", style: { marginTop: "10px" }, text: "Для больших папок это может занять много времени — можно продолжить, индексация пойдёт в фоне." }));
      }
      paintIndex();
      navButtons({ nextLabel: "Продолжить →" });
    }

    /* 6. Имя и стиль */
    function s6() {
      title("Имя и стиль общения");
      var assistant = M.input(w.assistant, { maxlength: 40 });
      var user = M.input(w.user, { maxlength: 60, placeholder: "Например, Алексей" });
      var style = M.select([["business", "Деловой"], ["friendly", "Дружелюбный"], ["strict", "Строгий, формальный"]], w.style);
      var verb = M.select([["brief", "Кратко"], ["detailed", "Подробно"]], w.verbosity);
      content.appendChild(h("div", { class: "cols" }, M.field("Как зовут помощника", assistant), M.field("Как к вам обращаться", user)));
      content.appendChild(h("div", { class: "cols" }, M.field("Стиль общения", style), M.field("Длина ответов", verb)));
      navButtons({ onNext: function () {
        w.assistant = assistant.value.trim() || "МОЛЛИ"; w.user = user.value.trim(); w.style = style.value; w.verbosity = verb.value; go(7);
      } });
    }

    /* 7. Почта */
    function s7() {
      title("Яндекс.Почта (по желанию)", "Можно подключить сейчас или позже в разделе «Почта».");
      var email = M.input("", { type: "email", placeholder: "name@yandex.ru", autocomplete: "off" });
      var pass = M.input("", { type: "password", placeholder: "Пароль приложения", autocomplete: "new-password" });
      var res = h("div", { style: { margin: "8px 0" } });
      content.appendChild(h("div", { class: "cols" }, M.field("Адрес почты", email), M.field("Пароль приложения", pass)));
      content.appendChild(h("div", { class: "hint", text: "В Яндекс.Почте включите доступ по IMAP и создайте пароль приложения (id.yandex.ru → Безопасность → Пароли приложений). Пароль хранится в защищённом хранилище Windows." }));
      content.appendChild(res);
      async function connect() {
        M.clear(res); res.appendChild(h("span", { class: "spinner" }));
        try {
          await M.api("/api/mail/credentials", { method: "PUT", body: { email: email.value.trim(), password: pass.value } });
          var t = await M.api("/api/mail/test", { method: "POST" });
          M.clear(res);
          res.appendChild(h("div", { class: t.ok ? "chip ok" : "msg-error", text: t.ok ? "Подключено" : ((t.imap && !t.imap.ok && t.imap.error) || (t.smtp && t.smtp.error) || "Не удалось подключиться") }));
        } catch (e) { M.clear(res); res.appendChild(h("div", { class: "msg-error", text: e.message })); }
      }
      navButtons({ extra: [h("button", { class: "btn", text: "Подключить и проверить", onclick: connect })], nextLabel: "Далее →" });
    }

    /* 8. Готово */
    function s8() {
      content.appendChild(h("div", { class: "wizard-hero" }, h("div", { class: "logo", text: "✓" }),
        h("h2", { text: "Всё готово" }),
        h("p", { style: { color: "var(--muted)", maxWidth: "460px", margin: "0 auto" }, text: "МОЛЛИ настроена. Задайте вопрос по документам вашего проекта. Настройки можно изменить в любой момент." })));
      navButtons({ nextLabel: "Начать работу", onNext: async function () {
        try {
          S.settings = await M.api("/api/settings", { method: "PUT", body: { data: {
            ollama_url: w.url, model: w.model, assistant_name: w.assistant, user_name: w.user,
            style: w.style, verbosity: w.verbosity, setup_done: true,
          } } });
          S.cfg.setup_done = true;
          S.cfg.assistant_name = S.settings.assistant_name; S.cfg.user = S.settings.user_name; S.cfg.model = S.settings.model;
          dlg.close();
          M.hooks.onIndex = null;
          M.checkOllama(true);
          M.showView("chat");
        } catch (e) { M.toast(e.message, true); }
      } });
    }

    go(1);
  };
})();
