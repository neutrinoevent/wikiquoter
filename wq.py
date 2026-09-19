#!/usr/bin/env python3
"""
wq — a Wikiquote command-line companion.

Pull random pages, specific pages, whole sections, search results, or a
wall of quotes from any Wikiquote language edition. Read them in the
terminal or fling them open in browser tabs.

Standard library only. Python 3.8+.

    wq random                     # a random page, with a few quotes
    wq random -n 5 --open         # five random pages, in browser tabs
    wq q Diligence                # quotes from a page
    wq q "Edward Bulwer-Lytton" -s "Zanoni (1842)"
    wq open Diligence "Samuel Johnson" --section "Quotes"
    wq search stoicism --quotes
    wq sections "Edward Bulwer-Lytton"
    wq category Themes --random 3
    wq wall -n 12
    wq save "Samuel Johnson" && wq saved --random

Run `wq selftest` to exercise the parser offline (no network).
"""

from __future__ import annotations

import argparse
import concurrent.futures as futures
import hashlib
import html
import json
import os
import random
import re
import shutil
import subprocess
import sys
import textwrap
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

__version__ = "1.0.0"

UA = (
    "wq/%s (Wikiquote CLI; "
    "https://github.com/neutrinoevent/wikiquoter; python-urllib) "
    % __version__
)

DEFAULT_LANG = os.environ.get("WQ_LANG", "en")

# ---------------------------------------------------------------------------
# paths
# ---------------------------------------------------------------------------


def _xdg(var: str, fallback: str) -> str:
    base = os.environ.get(var) or os.path.expanduser(fallback)
    return os.path.join(base, "wq")


CACHE_DIR = _xdg("XDG_CACHE_HOME", "~/.cache")
CONFIG_DIR = _xdg("XDG_CONFIG_HOME", "~/.config")
BOOKMARKS = os.path.join(CONFIG_DIR, "bookmarks.json")
SEEN = os.path.join(CACHE_DIR, "seen.json")


# ---------------------------------------------------------------------------
# terminal helpers
# ---------------------------------------------------------------------------


class Style:
    enabled = True

    @classmethod
    def setup(cls, force: bool = False, disable: bool = False) -> None:
        if disable or os.environ.get("NO_COLOR"):
            cls.enabled = False
        elif force:
            cls.enabled = True
        else:
            cls.enabled = sys.stdout.isatty()

    @classmethod
    def _w(cls, code: str, text: str) -> str:
        return "\033[%sm%s\033[0m" % (code, text) if cls.enabled else text

    @classmethod
    def dim(cls, t: str) -> str:
        return cls._w("2", t)

    @classmethod
    def bold(cls, t: str) -> str:
        return cls._w("1", t)

    @classmethod
    def cyan(cls, t: str) -> str:
        return cls._w("36", t)

    @classmethod
    def yellow(cls, t: str) -> str:
        return cls._w("33", t)

    @classmethod
    def green(cls, t: str) -> str:
        return cls._w("32", t)

    @classmethod
    def red(cls, t: str) -> str:
        return cls._w("31", t)


def term_width(cap: int = 96) -> int:
    try:
        w = shutil.get_terminal_size((80, 24)).columns
    except Exception:
        w = 80
    return max(40, min(cap, w - 2))


def warn(msg: str) -> None:
    print(Style.red("! ") + msg, file=sys.stderr)


def die(msg: str, code: int = 1) -> "NoReturn":  # type: ignore[valid-type]
    warn(msg)
    sys.exit(code)


# ---------------------------------------------------------------------------
# API client
# ---------------------------------------------------------------------------


def _retry_after(err) -> float:
    """Seconds requested by a Retry-After header, or 0.0 if absent/odd."""
    try:
        raw = (err.headers.get("Retry-After") or "").strip()
    except Exception:
        return 0.0
    if not raw:
        return 0.0
    try:
        return max(0.0, min(30.0, float(raw)))
    except ValueError:
        try:
            from email.utils import parsedate_to_datetime
            when = parsedate_to_datetime(raw).timestamp()
            return max(0.0, min(30.0, when - time.time()))
        except Exception:
            return 0.0


class WikiquoteError(Exception):
    pass


class MissingPage(WikiquoteError):
    pass


class Api:
    MIN_INTERVAL = 0.06   # seconds between requests, across all threads

    def __init__(self, lang: str = DEFAULT_LANG, cache_ttl: int = 86400,
                 use_cache: bool = True, timeout: int = 20,
                 contact: str = "", retries: int = 3):
        self.lang = lang
        self.endpoint = "https://%s.wikiquote.org/w/api.php" % lang
        self.site = "https://%s.wikiquote.org" % lang
        self.cache_ttl = cache_ttl
        self.use_cache = use_cache
        self.timeout = timeout
        self.retries = retries
        self.ua = UA + (contact or os.environ.get("WQ_CONTACT", ""))
        self._lock = threading.Lock()
        self._next_at = 0.0

    # -- low level ----------------------------------------------------------

    def _throttle(self) -> None:
        """Space requests out a little, however many threads are calling."""
        with self._lock:
            now = time.time()
            wait = self._next_at - now
            self._next_at = max(now, self._next_at) + self.MIN_INTERVAL
        if wait > 0:
            time.sleep(wait)

    def _cache_path(self, url: str) -> str:
        h = hashlib.sha1(url.encode("utf-8")).hexdigest()[:20]
        return os.path.join(CACHE_DIR, h + ".json")

    def _cache_read(self, url: str):
        if not self.use_cache:
            return None
        p = self._cache_path(url)
        try:
            if time.time() - os.path.getmtime(p) > self.cache_ttl:
                return None
            with open(p, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return None

    def _cache_write(self, url: str, data) -> None:
        if not self.use_cache:
            return
        try:
            os.makedirs(CACHE_DIR, exist_ok=True)
            tmp = self._cache_path(url) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
            os.replace(tmp, self._cache_path(url))
        except OSError:
            pass

    def get(self, params: dict, cacheable: bool = True):
        q = dict(params)
        q.setdefault("format", "json")
        q.setdefault("formatversion", "2")
        url = self.endpoint + "?" + urllib.parse.urlencode(q)

        if cacheable:
            hit = self._cache_read(url)
            if hit is not None:
                return hit

        req = urllib.request.Request(url, headers={
            "User-Agent": self.ua,
            "Accept": "application/json",
            "Accept-Encoding": "identity",
        })

        # Wikimedia answers bursts with 429; a handful of parallel workers hits
        # that easily. Retry on 429/5xx, honouring Retry-After when it is sent.
        raw = None
        for attempt in range(self.retries + 1):
            self._throttle()
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read().decode("utf-8", "replace")
                break
            except urllib.error.HTTPError as e:
                retryable = e.code == 429 or 500 <= e.code < 600
                if not retryable or attempt == self.retries:
                    raise WikiquoteError("HTTP %s from %s.wikiquote.org"
                                         % (e.code, self.lang))
                delay = _retry_after(e) or min(8.0, 0.6 * (2 ** attempt))
                time.sleep(delay)
            except (urllib.error.URLError, TimeoutError) as e:
                if attempt == self.retries:
                    reason = getattr(e, "reason", e)
                    if isinstance(e, TimeoutError):
                        raise WikiquoteError("request timed out")
                    raise WikiquoteError("network unreachable (%s)" % (reason,))
                time.sleep(min(8.0, 0.6 * (2 ** attempt)))
        if raw is None:
            raise WikiquoteError("request failed")

        try:
            data = json.loads(raw)
        except ValueError:
            raise WikiquoteError("bad JSON from API")

        if isinstance(data, dict) and "error" in data:
            info = data["error"].get("info", data["error"].get("code", "?"))
            if data["error"].get("code") == "missingtitle":
                raise MissingPage(info)
            raise WikiquoteError("API: %s" % info)

        if cacheable:
            self._cache_write(url, data)
        return data

    # -- high level ---------------------------------------------------------

    def random_titles(self, n: int = 1) -> list:
        out = []
        while len(out) < n:
            batch = min(10, n - len(out))
            data = self.get({
                "action": "query", "list": "random",
                "rnnamespace": "0", "rnlimit": str(batch),
                "rnfilterredir": "nonredirects",
            }, cacheable=False)
            for item in data.get("query", {}).get("random", []):
                out.append(item["title"])
        return out[:n]

    def wikitext(self, title: str) -> str:
        data = self.get({
            "action": "parse", "page": title,
            "prop": "wikitext", "redirects": "1",
        })
        return data.get("parse", {}).get("wikitext", "") or ""

    def sections(self, title: str) -> list:
        data = self.get({
            "action": "parse", "page": title,
            "prop": "sections", "redirects": "1",
        })
        return data.get("parse", {}).get("sections", []) or []

    def search(self, term: str, limit: int = 10, namespace: str = "0") -> list:
        data = self.get({
            "action": "query", "list": "search",
            "srsearch": term, "srlimit": str(limit),
            "srnamespace": namespace,
        })
        return data.get("query", {}).get("search", []) or []

    def prefix_search(self, term: str, limit: int = 10,
                      namespace: str = "0") -> list:
        """Title-prefix search. Unlike list=search this works in namespace 14,
        where category pages have almost no indexable body text."""
        data = self.get({
            "action": "query", "list": "prefixsearch",
            "pssearch": term, "pslimit": str(limit),
            "psnamespace": namespace,
        })
        return data.get("query", {}).get("prefixsearch", []) or []

    def all_pages(self, prefix: str, limit: int = 10,
                  namespace: str = "0") -> list:
        data = self.get({
            "action": "query", "list": "allpages",
            "apprefix": prefix, "aplimit": str(limit),
            "apnamespace": namespace,
        })
        return data.get("query", {}).get("allpages", []) or []

    def category_members(self, category: str, kind: str = "page",
                         limit: int = 500) -> list:
        if not category.lower().startswith(("category:", "kategorie:")):
            category = "Category:" + category
        out, cont = [], None
        while True:
            params = {
                "action": "query", "list": "categorymembers",
                "cmtitle": category, "cmlimit": "500", "cmtype": kind,
            }
            if cont:
                params["cmcontinue"] = cont
            data = self.get(params)
            out.extend(m["title"] for m in
                       data.get("query", {}).get("categorymembers", []))
            cont = data.get("continue", {}).get("cmcontinue")
            if not cont or len(out) >= limit:
                break
        if not out:
            raise MissingPage("no members in %s" % category)
        return out[:limit]

    def category_walk(self, category: str, depth: int = 1,
                      max_pages: int = 2000) -> list:
        """Collect pages in a category, descending `depth` levels of subcats."""
        pages, seen_cats = [], set()
        frontier = [category]
        for level in range(depth + 1):
            next_frontier = []
            for cat in frontier:
                key = cat.lower()
                if key in seen_cats:
                    continue
                seen_cats.add(key)
                try:
                    pages.extend(self.category_members(cat, "page"))
                except WikiquoteError:
                    continue
                if level < depth:
                    try:
                        next_frontier.extend(
                            self.category_members(cat, "subcat", limit=60))
                    except WikiquoteError:
                        pass
                if len(pages) >= max_pages:
                    return _dedupe(pages)[:max_pages]
            frontier = next_frontier
            if not frontier:
                break
        return _dedupe(pages)[:max_pages]

    # -- urls ---------------------------------------------------------------

    def page_url(self, title: str, section: str = "") -> str:
        t = urllib.parse.quote(title.replace(" ", "_"),
                               safe="/:()',!*-_.~$&+;=@")
        url = "%s/wiki/%s" % (self.site, t)
        if section:
            url += "#" + anchor_encode(section)
        return url


def _dedupe(seq):
    seen, out = set(), []
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def anchor_encode(text: str) -> str:
    """Approximate MediaWiki's HTML5 id encoding for section anchors.

    Spaces become underscores; parentheses and most punctuation are left
    alone (so "Zanoni (1842)" -> "Zanoni_(1842)").
    """
    text = text.strip()
    # accept an anchor that is already encoded
    if "%" in text and " " not in text:
        return text
    text = text.replace(" ", "_")
    out = []
    for ch in text:
        if ch in '#|"[]{}<>\\^`%' or ord(ch) < 0x21 or ord(ch) == 0x7F:
            out.extend("%%%02X" % b for b in ch.encode("utf-8"))
        else:
            out.append(ch)
    return "".join(out)


# ---------------------------------------------------------------------------
# wikitext -> quotes
# ---------------------------------------------------------------------------

SKIP_SECTIONS = re.compile(
    r"^(see also|external links?|references?|notes?|sources?|further reading|"
    r"bibliography|works about|filmography|discography|cast|categories|"
    r"related pages?|footnotes?)\b", re.I)

ABOUT_SECTION = re.compile(r"^quotes?\s+about\b|^about\b|^said\s+about\b", re.I)

RE_COMMENT = re.compile(r"<!--.*?-->", re.S)
RE_REF_PAIR = re.compile(r"<ref\b[^>]*>.*?</ref\s*>", re.S | re.I)
RE_REF_SELF = re.compile(r"<ref\b[^>]*/\s*>", re.I)
RE_NOWIKI = re.compile(r"</?nowiki\s*/?>", re.I)
RE_BR = re.compile(r"<br\s*/?>", re.I)
RE_TAG = re.compile(r"</?[a-zA-Z][^>]*>")
RE_EXTLINK = re.compile(r"\[(?:https?:|//|ftp:)[^\s\]]+(?:\s+([^\]]*))?\]")
RE_WIKILINK = re.compile(r"\[\[([^\[\]|]*)(?:\|([^\[\]]*))?\]\]")
RE_BOLDITAL = re.compile(r"'{2,5}")
RE_BULLET = re.compile(r"^(\*+)(:*)\s*(.*)$")
RE_INDENT = re.compile(r"^(:+)\s*(.*)$")
RE_HEADING = re.compile(r"^\s*(={2,6})\s*(.*?)\s*\1\s*$")

KEEP_FIRST_PARAM = {"quote", "quotation", "cquote", "quotebox", "blockquote",
                    "pull quote", "quote box"}
KEEP_LAST_PARAM = {"lang", "transl", "transliteration", "nowrap", "nobr",
                   # interwiki link templates: {{w|Carcosa}} -> Carcosa,
                   # {{w|The King in Yellow|that play}} -> that play.
                   # Without these the linked words vanish from the quote.
                   "w", "wp", "wikipedia", "wikt", "wiktionary", "wikisource",
                   "wikilink", "iw", "interwiki"}

# Wrappers whose first parameter is the visible text.
KEEP_FIRST_PARAM |= {"small", "smaller", "big", "larger", "nobold",
                     "noitalic", "em", "strong", "bracket"}


def _split_template(inner: str) -> list:
    """Split a template body on top-level pipes."""
    parts, buf, depth_c, depth_b = [], [], 0, 0
    i = 0
    while i < len(inner):
        two = inner[i:i + 2]
        if two == "{{":
            depth_c += 1
            buf.append(two)
            i += 2
            continue
        if two == "}}":
            depth_c -= 1
            buf.append(two)
            i += 2
            continue
        if two == "[[":
            depth_b += 1
            buf.append(two)
            i += 2
            continue
        if two == "]]":
            depth_b -= 1
            buf.append(two)
            i += 2
            continue
        ch = inner[i]
        if ch == "|" and depth_c == 0 and depth_b == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
        i += 1
    parts.append("".join(buf))
    return parts


def _template_text(inner: str) -> str:
    parts = _split_template(inner)
    name = parts[0].strip().lower()
    positional = [p for p in parts[1:] if "=" not in p.split("[[")[0][:40]]
    if name in KEEP_FIRST_PARAM and positional:
        return positional[0].strip()
    if name in KEEP_LAST_PARAM and positional:
        return positional[-1].strip()
    if name in ("sic", "'", "spaced ndash", "snd"):
        return "" if name == "sic" else " – "
    if name in ("nbsp",):
        return " "
    return ""


def strip_templates(text: str) -> str:
    out, i, n = [], 0, len(text)
    while i < n:
        if text.startswith("{{", i):
            depth, j = 1, i + 2
            start = j
            while j < n and depth:
                if text.startswith("{{", j):
                    depth += 1
                    j += 2
                elif text.startswith("}}", j):
                    depth -= 1
                    j += 2
                else:
                    j += 1
            inner = text[start:j - 2] if depth == 0 else text[start:]
            out.append(strip_templates(_template_text(inner)))
            i = j
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def _wikilink_sub(m: "re.Match") -> str:
    target, label = m.group(1) or "", m.group(2)
    if re.match(r"\s*(file|image|media)\s*:", target, re.I):
        return ""
    if re.match(r"\s*(category)\s*:", target, re.I):
        return ""
    return (label if label is not None else target).strip()


def clean(text: str) -> str:
    """Turn a chunk of wikitext into readable plain text."""
    if not text:
        return ""
    text = RE_COMMENT.sub("", text)
    text = RE_REF_PAIR.sub("", text)
    text = RE_REF_SELF.sub("", text)
    text = RE_NOWIKI.sub("", text)
    text = strip_templates(text)
    for _ in range(4):
        new = RE_WIKILINK.sub(_wikilink_sub, text)
        if new == text:
            break
        text = new
    text = RE_EXTLINK.sub(lambda m: (m.group(1) or "").strip(), text)
    text = RE_BR.sub("\n", text)
    text = RE_TAG.sub("", text)
    text = RE_BOLDITAL.sub("", text)
    text = html.unescape(text)
    text = text.replace(" ", " ")
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.split("\n")]
    return "\n".join(ln for ln in lines).strip()


class Quote:
    __slots__ = ("text", "source", "page", "section", "about", "kind")

    def __init__(self, text, source="", page="", section="", about=False,
                 kind="quote"):
        self.text = text
        self.source = source
        self.page = page
        self.section = section
        self.about = about
        self.kind = kind

    def __repr__(self):
        return "Quote(%r, %r)" % (self.text[:40], self.source[:30])

    def as_dict(self):
        return {"text": self.text, "source": self.source, "page": self.page,
                "section": self.section, "about": self.about,
                "kind": self.kind}

    def key(self):
        norm = unicodedata.normalize("NFKD", self.text.lower())
        norm = re.sub(r"\W+", " ", norm).strip()
        return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:16]


def parse_quotes(wikitext: str, page: str = "", min_len: int = 16,
                 include_dialogue: bool = True) -> list:
    """Extract quotes from a Wikiquote page's wikitext."""
    quotes: list = []
    heading_stack: list = []
    current: "Quote|None" = None
    dialogue_buf: list = []

    def section_name() -> str:
        return heading_stack[-1] if heading_stack else ""

    def skipping() -> bool:
        return any(SKIP_SECTIONS.match(h) for h in heading_stack)

    def flush_dialogue():
        nonlocal dialogue_buf
        if not dialogue_buf:
            return
        speakers = sum(1 for ln in dialogue_buf if re.match(r"'''[^']+'''", ln))
        block = "\n".join(clean(ln) for ln in dialogue_buf)
        block = "\n".join(ln for ln in block.split("\n") if ln.strip())
        want = include_dialogue and (
            speakers >= 2 or re.search(r"dialogue|conversation|exchange",
                                       section_name(), re.I))
        if want and len(block) >= min_len:
            quotes.append(Quote(block, "", page, section_name(),
                                ABOUT_SECTION.match(section_name()) is not None,
                                kind="dialogue"))
        dialogue_buf = []

    def close_quote():
        nonlocal current
        if current is not None:
            current.text = current.text.strip()
            current.source = current.source.strip(" —-–—;,")
            if len(current.text) >= min_len:
                quotes.append(current)
        current = None

    text = RE_COMMENT.sub("", wikitext)
    text = RE_REF_PAIR.sub("", text)
    text = RE_REF_SELF.sub("", text)

    for raw in text.split("\n"):
        line = raw.rstrip()

        m = RE_HEADING.match(line)
        if m:
            close_quote()
            flush_dialogue()
            level = len(m.group(1)) - 2
            title = clean(m.group(2))
            del heading_stack[level:]
            heading_stack.append(title)
            continue

        if skipping():
            continue

        m = RE_BULLET.match(line)
        if m:
            flush_dialogue()
            stars, colons, body = len(m.group(1)), len(m.group(2)), m.group(3)
            if not body.strip():
                continue
            if stars == 1 and colons == 0:
                close_quote()
                sec = section_name()
                current = Quote(clean(body), "", page, sec,
                                ABOUT_SECTION.match(sec) is not None)
            elif stars == 1 and colons > 0:
                if current is not None:
                    current.text += "\n" + clean(body)
            else:  # ** or deeper -> attribution / note
                if current is not None:
                    piece = clean(body)
                    if piece:
                        current.source += (" — " if current.source else "") + piece
            continue

        m = RE_INDENT.match(line)
        if m and m.group(2).strip():
            if current is not None:
                current.text += "\n" + clean(m.group(2))
            else:
                dialogue_buf.append(m.group(2))
            continue

        if not line.strip():
            flush_dialogue()
            close_quote()
            continue

        # a bare paragraph line ends any open bullet quote
        close_quote()
        flush_dialogue()

    close_quote()
    flush_dialogue()

    # drop junk that slipped through
    cleaned = []
    for q in quotes:
        t = q.text.strip()
        if not t or len(t) < min_len:
            continue
        if re.match(r"^(redirect|defaultsort|thumb\b)", t, re.I):
            continue
        cleaned.append(q)
    return cleaned


def filter_quotes(quotes, section="", grep="", minlen=0, maxlen=0,
                  about="include"):
    out = []
    sec_norm = section.replace("_", " ").strip().lower()
    rx = re.compile(grep, re.I) if grep else None
    for q in quotes:
        if sec_norm and sec_norm not in q.section.replace("_", " ").lower():
            continue
        if about == "only" and not q.about:
            continue
        if about == "exclude" and q.about:
            continue
        if minlen and len(q.text) < minlen:
            continue
        if maxlen and len(q.text) > maxlen:
            continue
        if rx and not (rx.search(q.text) or rx.search(q.source)):
            continue
        out.append(q)
    return out


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------


def render_quote(q: Quote, width: int, show_page: bool = True,
                 number: "int|None" = None) -> str:
    lines = []
    bullet = Style.cyan("%2d. " % number) if number is not None else "  "
    pad = " " * len(re.sub(r"\033\[[0-9;]*m", "", bullet))
    body_width = max(24, width - len(pad))

    paragraphs = q.text.split("\n")
    first = True
    for para in paragraphs:
        wrapped = textwrap.wrap(para, body_width) or [""]
        for ln in wrapped:
            prefix = bullet if first else pad
            lines.append(prefix + ln)
            first = False
    if q.source:
        for i, ln in enumerate(textwrap.wrap(q.source, body_width - 4)):
            mark = "— " if i == 0 else "  "
            lines.append(pad + "  " + Style.dim(mark + ln))
    if show_page:
        tag = q.page
        if q.section:
            tag += " › " + q.section
        if q.kind == "dialogue":
            tag += " [dialogue]"
        lines.append(pad + "  " + Style.dim(Style.yellow(tag)))
    return "\n".join(lines)


# Commands like `quote A B` and `random -p 3` emit once per page. For JSON
# that must still add up to a single document, so collect and flush in main().
_JSON_BUFFER = []


def print_quotes(quotes, args, api: Api, header: str = "") -> None:
    if args.format == "json":
        _JSON_BUFFER.extend(q.as_dict() for q in quotes)
        return
    if args.format == "plain":
        for q in quotes:
            print(q.text.replace("\n", " "))
        return
    if args.format == "md":
        for q in quotes:
            body = "\n> ".join(q.text.split("\n"))
            print("> " + body)
            attr = q.source or q.page
            if attr:
                print(">\n> — %s" % attr)
            print()
        return

    width = args.width or term_width()
    if header:
        print()
        print(Style.bold(header))
        print(Style.dim("─" * min(width, len(header) + 12)))
    for i, q in enumerate(quotes, 1):
        print()
        print(render_quote(q, width, show_page=not args.bare,
                           number=None if args.no_number else i))
    print()


def copy_to_clipboard(text: str) -> bool:
    for cmd in (["pbcopy"], ["wl-copy"], ["xclip", "-selection", "clipboard"],
                ["xsel", "--clipboard", "--input"], ["clip.exe"], ["clip"]):
        if shutil.which(cmd[0]):
            try:
                subprocess.run(cmd, input=text.encode("utf-8"), check=True)
                return True
            except Exception:
                continue
    return False


# ---------------------------------------------------------------------------
# page-reference parsing ("Page", "Page#Section", full URL)
# ---------------------------------------------------------------------------


def parse_ref(ref: str):
    """Return (title, section) from a title, 'Title#Section', or a URL."""
    ref = ref.strip()
    if ref.startswith("http://") or ref.startswith("https://"):
        u = urllib.parse.urlparse(ref)
        path = urllib.parse.unquote(u.path)
        title = re.sub(r"^/wiki/", "", path)
        if not title or title == path:
            qs = urllib.parse.parse_qs(u.query)
            title = (qs.get("title") or [path.lstrip("/")])[0]
        section = urllib.parse.unquote(u.fragment).replace("_", " ")
        return title.replace("_", " "), section
    if "#" in ref:
        title, _, section = ref.partition("#")
        return title.replace("_", " ").strip(), \
            urllib.parse.unquote(section).replace("_", " ").strip()
    return ref.replace("_", " "), ""


# ---------------------------------------------------------------------------
# state: bookmarks + seen quotes
# ---------------------------------------------------------------------------


def _load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def _save_json(path, data):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except OSError as e:
        warn("could not write %s (%s)" % (path, e))


def load_bookmarks():
    return _load_json(BOOKMARKS, [])


def save_bookmarks(items):
    _save_json(BOOKMARKS, items)


def load_seen():
    return set(_load_json(SEEN, []))


def remember_seen(keys, cap=800):
    seen = list(load_seen())
    seen.extend(k for k in keys if k not in seen)
    _save_json(SEEN, seen[-cap:])


# ---------------------------------------------------------------------------
# shared fetch helpers
# ---------------------------------------------------------------------------


def fetch_page_quotes(api: Api, title: str, args, section: str = ""):
    wt = api.wikitext(title)
    qs = parse_quotes(wt, page=title, min_len=args.min or 16,
                      include_dialogue=not args.no_dialogue)
    return filter_quotes(qs, section=section or args.section,
                         grep=args.grep, minlen=args.min, maxlen=args.max,
                         about=args.about)


def fetch_many(api: Api, titles, args, workers: int = 4):
    results = {}
    if len(titles) == 1:
        try:
            results[titles[0]] = fetch_page_quotes(api, titles[0], args)
        except WikiquoteError as e:
            warn("%s: %s" % (titles[0], e))
        return results
    with futures.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(fetch_page_quotes, api, t, args): t for t in titles}
        for fut in futures.as_completed(futs):
            t = futs[fut]
            try:
                results[t] = fut.result()
            except WikiquoteError as e:
                warn("%s: %s" % (t, e))
    return results


def pick(quotes, n, rng, fresh=False):
    pool = list(quotes)
    if fresh:
        seen = load_seen()
        unseen = [q for q in pool if q.key() not in seen]
        if unseen:
            pool = unseen
    if n <= 0 or n >= len(pool):
        rng.shuffle(pool)
        return pool
    return rng.sample(pool, n)


def open_urls(urls, args):
    if args.dry_run:
        for u in urls:
            print(u)
        return
    for i, u in enumerate(urls):
        ok = webbrowser.open_new_tab(u) if i else webbrowser.open(u)
        if not ok:
            print(u)
        time.sleep(0.15)
    print(Style.green("opened %d tab%s" % (len(urls), "" if len(urls) == 1 else "s")))


def notice(args, msg: str) -> None:
    """Human-facing aside. Keeps stdout clean for json/plain/md pipelines."""
    if getattr(args, "format", "pretty") == "pretty":
        print(Style.dim(msg))
    else:
        sys.stderr.write(msg + "\n")


def emit(quotes, args, api, header=""):
    if not quotes:
        notice(args, "(no quotes matched)")
        return
    print_quotes(quotes, args, api, header=header)
    if args.copy:
        blob = "\n\n".join(
            q.text + ("\n— " + (q.source or q.page) if (q.source or q.page) else "")
            for q in quotes)
        print(Style.green("copied to clipboard") if copy_to_clipboard(blob)
              else Style.dim("(no clipboard tool found)"))
    remember_seen([q.key() for q in quotes])


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------


def cmd_random(args, api: Api, rng):
    titles = []
    if args.category:
        pool = api.category_walk(args.category, depth=args.depth)
        if not pool:
            die("no pages found in category %r" % args.category)
        titles = rng.sample(pool, min(args.pages, len(pool)))
    else:
        titles = api.random_titles(args.pages * 2)

    if args.open:
        chosen = titles[:args.pages]
        open_urls([api.page_url(t, args.section) for t in chosen], args)
        for t in chosen:
            print(Style.dim("  " + t))
        return

    shown = 0
    for title in titles:
        if shown >= args.pages:
            break
        if re.match(r"^(List of|Wikiquote:)", title):
            continue
        try:
            qs = fetch_page_quotes(api, title, args)
        except WikiquoteError as e:
            warn("%s: %s" % (title, e))
            continue
        if not qs:
            continue
        chosen = pick(qs, args.number, rng, fresh=args.fresh)
        emit(chosen, args, api, header="%s  (%s)" % (title, api.page_url(title)))
        shown += 1
    if shown == 0:
        die("random pages had no quotes this time — try again")


def cmd_quote(args, api: Api, rng):
    refs = [parse_ref(r) for r in args.page]
    titles = [t for t, _ in refs]
    per_ref_section = {t: s for t, s in refs}

    results = fetch_many(api, titles, args)
    any_out = False
    for title in titles:
        qs = results.get(title)
        if qs is None:
            continue
        sec = per_ref_section.get(title) or args.section
        if sec:
            qs = filter_quotes(qs, section=sec)
        if not qs:
            notice(args, "(%s: nothing matched)" % title)
            continue
        chosen = qs if args.all else pick(qs, args.number, rng, fresh=args.fresh)
        if not args.all:
            chosen.sort(key=lambda q: (q.section, q.text[:40]))
        emit(chosen, args, api,
             header="%s%s" % (title, "  › " + sec if sec else ""))
        any_out = True
    if not any_out:
        sys.exit(2)


def cmd_open(args, api: Api, rng):
    urls = []
    for ref in args.page:
        title, sec = parse_ref(ref)
        urls.append(api.page_url(title, sec or args.section))
    open_urls(urls, args)


def cmd_search(args, api: Api, rng):
    hits = api.search(" ".join(args.terms), limit=args.number)
    if not hits:
        die("no results for %r" % " ".join(args.terms))
    titles = [h["title"] for h in hits]

    if args.open:
        open_urls([api.page_url(t, args.section) for t in titles], args)
        return

    if not args.quotes:
        width = term_width()
        print()
        for i, h in enumerate(hits, 1):
            snippet = clean(re.sub(r"</?span[^>]*>", "", h.get("snippet", "")))
            print(Style.cyan("%2d. " % i) + Style.bold(h["title"]))
            for ln in textwrap.wrap(snippet, width - 6):
                print("    " + Style.dim(ln))
            print("    " + Style.dim(api.page_url(h["title"])))
        print()
        return

    results = fetch_many(api, titles, args)
    for t in titles:
        qs = results.get(t) or []
        if not qs:
            continue
        emit(pick(qs, args.per_page, rng, fresh=args.fresh), args, api,
             header=t)


def cmd_sections(args, api: Api, rng):
    title, _ = parse_ref(args.page)
    secs = api.sections(title)
    if not secs:
        die("%s has no sections" % title)
    print()
    print(Style.bold(title) + Style.dim("  " + api.page_url(title)))
    print()
    for s in secs:
        lvl = int(s.get("toclevel", 1))
        indent = "  " * lvl
        line = clean(s.get("line", ""))
        anchor = s.get("anchor") or anchor_encode(line)
        print("%s%s" % (indent, Style.bold(line)))
        if args.urls:
            print("%s  %s" % (indent, Style.dim("%s#%s" % (
                api.page_url(title), anchor))))
        else:
            print("%s  %s" % (indent, Style.dim("#" + anchor)))
    print()
    # Suggest a section that actually holds quotes: 'See also' and friends are
    # dropped by the parser, so a tip pointing at one returns nothing.
    quotable = [clean(s.get("line", "")) for s in secs
                if not SKIP_SECTIONS.match(clean(s.get("line", "")))]
    if quotable:
        print(Style.dim('tip: wq q "%s" -s "%s"' % (title, quotable[0])))
    print()


def cmd_category(args, api: Api, rng):
    pool = api.category_walk(args.name, depth=args.depth)
    if not pool:
        die("nothing in that category (try: wq cat-search %s)" % args.name)

    if args.list:
        for t in pool[:args.limit]:
            print(t)
        print(Style.dim("\n%d page(s)" % len(pool)))
        return

    n = args.random or 1
    chosen = rng.sample(pool, min(n, len(pool)))
    if args.open:
        open_urls([api.page_url(t) for t in chosen], args)
        for t in chosen:
            print(Style.dim("  " + t))
        return
    results = fetch_many(api, chosen, args)
    for t in chosen:
        qs = results.get(t) or []
        if qs:
            emit(pick(qs, args.number, rng, fresh=args.fresh), args, api,
                 header=t)


def cmd_cat_search(args, api: Api, rng):
    term = " ".join(args.terms).strip()
    # Category pages carry no prose, so full-text search finds nothing in
    # namespace 14. Go by title: prefixsearch first (it tolerates partial
    # words), then a strict allpages prefix, then full text as a last resort.
    hits = api.prefix_search("Category:" + term, limit=args.number,
                             namespace="14")
    if not hits:
        hits = api.all_pages(term[:1].upper() + term[1:], limit=args.number,
                             namespace="14")
    if not hits:
        hits = api.search(term, limit=args.number, namespace="14")
    if not hits:
        die("no categories matched %r" % term)
    print()
    for h in hits:
        name = h["title"]
        print("  " + Style.bold(name.split(":", 1)[-1]) +
              Style.dim("   (" + name + ")"))
    print()
    print(Style.dim('use: wq category "%s" --random 3' %
                    hits[0]["title"].split(":", 1)[-1]))
    print()


def cmd_wall(args, api: Api, rng):
    """A screenful of quotes, one from each of several random pages."""
    wanted = args.number
    collected = []
    attempts = 0
    while len(collected) < wanted and attempts < 4:
        attempts += 1
        need = (wanted - len(collected))
        if args.category:
            pool = api.category_walk(args.category, depth=args.depth)
            titles = rng.sample(pool, min(need * 2, len(pool)))
        else:
            titles = api.random_titles(need * 2)
        titles = [t for t in titles if not re.match(r"^(List of|Wikiquote:)", t)]
        results = fetch_many(api, titles, args, workers=6)
        for t in titles:
            if len(collected) >= wanted:
                break
            qs = results.get(t) or []
            qs = [q for q in qs if len(q.text) <= (args.max or 320)]
            if qs:
                collected.append(pick(qs, 1, rng, fresh=args.fresh)[0])
    if not collected:
        die("could not gather quotes — try again or widen --max")
    emit(collected, args, api, header="Wikiquote wall")


RE_TEMPLATE_BLOCK = re.compile(r"\{\{.*?\}\}", re.S)


def qotd_from_wikitext(wt: str, page: str):
    """Pull quote + author out of a Quote-of-the-day style template."""
    i = wt.find("{{")
    while i != -1:
        depth, j = 1, i + 2
        while j < len(wt) and depth:
            if wt.startswith("{{", j):
                depth += 1
                j += 2
            elif wt.startswith("}}", j):
                depth -= 1
                j += 2
            else:
                j += 1
        inner = wt[i + 2:j - 2]
        parts = _split_template(inner)
        named, positional = {}, []
        for part in parts[1:]:
            if re.match(r"\s*[A-Za-z_][\w \-]*\s*=", part):
                k, _, v = part.partition("=")
                named[k.strip().lower()] = v.strip()
            else:
                positional.append(part.strip())
        text = ""
        for key in ("quote", "text", "cite", "1"):
            if named.get(key):
                text = named[key]
                break
        if not text and positional:
            text = positional[0]
        author = ""
        for key in ("author", "by", "attribution", "2"):
            if named.get(key):
                author = named[key]
                break
        if not author and len(positional) > 1:
            author = positional[1]
        src = named.get("source") or named.get("work") or ""
        text, author, src = clean(text), clean(author), clean(src)
        if src and src not in author:
            author = (author + ", " + src) if author else src
        if len(text) >= 20:
            return Quote(text, author, page, "Quote of the day")
        i = wt.find("{{", j)
    # last resort: first bullet on the page
    qs = parse_quotes(wt, page=page)
    return qs[0] if qs else None


def cmd_qotd(args, api: Api, rng):
    import datetime
    d = datetime.date.today()
    if args.date:
        try:
            d = datetime.datetime.strptime(args.date, "%Y-%m-%d").date()
        except ValueError:
            die("--date wants YYYY-MM-DD")
    month = d.strftime("%B")
    candidates = [
        "Wikiquote:Quote of the day/%s %d, %d" % (month, d.day, d.year),
        "Wikiquote:Quote of the day/%s %d" % (month, d.day),
        "Wikiquote:Quote of the day/%s_%d,_%d" % (month, d.day, d.year),
    ]
    if args.open:
        open_urls([api.page_url("Main Page")], args)
        return
    for title in candidates:
        try:
            wt = api.wikitext(title)
        except WikiquoteError:
            continue
        q = qotd_from_wikitext(wt, title)
        if q:
            emit([q], args, api, header="Quote of the day — %s" % d.isoformat())
            return
    warn("couldn't find the quote-of-the-day page for %s" % d.isoformat())
    print(Style.dim("open it yourself: " + api.page_url("Main Page")))
    print(Style.dim("or try:           wq wall -n 1"))


def cmd_save(args, api: Api, rng):
    items = load_bookmarks()
    for ref in args.page:
        title, sec = parse_ref(ref)
        entry = {"title": title, "section": sec or args.section or "",
                 "note": args.note or "", "added": time.strftime("%Y-%m-%d")}
        if any(b["title"] == title and b.get("section", "") == entry["section"]
               for b in items):
            print(Style.dim("already saved: " + title))
            continue
        items.append(entry)
        print(Style.green("saved ") + title + (" › " + sec if sec else ""))
    save_bookmarks(items)


def cmd_saved(args, api: Api, rng):
    items = load_bookmarks()
    if not items:
        die('nothing saved yet — try: wq save "Samuel Johnson"')
    if args.random:
        items = rng.sample(items, min(args.random, len(items)))
    if args.open:
        open_urls([api.page_url(b["title"], b.get("section", ""))
                   for b in items], args)
        return
    if args.quotes or args.random:
        for b in items:
            try:
                qs = fetch_page_quotes(api, b["title"], args,
                                       section=b.get("section", ""))
            except WikiquoteError as e:
                warn("%s: %s" % (b["title"], e))
                continue
            if qs:
                emit(pick(qs, args.number, rng, fresh=args.fresh), args, api,
                     header=b["title"])
        return
    print()
    for i, b in enumerate(items, 1):
        label = b["title"] + (" › " + b["section"] if b.get("section") else "")
        print(Style.cyan("%2d. " % i) + Style.bold(label))
        if b.get("note"):
            print("    " + Style.dim(b["note"]))
        print("    " + Style.dim(api.page_url(b["title"], b.get("section", ""))))
    print()


def cmd_forget(args, api: Api, rng):
    items = load_bookmarks()
    if not items:
        die("nothing saved")
    keep, dropped = [], []
    for b in items:
        label = b["title"].lower()
        if any(t.lower() in label for t in args.page):
            dropped.append(b["title"])
        else:
            keep.append(b)
    save_bookmarks(keep)
    print(Style.green("removed: ") + ", ".join(dropped) if dropped
          else Style.dim("no match"))


def cmd_cache(args, api: Api, rng):
    if args.clear:
        n = 0
        for name in os.listdir(CACHE_DIR) if os.path.isdir(CACHE_DIR) else []:
            if name.endswith(".json"):
                try:
                    os.remove(os.path.join(CACHE_DIR, name))
                    n += 1
                except OSError:
                    pass
        print(Style.green("cleared %d cached response(s)" % n))
        return
    size, count = 0, 0
    if os.path.isdir(CACHE_DIR):
        for name in os.listdir(CACHE_DIR):
            p = os.path.join(CACHE_DIR, name)
            try:
                size += os.path.getsize(p)
                count += 1
            except OSError:
                pass
    print("cache dir : %s" % CACHE_DIR)
    print("entries   : %d (%.1f KiB)" % (count, size / 1024.0))
    print("bookmarks : %s" % BOOKMARKS)


# ---------------------------------------------------------------------------
# selftest (offline)
# ---------------------------------------------------------------------------

FIXTURE_THEME = """\
{{wikipedia}}
'''Diligence''' is steadfast application, industriousness.

==Quotes==
{{quotebox|not a quote line}}
* Diligence is the mother of good fortune.<ref>cite web</ref>
** [[Miguel de Cervantes]], ''[[Don Quixote]]'' (1605), Part II
** Variant translation
* The leading rule for the [[lawyer]] is diligence.
** [[Abraham Lincoln]], ''Notes for a Law Lecture'' (1 July 1850)
* <!-- hidden --> To rank the effort above the prize may be called love.

===Poetry===
* I say to you: Make perfect your will.
*: I say: Take no thought of the harvest,
*: But only of proper sowing.
** T. S. Eliot

==Quotes about diligence==
* He was diligent beyond all men.
** A friend

==See also==
* [[Work]]
* [[Persistence]]

==External links==
{{wikipedia|Diligence}}
* [https://example.com Example link about diligence and things]
"""

FIXTURE_PERSON = """\
==Zanoni (1842)==
* The [[beautiful]] is above the ''pretty''.
** Book I, Chapter 1
* Beneath the rule of men entirely great, the pen is mightier than the sword.
** Richelieu (1839), Act II

==Dialogue==
: '''Alice''': Who are you?
: '''Bob''': Nobody in particular, thank you.

==References==
* Should never appear as a quote at all, not once.
"""


class _FakeErr:
    def __init__(self, headers):
        self.headers = headers


def _selftest():
    fails = []

    def check(cond, label):
        if cond:
            print(Style.green("  ok  ") + label)
        else:
            print(Style.red(" FAIL ") + label)
            fails.append(label)

    print(Style.bold("\nparser selftest\n"))

    qs = parse_quotes(FIXTURE_THEME, page="Diligence")
    texts = [q.text for q in qs]
    sources = [q.source for q in qs]

    check(len(qs) == 5, "theme page yields 5 quotes (got %d)" % len(qs))
    check(any(t.startswith("Diligence is the mother") for t in texts),
          "plain quote extracted")
    check(all("<ref" not in t and "cite web" not in t for t in texts),
          "<ref> stripped")
    check(any("Miguel de Cervantes, Don Quixote (1605), Part II — Variant"
              in s for s in sources),
          "attribution joined and links unwrapped")
    check(all("[[" not in t and "''" not in t for t in texts),
          "wiki markup stripped")
    check(not any("Work" == t or "Persistence" == t for t in texts),
          "See also section skipped")
    check(not any("Example link" in t for t in texts),
          "External links section skipped")
    check(not any("not a quote line" in t for t in texts),
          "template body dropped")
    # Regression guards for bugs found against live pages.
    check(clean("Must die unheard in Dim {{w|Carcosa}}.")
          == "Must die unheard in Dim Carcosa.",
          "{{w|X}} interwiki template keeps its text")
    check(clean("in ''{{w|The King in Yellow}}'' (1895)")
          == "in The King in Yellow (1895)",
          "{{w|X}} inside italics keeps its text")
    check(clean("{{w|The King in Yellow|that play}}") == "that play",
          "{{w|target|label}} keeps the label")
    check(clean("{{small|a note}}") == "a note",
          "{{small|...}} keeps its text")
    check(_retry_after(_FakeErr({"Retry-After": "2"})) == 2.0,
          "Retry-After seconds parsed")
    check(_retry_after(_FakeErr({})) == 0.0,
          "missing Retry-After is 0")

    check(any("hidden" not in t for t in texts) and
          all("hidden" not in t for t in texts),
          "HTML comment removed")

    poem = [q for q in qs if q.text.startswith("I say to you")]
    check(bool(poem) and poem[0].text.count("\n") == 2,
          "poetry line breaks preserved via '*:'")
    check(bool(poem) and poem[0].section == "Poetry",
          "subsection recorded (got %r)" % (poem[0].section if poem else None))

    about = [q for q in qs if q.about]
    check(len(about) == 1 and about[0].text.startswith("He was diligent"),
          "'Quotes about' section flagged")

    qs2 = parse_quotes(FIXTURE_PERSON, page="Edward Bulwer-Lytton")
    secs = {q.section for q in qs2}
    check("Zanoni (1842)" in secs, "person page section captured")
    check(not any("never appear" in q.text for q in qs2),
          "References section skipped")
    dlg = [q for q in qs2 if q.kind == "dialogue"]
    check(len(dlg) == 1 and "Alice" in dlg[0].text,
          "dialogue block captured")

    zan = filter_quotes(qs2, section="Zanoni_(1842)")
    check(len(zan) == 2, "filter by anchor-style section name (got %d)" % len(zan))

    check(anchor_encode("Zanoni (1842)") == "Zanoni_(1842)",
          "anchor encoding keeps parentheses")
    check(anchor_encode("A & B") == "A_&_B", "anchor encoding keeps ampersand")

    t, s = parse_ref("https://en.wikiquote.org/wiki/Edward_Bulwer-Lytton#Zanoni_(1842)")
    check((t, s) == ("Edward Bulwer-Lytton", "Zanoni (1842)"),
          "URL parsed into title + section (got %r)" % ((t, s),))
    t, s = parse_ref("Samuel Johnson#Rasselas")
    check((t, s) == ("Samuel Johnson", "Rasselas"), "Title#Section parsed")
    t, s = parse_ref("Diligence")
    check((t, s) == ("Diligence", ""), "bare title parsed")

    api = Api()
    check(api.page_url("Edward Bulwer-Lytton", "Zanoni (1842)") ==
          "https://en.wikiquote.org/wiki/Edward_Bulwer-Lytton#Zanoni_(1842)",
          "url round-trips the pasted link")

    check(clean("{{lang|fr|Je pense}}") == "Je pense", "lang template kept")
    check(clean("[[File:x.jpg|thumb|caption]] text") == "text",
          "file link dropped")
    check(clean("[https://a.example Label] tail") == "Label tail",
          "external link label kept")
    check(clean("&mdash;dash") == "—dash", "html entities decoded")

    print()
    if fails:
        print(Style.red("%d check(s) failed" % len(fails)))
        return 1
    print(Style.green("all checks passed"))
    return 0


# ---------------------------------------------------------------------------
# cli
# ---------------------------------------------------------------------------


def add_common(p, number_default=3):
    p.add_argument("-n", "--number", type=int, default=number_default,
                   help="quotes to show per page (0 = all)")
    p.add_argument("-s", "--section", default="",
                   help="only this section (substring or anchor, e.g. 'Zanoni (1842)')")
    p.add_argument("-g", "--grep", default="", help="regex filter on text/source")
    p.add_argument("--min", type=int, default=0, help="minimum characters")
    p.add_argument("--max", type=int, default=0, help="maximum characters")
    p.add_argument("--about", choices=["include", "exclude", "only"],
                   default="include", help="handling of 'Quotes about X' sections")
    p.add_argument("--no-dialogue", action="store_true",
                   help="skip film/TV dialogue blocks")
    p.add_argument("--fresh", action="store_true",
                   help="prefer quotes not shown before")
    p.add_argument("--copy", action="store_true", help="copy output to clipboard")
    p.add_argument("--bare", action="store_true", help="hide page/section tags")
    p.add_argument("--no-number", action="store_true", help="hide numbering")


def add_globals(p, suppress=False):
    """Global options. With suppress=True they are attached to subparsers
    too, so `wq open X --dry-run` works as well as `wq --dry-run open X`."""
    d = (lambda v: argparse.SUPPRESS) if suppress else (lambda v: v)
    g = p.add_argument_group("global options") if suppress else p
    g.add_argument("--lang", default=d(DEFAULT_LANG),
                   help="wikiquote language edition (default: %s)" % DEFAULT_LANG)
    g.add_argument("--format", choices=["pretty", "plain", "md", "json"],
                   default=d("pretty"))
    g.add_argument("--width", type=int, default=d(0), help="wrap width")
    g.add_argument("--color", choices=["auto", "always", "never"],
                   default=d("auto"))
    g.add_argument("--seed", type=int, default=d(None),
                   help="deterministic picks")
    g.add_argument("--no-cache", action="store_true", default=d(False))
    g.add_argument("--cache-ttl", type=int, default=d(86400),
                   help="cache lifetime in seconds (default 86400)")
    g.add_argument("--dry-run", action="store_true", default=d(False),
                   help="print URLs instead of opening a browser")


def build_parser():
    globals_parent = argparse.ArgumentParser(add_help=False)
    add_globals(globals_parent, suppress=True)

    p = argparse.ArgumentParser(
        prog="wq", description="a Wikiquote command-line companion",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            examples
              wq random                            a random page
              wq random -n 5 --open                five random pages in tabs
              wq random --category Themes -n 2     random *theme* pages
              wq q Diligence                       quotes from one page
              wq q "Edward Bulwer-Lytton" -s "Zanoni (1842)"
              wq q https://en.wikiquote.org/wiki/Samuel_Johnson --all
              wq open Diligence "Samuel Johnson" Main_Page
              wq search "memento mori" --quotes
              wq sections "Edward Bulwer-Lytton" --urls
              wq category Philosophers --random 3
              wq wall -n 12 --max 240
              wq save "Samuel Johnson" -N "for the essays"
              wq saved --random 1
        """))
    p.add_argument("-V", "--version", action="version", version="wq " + __version__)
    add_globals(p)

    sub = p.add_subparsers(dest="cmd")
    _add = sub.add_parser

    def sub_add(name, **kw):
        kw.setdefault("parents", [globals_parent])
        return _add(name, **kw)

    sub.add_parser = sub_add

    sp = sub.add_parser("random", aliases=["r"], help="random page(s)")
    sp.add_argument("-p", "--pages", type=int, default=1, help="how many pages")
    sp.add_argument("-o", "--open", action="store_true", help="open in browser")
    sp.add_argument("-c", "--category", default="",
                   help="restrict to a category, e.g. Themes")
    sp.add_argument("--depth", type=int, default=1, help="subcategory depth")
    add_common(sp)
    sp.set_defaults(func=cmd_random)

    sp = sub.add_parser("quote", aliases=["q"], help="quotes from named page(s)")
    sp.add_argument("page", nargs="+", help="title, Title#Section, or URL")
    sp.add_argument("-a", "--all", action="store_true", help="show every quote")
    add_common(sp)
    sp.set_defaults(func=cmd_quote)

    sp = sub.add_parser("open", aliases=["o"], help="open page(s) in browser tabs")
    sp.add_argument("page", nargs="+", help="title, Title#Section, or URL")
    sp.add_argument("-s", "--section", default="", help="section anchor for all")
    sp.set_defaults(func=cmd_open)

    sp = sub.add_parser("search", aliases=["s"], help="search Wikiquote")
    sp.add_argument("terms", nargs="+")
    sp.add_argument("-o", "--open", action="store_true", help="open all hits")
    sp.add_argument("--quotes", action="store_true", help="show quotes from hits")
    sp.add_argument("--per-page", type=int, default=2,
                   help="quotes per hit with --quotes")
    add_common(sp, number_default=8)
    sp.set_defaults(func=cmd_search)

    sp = sub.add_parser("sections", aliases=["sec"], help="list a page's sections")
    sp.add_argument("page")
    sp.add_argument("--urls", action="store_true", help="print full anchor URLs")
    sp.set_defaults(func=cmd_sections)

    sp = sub.add_parser("category", aliases=["cat"], help="browse a category")
    sp.add_argument("name")
    sp.add_argument("-l", "--list", action="store_true", help="list page titles")
    sp.add_argument("-r", "--random", type=int, default=0,
                   help="sample N pages for quotes")
    sp.add_argument("-o", "--open", action="store_true", help="open the sample")
    sp.add_argument("--depth", type=int, default=1, help="subcategory depth")
    sp.add_argument("--limit", type=int, default=200, help="max titles to list")
    add_common(sp, number_default=2)
    sp.set_defaults(func=cmd_category)

    sp = sub.add_parser("cat-search", help="find category names")
    sp.add_argument("terms", nargs="+")
    sp.add_argument("-n", "--number", type=int, default=15)
    sp.set_defaults(func=cmd_cat_search)

    sp = sub.add_parser("wall", aliases=["w"],
                        help="one quote each from several random pages")
    sp.add_argument("-c", "--category", default="", help="restrict to a category")
    sp.add_argument("--depth", type=int, default=1)
    add_common(sp, number_default=10)
    sp.set_defaults(func=cmd_wall)

    sp = sub.add_parser("qotd", help="Wikiquote's quote of the day")
    sp.add_argument("-d", "--date", default="", help="YYYY-MM-DD")
    sp.add_argument("-o", "--open", action="store_true",
                    help="open the Main Page instead")
    add_common(sp, number_default=1)
    sp.set_defaults(func=cmd_qotd)

    sp = sub.add_parser("save", help="bookmark a page")
    sp.add_argument("page", nargs="+")
    sp.add_argument("-s", "--section", default="")
    sp.add_argument("-N", "--note", default="")
    sp.set_defaults(func=cmd_save)

    sp = sub.add_parser("saved", help="list or use bookmarks")
    sp.add_argument("-o", "--open", action="store_true")
    sp.add_argument("-r", "--random", type=int, default=0,
                    help="pull quotes from N random bookmarks")
    sp.add_argument("--quotes", action="store_true", help="quotes from all")
    add_common(sp, number_default=2)
    sp.set_defaults(func=cmd_saved)

    sp = sub.add_parser("forget", help="remove bookmark(s)")
    sp.add_argument("page", nargs="+")
    sp.set_defaults(func=cmd_forget)

    sp = sub.add_parser("cache", help="inspect or clear the cache")
    sp.add_argument("--clear", action="store_true")
    sp.set_defaults(func=cmd_cache)

    sp = sub.add_parser("selftest", help="run offline parser checks")
    sp.set_defaults(func=None)

    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    Style.setup(force=args.color == "always", disable=args.color == "never")

    if args.cmd is None:
        parser.print_help()
        return 0
    if args.cmd == "selftest":
        return _selftest()

    # defaults for subcommands that skip add_common
    for name, default in (("number", 3), ("section", ""), ("grep", ""),
                          ("min", 0), ("max", 0), ("about", "include"),
                          ("no_dialogue", False), ("fresh", False),
                          ("copy", False), ("bare", False),
                          ("no_number", False)):
        if not hasattr(args, name):
            setattr(args, name, default)

    rng = random.Random(args.seed)
    api = Api(lang=args.lang, cache_ttl=args.cache_ttl,
              use_cache=not args.no_cache)

    try:
        args.func(args, api, rng)
        if args.format == "json" and _JSON_BUFFER:
            print(json.dumps(_JSON_BUFFER, ensure_ascii=False, indent=2))
    except MissingPage as e:
        die("page not found: %s" % e)
    except WikiquoteError as e:
        die(str(e))
    except KeyboardInterrupt:
        print()
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
