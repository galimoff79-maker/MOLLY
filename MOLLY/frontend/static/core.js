/* МОЛЛИ — ядро интерфейса: состояние, DOM-хелперы, API-клиент, окна. */
(function () {
  "use strict";

  var M = (window.Molly = {
    S: {
      cfg: null,          // /api/ui-config
      settings: null,     // /api/settings (только на основном ПК)
      chats: [],
      chatId: null,
      messages: [],
      view: "chat",
      streaming: false,
      abort: null,
      useDocs: true,
      ollama: null,       // результат /api/ollama/status
      index: null,        // результат /api/index/status
      project: null,
    },
    views: {},
    hooks: {},
  });

  /* ---------- DOM ---------- */

  // h("div", {class:"x", onclick:fn}, "текст", [дети], node)
  M.h = function (tag, attrs) {
    var el = document.createElement(tag);
    var i, k, v;
    if (attrs && typeof attrs === "object" && !(attrs instanceof Node) && !Array.isArray(attrs)) {
      for (k in attrs) {
        v = attrs[k];
        if (v === null || v === undefined || v === false) continue;
        if (k === "class") el.className = v;
        else if (k === "text") el.textContent = v;
        else if (k === "dataset") Object.keys(v).forEach(function (d) { el.dataset[d] = v[d]; });
        else if (k === "style" && typeof v === "object") Object.assign(el.style, v);
        else if (k.slice(0, 2) === "on" && typeof v === "function") el.addEventListener(k.slice(2).toLowerCase(), v);
        else if (k === "value" || k === "checked" || k === "disabled" || k === "selected" || k === "indeterminate") el[k] = v;
        else if (v === true) el.setAttribute(k, "");
        else el.setAttribute(k, String(v));
      }
      i = 2;
    } else {
      i = 1;
    }
    for (; i < arguments.length; i++) M.append(el, arguments[i]);
    return el;
  };

  M.append = function (el, child) {
    if (child === null || child === undefined || child === false) return;
    if (Array.isArray(child)) { child.forEach(function (c) { M.append(el, c); }); return; }
    if (child instanceof Node) el.appendChild(child);
    else el.appendChild(document.createTextNode(String(child)));
  };

  M.clear = function (el) { while (el.firstChild) el.removeChild(el.firstChild); return el; };
  M.$ = function (sel, root) { return (root || document).querySelector(sel); };

  /* ---------- форматирование ---------- */

  M.fmtSize = function (n) {
    n = Number(n) || 0;
    if (n < 1024) return n + " Б";
    if (n < 1048576) return (n / 1024).toFixed(1) + " КБ";
    if (n < 1073741824) return (n / 1048576).toFixed(1) + " МБ";
    return (n / 1073741824).toFixed(2) + " ГБ";
  };

  M.fmtDate = function (value) {
    if (!value) return "";
    var d = typeof value === "number" ? new Date(value * 1000) : new Date(value);
    if (isNaN(d.getTime())) return String(value);
    var now = new Date();
    var sameDay = d.toDateString() === now.toDateString();
    return sameDay
      ? d.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" })
      : d.toLocaleDateString("ru-RU", { day: "2-digit", month: "2-digit", year: "numeric" });
  };

  M.fmtNum = function (n) { return Number(n || 0).toLocaleString("ru-RU"); };

  /* ---------- тема ---------- */

  M.applyTheme = function (theme) {
    var root = document.documentElement;
    try {
      if (theme === "light" || theme === "dark") {
        root.dataset.theme = theme;
        localStorage.setItem("molly_theme", theme);
      } else {
        delete root.dataset.theme;
        localStorage.removeItem("molly_theme");
      }
    } catch (e) {
      if (theme === "light" || theme === "dark") root.dataset.theme = theme;
      else delete root.dataset.theme;
    }
  };

  /* ---------- буфер обмена (работает и по http в LAN) ---------- */

  M.copy = function (text) {
    function fallback() {
      var ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      var ok = false;
      try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
      document.body.removeChild(ta);
      return ok;
    }
    var done = function (ok) { M.toast(ok ? "Скопировано" : "Не удалось скопировать", !ok); };
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(function () { done(true); }, function () { done(fallback()); });
    } else {
      done(fallback());
    }
  };

  /* ---------- уведомления ---------- */

  M.toast = function (message, bad) {
    var box = document.getElementById("toasts");
    if (!box) return;
    var t = M.h("div", { class: "toast" + (bad ? " bad" : ""), text: message });
    box.appendChild(t);
    setTimeout(function () { if (t.parentNode) t.parentNode.removeChild(t); }, bad ? 6500 : 3000);
  };

  /* ---------- API ---------- */

  function ApiError(message, status) {
    var e = new Error(message);
    e.status = status;
    return e;
  }

  M.errorText = function (data, status) {
    if (status === 422) return "Некорректные данные запроса.";
    if (data) {
      if (typeof data.error === "string") return data.error;
      if (data.detail) {
        if (typeof data.detail === "string") return data.detail;
        if (typeof data.detail.error === "string") return data.detail.error;
      }
    }
    return "Ошибка " + status;
  };

  M.api = async function (path, opts) {
    opts = opts || {};
    var init = { method: opts.method || "GET", headers: {}, credentials: "same-origin" };
    if (opts.body !== undefined) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(opts.body);
    }
    var resp;
    try {
      resp = await fetch(path, init);
    } catch (e) {
      throw ApiError("Нет связи с МОЛЛИ. Возможно, приложение закрыто или сеть недоступна.", 0);
    }
    var data = null;
    var type = resp.headers.get("content-type") || "";
    if (type.indexOf("json") >= 0) {
      try { data = await resp.json(); } catch (e) { data = null; }
    }
    if (!resp.ok) {
      var err = ApiError(M.errorText(data, resp.status), resp.status);
      err.code = data && (data.code || (data.detail && data.detail.code));
      if (resp.status === 401 && data && data.login_required && M.hooks.loginRequired) M.hooks.loginRequired();
      throw err;
    }
    return data;
  };

  /* ---------- модальные окна ---------- */

  // modal({title, body, buttons:[{label, primary, danger, onClick}], wide, closable})
  // onClick(close) может вернуть false (или Promise<false>), чтобы окно не закрывалось.
  M.modal = function (opts) {
    var backdrop = M.h("div", { class: "backdrop" });
    var body = M.h("div", { class: "modal-body" }, opts.body);
    var foot = M.h("div", { class: "modal-foot" });
    var modal = M.h("div", { class: "modal" + (opts.wide ? " wide" : ""), role: "dialog", "aria-modal": "true" },
      M.h("div", { class: "modal-head" }, M.h("span", { class: "grow", text: opts.title || "" })),
      body, foot);
    backdrop.appendChild(modal);

    function onKey(e) { if (e.key === "Escape" && opts.closable !== false) close(); }
    function close() {
      document.removeEventListener("keydown", onKey);
      if (backdrop.parentNode) backdrop.parentNode.removeChild(backdrop);
      if (opts.onClose) opts.onClose();
    }

    (opts.buttons || []).forEach(function (b) {
      var btn = M.h("button", {
        class: "btn" + (b.primary ? " primary" : "") + (b.danger ? " danger" : ""),
        text: b.label,
        onclick: async function () {
          if (!b.onClick) { close(); return; }
          btn.disabled = true;
          try {
            var r = await b.onClick(close, btn);
            if (r !== false) close();
          } catch (e) {
            M.toast(e.message || String(e), true);
          } finally {
            btn.disabled = false;
          }
        },
      });
      foot.appendChild(btn);
    });
    if (!(opts.buttons || []).length) foot.hidden = true;

    document.addEventListener("keydown", onKey);
    document.body.appendChild(backdrop);
    var first = modal.querySelector("input, textarea, select");
    if (first && opts.autofocus !== false) first.focus();
    return { close: close, body: body, el: modal };
  };

  M.confirm = function (title, text, okLabel, danger) {
    return new Promise(function (resolve) {
      M.modal({
        title: title,
        body: M.h("p", { text: text, style: { margin: "8px 0" } }),
        buttons: [
          { label: "Отмена", onClick: function () { resolve(false); } },
          { label: okLabel || "Да", primary: !danger, danger: !!danger, onClick: function () { resolve(true); } },
        ],
        onClose: function () { resolve(false); },
      });
    });
  };

  /* ---------- прочее ---------- */

  M.debounce = function (fn, ms) {
    var t = null;
    return function () {
      var args = arguments, self = this;
      clearTimeout(t);
      t = setTimeout(function () { fn.apply(self, args); }, ms);
    };
  };

  M.download = function (filename, text, type) {
    var blob = new Blob([text], { type: type || "text/plain;charset=utf-8" });
    var a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    setTimeout(function () { URL.revokeObjectURL(a.href); a.remove(); }, 500);
  };

  M.readFile = function (file) {
    return new Promise(function (resolve, reject) {
      var r = new FileReader();
      r.onload = function () { resolve(String(r.result)); };
      r.onerror = function () { reject(new Error("Не удалось прочитать файл")); };
      r.readAsText(file, "utf-8");
    });
  };

  M.fileToBase64 = function (file) {
    return new Promise(function (resolve, reject) {
      var r = new FileReader();
      r.onload = function () { resolve(String(r.result).split(",")[1] || ""); };
      r.onerror = function () { reject(new Error("Не удалось прочитать файл")); };
      r.readAsDataURL(file);
    });
  };

  // Поле формы: подпись + элемент + подсказка.
  M.field = function (label, control, hint) {
    return M.h("div", { class: "field" },
      label ? M.h("label", { text: label }) : null,
      control,
      hint ? M.h("div", { class: "hint", text: hint }) : null);
  };

  M.input = function (value, attrs) {
    return M.h("input", Object.assign({ class: "input", type: "text", value: value == null ? "" : String(value) }, attrs || {}));
  };

  M.select = function (options, value, attrs) {
    var sel = M.h("select", Object.assign({ class: "input" }, attrs || {}));
    options.forEach(function (o) {
      sel.appendChild(M.h("option", { value: o[0], text: o[1], selected: o[0] === value }));
    });
    sel.value = value;
    return sel;
  };

  M.checkbox = function (label, checked, hint) {
    var box = M.h("input", { type: "checkbox", checked: !!checked });
    var wrap = M.h("label", { class: "check" }, box,
      M.h("span", null, label, hint ? M.h("div", { class: "hint", text: hint }) : null));
    wrap.input = box;
    return wrap;
  };
})();
