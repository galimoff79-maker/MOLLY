/* МОЛЛИ — чат. */
(function () {
  "use strict";
  var M = window.Molly, S = M.S, h = M.h;

  var els = {};            // ссылки на элементы текущего вида
  var stick = true;        // автопрокрутка вниз, пока пользователь не прокрутил вверх
  var renderQueued = false;

  var SUGGESTIONS = [
    "Найди акт скрытых работ по бетонированию плиты",
    "Какие документы есть по разделу КЖ?",
    "Что написано в журнале бетонных работ про температуру?",
    "Помоги составить ответ на замечание технадзора",
  ];

  /* ---------- список чатов (боковая панель) ---------- */

  M.loadChats = async function () {
    try {
      S.chats = (await M.api("/api/chats")).chats || [];
    } catch (e) {
      if (e.status !== 401) M.toast(e.message, true);
    }
    M.renderChatList();
  };

  function groupName(iso) {
    var d = new Date(iso), now = new Date();
    var day = 86400000;
    var diff = new Date(now.toDateString()) - new Date(d.toDateString());
    if (diff <= 0) return "Сегодня";
    if (diff <= day) return "Вчера";
    if (diff <= 7 * day) return "Последние 7 дней";
    return "Ранее";
  }

  M.renderChatList = function () {
    var box = M.$("#chat-list");
    if (!box) return;
    M.clear(box);
    if (!S.chats.length) {
      box.appendChild(h("div", { class: "empty-note", text: "Здесь появятся ваши чаты." }));
      return;
    }
    var last = null;
    S.chats.forEach(function (c) {
      var g = groupName(c.updated_at);
      if (g !== last) { box.appendChild(h("div", { class: "group", text: g })); last = g; }
      box.appendChild(h("div", {
        class: "chat-item" + (c.id === S.chatId && S.view === "chat" ? " active" : ""),
        title: c.title,
        onclick: function () { M.showView("chat"); M.openChat(c.id); M.closeSidebar(); },
      },
        h("span", { class: "t", text: c.title }),
        h("button", { class: "x", title: "Удалить чат", "aria-label": "Удалить чат", text: "✕", onclick: function (e) {
          e.stopPropagation();
          deleteChat(c);
        } })));
    });
  };

  async function deleteChat(c) {
    var ok = await M.confirm("Удалить чат?", "«" + c.title + "» будет удалён без возможности восстановления.", "Удалить", true);
    if (!ok) return;
    try {
      await M.api("/api/chats/" + c.id, { method: "DELETE" });
      if (S.chatId === c.id) M.newChat();
      await M.loadChats();
    } catch (e) { M.toast(e.message, true); }
  }

  /* ---------- открытие / новый чат ---------- */

  M.newChat = function () {
    if (S.streaming) return;
    S.chatId = null;
    S.messages = [];
    M.renderChatList();
    if (S.view === "chat") renderMessages();
  };

  M.openChat = async function (id) {
    if (S.streaming) return;
    try {
      var chat = await M.api("/api/chats/" + id);
      S.chatId = chat.id;
      S.messages = chat.messages.map(function (m) {
        return { role: m.role, content: m.content, sources: m.sources || [], stopped: m.stopped };
      });
      M.renderChatList();
      renderMessages();
      scrollDown(true);
    } catch (e) { M.toast(e.message, true); }
  };

  /* ---------- вид ---------- */

  M.views.chat = {
    render: function (view) {
      M.clear(view);
      els = {};
      els.banners = h("div", { id: "banners" });
      els.messages = h("div", { class: "messages" });
      els.inner = h("div", { class: "messages-inner" });
      els.messages.appendChild(els.inner);
      els.messages.addEventListener("scroll", function () {
        stick = els.messages.scrollHeight - els.messages.scrollTop - els.messages.clientHeight < 90;
      });

      els.input = h("textarea", {
        rows: 1, placeholder: "Напишите сообщение… (Enter — отправить, Shift+Enter — новая строка)",
        "aria-label": "Сообщение",
      });
      els.input.addEventListener("input", autosize);
      els.input.addEventListener("keydown", function (e) {
        if (e.key === "Enter" && !e.shiftKey && !e.isComposing && e.keyCode !== 229) {
          e.preventDefault();
          submit();
        }
      });

      els.docs = h("button", { class: "toggle-docs", title: "Использовать документы рабочей папки в ответе", onclick: function () {
        S.useDocs = !S.useDocs;
        updateComposer();
      } });
      els.send = h("button", { class: "send-btn", "aria-label": "Отправить", onclick: function () {
        if (S.streaming) stop(); else submit();
      } });

      view.appendChild(els.banners);
      view.appendChild(els.messages);
      view.appendChild(h("div", { class: "composer-wrap" },
        h("div", { class: "composer" }, els.docs, els.input, els.send),
        h("div", { class: "hint-line", text: "МОЛЛИ может ошибаться. Проверяйте важные данные по исходным документам." })));

      updateComposer();
      renderMessages();
      M.refreshBanners();
      scrollDown(true);
      setTimeout(function () { els.input.focus(); }, 50);
    },
  };

  function autosize() {
    els.input.style.height = "auto";
    els.input.style.height = Math.min(els.input.scrollHeight, 200) + "px";
    updateComposer();
  }

  function updateComposer() {
    if (!els.send) return;
    var text = els.input.value.trim();
    if (S.streaming) {
      els.send.className = "send-btn stop";
      els.send.textContent = "■";
      els.send.title = "Остановить генерацию";
      els.send.setAttribute("aria-label", "Остановить генерацию");
      els.send.disabled = false;
    } else {
      els.send.className = "send-btn";
      els.send.textContent = "↑";
      els.send.title = "Отправить";
      els.send.setAttribute("aria-label", "Отправить");
      els.send.disabled = !text;
    }
    var hasProject = !!(S.project && S.project.path);
    els.docs.className = "toggle-docs" + (S.useDocs && hasProject ? " on" : "");
    els.docs.textContent = "📄 Документы" + (S.useDocs && hasProject ? ": вкл" : ": выкл");
    els.docs.disabled = !hasProject;
    els.docs.title = hasProject ? "Использовать документы рабочей папки в ответе" : "Рабочая папка не выбрана";
  }
  M.updateComposer = updateComposer;

  /* ---------- баннеры ---------- */

  M.refreshBanners = function () {
    if (!els.banners) return;
    M.clear(els.banners);
    var isLocal = S.cfg && S.cfg.is_local;
    var o = S.ollama;

    if (isLocal && o && !o.ok) {
      els.banners.appendChild(h("div", { class: "banner bad" },
        h("div", { class: "grow", text: o.error || "Ollama недоступна." }),
        h("button", { class: "btn sm", text: "Проверить снова", onclick: function () { M.checkOllama(true); } }),
        h("button", { class: "btn sm", text: "Настройки модели", onclick: function () { M.openSettings("model"); } })));
    } else if (isLocal && o && o.ok && S.settings && S.settings.model && !o.model_present) {
      els.banners.appendChild(h("div", { class: "banner warn" },
        h("div", { class: "grow", text: "Модель «" + S.settings.model + "» не найдена в Ollama. Выберите другую в верхней панели или загрузите: ollama pull " + S.settings.model }),
        h("button", { class: "btn sm", text: "Настройки модели", onclick: function () { M.openSettings("model"); } })));
    }

    if (isLocal && !(S.project && S.project.path)) {
      els.banners.appendChild(h("div", { class: "banner info" },
        h("div", { class: "grow", text: "Рабочая папка не выбрана — МОЛЛИ отвечает без документов." }),
        h("button", { class: "btn sm", text: "Выбрать папку", onclick: function () { M.openSettings("docs"); } })));
    }
  };

  /* ---------- сообщения ---------- */

  function renderMessages() {
    if (!els.inner) return;
    M.clear(els.inner);

    if (!S.messages.length) {
      var name = (S.cfg && S.cfg.assistant_name) || "МОЛЛИ";
      var user = S.cfg && S.cfg.user;
      els.inner.appendChild(h("div", { class: "welcome" },
        h("div", { class: "logo", style: { margin: "0 auto", width: "56px", height: "56px", fontSize: "1.6rem", borderRadius: "16px" }, text: "М" }),
        h("h1", { text: user ? "Здравствуйте, " + user + "!" : "Здравствуйте!" }),
        h("p", { text: "Я " + name + ", AI-помощник ПТО. Ищу информацию в документах вашей рабочей папки, помогаю с актами, письмами и ответами на замечания." }),
        h("div", { class: "suggest" }, SUGGESTIONS.map(function (s) {
          return h("button", { text: s, onclick: function () {
            els.input.value = s; autosize(); els.input.focus();
          } });
        }))));
      return;
    }

    S.messages.forEach(function (m, i) { els.inner.appendChild(buildMessage(m, i)); });
    if (S.streaming) S.streamNode = els.inner.lastChild;
  }

  function buildMessage(m, index) {
    var name = (S.cfg && S.cfg.assistant_name) || "МОЛЛИ";

    if (m.role === "user") {
      return h("div", { class: "msg user" }, h("div", { class: "bubble", text: m.content }));
    }

    var content = h("div", { class: "md" });
    var bubble = h("div", { class: "bubble" },
      h("div", { class: "who", text: name }), content);
    var node = h("div", { class: "msg assistant" }, h("div", { class: "avatar", text: "М" }), bubble);
    node._parts = { bubble: bubble, content: content, index: index };
    paintAssistant(node, m);
    return node;
  }

  function paintAssistant(node, m) {
    var p = node._parts;
    // Содержимое и хвосты пересобираем целиком — сообщение короткое.
    M.clear(p.content);
    if (m.content) {
      p.content.innerHTML = window.MollyMd.render(m.content);
    } else if (!m.error && !m.stopped) {
      p.content.appendChild(h("div", { class: "typing", "aria-label": "МОЛЛИ печатает" }, h("i"), h("i"), h("i")));
    }

    Array.prototype.slice.call(p.bubble.querySelectorAll(".sources,.msg-error,.msg-note,.msg-actions"))
      .forEach(function (e) { e.remove(); });

    if (m.sources && m.sources.length) p.bubble.appendChild(buildSources(m.sources));

    if (m.error) {
      var err = h("div", { class: "msg-error" }, m.error);
      if (m.retryText) {
        err.appendChild(h("div", { style: { marginTop: "8px" } },
          h("button", { class: "btn sm", text: "Повторить", onclick: function () { retry(p.index); } })));
      }
      p.bubble.appendChild(err);
    }
    if (m.stopped && !m.error) {
      p.bubble.appendChild(h("div", { class: "msg-note", text: "Генерация остановлена." }));
    }
    if (m.content && !(S.streaming && p.index === S.messages.length - 1)) {
      p.bubble.appendChild(h("div", { class: "msg-actions" },
        h("button", { class: "btn ghost sm", text: "Копировать", onclick: function () { M.copy(m.content); } })));
    }

    // Клик по ссылке [n] открывает источник.
    Array.prototype.forEach.call(p.content.querySelectorAll("sup.cite"), function (sup) {
      sup.addEventListener("click", function () {
        var det = p.bubble.querySelector("details.sources");
        if (!det) return;
        det.open = true;
        var target = det.querySelector('[data-n="' + sup.dataset.n + '"]');
        if (target) {
          target.scrollIntoView({ block: "nearest", behavior: "smooth" });
          target.classList.add("flash");
          setTimeout(function () { target.classList.remove("flash"); }, 1400);
        }
      });
    });
  }

  function buildSources(sources) {
    var det = h("details", { class: "sources" }, h("summary", { text: "Источники (" + sources.length + ")" }));
    sources.forEach(function (s) {
      det.appendChild(h("div", { class: "src", dataset: { n: s.n } },
        h("div", { class: "src-head" },
          h("span", { class: "src-n", text: "[" + s.n + "]" }),
          h("span", { class: "src-name", text: s.filename }),
          s.location ? h("span", { class: "src-loc", text: s.location }) : null,
          s.section ? h("span", { class: "src-loc", text: "раздел " + s.section }) : null),
        h("div", { class: "src-path", text: s.path }),
        s.fragment ? h("div", { class: "src-frag", text: s.fragment }) : null,
        h("div", { class: "row", style: { gap: "6px" } },
          h("button", { class: "btn sm", text: "Открыть текст документа", onclick: function () { openDocument(s); } }),
          h("button", { class: "btn ghost sm", text: "Копировать путь", onclick: function () { M.copy(s.path); } }))));
    });
    return det;
  }

  async function openDocument(s) {
    var body = h("div", null, h("div", { class: "hint", text: "Загрузка…" }));
    var dlg = M.modal({ title: s.filename, body: body, wide: true, buttons: [{ label: "Закрыть" }] });
    try {
      var d = (await M.api("/api/file/" + s.document_id)).document;
      var text = d.text || "";
      var LIMIT = 80000;
      M.clear(body);
      body.appendChild(h("div", { class: "hint", style: { marginBottom: "8px", overflowWrap: "anywhere" } }, s.path));
      if (d.page_count) body.appendChild(h("div", { class: "hint", text: "Страниц: " + d.page_count }));
      body.appendChild(h("div", { class: "docview", text: text.length > LIMIT ? text.slice(0, LIMIT) + "\n\n… (показано первых " + M.fmtNum(LIMIT) + " символов из " + M.fmtNum(text.length) + ")" : (text || "(текст не извлечён)") }));
    } catch (e) {
      M.clear(body);
      body.appendChild(h("div", { class: "msg-error", text: e.message }));
    }
    return dlg;
  }

  /* ---------- отправка ---------- */

  function submit() {
    var text = els.input.value.trim();
    if (!text || S.streaming) return;
    els.input.value = "";
    autosize();
    send(text, false);
  }

  function retry(index) {
    var m = S.messages[index];
    if (!m || !m.retryText || S.streaming) return;
    // Убираем сообщение об ошибке и пользовательское сообщение перед ним — они будут созданы заново.
    S.messages.splice(index - 1, 2);
    renderMessages();
    send(m.retryText, true);
  }

  function stop() { if (S.abort) S.abort.abort(); }

  function scrollDown(force) {
    if (!els.messages) return;
    if (force || stick) els.messages.scrollTop = els.messages.scrollHeight;
  }

  function scheduleRender(node, msg) {
    if (renderQueued) return;
    renderQueued = true;
    requestAnimationFrame(function () {
      renderQueued = false;
      paintAssistant(S.streamNode || node, msg);
      scrollDown(false);
    });
  }

  async function send(text, isRetry) {
    if (S.streaming) return;
    S.streaming = true;
    stick = true;

    S.messages.push({ role: "user", content: text });
    var asst = { role: "assistant", content: "", sources: [], stopped: false, error: null };
    S.messages.push(asst);
    renderMessages();
    S.streamNode = els.inner.lastChild;
    scrollDown(true);
    updateComposer();

    var ctrl = new AbortController();
    S.abort = ctrl;

    try {
      var resp = await fetch("/api/chat/stream", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        signal: ctrl.signal,
        body: JSON.stringify({
          chat_id: S.chatId,
          message: text,
          use_documents: !!(S.useDocs && S.project && S.project.path),
          retry: !!isRetry,
        }),
      });

      if (!resp.ok) {
        var data = null;
        try { data = await resp.json(); } catch (e) { data = null; }
        var msg = M.errorText(data, resp.status);
        if (resp.status === 401 && M.hooks.loginRequired) M.hooks.loginRequired();
        throw new Error(msg);
      }

      var reader = resp.body.getReader();
      var decoder = new TextDecoder("utf-8");
      var buf = "";

      for (;;) {
        var chunk = await reader.read();
        if (chunk.done) break;
        buf += decoder.decode(chunk.value, { stream: true });
        var idx;
        while ((idx = buf.indexOf("\n")) >= 0) {
          var line = buf.slice(0, idx).trim();
          buf = buf.slice(idx + 1);
          if (!line) continue;
          var ev;
          try { ev = JSON.parse(line); } catch (e) { continue; }
          handleEvent(ev, asst);
        }
      }
    } catch (e) {
      if (e.name === "AbortError") {
        asst.stopped = true;
      } else {
        asst.error = e.message || "Не удалось получить ответ.";
        asst.retryText = text;
      }
    } finally {
      S.streaming = false;
      S.abort = null;
      if (S.streamNode) paintAssistant(S.streamNode, asst);
      S.streamNode = null;
      updateComposer();
      M.loadChats();
      if (els.input) els.input.focus();
    }
  }

  function handleEvent(ev, asst) {
    var node = S.streamNode;
    switch (ev.type) {
      case "meta":
        if (S.chatId == null) S.chatId = ev.chat_id;
        break;
      case "sources":
        asst.sources = ev.sources || [];
        if (node) scheduleRender(node, asst);
        break;
      case "token":
        asst.content += ev.text;
        if (node) scheduleRender(node, asst);
        break;
      case "model":
        // автоматическая смена модели FreeLLMAPI — ненавязчивое уведомление
        if (ev.fallback) {
          M.toast("Модель временно недоступна. Автоматически подключена: " + (ev.model || "?"), false);
        }
        break;
      case "error":
        asst.error = ev.message;
        asst.retryText = S.messages[S.messages.length - 2] && S.messages[S.messages.length - 2].content;
        if (ev.code === "ollama_unavailable" || ev.code === "model_not_found") M.checkOllama(true);
        if (ev.code === "online_unavailable" || ev.code === "online_models_exhausted") M.checkFreellm(false);
        break;
      case "done":
        asst.stopped = !!ev.stopped;
        break;
    }
  }

  // Подготовить новый чат с готовым текстом (используется почтой).
  M.startChatWith = function (text) {
    M.showView("chat");
    M.newChat();
    setTimeout(function () {
      if (els.input) { els.input.value = text; autosize(); els.input.focus(); }
    }, 30);
  };
})();
