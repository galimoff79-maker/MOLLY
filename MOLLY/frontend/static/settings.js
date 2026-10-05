/* МОЛЛИ — настройки. */
(function () {
  "use strict";
  var M = window.Molly, S = M.S, h = M.h;

  var TABS = [
    ["molly", "МОЛЛИ"],
    ["model", "Модель"],
    ["docs", "Документы"],
    ["mail", "Почта"],
    ["lan", "Сервер"],
    ["data", "Данные"],
  ];

  var currentTab = "molly";
  var timers = [];

  M.clearTimers = function () { timers.forEach(clearInterval); timers = []; };

  M.openSettings = function (tab) {
    if (tab) currentTab = tab;
    M.showView("settings");
  };

  /* ---------- общие помощники ---------- */

  function afterSettingsChanged() {
    var s = S.settings;
    if (!s) return;
    S.cfg.assistant_name = s.assistant_name;
    S.cfg.user = s.user_name;
    S.cfg.theme = s.theme;
    S.cfg.model = s.model;
    S.useDocs = s.use_documents;
    M.applyTheme(s.theme);
    if (M.renderTopbar) M.renderTopbar();
    if (M.refreshBanners) M.refreshBanners();
  }

  async function save(patch, statusEl) {
    statusEl.className = "status-text";
    statusEl.textContent = "Сохранение…";
    try {
      S.settings = await M.api("/api/settings", { method: "PUT", body: { data: patch } });
      afterSettingsChanged();
      statusEl.className = "status-text ok";
      statusEl.textContent = "Сохранено ✓";
      setTimeout(function () { if (statusEl.textContent === "Сохранено ✓") statusEl.textContent = ""; }, 2500);
      return true;
    } catch (e) {
      statusEl.className = "status-text bad";
      statusEl.textContent = e.message;
      return false;
    }
  }

  function saveBar(onSave) {
    var status = h("span", { class: "status-text" });
    var btn = h("button", { class: "btn primary", text: "Сохранить", onclick: async function () {
      btn.disabled = true;
      try { await onSave(status); } finally { btn.disabled = false; }
    } });
    return h("div", { class: "save-bar" }, btn, status);
  }

  function panel(title, lead) {
    return h("div", null, h("h2", { text: title }), lead ? h("p", { class: "lead", text: lead }) : null);
  }

  function num(value, attrs) {
    return M.input(value, Object.assign({ type: "number" }, attrs || {}));
  }

  /* ---------- выбор папки ---------- */

  M.pickFolder = function (start) {
    return new Promise(function (resolve) {
      var current = "";
      var pathInput = M.input("", { placeholder: "Путь к папке, например D:\\Проект" });
      var list = h("div", { class: "folder-list" });
      var info = h("div", { class: "hint" });
      var chooseBtn;

      async function load(p) {
        try {
          var d = await M.api("/api/fs/list?path=" + encodeURIComponent(p || ""));
          current = d.path;
          pathInput.value = d.path;
          info.textContent = d.path ? "" : "Выберите диск.";
          M.clear(list);
          if (d.path) {
            list.appendChild(h("button", { text: "⬆  Вверх", onclick: function () { load(d.parent || ""); } }));
          }
          d.dirs.forEach(function (x) {
            list.appendChild(h("button", { text: "📁  " + x.name, onclick: function () { load(x.path); } }));
          });
          if (!d.dirs.length) list.appendChild(h("div", { class: "empty-note", text: "Вложенных папок нет." }));
          if (chooseBtn) chooseBtn.disabled = !d.path;
        } catch (e) {
          info.textContent = e.message;
        }
      }

      pathInput.addEventListener("keydown", function (e) { if (e.key === "Enter") load(pathInput.value.trim()); });

      var dlg = M.modal({
        title: "Выбор рабочей папки",
        wide: true,
        body: h("div", null,
          h("div", { class: "row" },
            h("div", { class: "grow" }, pathInput),
            h("button", { class: "btn", text: "Перейти", onclick: function () { load(pathInput.value.trim()); } })),
          info, list,
          h("div", { class: "hint", text: "Откройте нужную папку и нажмите «Выбрать эту папку». Файлы внутри не изменяются." })),
        buttons: [
          { label: "Отмена", onClick: function () { resolve(null); } },
          { label: "Выбрать эту папку", primary: true, onClick: function () { resolve(current || null); } },
        ],
        onClose: function () { resolve(null); },
        autofocus: false,
      });
      chooseBtn = dlg.el.querySelector(".modal-foot .btn.primary");
      chooseBtn.disabled = true;
      load(start || "");
    });
  };

  /* ---------- вкладка «МОЛЛИ» ---------- */

  function tabMolly() {
    var s = S.settings;
    var name = M.input(s.assistant_name, { maxlength: 40 });
    var user = M.input(s.user_name, { maxlength: 60, placeholder: "Как к вам обращаться" });
    var style = M.select([["business", "Деловой"], ["friendly", "Дружелюбный"], ["strict", "Строгий, формальный"]], s.style);
    var verb = M.select([["brief", "Кратко"], ["detailed", "Подробно"]], s.verbosity);
    var theme = M.select([["system", "Как в Windows"], ["light", "Светлая"], ["dark", "Тёмная"]], s.theme);
    var docs = M.checkbox("Автоматически использовать документы рабочей папки в ответах", s.use_documents,
      "МОЛЛИ сама ищет нужные фрагменты и показывает источники. Документы отправляются только в вашу локальную модель.");
    var topk = num(s.top_k, { min: 1, max: 15 });
    var prompt = h("textarea", { class: "input mono", rows: 10, spellcheck: "false", placeholder: "Если пусто — используется стандартная инструкция МОЛЛИ." });
    prompt.value = s.system_prompt;
    var file = h("input", { type: "file", accept: ".txt,.md,text/plain", hidden: true });
    file.addEventListener("change", async function () {
      if (!file.files[0]) return;
      try { prompt.value = await M.readFile(file.files[0]); M.toast("Инструкции загружены. Нажмите «Сохранить»."); }
      catch (e) { M.toast(e.message, true); }
      file.value = "";
    });

    var root = panel("МОЛЛИ", "Имя, стиль общения и инструкции.");
    root.appendChild(h("div", { class: "cols" }, M.field("Имя помощника", name), M.field("Как обращаться к вам", user)));
    root.appendChild(h("div", { class: "cols" }, M.field("Стиль общения", style), M.field("Длина ответов", verb)));
    root.appendChild(M.field("Тема интерфейса", theme));
    root.appendChild(h("div", { class: "field" }, docs));
    root.appendChild(M.field("Сколько фрагментов документов подставлять в ответ", topk, "От 1 до 15. Больше фрагментов — точнее, но дольше и тяжелее для модели."));
    root.appendChild(M.field("Системный prompt (инструкции)", h("div", null, prompt,
      h("div", { class: "row", style: { marginTop: "8px" } },
        h("button", { class: "btn sm", text: "Подставить стандартный", onclick: async function () {
          try { prompt.value = (await M.api("/api/settings/default-prompt")).prompt; } catch (e) { M.toast(e.message, true); }
        } }),
        h("button", { class: "btn sm", text: "Загрузить из файла…", onclick: function () { file.click(); } }),
        h("button", { class: "btn sm", text: "Сохранить в файл", onclick: function () { M.download("molly-prompt.txt", prompt.value); } }),
        h("button", { class: "btn sm ghost", text: "Очистить", onclick: function () { prompt.value = ""; } }),
        file)),
      "Здесь можно передать МОЛЛИ собственные правила работы. Стиль и длина ответов из настроек выше добавляются автоматически."));
    root.appendChild(saveBar(function (st) {
      return save({
        assistant_name: name.value, user_name: user.value, style: style.value, verbosity: verb.value,
        theme: theme.value, use_documents: docs.input.checked, top_k: Number(topk.value), system_prompt: prompt.value,
      }, st);
    }));
    return root;
  }

  /* ---------- вкладка «Модель» ---------- */

  function tabModel() {
    var s = S.settings;

    /* ---------- переключатель LOCAL / ONLINE ---------- */
    var provOpts = [
      ["ollama", "LOCAL — Ollama (локальная модель)"],
      ["freellmapi", "ONLINE — FreeLLMAPI (онлайн-модели)"],
    ];
    var providerSel = M.select(provOpts, s.provider || "ollama");
    var ollamaBox = h("div", null);
    var onlineBox = h("div", null);

    function syncProviderBoxes() {
      ollamaBox.style.display = providerSel.value === "ollama" ? "" : "none";
      onlineBox.style.display = providerSel.value === "freellmapi" ? "" : "none";
    }

    /* ---------- секция Ollama ---------- */
    var url = M.input(s.ollama_url);
    var model = M.input(s.model, { list: "model-list", placeholder: "например, molly-pto", autocomplete: "off" });
    var datalist = h("datalist", { id: "model-list" });
    var result = h("div", { style: { marginTop: "8px" } });
    var temp = num(s.temperature, { step: "0.05", min: 0, max: 2 });
    var ctx = num(s.num_ctx, { step: "512", min: 512, max: 131072 });
    var maxTok = num(s.max_tokens, { step: "64", min: 16, max: 32768 });
    var keepOptions = [
      ["0", "Сразу после ответа (экономит память)"],
      ["5m", "Через 5 минут простоя (рекомендуется)"],
      ["30m", "Через 30 минут простоя"],
      ["1h", "Через 1 час простоя"],
      ["-1", "Никогда (быстрее ответы, больше занятой памяти)"],
    ];
    if (!keepOptions.some(function (o) { return o[0] === s.keep_alive; })) keepOptions.push([s.keep_alive, s.keep_alive]);
    var keep = M.select(keepOptions, s.keep_alive);

    function fillModels(models) {
      M.clear(datalist);
      (models || []).forEach(function (m) { datalist.appendChild(h("option", { value: m.name })); });
    }
    if (S.ollama && S.ollama.models) fillModels(S.ollama.models);

    async function check() {
      M.clear(result);
      result.appendChild(h("span", { class: "spinner" }));
      try {
        var r = await M.api("/api/ollama/status?url=" + encodeURIComponent(url.value.trim()));
        M.clear(result);
        if (r.ok) {
          fillModels(r.models);
          result.appendChild(h("span", { class: "chip ok" }, h("span", { class: "dot" }), "Подключено · Ollama " + (r.version || "") + " · моделей: " + r.models.length));
          if (r.models.length) {
            result.appendChild(h("div", { class: "hint", text: "Доступные модели: " + r.models.map(function (m) { return m.name; }).join(", ") }));
          } else {
            result.appendChild(h("div", { class: "hint", text: "В Ollama пока нет моделей. Загрузите модель командой «ollama pull имя» или создайте свою (molly-pto)." }));
          }
        } else {
          result.appendChild(h("div", { class: "msg-error", text: r.error }));
        }
      } catch (e) {
        M.clear(result);
        result.appendChild(h("div", { class: "msg-error", text: e.message }));
      }
    }

    /* ---------- секция FreeLLMAPI ---------- */
    var flUrl = M.input((s.freellmapi && s.freellmapi.url) || "http://127.0.0.1:31415");
    var flKey = M.input("", { type: "password", placeholder: (s.freellmapi && s.freellmapi.key_masked) ? "Сохранён: " + s.freellmapi.key_masked + " (введите для замены)" : "API-ключ (не обязателен локально)", autocomplete: "off" });
    var flResult = h("div", { style: { marginTop: "8px" } });
    var modeOpts = [
      ["auto", "AUTO — самая мощная доступная модель"],
      ["manual", "Ручной выбор модели"],
    ];
    var flMode = M.select(modeOpts, s.model_mode || "auto");
    var flModelSel = M.select([["", "—"]], "");

    async function refreshFlModels() {
      try {
        var r = await M.api("/api/freellmapi/status?refresh=true");
        S.freellm = r;
        M.clear(flModelSel);
        flModelSel.appendChild(h("option", { value: "", text: "AUTO — лучшая доступная" }));
        (r.models || []).forEach(function (m) {
          flModelSel.appendChild(h("option", { value: m.id, text: (m.available ? "" : "⏳ ") + (m.name || m.id) }));
        });
        var saved = s.online_model || "";
        flModelSel.value = saved;
        if (flModelSel.value !== saved) {
          // сохранённой модели нет в списке — добавим, чтобы не потерять выбор
          flModelSel.appendChild(h("option", { value: saved, text: saved + " (из настроек)" }));
          flModelSel.value = saved;
        }
        return r;
      } catch (e) { return { connected: false, error: e.message }; }
    }

    async function checkFreellm() {
      M.clear(flResult);
      flResult.appendChild(h("span", { class: "spinner" }));
      try {
        var r = await M.api("/api/freellmapi/test", { method: "POST", body: { url: flUrl.value.trim() } });
        M.clear(flResult);
        if (r.ok) {
          flResult.appendChild(h("span", { class: "chip ok" }, h("span", { class: "dot" }),
            "Подключено · моделей: " + (r.models_count != null ? r.models_count : "—")));
          var rr = await refreshFlModels();
          if (rr && rr.active_name) {
            flResult.appendChild(h("div", { class: "hint", text: "Текущая лучшая модель AUTO: " + rr.active_name }));
          }
        } else {
          flResult.appendChild(h("div", { class: "msg-error", text: r.error || "FreeLLMAPI недоступен" }));
        }
      } catch (e) {
        M.clear(flResult);
        flResult.appendChild(h("div", { class: "msg-error", text: e.message }));
      }
    }

    async function saveFlKey(st) {
      var key = flKey.value.trim();
      if (!key) return true;
      try {
        await M.api("/api/freellmapi/key", { method: "POST", body: { api_key: key } });
        flKey.value = "";
        if (S.settings && S.settings.freellmapi) S.settings.freellmapi.key_set = true;
        return true;
      } catch (e) {
        st.className = "status-text bad"; st.textContent = e.message;
        return false;
      }
    }

    providerSel.addEventListener("change", syncProviderBoxes);

    var root = panel("Модель", "Локальная модель (Ollama) или онлайн-модели (FreeLLMAPI). Индекс документов одинаков для обоих режимов.");
    root.appendChild(M.field("Источник ответов", providerSel,
      "LOCAL: документы не покидают компьютер. ONLINE: МОЛЛИ отправляет только найденные фрагменты, не папку целиком."));

    ollamaBox.appendChild(M.field("Адрес Ollama", h("div", null,
      h("div", { class: "row" }, h("div", { class: "grow" }, url), h("button", { class: "btn", text: "Проверить подключение", onclick: check })),
      result), "Обычно http://127.0.0.1:11434. Ollama работает на вашем компьютере, документы никуда не отправляются."));
    ollamaBox.appendChild(M.field("Модель", h("div", null, model, datalist), "Выберите из списка или введите имя вручную."));
    ollamaBox.appendChild(h("div", { class: "cols" },
      M.field("Temperature", temp, "0 — строго и предсказуемо, 1 и выше — свободнее. Для документов лучше 0.1–0.4."),
      M.field("Максимум токенов в ответе", maxTok, "Ограничивает длину ответа.")));
    ollamaBox.appendChild(h("div", { class: "cols" },
      M.field("Контекст (токенов)", ctx, "Сколько текста модель «видит» за раз. Больше — точнее по документам, но требует больше оперативной памяти."),
      M.field("Держать модель в памяти", keep, "После этого времени модель выгружается из памяти — приложение не занимает RAM, пока вы им не пользуетесь.")));
    ollamaBox.appendChild(saveBar(async function (st) {
      var ok = await save({
        ollama_url: url.value, model: model.value.trim(), temperature: Number(temp.value),
        num_ctx: Number(ctx.value), max_tokens: Number(maxTok.value), keep_alive: keep.value,
        provider: providerSel.value,
      }, st);
      if (ok) M.checkOllama(true);
    }));

    onlineBox.appendChild(M.field("URL FreeLLMAPI", h("div", null,
      h("div", { class: "row" }, h("div", { class: "grow" }, flUrl), h("button", { class: "btn", text: "Проверить подключение", onclick: checkFreellm })),
      flResult), "Обычно http://127.0.0.1:31415."));
    onlineBox.appendChild(M.field("API-ключ", flKey, "Хранится в защищённом хранилище Windows (DPAPI), не показывается открытым текстом и не попадает в логи."));
    onlineBox.appendChild(h("div", { class: "cols" },
      M.field("Режим выбора модели", flMode, "AUTO — МОЛЛИ сама выбирает самую мощную доступную модель и автоматически переключается при ошибках (429/503/timeout) с последующим возвратом."),
      M.field("Модель (для ручного режима)", flModelSel, "Список загружается динамически из /v1/models FreeLLMAPI.")));
    onlineBox.appendChild(saveBar(async function (st) {
      if (!(await saveFlKey(st))) return;
      var patch = {
        provider: providerSel.value,
        model_mode: flMode.value,
        online_model: flMode.value === "manual" ? flModelSel.value : "",
        freellmapi: { enabled: providerSel.value === "freellmapi", url: flUrl.value.trim() },
      };
      var ok = await save(patch, st);
      if (ok) { M.checkFreellm(false); M.renderTopbar(); }
    }));
    refreshFlModels().then(function (r) {
      if (r && r.connected) {
        M.clear(flResult);
        M.clear(flResult);
        flResult.appendChild(h("span", { class: "chip ok" }, h("span", { class: "dot" }), "Подключено · моделей: " + (r.models_count || 0)));
      }
    });

    root.appendChild(ollamaBox);
    root.appendChild(onlineBox);
    syncProviderBoxes();
    check();
    return root;
  }

  /* ---------- вкладка «Документы» ---------- */

  function indexStatusView(box) {
    M.clear(box);
    var idx = S.index;
    if (!idx) {
      box.appendChild(h("div", { class: "hint", text: "Рабочая папка не выбрана." }));
      return;
    }
    var st = idx.status || {};
    var running = idx.running;
    var statusName = { idle: "Готово", indexing: "Идёт индексация", error: "Ошибка", interrupted: "Прервано", stopped: "Остановлено" }[st.status] || st.status || "—";
    box.appendChild(h("div", { class: "row", style: { marginBottom: "6px" } },
      h("span", { class: "chip " + (running ? "warn" : st.status === "error" ? "bad" : "ok") }, running ? h("span", { class: "spinner" }) : h("span", { class: "dot" }), statusName),
      h("span", { class: "status-text", text: "Документов в индексе: " + M.fmtNum(idx.documents) })));
    if (running) {
      box.appendChild(h("div", { class: "progress indet" }, h("div")));
      box.appendChild(h("dl", { class: "kv" },
        h("dt", { text: "Просмотрено файлов" }), h("dd", { text: M.fmtNum(st.scanned) }),
        h("dt", { text: "Добавлено/обновлено" }), h("dd", { text: M.fmtNum(st.indexed) }),
        h("dt", { text: "Без изменений" }), h("dd", { text: M.fmtNum(st.skipped) }),
        h("dt", { text: "Сейчас" }), h("dd", { text: st.current_file || "—" })));
    } else {
      box.appendChild(h("dl", { class: "kv" },
        h("dt", { text: "Последний запуск" }), h("dd", { text: st.finished_at ? new Date(st.finished_at).toLocaleString("ru-RU") : "—" }),
        h("dt", { text: "Просмотрено / добавлено" }), h("dd", { text: M.fmtNum(st.scanned) + " / " + M.fmtNum(st.indexed) }),
        h("dt", { text: "Удалено из индекса" }), h("dd", { text: M.fmtNum(st.removed) }),
        h("dt", { text: "Ошибок чтения" }), h("dd", { text: M.fmtNum(st.errors) })));
    }
    if (st.error_message) box.appendChild(h("div", { class: "msg-error", text: "Последняя ошибка: " + st.error_message }));
  }

  async function startIndexing() {
    try {
      var r = await M.api("/api/index", { method: "POST" });
      M.toast(r.started ? "Индексация запущена" : "Индексация уже идёт");
      M.refreshIndexStatus();
    } catch (e) { M.toast(e.message, true); }
  }
  M.startIndexing = startIndexing;

  M.chooseProject = async function (path) {
    try {
      var r = await M.api("/api/project/choose", { method: "POST", body: { path: path } });
      S.project = r.project;
      await M.refreshIndexStatus();
      if (M.renderTopbar) M.renderTopbar();
      if (M.refreshBanners) M.refreshBanners();
      if (M.updateComposer) M.updateComposer();
      return true;
    } catch (e) {
      M.toast(e.message, true);
      return false;
    }
  };

  function tabDocs() {
    var s = S.settings;
    var pathText = h("div", { class: "status-text", style: { overflowWrap: "anywhere" } });
    var manual = M.input("", { placeholder: "или вставьте путь, например D:\\1. МДП Каркатеевы" });
    var excluded = h("textarea", { class: "input mono", rows: 5, spellcheck: "false", placeholder: "Архив\nЧерновики\\старое\n*.bak\n~*" });
    excluded.value = (s.excluded || []).join("\n");
    var auto = num(s.auto_reindex_minutes, { min: 0, max: 1440 });
    var statusBox = h("div");

    function showPath() {
      pathText.textContent = S.project && S.project.path ? S.project.path : "Папка не выбрана";
      pathText.style.fontWeight = "600";
    }
    showPath();

    async function apply(path) {
      if (!path) return;
      if (await M.chooseProject(path)) {
        showPath();
        M.toast("Рабочая папка выбрана");
        var go = await M.confirm("Индексировать документы?", "Запустить индексацию сейчас? Сначала МОЛЛИ прочитает все документы папки; потом будет обновлять только новые и изменённые.", "Индексировать");
        if (go) startIndexing();
      }
    }

    M.hooks.onIndex = function () { if (statusBox.isConnected) indexStatusView(statusBox); };
    indexStatusView(statusBox);

    var root = panel("Документы", "Рабочая папка проекта и индекс для поиска.");
    root.appendChild(h("div", { class: "card" },
      h("h3", { text: "Рабочая папка" }), pathText,
      h("div", { class: "row", style: { marginTop: "10px" } },
        h("button", { class: "btn primary", text: "Выбрать папку…", onclick: async function () {
          var p = await M.pickFolder(S.project && S.project.path ? S.project.path : "");
          if (p) apply(p);
        } })),
      h("div", { class: "row", style: { marginTop: "10px" } },
        h("div", { class: "grow" }, manual),
        h("button", { class: "btn", text: "Применить", onclick: function () { apply(manual.value.trim().replace(/^"|"$/g, "")); } })),
      h("div", { class: "hint", text: "МОЛЛИ только читает файлы папки и ничего в ней не изменяет. Поддерживаются PDF, DOC, DOCX, XLS, XLSX, XLSM, TXT, CSV, XML, JSON; DWG показываются в списке без чтения чертежа." })));

    root.appendChild(h("div", { class: "card" },
      h("h3", { text: "Индекс" }), statusBox,
      h("div", { class: "row", style: { marginTop: "10px" } },
        h("button", { class: "btn primary", text: "Обновить индекс сейчас", onclick: startIndexing }),
        h("button", { class: "btn", text: "Остановить", onclick: async function () {
          try { await M.api("/api/index/stop", { method: "POST" }); M.toast("Остановка запрошена"); } catch (e) { M.toast(e.message, true); }
        } }),
        h("button", { class: "btn ghost", text: "Пересобрать поиск", title: "Пересоздаёт полнотекстовый индекс без повторного чтения файлов", onclick: async function () {
          try { var r = await M.api("/api/index/rebuild-fts", { method: "POST" }); M.toast("Готово, документов: " + r.documents); } catch (e) { M.toast(e.message, true); }
        } }))));

    root.appendChild(M.field("Исключения из индекса (по одному в строке)", excluded,
      "Папки или файлы (полный путь или относительно рабочей папки) и маски: «Архив», «Черновики\\старое», «*.bak», «~*». Исключённое будет удалено из индекса при следующем обновлении."));
    root.appendChild(M.field("Автоматически проверять новые и изменённые файлы, минут", auto,
      "0 — выключено. Проверка быстрая: перечитываются только изменившиеся файлы."));
    root.appendChild(saveBar(function (st) {
      return save({
        excluded: excluded.value.split("\n").map(function (x) { return x.trim(); }).filter(Boolean),
        auto_reindex_minutes: Number(auto.value),
      }, st);
    }));
    return root;
  }

  /* ---------- вкладка «Почта» ---------- */

  function tabMail() {
    var root = panel("Почта", "Подключение Яндекс.Почты (IMAP/SMTP). Пароль хранится в защищённом хранилище Windows.");
    var body = h("div", null, h("span", { class: "spinner" }));
    root.appendChild(body);

    M.api("/api/mail/status").then(function (st) {
      M.clear(body);
      var email = M.input(st.email, { type: "email", placeholder: "name@yandex.ru", autocomplete: "off" });
      var pass = M.input("", { type: "password", autocomplete: "new-password", placeholder: st.has_password ? "•••••••• (сохранён; введите, чтобы заменить)" : "Пароль приложения" });
      var imapHost = M.input(st.imap_host), imapPort = num(st.imap_port);
      var smtpHost = M.input(st.smtp_host), smtpPort = num(st.smtp_port);
      var confirmSend = M.checkbox("Подтверждать отправку каждого письма", st.confirm_send, "Рекомендуется. Письмо уходит только после вашего нажатия «Отправить» в окне предпросмотра.");
      var result = h("div", { style: { marginTop: "10px" } });

      if (!st.secure_storage) {
        body.appendChild(h("div", { class: "banner warn", style: { margin: "0 0 14px", width: "auto" } },
          "Безопасное хранилище паролей доступно только в Windows. На этом компьютере пароль сохранить нельзя, и почта работать не будет."));
      }
      body.appendChild(h("div", { class: "card" }, h("h3", { text: "Как получить пароль приложения" }),
        h("div", { class: "hint", style: { marginTop: 0 }, text: st.hint })));
      body.appendChild(M.field("Адрес почты", email));
      body.appendChild(M.field("Пароль приложения", pass, "Не пароль от аккаунта Яндекса. Он не попадает в настройки, экспорт и журналы."));
      body.appendChild(h("details", { style: { marginBottom: "16px" } }, h("summary", { text: "Параметры серверов (обычно менять не нужно)", style: { cursor: "pointer", marginBottom: "10px" } }),
        h("div", { class: "cols" }, M.field("IMAP-сервер", imapHost), M.field("IMAP-порт", imapPort)),
        h("div", { class: "cols" }, M.field("SMTP-сервер", smtpHost), M.field("SMTP-порт", smtpPort))));
      body.appendChild(h("div", { class: "field" }, confirmSend));

      async function saveCreds(statusEl) {
        statusEl.className = "status-text"; statusEl.textContent = "Сохранение…";
        try {
          await M.api("/api/mail/credentials", { method: "PUT", body: {
            email: email.value.trim(), password: pass.value || null,
            imap_host: imapHost.value.trim(), imap_port: Number(imapPort.value),
            smtp_host: smtpHost.value.trim(), smtp_port: Number(smtpPort.value),
            confirm_send: confirmSend.input.checked,
          } });
          pass.value = "";
          statusEl.className = "status-text ok"; statusEl.textContent = "Сохранено ✓";
          S.settings = await M.api("/api/settings");
          return true;
        } catch (e) {
          statusEl.className = "status-text bad"; statusEl.textContent = e.message;
          return false;
        }
      }

      var bar = saveBar(saveCreds);
      bar.appendChild(h("button", { class: "btn", text: "Проверить подключение", onclick: async function () {
        var tmp = h("span");
        if (!(await saveCreds(tmp))) { M.toast(tmp.textContent, true); return; }
        M.clear(result); result.appendChild(h("span", { class: "spinner" }));
        try {
          var r = await M.api("/api/mail/test", { method: "POST" });
          M.clear(result);
          [["IMAP (чтение писем)", r.imap], ["SMTP (отправка)", r.smtp]].forEach(function (x) {
            var ok = x[1] && x[1].ok;
            result.appendChild(h("div", { class: "diag-row" }, h("div", { class: "ic", text: ok ? "✅" : "❌" }),
              h("div", null, h("div", { class: "nm", text: x[0] }), ok ? null : h("div", { class: "dt", text: x[1] && x[1].error || "Ошибка" }))));
          });
        } catch (e) { M.clear(result); result.appendChild(h("div", { class: "msg-error", text: e.message })); }
      } }));
      bar.appendChild(h("button", { class: "btn danger", text: "Удалить пароль", onclick: async function () {
        if (!(await M.confirm("Удалить пароль?", "Сохранённый пароль приложения будет удалён с этого компьютера.", "Удалить", true))) return;
        try { await M.api("/api/mail/credentials", { method: "DELETE" }); M.toast("Пароль удалён"); M.showView("settings"); } catch (e) { M.toast(e.message, true); }
      } }));
      body.appendChild(bar);
      body.appendChild(result);
    }).catch(function (e) { M.clear(body); body.appendChild(h("div", { class: "msg-error", text: e.message })); });
    return root;
  }

  /* ---------- вкладка «Сервер» ---------- */

  function tabLan() {
    var root = panel("Сервер в локальной сети", "Откройте МОЛЛИ с других компьютеров в той же Wi-Fi/LAN сети через браузер.");
    var body = h("div", null, h("span", { class: "spinner" }));
    root.appendChild(body);
    var portInput = null;

    async function load() {
      var st;
      try { st = await M.api("/api/lan"); } catch (e) { M.clear(body); body.appendChild(h("div", { class: "msg-error", text: e.message })); return; }
      if (portInput && document.activeElement === portInput) return; // не мешаем вводу порта
      M.clear(body);

      portInput = num(st.port, { min: 1024, max: 65535, style: { width: "120px" }, disabled: st.running });
      body.appendChild(h("div", { class: "card" },
        h("div", { class: "row" },
          h("span", { class: "chip " + (st.running ? "ok" : "") }, h("span", { class: "dot" }), st.running ? "Сервер запущен" : "Сервер выключен"),
          h("div", { class: "grow" }),
          st.running
            ? h("button", { class: "btn danger", text: "Остановить сервер", onclick: async function () {
                try { await M.api("/api/lan/stop", { method: "POST" }); M.toast("Сервер остановлен"); load(); } catch (e) { M.toast(e.message, true); }
              } })
            : h("button", { class: "btn primary", text: "Запустить сервер в локальной сети", onclick: async function () {
                try { await M.api("/api/lan/start", { method: "POST", body: { port: Number(portInput.value) } }); M.toast("Сервер запущен"); portInput = null; load(); }
                catch (e) { M.toast(e.message, true); }
              } })),
        h("div", { class: "row", style: { marginTop: "12px" } }, h("label", { text: "Порт:", style: { fontWeight: "600" } }), portInput),
        st.error ? h("div", { class: "msg-error", text: st.error }) : null));

      var accessCard = h("div", { class: "card" }, h("h3", { text: "Доступ" }),
        h("div", { class: "hint", style: { marginTop: 0 }, text: "Токен доступа (его вводят на других компьютерах):" }),
        h("div", { class: "row", style: { margin: "6px 0" } },
          h("span", { class: "token", text: st.token || "— будет создан при первом запуске —" }),
          st.token ? h("button", { class: "btn sm", text: "Копировать", onclick: function () { M.copy(st.token); } }) : null,
          h("button", { class: "btn sm", text: "Создать новый", onclick: async function () {
            if (!(await M.confirm("Создать новый токен?", "Все подключённые пользователи будут отключены и должны будут войти заново.", "Создать"))) return;
            try { await M.api("/api/lan/token", { method: "POST" }); load(); } catch (e) { M.toast(e.message, true); }
          } })));
      if (st.running) {
        accessCard.appendChild(h("div", { class: "hint", text: "Адрес для других компьютеров:" }));
        st.urls.forEach(function (u) {
          accessCard.appendChild(h("div", { class: "url-line" }, h("code", { text: u }), h("button", { class: "btn sm", text: "Копировать", onclick: function () { M.copy(u); } })));
        });
        if (!st.urls.length) accessCard.appendChild(h("div", { class: "hint", text: "Не удалось определить IP-адрес. Проверьте подключение к сети (ipconfig)." }));
      }
      body.appendChild(accessCard);

      if (st.running) {
        var clients = h("div", { class: "card" }, h("h3", { text: "Подключённые пользователи" }));
        if (!st.clients.length) clients.appendChild(h("div", { class: "hint", style: { marginTop: 0 }, text: "Пока никто не подключался." }));
        else {
          var tbody = h("tbody");
          st.clients.forEach(function (c) {
            tbody.appendChild(h("tr", null,
              h("td", null, h("span", { class: "chip " + (c.online ? "ok" : "") }, h("span", { class: "dot" }), c.online ? "в сети" : "неактивен")),
              h("td", { text: c.name }), h("td", { text: c.ip }), h("td", { text: M.fmtDate(c.last_seen) }),
              h("td", null, h("button", { class: "btn sm danger", text: "Отключить", onclick: async function () {
                try { await M.api("/api/lan/kick", { method: "POST", body: { id: c.id } }); load(); } catch (e) { M.toast(e.message, true); }
              } }))));
          });
          clients.appendChild(h("table", { class: "plain" }, h("thead", null, h("tr", null, h("th", { text: "Статус" }), h("th", { text: "Имя" }), h("th", { text: "IP" }), h("th", { text: "Активность" }), h("th"))), tbody));
        }
        body.appendChild(clients);
      }

      body.appendChild(h("div", { class: "card" }, h("h3", { text: "Безопасность" }),
        h("ul", { class: "hint", style: { margin: "0", paddingLeft: "18px" } },
          h("li", { text: "Пока сервер выключен, порт закрыт: доступ есть только с этого компьютера." }),
          h("li", { text: "Принимаются подключения только из локальных сетей (192.168.x.x, 10.x.x.x, 172.16–31.x.x). Адреса из интернета отклоняются." }),
          h("li", { text: "Вход по токену; после 5 неверных попыток IP блокируется на 5 минут." }),
          h("li", { text: "Другим компьютерам доступны чат и поиск по документам. Настройки, почта и управление сервером — только здесь." }),
          h("li", { text: "При первом запуске Windows может спросить о доступе брандмауэра: разрешите для частных сетей." }),
          h("li", { text: "Связь идёт по http без шифрования — используйте только в доверенной сети." }))));
    }

    load();
    timers.push(setInterval(function () { if (root.isConnected) load(); }, 5000));
    return root;
  }

  /* ---------- вкладка «Данные» ---------- */

  function tabData() {
    var root = panel("Данные и диагностика", "Экспорт и импорт настроек, резервные копии, проверка работы.");

    var file = h("input", { type: "file", accept: ".json,application/json", hidden: true });
    file.addEventListener("change", async function () {
      if (!file.files[0]) return;
      try {
        var data = JSON.parse(await M.readFile(file.files[0]));
        S.settings = await M.api("/api/settings/import", { method: "POST", body: { data: data } });
        afterSettingsChanged();
        M.toast("Настройки импортированы");
        M.showView("settings");
      } catch (e) { M.toast(e.message || "Неверный файл настроек", true); }
      file.value = "";
    });

    root.appendChild(h("div", { class: "card" }, h("h3", { text: "Настройки" }),
      h("div", { class: "row" },
        h("a", { class: "btn", href: "/api/settings/export", download: "molly-settings.json", text: "Экспорт настроек" }),
        h("button", { class: "btn", text: "Импорт настроек…", onclick: function () { file.click(); } }), file),
      h("div", { class: "hint", text: "В экспорт не попадают пароли и токен доступа. Системный prompt входит в настройки (его же можно сохранить в файл на вкладке «МОЛЛИ»)." })));

    var backupsBox = h("div");
    async function loadBackups() {
      try {
        var r = await M.api("/api/settings/backups");
        M.clear(backupsBox);
        if (!r.backups.length) { backupsBox.appendChild(h("div", { class: "hint", text: "Резервных копий пока нет — они создаются автоматически при каждом сохранении настроек." })); return; }
        r.backups.slice(0, 6).forEach(function (b) {
          backupsBox.appendChild(h("div", { class: "row", style: { padding: "5px 0" } },
            h("span", { class: "grow", text: new Date(b.modified * 1000).toLocaleString("ru-RU") }),
            h("button", { class: "btn sm", text: "Восстановить", onclick: async function () {
              if (!(await M.confirm("Восстановить настройки?", "Текущие настройки будут заменены копией от " + new Date(b.modified * 1000).toLocaleString("ru-RU") + ".", "Восстановить"))) return;
              try { S.settings = await M.api("/api/settings/restore", { method: "POST", body: { name: b.name } }); afterSettingsChanged(); M.toast("Настройки восстановлены"); loadBackups(); } catch (e) { M.toast(e.message, true); }
            } })));
        });
      } catch (e) { backupsBox.appendChild(h("div", { class: "msg-error", text: e.message })); }
    }
    root.appendChild(h("div", { class: "card" }, h("h3", { text: "Резервные копии настроек" }), backupsBox,
      h("div", { class: "row", style: { marginTop: "8px" } }, h("button", { class: "btn", text: "Создать копию сейчас", onclick: async function () {
        try { await M.api("/api/settings/backups", { method: "POST" }); M.toast("Копия создана"); loadBackups(); } catch (e) { M.toast(e.message, true); }
      } }))));
    loadBackups();

    var diagBox = h("div");
    var logBox = h("div");
    root.appendChild(h("div", { class: "card" }, h("h3", { text: "Диагностика" }),
      h("div", { class: "row" },
        h("button", { class: "btn primary", text: "Запустить диагностику", onclick: async function (e) {
          var btn = e.currentTarget; btn.disabled = true;
          M.clear(diagBox); diagBox.appendChild(h("span", { class: "spinner" }));
          try {
            var r = await M.api("/api/diagnostics");
            M.clear(diagBox);
            diagBox.appendChild(h("div", { class: "chip " + (r.ok ? "ok" : "warn"), style: { marginBottom: "8px" }, text: r.ok ? "Всё в порядке" : "Есть замечания" }));
            r.checks.forEach(function (c) {
              diagBox.appendChild(h("div", { class: "diag-row" }, h("div", { class: "ic", text: c.ok ? "✅" : "⚠️" }),
                h("div", null, h("div", { class: "nm", text: c.name }), c.detail ? h("div", { class: "dt", text: c.detail }) : null,
                  !c.ok && c.hint ? h("div", { class: "dt", style: { color: "var(--warn)" }, text: c.hint }) : null)));
            });
          } catch (err) { M.clear(diagBox); diagBox.appendChild(h("div", { class: "msg-error", text: err.message })); }
          finally { btn.disabled = false; }
        } }),
        h("button", { class: "btn", text: "Показать журнал", onclick: async function () {
          M.clear(logBox);
          try {
            var r = await M.api("/api/logs?lines=300");
            logBox.appendChild(h("div", { class: "hint", text: r.path }));
            logBox.appendChild(h("div", { class: "logbox", text: r.text || "(журнал пуст)" }));
            logBox.appendChild(h("div", { class: "row", style: { marginTop: "8px" } }, h("button", { class: "btn sm", text: "Копировать журнал", onclick: function () { M.copy(r.text); } })));
          } catch (err) { logBox.appendChild(h("div", { class: "msg-error", text: err.message })); }
        } })),
      diagBox, logBox));

    root.appendChild(h("div", { class: "card" }, h("h3", { text: "Мастер первоначальной настройки" }),
      h("div", { class: "row" }, h("button", { class: "btn", text: "Запустить мастер снова", onclick: function () { M.runWizard(); } })),
      h("div", { class: "hint", text: "Пошаговая проверка Ollama, выбор модели и папки, имя и стиль." })));
    return root;
  }

  /* ---------- вид ---------- */

  M.views.settings = {
    render: async function (view) {
      M.clearTimers();
      M.clear(view);
      var scroller = h("div", { class: "scroll" });
      view.appendChild(scroller);
      try {
        S.settings = await M.api("/api/settings");
      } catch (e) {
        scroller.appendChild(h("div", { class: "center-empty" }, h("h2", { text: "Настройки недоступны" }), h("p", { text: e.message })));
        return;
      }

      var body = h("div", { class: "settings-body" });
      var nav = h("div", { class: "settings-nav", role: "tablist" });
      var builders = { molly: tabMolly, model: tabModel, docs: tabDocs, mail: tabMail, lan: tabLan, data: tabData };

      function show(tab) {
        currentTab = tab;
        M.clearTimers();
        M.hooks.onIndex = null;
        Array.prototype.forEach.call(nav.children, function (b) { b.classList.toggle("active", b.dataset.tab === tab); });
        M.clear(body);
        body.appendChild(builders[tab]());
      }

      TABS.forEach(function (t) {
        nav.appendChild(h("button", { dataset: { tab: t[0] }, text: t[1], role: "tab", onclick: function () { show(t[0]); } }));
      });

      scroller.appendChild(h("div", { class: "settings" }, nav, body));
      show(currentTab);
    },
  };
})();
