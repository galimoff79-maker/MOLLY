/* Лёгкий и безопасный markdown-рендер для ответов МОЛЛИ.
 * Весь текст сначала экранируется, поэтому HTML из ответа модели
 * или из документов никогда не исполняется.
 * Поддержка: заголовки, **жирный**, *курсив*, `код`, блоки ```код```,
 * списки (-, *, 1.), цитаты (>), таблицы, ссылки на источники [1].
 */
(function (root) {
  "use strict";

  function esc(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function inline(text) {
    var codes = [];
    // Инлайн-код выносим, чтобы внутри него ничего не разбиралось.
    text = text.replace(/`([^`\n]+)`/g, function (_, c) {
      codes.push("<code>" + esc(c) + "</code>");
      return "\u0000" + (codes.length - 1) + "\u0000";
    });
    text = esc(text);
    text = text.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
    text = text.replace(/(^|[^*\w])\*([^*\n]+)\*(?!\w)/g, "$1<em>$2</em>");
    // Ссылки на источники вида [1] или [1][2].
    text = text.replace(/\[(\d{1,2})\]/g, '<sup class="cite" data-n="$1">[$1]</sup>');
    text = text.replace(/\u0000(\d+)\u0000/g, function (_, i) { return codes[+i]; });
    return text;
  }

  function isTableSep(line) {
    return /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(line);
  }

  function cells(line) {
    line = line.trim();
    if (line[0] === "|") line = line.slice(1);
    if (line[line.length - 1] === "|") line = line.slice(0, -1);
    return line.split("|").map(function (c) { return c.trim(); });
  }

  function render(src) {
    var lines = String(src == null ? "" : src).replace(/\r\n?/g, "\n").split("\n");
    var out = [];
    var i = 0;

    while (i < lines.length) {
      var line = lines[i];

      // Блок кода.
      var fence = line.match(/^\s*```(\w*)\s*$/);
      if (fence) {
        var code = [];
        i++;
        while (i < lines.length && !/^\s*```\s*$/.test(lines[i])) { code.push(lines[i]); i++; }
        i++; // закрывающая ```
        out.push('<pre><code>' + esc(code.join("\n")) + "</code></pre>");
        continue;
      }

      // Заголовок.
      var hm = line.match(/^(#{1,4})\s+(.*)$/);
      if (hm) {
        var level = Math.min(hm[1].length + 1, 5);
        out.push("<h" + level + ">" + inline(hm[2]) + "</h" + level + ">");
        i++;
        continue;
      }

      // Таблица.
      if (line.indexOf("|") >= 0 && i + 1 < lines.length && isTableSep(lines[i + 1])) {
        var head = cells(line);
        i += 2;
        var rows = [];
        while (i < lines.length && lines[i].indexOf("|") >= 0 && lines[i].trim() !== "") {
          rows.push(cells(lines[i])); i++;
        }
        var t = '<div class="table-wrap"><table><thead><tr>' +
          head.map(function (c) { return "<th>" + inline(c) + "</th>"; }).join("") +
          "</tr></thead><tbody>" +
          rows.map(function (r) {
            return "<tr>" + r.map(function (c) { return "<td>" + inline(c) + "</td>"; }).join("") + "</tr>";
          }).join("") + "</tbody></table></div>";
        out.push(t);
        continue;
      }

      // Цитата.
      if (/^\s*>\s?/.test(line)) {
        var q = [];
        while (i < lines.length && /^\s*>\s?/.test(lines[i])) { q.push(lines[i].replace(/^\s*>\s?/, "")); i++; }
        out.push("<blockquote>" + inline(q.join("\n")).replace(/\n/g, "<br>") + "</blockquote>");
        continue;
      }

      // Списки.
      var lm = line.match(/^\s*([-*+]|\d+[.)])\s+(.*)$/);
      if (lm) {
        var ordered = /\d/.test(lm[1]);
        var items = [];
        while (i < lines.length) {
          var m = lines[i].match(/^\s*([-*+]|\d+[.)])\s+(.*)$/);
          if (!m || /\d/.test(m[1]) !== ordered) break;
          items.push("<li>" + inline(m[2]) + "</li>");
          i++;
        }
        out.push((ordered ? "<ol>" : "<ul>") + items.join("") + (ordered ? "</ol>" : "</ul>"));
        continue;
      }

      // Пустая строка.
      if (line.trim() === "") { i++; continue; }

      // Абзац: собираем строки до пустой / начала другого блока.
      var para = [];
      while (
        i < lines.length &&
        lines[i].trim() !== "" &&
        !/^\s*```/.test(lines[i]) &&
        !/^(#{1,4})\s+/.test(lines[i]) &&
        !/^\s*([-*+]|\d+[.)])\s+/.test(lines[i]) &&
        !/^\s*>\s?/.test(lines[i])
      ) { para.push(lines[i]); i++; }
      out.push("<p>" + inline(para.join("\n")).replace(/\n/g, "<br>") + "</p>");
    }

    return out.join("");
  }

  var api = { render: render, escape: esc };

  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.MollyMd = api;
})(typeof window !== "undefined" ? window : this);
