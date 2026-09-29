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
    head.appendChild(el("span", "count", countLine(res, qs.length)));
    out.appendChild(head);
    renderWiden(res.next);

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
        // wq supplies the full attribution (author + work on a person's
        // page); fall back to the bare source for older output.
        var credit = q.credit != null ? q.credit : q.source;
        var blob = q.text + (credit ? "\n— " + credit : "");
        copy(blob, card);
      });

      out.appendChild(card);
    });
  }

  // "3 of 22 quotes" when wq says the page held more than it showed;
  // "· pages 1–20 of 2,220" when the quotes came from a search.
  function countLine(res, shown) {
    var total = 0, exact = true;
    (res.pages || []).forEach(function (p) { total += p.total; });
    if (!res.pages || !res.pages.length) exact = false;
    var line = (exact && total > shown)
      ? shown + " of " + total + " quotes"
      : shown + (shown === 1 ? " quote" : " quotes");
    var s = res.search;
    if (s) {
      line += " · pages " + s.from + "–" + s.to + " of " +
              Number(s.total).toLocaleString("en");
    }
    return line;
  }

  // wq's suggested next steps. Each is a command wq itself composed; the
  // page only lays them out and runs them when clicked.

  var CHIPS = { page: "pages about it", nearby: "also starting",
                category: "categories" };

  function fmt(n) { return Number(n).toLocaleString("en"); }

  function stepButton(s, cls, text) {
    var b = el("button", cls, text);
    b.type = "button";
    b.dataset.cmd = s.cmd;
    return b;
  }

  function onStep(box) {
    box.addEventListener("click", function (ev) {
      var j = ev.target.closest("[data-jump]");
      if (j) {
        var t = document.getElementById("next");
        if (t) t.scrollIntoView({ behavior: "smooth", block: "start" });
        return;
      }
      var b = ev.target.closest("button[data-cmd]");
      if (!b) return;
      input.value = b.dataset.cmd;
      run(b.dataset.cmd);
    });
  }

  // One line under the heading that says, up front, how far this can go:
  // "widen  all 71 · 5 pages about it · 1 category · on 347 pages · quantum".
  // Runnable steps run; groups jump to the full list at the bottom.
  function renderWiden(steps) {
    if (!steps || !steps.length) return;
    var bar = el("nav", "widen");
    bar.setAttribute("aria-label", "Ways to widen this");
    bar.appendChild(el("span", "label", "widen"));
    var items = [];
    var groups = {};
    steps.forEach(function (s) {
      if (CHIPS[s.kind]) { groups[s.kind] = (groups[s.kind] || 0) + 1; return; }
      var text;
      if (s.kind === "more") text = "all " + fmt(s.count || "");
      else if (s.kind === "mentions") text = "mentioned on " + fmt(s.count) + " pages";
      else if (s.kind === "broader") text = "“" + s.term + "” (" + fmt(s.count) + ")";
      else if (s.kind === "next-batch") text = "next batch";
      else return;
      if (s.kind === "more" && items.some(function (i) { return i.more; })) return;
      var b = stepButton(s, null, text);
      b.title = s.label;
      items.push({ node: b, more: s.kind === "more" });
    });
    Object.keys(CHIPS).forEach(function (k) {
      if (!groups[k]) return;
      var n = groups[k];
      var text = k === "page" ? n + (n === 1 ? " page" : " pages") + " about it"
               : k === "nearby" ? n + " more by name"
               : n + (n === 1 ? " category" : " categories");
      var b = el("button", null, text + " ↓");
      b.type = "button";
      b.dataset.jump = "1";
      items.push({ node: b });
    });
    items.forEach(function (i, n) {
      if (n) bar.appendChild(el("span", "dot", "·"));
      bar.appendChild(i.node);
    });
    onStep(bar);
    out.appendChild(bar);
  }

  // The full list, under the results.
  function renderNext(steps) {
    if (!steps || !steps.length) return;
    var box = el("section", "next");
    box.id = "next";
    box.setAttribute("aria-label", "Keep reading");
    box.appendChild(el("h3", null, "keep reading"));

    steps.forEach(function (s) {
      if (CHIPS[s.kind]) return;
      var row = stepButton(s, "step " + s.kind);
      row.appendChild(el("span", "what", s.label));
      var right = el("span", "arrow");
      if (s.count && (s.kind === "mentions" || s.kind === "broader")) {
        right.appendChild(el("span", "pages", fmt(s.count) + " pages"));
      }
      right.appendChild(document.createTextNode("→"));
      row.appendChild(right);
      box.appendChild(row);
    });

    Object.keys(CHIPS).forEach(function (k) {
      var group = steps.filter(function (s) { return s.kind === k; });
      if (!group.length) return;
      var line = el("div", "related");
      var label = CHIPS[k];
      if (k === "nearby" && group[0].term) label += " “" + group[0].term + "”";
      line.appendChild(el("span", "label", label));
      group.forEach(function (s) { line.appendChild(stepButton(s, null, s.label)); });
      box.appendChild(line);
    });

    onStep(box);
    out.appendChild(box);
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
    renderNext(res.next);

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
