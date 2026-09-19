#!/usr/bin/env python3
"""
wq serve — a reading room for wq.py.

    ./serve.py            # http://127.0.0.1:8787, opens a browser
    ./serve.py -p 9000 --no-open

Standard library only, like wq.py itself. The browser sends the same command
grammar you would type at the shell ('q Diligence -n 5', 'wall', a pasted URL);
the server runs wq.py as a subprocess with --format json and hands back the
quotes. wq.py stays the single source of truth — this file adds no parsing and
no opinions about what a quote is.
"""

from __future__ import annotations

import argparse
import http.server
import json
import mimetypes
import os
import shlex
import socketserver
import subprocess
import sys
import threading
import urllib.parse
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
UI = os.path.join(HERE, "ui")
WQ = os.path.join(HERE, "wq.py")

# Subcommands wq.py accepts, aliases included. Anything else the user types is
# treated as a page title (then, if there is no such page, as a search).
COMMANDS = {
    "random", "r", "quote", "q", "open", "o", "search", "s", "sections", "sec",
    "category", "cat", "cat-search", "wall", "w", "qotd", "save", "saved",
    "forget", "cache", "selftest",
}

# Commands whose output is a list of quotes; everything else comes back as text.
TEXTUAL = {"sections", "sec", "search", "s", "cat-search", "cache", "saved",
           "category", "cat", "open", "o", "selftest", "forget", "save"}


class Result(dict):
    pass


def run_wq(argv, timeout=45):
    """Run wq.py and return (exit_code, stdout, stderr)."""
    try:
        p = subprocess.run(
            [sys.executable, WQ] + argv,
            capture_output=True, text=True, timeout=timeout,
            cwd=HERE,
        )
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timed out after %ds" % timeout


def build_argv(raw, lang="en"):
    """Turn a typed line into an argv for wq.py.

    Returns (argv, command, wants_json, note). Raises ValueError on bad quoting.
    """
    tokens = shlex.split(raw.strip())
    if not tokens:
        raise ValueError("empty command")

    note = ""
    head = tokens[0]
    if head in COMMANDS:
        cmd, rest = head, tokens[1:]
    else:
        # Bare input: read it as a page (URL, 'Title#Section', or a title).
        # Bare input is one page, not one page per word: in a search box
        # 'marcus aurelius' means a single title, where the shell's `q A B`
        # means two. URLs and 'Title#Section' still parse the usual way.
        cmd, rest = "quote", [raw.strip()]
        note = "page"

    argv = [cmd] + rest
    if "--color" not in argv:
        argv += ["--color", "never"]
    if lang and "--lang" not in argv:
        argv += ["--lang", lang]

    # 'open' would spawn browser tabs from the server process; in a browser the
    # useful answer is the list of URLs, which the UI renders as links.
    if cmd in ("open", "o") and "--dry-run" not in argv:
        argv += ["--dry-run"]

    wants_json = cmd not in TEXTUAL and not any(
        t == "--format" for t in argv)
    if wants_json:
        argv += ["--format", "json"]
    return argv, cmd, wants_json, note


def execute(raw, lang="en"):
    try:
        argv, cmd, wants_json, note = build_argv(raw, lang)
    except ValueError as e:
        return Result(ok=False, error=str(e), kind="error")

    code, out, err = run_wq(argv)

    # A bare title that is not a page: fall back to a search, and say so.
    # (Any failure counts — the API words 'no such page' several ways.)
    if note == "page" and code != 0:
        best = nearest_page(raw.strip(), lang)
        if best:
            bargv, _, _, _ = build_argv('quote "%s"' % best.replace('"', ""),
                                        lang)
            bcode, bout, _berr = run_wq(bargv)
            if bcode == 0 and bout.strip():
                try:
                    data = json.loads(bout)
                except ValueError:
                    data = None
                if data:
                    return Result(ok=True, kind="quotes", cmd="quote",
                                  quotes=data, argv=" ".join(bargv),
                                  note=u"No page called “%s” — showing “%s”."
                                       % (raw.strip(), best))
        sargv, _, _, _ = build_argv("search " + raw, lang)
        scode, sout, serr = run_wq(sargv)
        if scode == 0:
            return Result(ok=True, kind="text", cmd="search", text=sout,
                          argv=" ".join(sargv),
                          note="No page called “%s” — searched instead."
                               % raw.strip())

    # wq exits 2 for "ran fine, found nothing" — an empty state, not a failure.
    if code == 2:
        return Result(ok=True, kind="text", cmd=cmd,
                      text=(out or err or "").strip() or "(nothing matched)",
                      argv=" ".join(argv))

    if code != 0:
        msg = (err or out or "wq exited with %d" % code).strip()
        return Result(ok=False, kind="error", error=msg,
                      argv=" ".join(argv))

    if wants_json:
        try:
            data = json.loads(out) if out.strip() else []
        except ValueError:
            # e.g. '(no quotes matched)' printed as prose
            return Result(ok=True, kind="text", cmd=cmd, text=out.strip(),
                          argv=" ".join(argv))
        return Result(ok=True, kind="quotes", cmd=cmd, quotes=data,
                      argv=" ".join(argv))

    return Result(ok=True, kind="text", cmd=cmd, text=out, argv=" ".join(argv))


def nearest_page(term, lang="en"):
    """Best-matching real page title for a phrase, or None.

    Uses wq.py's own Api so the lookup obeys the same cache, throttle and
    User-Agent as every other request.
    """
    try:
        if HERE not in sys.path:
            sys.path.insert(0, HERE)
        sys.dont_write_bytecode = True   # no __pycache__ in the project dir
        import wq
        api = wq.Api(lang=lang)
        for finder in (api.prefix_search, api.search):
            for hit in finder(term, limit=5):
                title = hit.get("title", "")
                if title and not title.startswith(("List of", "Wikiquote:")):
                    return title
    except Exception:
        return None
    return None


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "wq-serve"

    def log_message(self, fmt, *a):  # quieter than the default
        if os.environ.get("WQ_SERVE_VERBOSE"):
            sys.stderr.write("  %s\n" % (fmt % a))

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def _index(self, raw, lang):
        """index.html with the first result already in it.

        Seeding the opening view keeps the page from flashing empty, and makes
        a query shareable: /?q=wall+-n+8 renders that wall directly.
        """
        with open(os.path.join(UI, "index.html"), encoding="utf-8") as fh:
            page = fh.read()
        try:
            seed = {"cmd": raw, "lang": lang, "result": execute(raw, lang)}
            blob = json.dumps(seed, ensure_ascii=False)
        except Exception:
            return page
        # </script> inside JSON would close this tag early.
        blob = blob.replace("</", "<\\/")
        tag = ('<script id="seed" type="application/json">%s</script>\n</head>'
               % blob)
        return page.replace("</head>", tag, 1)

    def do_GET(self):
        parts = urllib.parse.urlsplit(self.path)
        path = parts.path

        if path == "/api/run":
            qs = urllib.parse.parse_qs(parts.query)
            raw = (qs.get("cmd") or [""])[0]
            lang = (qs.get("lang") or ["en"])[0]
            if not raw.strip():
                return self._send(400, json.dumps(
                    {"ok": False, "kind": "error", "error": "empty command"}))
            res = execute(raw, lang)
            return self._send(200, json.dumps(res, ensure_ascii=False))

        if path in ("/", "", "/index.html"):
            qs = urllib.parse.parse_qs(parts.query)
            raw = (qs.get("q") or ["qotd"])[0]
            lang = (qs.get("lang") or ["en"])[0]
            return self._send(200, self._index(raw, lang),
                              "text/html; charset=utf-8")

        # static files out of ui/
        rel = path.lstrip("/")
        target = os.path.normpath(os.path.join(UI, rel))
        if not target.startswith(UI) or not os.path.isfile(target):
            return self._send(404, "not found", "text/plain; charset=utf-8")
        ctype = mimetypes.guess_type(target)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype.endswith(("javascript", "json")):
            ctype += "; charset=utf-8"
        with open(target, "rb") as fh:
            self._send(200, fh.read(), ctype)


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main(argv=None):
    ap = argparse.ArgumentParser(description="a reading room for wq.py")
    ap.add_argument("-p", "--port", type=int, default=8787)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-open", action="store_true",
                    help="don't open a browser")
    a = ap.parse_args(argv)

    if not os.path.isfile(WQ):
        sys.exit("wq.py not found next to serve.py")

    srv = Server((a.host, a.port), Handler)
    url = "http://%s:%d/" % (a.host, a.port)
    print("wq reading room  →  %s   (ctrl-c to stop)" % url)
    if not a.no_open:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
