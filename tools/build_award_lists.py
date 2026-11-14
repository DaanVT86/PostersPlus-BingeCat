#!/usr/bin/env python3
"""Regenerate the cult film, festival winner and Best Picture lists in data/.

    python3 tools/build_award_lists.py            # write all three files
    python3 tools/build_award_lists.py cult       # just one: cult | festivals | best_picture

The lists replace MDBList keywords as the source of the Cult Classic, festival
and Oscar badges (see docs/plans/mdblist-bulk-ratings-plan.md), so ratings can
be fetched in bulk, which carries no keywords.  Each list comes from English
Wikipedia: the film named in each table row is resolved to its Wikidata item
(``prop=pageprops``, which follows redirects) and from there to its IMDb id
(P345).  A row whose film has no article, or whose item has no IMDb id, is
dropped.

    data/cult_films.json            {imdb_id: title}           "List of cult films: 0–9 / A–Z"
    data/festival_winners.json      {festival: [imdb_id, ...]} the five top prizes' winners tables
    data/awards_best_picture.json   {"winners": [...], "nominees": [...]}

The content is derived from Wikipedia and is CC BY-SA 4.0; README "Data
sources" and the admin page footer carry the credit.

MediaWiki answers 429 quickly, so calls are spaced a second apart and a 429's
Retry-After is honoured.  A full run is about 200 calls.  Nothing here runs in
production: the files it writes are reviewed and committed like any change.
"""

import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = REPO_ROOT / "data"

WIKIPEDIA_API = "https://en.wikipedia.org/w/api.php"
WIKIDATA_API = "https://www.wikidata.org/w/api.php"
USER_AGENT = "PostersPlus-list-builder/1.0"
CALL_SPACING = 1.0
BATCH = 50   # titles / ids per pageprops and wbgetentities call

CULT_PAGES = ["List of cult films: 0–9"] + [f"List of cult films: {c}" for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"]

# festival key -> (page, section the winners tables sit under, subsections to skip)
FESTIVAL_PAGES: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "cannes":   ("Palme d'Or",              "Winners", ("Special Palme d'Or",)),
    "venice":   ("Golden Lion",             "Winners", ()),
    "berlin":   ("Golden Bear",             "Winners", ()),
    "locarno":  ("Golden Leopard",          "Winners", ()),
    "sundance": ("Grand Jury Prize Dramatic", "Winners", ()),
}

BEST_PICTURE_PAGE = ("Academy Award for Best Picture", "Winners and nominees")


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

_calls = 0


def _get(url: str, params: dict) -> dict:
    global _calls
    for _attempt in range(6):
        _calls += 1
        time.sleep(CALL_SPACING)
        req = urllib.request.Request(url + "?" + urllib.parse.urlencode(params), headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            if exc.code not in (429, 503):
                raise
            wait = exc.headers.get("Retry-After")
            try:
                wait = float(wait) if wait else 10.0
            except ValueError:
                wait = 10.0
            print(f"  {exc.code}; waiting {wait:.0f}s", file=sys.stderr)
            time.sleep(wait + 1)
    raise RuntimeError(f"still rate limited: {url}")


def _wikitext(page: str) -> str:
    data = _get(WIKIPEDIA_API, {"action": "parse", "page": page, "prop": "wikitext",
                                "redirects": 1, "format": "json", "formatversion": 2})
    if "error" in data:
        raise RuntimeError(f"{page}: {data['error'].get('info')}")
    return data.get("parse", {}).get("wikitext", "")


def resolve_imdb_ids(titles: list[str]) -> dict[str, tuple[str, str]]:
    """Article title -> (imdb_id, English label), for the titles that resolve."""
    titles = list(dict.fromkeys(t for t in titles if t))
    # Article -> Wikidata item.  Redirects and normalisation rename the page,
    # so both maps are walked back to the title that was asked for.
    qid_by_title: dict[str, str] = {}
    for i in range(0, len(titles), BATCH):
        chunk = titles[i:i + BATCH]
        r = _get(WIKIPEDIA_API, {"action": "query", "prop": "pageprops", "ppprop": "wikibase_item",
                                 "redirects": 1, "titles": "|".join(chunk),
                                 "format": "json", "formatversion": 2})
        q = r.get("query", {})
        renamed: dict[str, str] = {}
        for step in ("normalized", "redirects"):
            for m in q.get(step, []):
                renamed[m["to"]] = m["from"]
        for p in q.get("pages", []):
            item = p.get("pageprops", {}).get("wikibase_item")
            if not item:
                continue
            name = p["title"]
            while name in renamed and renamed[name] != name:
                name = renamed.pop(name)
            qid_by_title[name] = item

    imdb_by_qid: dict[str, tuple[str, str]] = {}
    qids = list(dict.fromkeys(qid_by_title.values()))
    for i in range(0, len(qids), BATCH):
        r = _get(WIKIDATA_API, {"action": "wbgetentities", "ids": "|".join(qids[i:i + BATCH]),
                                "props": "claims|labels", "languages": "en", "format": "json"})
        for qid, entity in r.get("entities", {}).items():
            imdb = first_imdb_claim(entity)
            if imdb:
                imdb_by_qid[qid] = (imdb, entity.get("labels", {}).get("en", {}).get("value", qid))

    return {t: imdb_by_qid[q] for t, q in qid_by_title.items() if q in imdb_by_qid}


def first_imdb_claim(entity: dict) -> str | None:
    """The item's first title-shaped (tt…) P345 value."""
    for claim in entity.get("claims", {}).get("P345", []):
        value = claim.get("mainsnak", {}).get("datavalue", {}).get("value", "")
        if isinstance(value, str) and value.startswith("tt"):
            return value
    return None


# ---------------------------------------------------------------------------
# Wikitext parsing (pure — tested on fixture text)
# ---------------------------------------------------------------------------

_SORTNAME = re.compile(r"\{\{\s*sortname\s*\|([^}]*)\}\}", re.I)
_LINK = re.compile(r"\[\[([^\]|#]+)")
# A film title is italic: ''[[Film]]'', '''''[[Winner]]''''' or [[Film|''Film'']].
_ITALIC_LINK = re.compile(r"''\s*\[\[([^\]|#]+)|\[\[([^\]|#]+)\|\s*''")
# Winners tables shade their "no award" / "cancelled" note rows and sort them last.
_NOTE_ROW = 'data-sort-value="ω"'


def _clean(title: str) -> str:
    title = title.replace("_", " ").strip()
    return title[:1].upper() + title[1:]


def _sortname_title(inner: str) -> str | None:
    """{{sortname|The|Rocky Horror Picture Show|dab=film}} -> "The Rocky Horror Picture Show (film)"."""
    parts = [p.strip() for p in inner.split("|")]
    named = [p for p in parts if "=" not in p]
    opts = dict(p.split("=", 1) for p in parts if "=" in p)
    if opts.get("nolink"):
        return None
    title = opts.get("1") or " ".join(named[:2])
    if opts.get("dab"):
        title += f" ({opts['dab']})"
    return title.strip() or None


def parse_cult_rows(wikitext: str) -> list[str]:
    """Article titles of the films on one "List of cult films" page: the first
    cell of each table row, as a plain link or in the sortname form."""
    out: list[str] = []
    for row in wikitext.split("\n|-"):
        lines = [l for l in row.split("\n") if l.startswith("|") or l.startswith("!")]
        if not lines:
            continue
        cell = lines[0].split("||")[0]
        s = _SORTNAME.search(cell)
        if s:
            title = _sortname_title(s.group(1))
            if title:
                out.append(_clean(title))
            continue
        m = _LINK.search(cell)
        if m:
            out.append(_clean(m.group(1)))
    return list(dict.fromkeys(out))


def section(wikitext: str, heading: str, skip: tuple[str, ...] = ()) -> str:
    """The text under ``== heading ==`` up to the next heading of the same or a
    higher level, leaving out any subsection named in *skip*."""
    lines = wikitext.split("\n")
    head_re = re.compile(r"^(=+)\s*(.*?)\s*\1\s*$")
    out: list[str] = []
    level = None
    skipping_at = None
    for line in lines:
        m = head_re.match(line)
        if m:
            depth, name = len(m.group(1)), m.group(2)
            if level is None:
                if name == heading:
                    level = depth
                continue
            if depth <= level:
                break
            if skipping_at is not None and depth <= skipping_at:
                skipping_at = None
            if name in skip:
                skipping_at = depth
            continue
        if level is not None and skipping_at is None:
            out.append(line)
    return "\n".join(out)


def _row_cells(row: str) -> list[str]:
    cells: list[str] = []
    for line in row.split("\n"):
        if line.startswith(("|}", "|+", "{|")):
            continue
        if line.startswith("|") or line.startswith("!"):
            sep = "||" if line.startswith("|") else "!!"
            cells.extend(c for c in line[1:].split(sep))
    return cells


def _film_in(cell: str) -> str | None:
    m = _ITALIC_LINK.search(cell)
    if not m:
        return None
    return _clean(m.group(1) or m.group(2))


def parse_winner_rows(wikitext: str) -> list[str]:
    """Films named in a prize's winners tables: the first cell of each row that
    holds an italic link.  Runner-up rows ("second place") are left out, and so
    are the shaded note rows ("No award given", "Festival cancelled"), whose
    prose can name films that did not win."""
    out: list[str] = []
    for row in wikitext.split("\n|-"):
        if _NOTE_ROW in row:
            continue
        for cell in _row_cells(row):
            film = _film_in(cell)
            if film:
                if "second place" not in cell.lower():
                    out.append(film)
                break
    return list(dict.fromkeys(out))


def parse_best_picture_rows(wikitext: str) -> tuple[list[str], list[str]]:
    """(winners, nominees) from the Best Picture tables.  The winner's title is
    set in bold italic (``'''''[[Film]]'''''``); every other film is a nominee."""
    winners: list[str] = []
    nominees: list[str] = []
    for row in wikitext.split("\n|-"):
        for cell in _row_cells(row):
            film = _film_in(cell)
            if film:
                (winners if "'''''" in cell else nominees).append(film)
                break
    winners = list(dict.fromkeys(winners))
    nominees = [f for f in dict.fromkeys(nominees) if f not in set(winners)]
    return winners, nominees


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------

def _write(name: str, payload) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    path = DATA_DIR / name
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True) + "\n")
    print(f"wrote {path.relative_to(REPO_ROOT)}")


def build_cult() -> None:
    titles: list[str] = []
    for page in CULT_PAGES:
        titles.extend(parse_cult_rows(_wikitext(page)))
    titles = list(dict.fromkeys(titles))
    resolved = resolve_imdb_ids(titles)
    films = {imdb: label for imdb, label in resolved.values()}
    print(f"cult: {len(titles)} rows, {len(films)} IMDb ids")
    _write("cult_films.json", films)


def build_festivals() -> None:
    out: dict[str, list[str]] = {}
    for key, (page, heading, skip) in FESTIVAL_PAGES.items():
        titles = parse_winner_rows(section(_wikitext(page), heading, skip))
        resolved = resolve_imdb_ids(titles)
        ids = sorted({resolved[t][0] for t in titles if t in resolved})
        print(f"{key}: {len(titles)} rows, {len(ids)} IMDb ids")
        out[key] = ids
    _write("festival_winners.json", out)


def build_best_picture() -> None:
    page, heading = BEST_PICTURE_PAGE
    winners, nominees = parse_best_picture_rows(section(_wikitext(page), heading))
    resolved = resolve_imdb_ids(winners + nominees)
    win_ids = sorted({resolved[t][0] for t in winners if t in resolved})
    nom_ids = sorted({resolved[t][0] for t in nominees if t in resolved} - set(win_ids))
    print(f"best picture: {len(winners)} winners -> {len(win_ids)}, {len(nominees)} nominees -> {len(nom_ids)}")
    _write("awards_best_picture.json", {"winners": win_ids, "nominees": nom_ids})


BUILDERS = {"cult": build_cult, "festivals": build_festivals, "best_picture": build_best_picture}


def main(argv: list[str]) -> int:
    names = argv or list(BUILDERS)
    unknown = [n for n in names if n not in BUILDERS]
    if unknown:
        print(f"unknown list(s): {', '.join(unknown)}; choose from {', '.join(BUILDERS)}", file=sys.stderr)
        return 2
    for name in names:
        BUILDERS[name]()
    print(f"{_calls} HTTP calls")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
