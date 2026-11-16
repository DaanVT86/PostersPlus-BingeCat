"""Cult film, festival top-prize and Best Picture lists, by IMDb id.

The data files in ``data/`` are generated from Wikipedia by
``tools/build_award_lists.py`` (CC BY-SA 4.0; credited in README and the admin
page footer) and committed.  They replace MDBList keywords as the source of the
Cult Classic, festival and Oscar badges: MDBList's bulk ratings endpoint, which
answers 200 titles in one request, carries no keywords.

The flags are derived when a poster is drawn, from the title's IMDb id and these
lists, rather than stored on the rating row — so a list update reaches every
cached rating at once.  ``signature()`` feeds a title's memberships into the
composite cache key, so a list update re-renders just the posters it changes.

The keyword-derived flags still count for now (see ``derive``): they are the
fallback until the keyword call is dropped.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent / "data"

# festival_winners.json key -> the festival keyword festivals.py resolves.
FESTIVAL_KEYWORDS: dict[str, str] = {
    "cannes":   "festival-cannes-winner",
    "venice":   "festival-venice-winner",
    "berlin":   "festival-berlin-winner",
    "locarno":  "festival-locarno-winner",
    "sundance": "festival-sundance-winner",
}

# Metacritic Must-See, from the score itself: Metacritic awards the badge to a
# metascore of 81+ with at least 15 critic reviews.
METACRITIC_MUST_SEE_SCORE = 81
METACRITIC_MUST_SEE_REVIEWS = 15

_cult: frozenset[str] | None = None
_festival: dict[str, str] | None = None        # imdb id -> festival keyword
_best_picture: dict[str, str] | None = None    # imdb id -> "win" | "nom"
_version: str = ""                             # short hash of all three lists


def _read(name: str):
    path = DATA_DIR / name
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.warning(f"Award list {path.name} missing — its badges fall back to MDBList keywords")
    except (OSError, ValueError) as exc:
        logger.warning(f"Award list {path.name} unreadable ({exc}) — its badges fall back to MDBList keywords")
    return None


def _load() -> None:
    global _cult, _festival, _best_picture, _version
    if _cult is not None:
        return
    cult = _read("cult_films.json") or {}
    festivals = _read("festival_winners.json") or {}
    best_picture = _read("awards_best_picture.json") or {}

    fest: dict[str, str] = {}
    # FESTIVAL_KEYWORDS is in festivals.py's prestige order, so a film that
    # won two top prizes is named for the first.
    for key, keyword in FESTIVAL_KEYWORDS.items():
        for imdb_id in festivals.get(key) or []:
            fest.setdefault(imdb_id, keyword)

    bp: dict[str, str] = {i: "nom" for i in best_picture.get("nominees") or []}
    bp.update({i: "win" for i in best_picture.get("winners") or []})

    _version = hashlib.sha256(
        json.dumps([sorted(cult), festivals, best_picture], sort_keys=True).encode()
    ).hexdigest()[:8]
    _festival, _best_picture = fest, bp
    _cult = frozenset(cult)


def reload() -> None:
    """Drop the loaded lists (tests, or after regenerating the files)."""
    global _cult, _festival, _best_picture
    _cult = _festival = _best_picture = None


def is_cult(imdb_id: str | None) -> bool:
    _load()
    return bool(imdb_id) and imdb_id in _cult


def festival_win(imdb_id: str | None) -> str | None:
    """The festival keyword of the top prize *imdb_id* won, or None."""
    _load()
    return _festival.get(imdb_id) if imdb_id else None


def best_picture(imdb_id: str | None) -> str | None:
    """"win", "nom" or None for the Academy Award for Best Picture."""
    _load()
    return _best_picture.get(imdb_id) if imdb_id else None


def signature(imdb_id: str | None) -> str:
    """This title's list memberships, for the composite cache key: empty for a
    title on no list, so most keys are unchanged by the lists existing.

    Without an IMDb id (a TMDB-id request, whose IMDb id is only known once its
    metadata is read) the memberships can't be told yet, so the key carries
    the lists' version instead: a list update re-renders every such poster."""
    if not imdb_id:
        _load()
        return f"|alv={_version}"
    parts = []
    if is_cult(imdb_id):
        parts.append("c")
    fest = festival_win(imdb_id)
    if fest:
        parts.append(fest.split("-")[1])
    bp = best_picture(imdb_id)
    if bp:
        parts.append("bp" + bp)
    return f"|al={'.'.join(parts)}" if parts else ""


def metacritic_must_see(score, reviews) -> bool:
    try:
        return (float(score) >= METACRITIC_MUST_SEE_SCORE
                and int(reviews) >= METACRITIC_MUST_SEE_REVIEWS)
    except (TypeError, ValueError):
        return False


def derive(
    imdb_id: str | None,
    *,
    award_wins: list[str],
    award_noms: list[str],
    keyword_cult: bool | None,
    keyword_true_story: bool | None,
    tmdb_true_story: bool | None = None,
) -> tuple[list[str], list[str], bool, bool]:
    """Fold the lists into the flags a render read from the rating row.

    Returns ``(award_wins, award_noms, is_cult, is_true_story)``.  A list hit
    adds its flag; a keyword flag the lists don't confirm is kept as the
    fallback.  A Best Picture win on the list replaces a keyword "Oscar
    Nominee".  The festival list is read by festivals.festival_label, which
    names a listed winner's top prize."""
    wins, noms = list(award_wins or []), list(award_noms or [])
    bp = best_picture(imdb_id)
    if bp == "win":
        noms = [n for n in noms if n != "Oscar Nominee"]
        if "Oscar Winner" not in wins:
            wins.insert(0, "Oscar Winner")
    elif bp == "nom" and "Oscar Winner" not in wins and "Oscar Nominee" not in noms:
        noms.insert(0, "Oscar Nominee")

    return (
        wins,
        noms,
        is_cult(imdb_id) or bool(keyword_cult),
        bool(tmdb_true_story) or bool(keyword_true_story),
    )
