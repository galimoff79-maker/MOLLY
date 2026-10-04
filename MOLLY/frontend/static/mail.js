/* МОЛЛИ — почта. */
(function () {
  "use strict";
  var M = window.Molly, S = M.S, h = M.h;

  var st = null;        // состояние вида
  var MAX_FILE = 10 * 1024 * 1024;
  var MAX_TOTAL = 25 * 1024 * 1024;

  function addrOnly(value) {
    var m = String(value || "").match(/<([^>]+)>/);
    return (m ? m[1] : String(value || "")).trim();
  }

  M.views.mail = {
    render: async function (view) {
      M.clear(view);
      var scroller = h("div", { class: "scroll" }, h("div", { class: "center-empty" }, h("span", { class: "spinner" })));
      view.appendChild(scroller);

      var status;
      try { status = await M.api("/api/mail/status"); }
      catch (e) { M.clear(scroller); scroller.appendChild(h("div", { class: "center-empty" }, h("h2", { text: "Почта недоступна" }), h("p", { text: e.message }))); return; }

      if (!status.email || !status.has_password) {
        M.clear(scroller);
        scroller.appendChild(h("div", { class: "center-empty" },
          h("div", { style: { fontSize: "2.6rem" }, text: "✉️" }),
          h("h2", { text: "Подключите Яндекс.Почту" }),
          h("p", { text: "Укажите адрес и пароль приложения — МОЛЛИ покажет письма, найдёт нужное и поможет составить ответ. Отправка — только после вашего подтверждения." }),
          h("button", { class: "btn primary", text: "Подключить почту", onclick: function () { M.openSettings("mail"); } })));
        return;
      }

      st = { folders: [], folder: null, page: 1, pages: 1, total: 0, q: "", messages: [], uid: null, confirmSend: status.confirm_send, email: status.email };

      M.clear(view);
      var root = h("div", { class: "mail" });
      st.elFolders = h("div", { class: "mail-col mail-folders" });
      st.elList = h("div", { class: "mail-col mail-list" });
      st.elRead = h("div", { class: "mail-col mail-reader" });
      st.root = root;
      root.appendChild(st.elFolders);
      root.appendChild(st.elList);
      root.appendChild(st.elRead);
      view.appendChild(root);

      renderReaderEmpty();
      renderList();

      try {
        st.folders = (await M.api("/api/mail/folders")).folders;
        var inbox = st.folders.filter(function (f) { return f.name === "INBOX"; })[0] || st.folders[0];
        st.folder = inbox ? inbox.raw : "INBOX";
        renderFolders();
        loadMessages();
      } catch (e) {
        showListError(e.message);
      }
    },
  };

  function renderFolders() {
    M.clear(st.elFolders);
    st.folders.forEach(function (f) {
      st.elFolders.appendChild(h("button", { class: "folder" + (f.raw === st.folder ? " active" : ""), text: f.title, onclick: function () {
        st.folder = f.raw; st.page = 1; st.uid = null; renderFolders(); renderReaderEmpty(); st.root.classList.remove("reading"); loadMessages();
      } }));
    });
  }

  function renderReaderEmpty() {
    M.clear(st.elRead);
    st.elRead.appendChild(h("div", { class: "center-empty" }, h("p", { text: "Выберите письмо слева." })));
  }

  function showListError(msg) {
    M.clear(st.elList);
    st.elList.appendChild(toolbar());
    st.elList.appendChild(h("div", { style: { padding: "14px" } },
      h("div", { class: "msg-error", text: msg }),
      h("div", { style: { marginTop: "10px" } }, h("button", { class: "btn sm", text: "Повторить", onclick: function () { loadMessages(); } }))));
  }

  function toolbar() {
    var search = h("input", { class: "input", type: "search", placeholder: "Поиск писем…", value: st.q, "aria-label": "Поиск писем" });
    search.addEventListener("keydown", function (e) {
      if (e.key === "Enter") { st.q = search.value.trim(); st.page = 1; loadMessages(); }
    });
    return h("div", { class: "mail-tools" }, search,
      h("button", { class: "btn icon", title: "Обновить", "aria-label": "Обновить", text: "⟳", onclick: function () { loadMessages(); } }),
      h("button", { class: "btn primary", text: "Написать", onclick: function () { compose({}); } }));
  }

  function renderList() {
    M.clear(st.elList);
    st.elList.appendChild(toolbar());
    if (st.loading) {
      st.elList.appendChild(h("div", { class: "center-empty" }, h("span", { class: "spinner" })));
      return;
    }
    if (!st.messages.length) {
      st.elList.appendChild(h("div", { class: "center-empty", style: { padding: "30px 14px" } }, h("p", { text: st.q ? "Ничего не найдено." : "В этой папке нет писем." })));
      return;
    }
    st.messages.forEach(function (m) {
      st.elList.appendChild(h("div", { class: "mail-item" + (m.unread ? " unread" : "") + (m.uid === st.uid ? " active" : ""), onclick: function () { openMessage(m.uid); } },
        h("div", { class: "from", text: (m.from || "").replace(/<.*>/, "").trim() || m.from }),
        h("div", { class: "subj", text: m.subject }),
        h("div", { class: "date", text: formatMailDate(m.date) })));
    });
    st.elList.appendChild(h("div", { class: "pager" },
      h("button", { class: "btn sm", text: "←", disabled: st.page <= 1, onclick: function () { st.page--; loadMessages(); } }),
      h("span", { text: "Стр. " + st.page + " из " + st.pages + " · писем: " + st.total }),
      h("button", { class: "btn sm", text: "→", disabled: st.page >= st.pages, onclick: function () { st.page++; loadMessages(); } })));
  }

  function formatMailDate(s) {
    var d = new Date(s);
    if (isNaN(d.getTime())) return s || "";
    return d.toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", year: "2-digit", hour: "2-digit", minute: "2-digit" });
  }

  async function loadMessages() {
    st.loading = true;
    renderList();
    try {
      var r = await M.api("/api/mail/messages?folder=" + encodeURIComponent(st.folder) + "&page=" + st.page + (st.q ? "&q=" + encodeURIComponent(st.q) : ""));
      st.messages = r.messages; st.pages = r.pages; st.total = r.total; st.page = r.page;
      st.loading = false;
      renderList();
    } catch (e) {
      st.loading = false;
      showListError(e.message);
    }
  }

  async function openMessage(uid) {
    st.uid = uid;
    st.root.classList.add("reading");
    renderList();
    M.clear(st.elRead);
    st.elRead.appendChild(h("div", { class: "center-empty" }, h("span", { class: "spinner" })));
    try {
      var m = await M.api("/api/mail/message?folder=" + encodeURIComponent(st.folder) + "&uid=" + encodeURIComponent(uid));
      renderMessage(m);
    } catch (e) {
      M.clear(st.elRead);
      st.elRead.appendChild(h("div", { class: "mail-read" }, h("div", { class: "msg-error", text: e.message })));
    }
  }

  function renderMessage(m) {
    M.clear(st.elRead);
    var moveSel = h("select", { class: "input model-select", "aria-label": "Переместить в папку" },
      h("option", { value: "", text: "Переместить в…" }),
      st.folders.filter(function (f) { return f.raw !== st.folder; }).map(function (f) { return h("option", { value: f.raw, text: f.title }); }));
    moveSel.addEventListener("change", async function () {
      if (!moveSel.value) return;
      try {
        await M.api("/api/mail/move", { method: "POST", body: { folder: st.folder, uid: m.uid, target: moveSel.value } });
        M.toast("Письмо перемещено");
        st.uid = null; renderReaderEmpty(); st.root.classList.remove("reading"); loadMessages();
      } catch (e) { M.toast(e.message, true); moveSel.value = ""; }
    });

    var atts = h("div", { style: { margin: "0 0 12px" } });
    m.attachments.forEach(function (a) {
      atts.appendChild(h("a", { class: "att", href: "/api/mail/attachment?folder=" + encodeURIComponent(st.folder) + "&uid=" + encodeURIComponent(m.uid) + "&index=" + a.index, download: a.name },
        "📎 " + a.name + " (" + M.fmtSize(a.size) + ")"));
    });

    st.elRead.appendChild(h("div", { class: "mail-read" },
      h("button", { class: "btn sm ghost menu-back", text: "← К списку", style: { display: window.innerWidth <= 860 ? "inline-flex" : "none", marginBottom: "8px" }, onclick: function () { st.root.classList.remove("reading"); } }),
      h("h2", { text: m.subject }),
      h("div", { class: "mail-meta" },
        h("div", { text: "От: " + m.from }), h("div", { text: "Кому: " + m.to }),
        m.cc ? h("div", { text: "Копия: " + m.cc }) : null, h("div", { text: "Дата: " + formatMailDate(m.date) })),
      h("div", { class: "row", style: { marginBottom: "14px" } },
        h("button", { class: "btn primary", text: "Ответить", onclick: function () { reply(m); } }),
        h("button", { class: "btn", text: "Спросить МОЛЛИ", title: "Открыть новый чат с текстом этого письма", onclick: function () { askMolly(m); } }),
        moveSel),
      atts,
      h("div", { class: "mail-body", text: m.body || "(пустое письмо)" })));
  }

  function askMolly(m) {
    var text = (m.body || "").slice(0, 6000);
    M.startChatWith(
      "Вот письмо, которое я получил.\n\nОт: " + m.from + "\nТема: " + m.subject + "\nДата: " + m.date + "\n\n" + text +
      "\n\n---\nКратко перескажи, что от меня требуется, и предложи вариант ответа.");
  }

  function reply(m) {
    var quoted = (m.body || "").slice(0, 4000).split("\n").map(function (l) { return "> " + l; }).join("\n");
    var subj = /^re:/i.test(m.subject) ? m.subject : "Re: " + m.subject;
    compose({
      to: addrOnly(m.reply_to || m.from), subject: subj,
      body: "\n\n" + formatMailDate(m.date) + ", " + m.from + " писал(а):\n" + quoted,
      replyFolder: st.folder, replyUid: m.uid,
    });
  }

  /* ---------- создание письма ---------- */

  function compose(init) {
    var to = M.input(init.to || "", { placeholder: "Кому (несколько адресов — через запятую)" });
    var cc = M.input("", { placeholder: "Копия (необязательно)" });
    var subject = M.input(init.subject || "", { placeholder: "Тема" });
    var body = h("textarea", { class: "input", rows: 12 });
    body.value = init.body || "";
    var files = [];
    var fileList = h("div", { class: "hint" });
    var picker = h("input", { type: "file", multiple: true, hidden: true });

    function paintFiles() {
      M.clear(fileList);
      files.forEach(function (f, i) {
        fileList.appendChild(h("div", { class: "row", style: { gap: "6px", margin: "3px 0" } },
          h("span", { text: "📎 " + f.name + " (" + M.fmtSize(f.size) + ")" }),
          h("button", { class: "btn ghost sm", text: "✕", "aria-label": "Убрать вложение", onclick: function () { files.splice(i, 1); paintFiles(); } })));
      });
    }
    picker.addEventListener("change", function () {
      Array.prototype.forEach.call(picker.files, function (f) {
        var total = files.reduce(function (a, x) { return a + x.size; }, 0) + f.size;
        if (f.size > MAX_FILE) M.toast("«" + f.name + "» больше 10 МБ", true);
        else if (total > MAX_TOTAL) M.toast("Общий размер вложений не должен превышать 25 МБ", true);
        else files.push(f);
      });
      picker.value = "";
      paintFiles();
    });

    var dlg = M.modal({
      title: init.replyUid ? "Ответ" : "Новое письмо",
      wide: true,
      body: h("div", null, M.field("", to), M.field("", cc), M.field("", subject), body,
        h("div", { class: "row", style: { marginTop: "10px" } }, h("button", { class: "btn sm", text: "📎 Добавить вложение", onclick: function () { picker.click(); } }), picker),
        fileList,
        h("div", { class: "hint", text: "Письмо не будет отправлено, пока вы не подтвердите это на следующем шаге." })),
      buttons: [
        { label: "Отмена" },
        { label: "Далее", primary: true, onClick: async function () {
          var attachments = [];
          for (var i = 0; i < files.length; i++) {
            attachments.push({ name: files[i].name, type: files[i].type || "application/octet-stream", content_b64: await M.fileToBase64(files[i]) });
          }
          var draft = await M.api("/api/mail/drafts", { method: "POST", body: {
            to: to.value, cc: cc.value, subject: subject.value, body: body.value, attachments: attachments,
            reply_folder: init.replyFolder || null, reply_uid: init.replyUid || null,
          } });
          dlg.close();
          preview(draft, init);
          return false;
        } },
      ],
    });
    return dlg;
  }

  function preview(draft, init) {
    function row(label, value) {
      return h("div", { style: { display: "flex", gap: "10px", margin: "4px 0" } },
        h("div", { class: "status-text", style: { width: "70px", flex: "none" }, text: label }), h("div", { style: { overflowWrap: "anywhere" }, text: value }));
    }
    var dlg = M.modal({
      title: "Проверьте письмо перед отправкой",
      wide: true,
      body: h("div", null,
        row("Кому", draft.to.join(", ")), draft.cc.length ? row("Копия", draft.cc.join(", ")) : null, row("Тема", draft.subject),
        draft.attachments.length ? row("Вложения", draft.attachments.map(function (a) { return a.name + " (" + M.fmtSize(a.size) + ")"; }).join(", ")) : null,
        h("div", { class: "mail-body", style: { marginTop: "10px", maxHeight: "40vh", overflowY: "auto" }, text: draft.body || "(пустое письмо)" })),
      buttons: [
        { label: "Не отправлять", onClick: async function () { try { await M.api("/api/mail/drafts/" + draft.id, { method: "DELETE" }); } catch (e) { /* не важно */ } } },
        { label: "← Изменить", onClick: function () {
          M.api("/api/mail/drafts/" + draft.id, { method: "DELETE" }).catch(function () {});
          setTimeout(function () { compose({ to: draft.to.join(", "), subject: draft.subject, body: draft.body, replyFolder: init.replyFolder, replyUid: init.replyUid }); }, 0);
        } },
        { label: "Отправить", primary: true, onClick: async function () {
          await M.api("/api/mail/drafts/" + draft.id + "/send", { method: "POST", body: { confirmed: true } });
          M.toast("Письмо отправлено");
        } },
      ],
    });
    return dlg;
  }
})();
