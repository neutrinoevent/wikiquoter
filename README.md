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

No install, no dependencies. `wq selftest` runs 32 offline parser checks.
For a browser front end, see [the reading room](#the-reading-room).

## A quick tour

```bash
wq qotd                                    # Main_Page's quote of the day
wq q Diligence                             # the Diligence page
wq q "Samuel Johnson" -n 5                 # five from Samuel Johnson
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
| `wq search TERMS` (`s`) | search Wikiquote | `--quotes` show quotes from hits · `--per-page N` how many each · `-o` open every hit |
| `wq sections PAGE` (`sec`) | section tree with `#anchors` | `--urls` full anchor URLs |
| `wq category NAME` (`cat`) | browse a category | `-l` list titles · `-r N` sample N pages · `-o` open the sample · `--limit N` · `--depth N` |
| `wq cat-search TERMS` | find category names worth using | `-n N` how many |
| `wq wall -n 12` (`w`) | one quote each from several random pages | `-c CAT` confine it to a category · `--depth N` |
| `wq qotd` | Wikiquote's quote of the day | `-d YYYY-MM-DD` another day · `-o` open the Main Page |
| `wq save PAGE` | bookmark a page | `-s SECTION` · `-N NOTE` |
| `wq saved` | list or use bookmarks | `-r N` quotes from N random bookmarks · `--quotes` from all · `-o` open them |
| `wq forget PAGE...` | drop bookmarks | |
| `wq cache` | cache location and size | `--clear` |
| `wq selftest` | 32 offline parser checks | |

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
```

`--dry-run` prints URLs instead of opening a browser — handy over SSH.

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

`/` focuses the box, `↑`/`↓` walk the history, `◐` toggles light and dark.

## Things worth trying

```bash
wq wall -n 15 --max 200                  # short ones, a screenful
wq q Stoicism --format md >> notes.md    # markdown blockquotes
wq q Diligence --format json | jq -r '.[].text'
wq random --format plain -n 1 --bare     # pipe into cowsay / a login banner
wq q "Samuel Johnson" -g "idleness|dictionary"
wq q Socrates --about only               # what others said *about* him
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
explicitly. `wq selftest` pins all of these (32 checks, no network needed):

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
