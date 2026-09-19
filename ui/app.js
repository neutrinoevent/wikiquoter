/* wq reading room — the only client logic is: send the typed line, render
   what wq.py sends back. No quote parsing happens here. */

(function () {
  "use strict";

  var form   = document.getElementById("form");
  var input  = document.getElementById("cmd");
  var go     = document.getElementById("go");
  var out    = document.getElementById("out");
  var status = document.getElementById("status");
  var langEl = document.getElementById("lang");

  var history = [];
  var histPos = -1;
  var busy = false;

  /* ── theme ───────────────────────────────────────────────────────────── */

  var root = document.documentElement;
  try {
    var saved = localStorage.getItem("wq-theme");
    if (saved === "dark" || saved === "light") root.dataset.theme = saved;
    var lang = localStorage.getItem("wq-lang");
    if (lang) langEl.value = lang;
  } catch (e) { /* private window; defaults are fine */ }

  document.getElementById("theme").addEventListener("click", function () {
    var now = root.dataset.theme;
    if (!now) {
      var dark = matchMedia("(prefers-color-scheme: dark)").matches;
      now = dark ? "dark" : "light";
    }
    var next = now === "dark" ? "light" : "dark";
    root.dataset.theme = next;
    try { localStorage.setItem("wq-theme", next); } catch (e) {}
  });

  langEl.addEventListener("change", function () {
    try { localStorage.setItem("wq-lang", langEl.value); } catch (e) {}
  });

  /* ── helpers ─────────────────────────────────────────────────────────── */

  function say(msg, tone) {
    status.textContent = msg || "";
    if (tone) status.dataset.tone = tone; else delete status.dataset.tone;
  }

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  function wikiUrl(page, section) {
    if (!page) return null;
    var u = "https://" + langEl.value + ".wikiquote.org/wiki/" +
            encodeURIComponent(page.replace(/ /g, "_"));
    if (section) u += "#" + encodeURIComponent(section.replace(/ /g, "_"));
    return u;
  }

  function copy(text, node) {
    var done = function () {
      node.classList.add("copied");
      say("copied", "ok");
      setTimeout(function () { node.classList.remove("copied"); }, 900);
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done, function () {
        say("could not copy", "error");
      });
    } else {
      var ta = el("textarea"); ta.value = text;
      document.body.appendChild(ta); ta.select();
      try { document.execCommand("copy"); done(); }
      catch (e) { say("could not copy", "error"); }
      document.body.removeChild(ta);
    }
  }

  /* ── rendering ───────────────────────────────────────────────────────── */

  function renderQuotes(res) {
    var qs = res.quotes || [];
    if (!qs.length) {
      out.appendChild(el("p", "empty", "Nothing came back for that."));
      return;
    }

    var pages = {};
    qs.forEach(function (q) { if (q.page) pages[q.page] = 1; });
    var names = Object.keys(pages);

    var head = el("div", "head");
    head.appendChild(el("h2", null,
      names.length === 1 ? names[0]
        : names.length ? names.length + " pages" : "Quotes"));
    head.appendChild(el("span", "count",
      qs.length + (qs.length === 1 ? " quote" : " quotes")));
    out.appendChild(head);

    qs.forEach(function (q, i) {
      var card = el("div", "q");
      card.appendChild(el("div", "n", String(i + 1)));

      var body = el("div", "body");

      var text = el("p", "text", q.text || "");
      if (q.about) {
        var tag = el("span", "tag", "about");
        text.appendChild(document.createTextNode(" "));
        text.appendChild(tag);
      }
      body.appendChild(text);

      if (q.source) body.appendChild(el("p", "attr", q.source));

      var where = el("p", "where");
      var url = wikiUrl(q.page, q.section);
      if (url) {
        var a = el("a", null, q.page);
        a.href = url; a.target = "_blank"; a.rel = "noopener";
        where.appendChild(a);
      } else if (q.page) {
        where.appendChild(document.createTextNode(q.page));
      }
      if (q.section) {
        where.appendChild(el("span", "sep", "/"));
        where.appendChild(document.createTextNode(q.section));
      }
      if (where.childNodes.length) body.appendChild(where);

      card.appendChild(body);

      card.addEventListener("click", function (ev) {
        if (ev.target.closest("a")) return;   // let links be links
        var blob = q.text + (q.source ? "\n— " + q.source : "");
        copy(blob, card);
      });

      out.appendChild(card);
    });
  }

  // Plain wq output, with bare URLs turned into links.
  function renderText(res) {
    var pre = el("pre", "text-out");
    var txt = (res.text || "").replace(/^\n+|\n+$/g, "");
    if (!txt) {
      out.appendChild(el("p", "empty", "Nothing came back for that."));
      return;
    }
    var re = /https?:\/\/[^\s)]+/g, last = 0, m;
    while ((m = re.exec(txt))) {
      if (m.index > last) {
        pre.appendChild(document.createTextNode(txt.slice(last, m.index)));
      }
      var a = el("a", null, m[0]);
      a.href = m[0]; a.target = "_blank"; a.rel = "noopener";
      pre.appendChild(a);
      last = m.index + m[0].length;
    }
    if (last < txt.length) {
      pre.appendChild(document.createTextNode(txt.slice(last)));
    }
    out.appendChild(pre);
  }

  /* ── running ─────────────────────────────────────────────────────────── */

  // Draw a result object (from the server seed, or from /api/run).
  function paint(res, raw) {
    out.innerHTML = "";
    if (!res || !res.ok) {
      say((res && res.error) || "something went wrong", "error");
      return;
    }
    if (res.note) {
      say(res.note, null);
    } else if (res.argv) {
      say("");
      status.appendChild(el("span", "argv", "wq " + res.argv));
    } else {
      say("");
    }
    if (res.kind === "quotes") renderQuotes(res);
    else renderText(res);

    // Keep the address bar in step, so the view can be shared or reloaded.
    if (raw) {
      var u = "/?q=" + encodeURIComponent(raw);
      if (langEl.value !== "en") u += "&lang=" + encodeURIComponent(langEl.value);
      try { window.history.replaceState(null, "", u); } catch (e) {}
    }
  }

  function run(raw) {
    if (busy) return;
    raw = (raw || "").trim();
    if (!raw) return;

    busy = true;
    go.disabled = true;
    say("reading…", "busy");

    var url = "/api/run?cmd=" + encodeURIComponent(raw) +
              "&lang=" + encodeURIComponent(langEl.value);

    fetch(url).then(function (r) { return r.json(); }).then(function (res) {
      paint(res, raw);
      window.scrollTo({ top: 0, behavior: "smooth" });
    }).catch(function (e) {
      say("server unreachable — is serve.py still running?", "error");
    }).then(function () {
      busy = false;
      go.disabled = false;
    });

    if (history[history.length - 1] !== raw) history.push(raw);
    histPos = history.length;
  }

  form.addEventListener("submit", function (ev) {
    ev.preventDefault();
    run(input.value);
  });

  document.getElementById("chips").addEventListener("click", function (ev) {
    var b = ev.target.closest("button[data-cmd]");
    if (!b) return;
    input.value = b.dataset.cmd;
    run(input.value);
  });

  input.addEventListener("keydown", function (ev) {
    if (ev.key === "ArrowUp") {
      if (!history.length) return;
      histPos = Math.max(0, histPos - 1);
      input.value = history[histPos];
      ev.preventDefault();
    } else if (ev.key === "ArrowDown") {
      if (!history.length) return;
      histPos = Math.min(history.length, histPos + 1);
      input.value = histPos === history.length ? "" : history[histPos];
      ev.preventDefault();
    }
  });

  document.addEventListener("keydown", function (ev) {
    if (ev.key === "/" && document.activeElement !== input) {
      ev.preventDefault();
      input.focus();
      input.select();
    }
  });

  // The server hands us the opening result inline; no first-paint round trip.
  var seedEl = document.getElementById("seed");
  var seeded = false;
  if (seedEl) {
    try {
      var seed = JSON.parse(seedEl.textContent);
      if (seed.lang) langEl.value = seed.lang;
      input.value = seed.cmd === "qotd" ? "" : seed.cmd;
      history.push(seed.cmd);
      histPos = history.length;
      paint(seed.result, null);
      seeded = true;
    } catch (e) { /* fall through to a live call */ }
  }

  input.focus();
  if (!seeded) run("qotd");
})();
