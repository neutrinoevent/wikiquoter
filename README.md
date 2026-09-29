# wq — a Wikiquote command-line companion

Standard library only, Python 3.8+. `wq.py` reads quotes out of Wikiquote's
wikitext and either prints them nicely or throws pages open in browser tabs;
`serve.py` puts the same commands behind a browser front end.

```bash
chmod +x wq.py
./wq.py random
# optional, so it's just `wq`:
mv wq.py ~/.local/bin/wq      # or:  ln -s "$PWD/wq.py" /usr/local/bin/wq
```

No install, no dependencies. `wq selftest` runs 41 offline checks.
For a browser front end, see [the reading room](#the-reading-room).

## A quick tour

```bash
wq qotd                                    # Main_Page's quote of the day
wq q Diligence                             # the Diligence page
wq q "Samuel Johnson" -n 5                 # five from Samuel Johnson
wq mentions birthday                       # quotes on any page that say it
wq sections "Edward Bulwer-Lytton"         # every section + its #anchor
wq q "Edward Bulwer-Lytton" -s "Zanoni (1842)" --all
wq open https://en.wikiquote.org/wiki/Edward_Bulwer-Lytton#Zanoni_\(1842\)
```

Anywhere a page is expected you can pass a **title**, `Title#Section`, or a
**pasted URL** — all three parse the same way.

## Commands

Every reading command also takes the filters in the next section.

| command | what it does | its own flags |
|---|---|---|
| `wq random` (`r`) | a random page with a few of its quotes | `-p N` pages · `-o` open in tabs · `-c CAT` draw from a category · `--depth N` subcategory depth |
| `wq quote PAGE...` (`q`) | quotes from named pages, fetched in parallel | `-a` every quote |
| `wq open PAGE...` (`o`) | open pages/sections in browser tabs | `-s SECTION` |
| `wq search TERMS` (`s`) | search Wikiquote | `--quotes` show quotes from hits · `--per-page N` how many each · `--offset N` page on · `-o` open every hit |
| `wq mentions TERMS` (`m`) | quotes from any page whose text contains the phrase | `-n N` pages searched per run (20) · `--per-page N` quotes each (3, 0 = all) · `--offset N` the next batch |
| `wq sections PAGE` (`sec`) | section tree with `#anchors` | `--urls` full anchor URLs |
| `wq category NAME` (`cat`) | browse a category | `-l` list titles · `-r N` sample N pages · `-o` open the sample · `--limit N` · `--depth N` |
| `wq cat-search TERMS` | find category names worth using | `-n N` how many |
| `wq wall -n 12` (`w`) | one quote each from several random pages | `-c CAT` confine it to a category · `--depth N` |
| `wq qotd` | Wikiquote's quote of the day | `-d YYYY-MM-DD` another day · `-o` open the Main Page |
| `wq save PAGE` | bookmark a page | `-s SECTION` · `-N NOTE` |
| `wq saved` | list or use bookmarks | `-r N` quotes from N random bookmarks · `--quotes` from all · `-o` open them |
| `wq forget PAGE...` | drop bookmarks | |
| `wq cache` | cache location and size | `--clear` |
| `wq selftest` | 41 offline checks | |

## Filtering and output

Available on every reading command:

```
-n, --number N     quotes per page (0 = all)      -a, --all       every quote
-s, --section S    section substring or anchor    -g, --grep RE   regex filter
--min / --max N    length bounds                  --fresh         prefer unseen
--about include|exclude|only    'Quotes about X' sections
--no-dialogue      skip film/TV dialogue blocks
--copy             to clipboard   --bare  --no-number
```

Global (accepted before *or* after the subcommand):

```
--format pretty|plain|md|json    --lang de|fr|es|...   --seed N
--width N   --color auto|always|never   --dry-run   --no-cache  --cache-ttl S
--envelope    with --format json: an object instead of a bare list
```

`--dry-run` prints URLs instead of opening a browser — handy over SSH.

### Reading further

A reading command shows a sample — three quotes from a page by default — and
says where the rest is. In the default format it ends with the commands that
widen the net:

```
keep reading
  wq q "Quantum mechanics" -a      All 71 quotes from Quantum mechanics
  wq mentions "Quantum mechanics"  Quotes that mention “Quantum mechanics” on other pages (347 pages)
  wq mentions Quantum              Quotes that mention “Quantum” (905 pages)
  wq mentions mechanics            Quotes that mention “mechanics” (987 pages)
  related pages: History of quantum mechanics · Measurement in quantum mechanics · …
  also starting: Quantum field theory · Quantum computing · Quantum gravity · …
  categories: Quantum mechanics  (wq random -c NAME -p 3)
```

The directions, widest last:

- **the rest of the page** — every quote, with any filters in play (`-s`,
  `-g`, `--max`, …) carried over; or, if the filters left nothing, the page
  without them;
- **related pages** — pages whose titles contain the page's name;
- **also starting** — pages whose titles share its first word;
- **categories** the page belongs to — three random pages from one, and
  three more each time;
- **mentions** — quotes on any page that contain the phrase, and, for a
  phrase of several words, each word on its own. Counts are pages found by
  full-text search. `mentions` reads search results a batch at a time and
  suggests the next batch.

A phrase that is not a page still gets the mentions and broader-word steps.

`--format json` stays a bare list of quotes, one document per run.
`--format json --envelope` wraps the same list with what was left out:

```json
{
  "quotes": [ ... ],
  "pages":  [{"title": "Birthday", "section": "", "shown": 3, "total": 22,
              "all_cmd": "q Birthday -a", "url": "https://…"}],
  "next":   [{"kind": "more", "label": "All 22 quotes from Birthday",
              "cmd": "q Birthday -a"}, ...],
  "search": null
}
```

`next[].kind` is `more` (the rest of a page), `page` (a related page),
`nearby` (same first word), `category`, `mentions`, `broader` (one word of
the phrase), or `next-batch`. `cmd` is a `wq` command line without the leading
`wq`; `count`, where present, is the quote total (`more`) or pages found
(`mentions`, `broader`); `term` is the word or phrase a step is about.
`search` is set by `mentions`: which result pages were read, out of how many.
When nothing matched (exit 2) the envelope is still printed, with an empty
`quotes` list and whatever steps apply.

## The reading room

A browser front end, same standard-library-only rule:

```bash
./serve.py                 # http://127.0.0.1:8787, opens a browser
./serve.py -p 9000 --no-open
```

The box takes the **same grammar as the shell** — `wall -n 8`,
`q "Samuel Johnson" -n 5`, `qotd`, a pasted URL — so nothing new to learn.
Type a bare phrase and it is read as a page title; if there is no such page,
it finds the nearest one that exists and says so (`marcus aurelius` →
*Marcus Aurelius*).

`serve.py` runs `wq.py` as a subprocess with `--format json` and renders what
comes back, so the CLI stays the single source of truth — the web view can't
drift from the terminal one. Quotes are set in a serif at a reading measure;
click any one to copy it.

Every view is a URL, so a query can be bookmarked or shared:

```
http://127.0.0.1:8787/?q=wall+-n+8
http://127.0.0.1:8787/?q=q+Stoicism&lang=de
```

Results show how much there was — *3 of 71 quotes* — and, right under the
heading, a **widen** line that says how far a reading can go: *all 71 ·
mentioned on 347 pages · “Quantum” (905) · 5 pages about it · 1 category*.
The full **keep reading** list under the quotes holds the same suggestions the
terminal prints, grouped the same way. Everything in both is a click, and a
search that finds nothing still offers where else to look.

`/` focuses the box, `↑`/`↓` walk the history, `◐` toggles light and dark.

## Things worth trying

```bash
wq wall -n 15 --max 200                  # short ones, a screenful
wq q Stoicism --format md >> notes.md    # markdown blockquotes
wq q Diligence --format json | jq -r '.[].text'
wq random --format plain -n 1 --bare     # pipe into cowsay / a login banner
wq q "Samuel Johnson" -g "idleness|dictionary"
wq q Socrates --about only               # what others said *about* him
wq mentions "memento mori" --per-page 1  # one line each, from many pages
wq cat-search philosoph                  # then: wq cat Philosophers -r 3
wq --lang de random                      # any language edition
wq q Hope --copy                         # straight to the clipboard
```

A fortune-style shell greeting:

```bash
echo 'wq wall -n 1 --max 220 --fresh 2>/dev/null' >> ~/.zshrc
```

## Notes on how it works

- Talks to the MediaWiki API (`action=parse&prop=wikitext`), parses the
  wikitext itself: `*` lines are quotes, `**` lines their attribution, `*:`
  lines keep poetry line breaks, and `See also` / `References` /
  `External links` sections are skipped.
- Templates, `<ref>` tags, comments, file links and wiki markup are stripped;
  `{{lang|fr|…}}` and `{{quote|…}}` keep their text.
- Responses are cached 24h under `~/.cache/wq`; bookmarks live in
  `~/.config/wq/bookmarks.json`.
- Requests are sequential per page with a small thread pool (4–6) for
  multi-page commands, spaced ~60ms apart across threads, with exponential
  backoff on 429/5xx that honours `Retry-After` — which keeps it polite
  toward Wikimedia's servers. Set
  `WQ_CONTACT="you@example.com"` to add contact info to the User-Agent, as
  the API etiquette guidelines ask.
- `WQ_LANG=de` sets a default language edition.

Wikiquote text is CC BY-SA; if you republish quotes, credit the page.

## Parsing notes

Wikitext is loose, and a few constructs bite if they are not handled
explicitly. `wq selftest` pins all of these (no network needed):

- **Interwiki-link templates carry visible text.** `{{w|Carcosa}}` renders as
  *Carcosa*, and `{{w|The King in Yellow|that play}}` as *that play*. A
  stripper that drops unknown templates wholesale will quietly delete words
  from the middle of a quote.
- **Category pages have no prose to index**, so a full-text search in
  namespace 14 returns nothing. `cat-search` goes by title instead
  (`list=prefixsearch`), falling back to an `allpages` prefix.
- **`--format json` is one document, not one per page.** `wq q A B` and
  `wq random -p 3` render several pages, so quotes are buffered and flushed
  once; concatenated arrays would not survive a pipe into `jq`. Human-facing
  notices go to stderr in the machine formats, leaving stdout clean.
- **`See also` / `References` / `External links` are skipped**, so anything
  that suggests a section to read — such as the tip `wq sections` prints —
  has to pick one that actually holds quotes.

If a command misbehaves on real data, run it with `--format json`; the
fix is almost certainly in `parse_quotes()`.
