# i18n.py
#
# Poster-output translation with per-key English fallback.
#
# Each languages/<code>.json supplies genreLabels / sashLabels maps keyed by the
# CANONICAL ENGLISH strings the renderer produces (see languages/en.json for the
# reference vocabulary).  Translation is display-only: every internal decision
# (award-star matching, sash priority, font/colour lookups) stays in English, so
# a missing key, a malformed file, or a language with no JSON at all simply falls
# back to the English canonical string.  Nothing breaks if a translation is
# absent — it just renders in English.
#
# The operator can reword any sash label per language from the dashboard's
# Sash labels view, without touching languages/: the overrides live in
# SASH_LABELS_PATH (the cache volume) and sit on top of the bundled table.
import json
from datetime import date
import logging
import os
import re
from collections.abc import Iterable
import time
import unicodedata
from contextvars import ContextVar

from bidi import get_display

import config as _cfg

logger = logging.getLogger(__name__)

_LANG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "languages")
_LANGS: dict[str, dict] = {}

# Trending labels are produced as "#<rank> Today"; translated via the
# "trendingToday" template key (e.g. "#{rank} Aujourd'hui") so the rank stays.
_TRENDING_RE = re.compile(r"^#(\d+)\s+Today$")

# Composite nominee labels are joined with this separator in discovery.pick_sash.
_NOM_SEP = " • "

_V2_LOCALES = frozenset({"en", "pt", "nl", "de", "es"})
_LOCALE_RE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")


def normalize_locale(value: str | None) -> str:
    """Return a supported BingeCat locale, falling back deterministically.

    This is intentionally stricter than the legacy translation lookup: v2
    contracts accept only the five published locales, while the configurator
    may still use any translation file installed by an operator.
    """
    candidate = (value or "").strip().replace("_", "-").lower()
    if not _LOCALE_RE.fullmatch(candidate):
        return "en"
    base = candidate.split("-", 1)[0]
    return base if base in _V2_LOCALES else "en"


def resolve_locale_chain(requested: str, available: Iterable[str]) -> list[str]:
    """Resolve requested language then English from a supplied locale set."""
    available_codes = {str(code).strip().lower().replace("_", "-") for code in available}
    resolved = normalize_locale(requested)
    chain = [resolved, "en"]
    return [code for index, code in enumerate(chain) if code in available_codes and code not in chain[:index]]
# Dated release-status labels come out of discovery.release_date_label as
# "Oct 16 Cinema" or "Dec 2027 Cinema".  They translate through the
# "releaseDay" / "releaseMonth" templates ({month}, {day}, {year}, {window})
# so a language can reorder the parts; the window is the plain status label
# ("Cinema" / "Streaming" / "Physical") and translates through its own entry,
# and the month name comes from the top-level "monthsShort" list (twelve
# entries, January first).  A window can have its own pair of templates,
# "releaseDay<Window>" / "releaseMonth<Window>", for when the plain status
# word reads badly with a date — English dates Streaming as "Streams Sep 29".
_RELEASE_DATE_RE = re.compile(
    r"^([A-Z][a-z]{2}) (\d{1,2}|\d{4}) (Cinema|Streaming|Physical|Premiere|Premiered|Returns|Airing|Season \d+)$"
)
_SEASON_WINDOW_RE = re.compile(r"^Season (\d+)$")
_MONTHS_EN = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def load_languages() -> None:
    """Load every languages/*.json into memory once (call at startup)."""
    global _SASH_TABLES
    _LANGS.clear()
    _SASH_TABLES = {}
    if not os.path.isdir(_LANG_DIR):
        return
    for fn in os.listdir(_LANG_DIR):
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(_LANG_DIR, fn), encoding="utf-8") as f:
                data = json.load(f)
            code = (data.get("code") or os.path.splitext(fn)[0]).strip().lower()
            if code:
                _LANGS[code] = data
        except Exception as e:  # malformed file → skip, English fallback stands
            logger.warning(f"i18n: could not load language file {fn!r}: {e}")
    if _LANGS:
        logger.info(f"i18n: loaded languages {sorted(_LANGS)}")


def _lang_candidates(lang: str | None) -> list[str]:
    code = (lang or "").strip().lower().replace("_", "-")
    if not code:
        return []
    base = code.split("-", 1)[0]
    return list(dict.fromkeys([code, base]))


def has_language(lang: str | None) -> bool:
    return any(code in _LANGS for code in _lang_candidates(lang))


def _table(lang: str | None, key: str) -> dict:
    for code in _lang_candidates(lang):
        table = _LANGS.get(code, {}).get(key, {}) or {}
        if table:
            return table
    return {}


# ── Operator sash label overrides ────────────────────────────────────────────
# {"<lang code>": {"<sashLabels key>": "<text>"}, ...}, written by the
# dashboard.  Keys are the English file's sashLabels keys; templates keep the
# placeholders their English entry has ("{rank}", "{month}", ...).
_OVERRIDE_PATH = _cfg.SASH_LABELS_PATH
_OVERRIDES: dict[str, dict[str, str]] = {}
# BingeCat v2 renders read the bundled tables only: an operator override is
# runtime state outside the renderer revision, so it must not reach pixels.
BUNDLED_LABELS_ONLY: ContextVar[bool] = ContextVar("bundled_labels_only", default=False)


def _active_overrides() -> dict[str, dict[str, str]]:
    return {} if BUNDLED_LABELS_ONLY.get() else _OVERRIDES
# Same limit as the notable-name labels: the sash is narrow.  Counted without
# the placeholders, which the renderer fills in.
MAX_OVERRIDE_LENGTH = 40
_PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")
# (mtime_ns, size) of the file the overrides were last read from.
_loaded_stamp: tuple[int, int] | None = None
# Merged tables by the chain of codes that built them (bounded by the codes
# that exist, whatever *lang* a request sends).  Replaced, never cleared, on
# a reload, so a render still holding the old overrides fills the old one.
_SASH_TABLES: dict[tuple[str, ...], dict] = {}
_checked_at = 0.0
_CHECK_INTERVAL = 3.0


def _file_stamp() -> tuple[int, int] | None:
    try:
        st = os.stat(_OVERRIDE_PATH)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def _read_override_file() -> dict:
    try:
        with open(_OVERRIDE_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}                     # normal — no overrides configured
    except Exception as exc:
        logger.warning(f"sash_labels.json: failed to parse ({exc}) — using the bundled labels")
        return {}
    if not isinstance(data, dict):
        logger.warning("sash_labels.json: root must be a JSON object — ignoring")
        return {}
    out: dict[str, dict[str, str]] = {}
    for code, table in data.items():
        if isinstance(table, dict):
            clean = {str(k): str(v) for k, v in table.items() if str(v).strip()}
            if clean:
                out[str(code).strip().lower()] = clean
    return out


def load_sash_overrides() -> None:
    """(Re)read the operator's overrides.  Swapped in whole, so a render
    reading them mid-reload sees one version or the other."""
    global _OVERRIDES, _SASH_TABLES, _loaded_stamp
    _loaded_stamp = _file_stamp()
    _OVERRIDES = _read_override_file()
    _SASH_TABLES = {}         # after _OVERRIDES: _sash_table reads them the other way round
    if _OVERRIDES:
        n = sum(map(len, _OVERRIDES.values()))
        logger.info(f"sash_labels.json loaded: {n} labels across {sorted(_OVERRIDES)}")


def refresh_sash_overrides() -> bool:
    """Reload the overrides if the file changed — the dashboard writes it from
    whichever worker served the save.  A clock read on most calls, one stat
    every few seconds.  True when they were reloaded."""
    global _checked_at
    now = time.monotonic()
    if now - _checked_at < _CHECK_INTERVAL:
        return False
    _checked_at = now
    if _file_stamp() == _loaded_stamp:
        return False
    load_sash_overrides()
    return True


def _sash_chain(lang: str | None, overrides: dict | None = None) -> tuple[str, ...]:
    """The codes a poster in *lang* reads sash labels from, most specific
    first, ending at the first with a bundled table (English when none has
    one).  Only codes with a table or overrides count."""
    if overrides is None:
        overrides = _active_overrides()
    chain = []
    for code in dict.fromkeys([*_lang_candidates(lang), "en"]):
        has_table = bool(_LANGS.get(code, {}).get("sashLabels"))
        if has_table or code in overrides:
            chain.append(code)
        if has_table:
            break
    return tuple(chain)


def _sash_table(lang: str | None) -> dict:
    """The sashLabels table a poster in *lang* reads: the bundled table of
    the first candidate language that has one (English when none does),
    with the operator's overrides for every code down to it on top, the
    most specific winning.  So an override for "fr-ca" rewords French for
    fr-CA posters without hiding the rest of the French table."""
    overrides = _active_overrides()
    chain = _sash_chain(lang, overrides)
    cache, key = _SASH_TABLES, (chain, overrides is _OVERRIDES)
    table = cache.get(key)
    if table is None:
        table = dict(_LANGS.get(chain[-1], {}).get("sashLabels") or {}) if chain else {}
        for code in reversed(chain):
            table.update(overrides.get(code, {}))
        cache[key] = table
    return table


def sash_vocabulary() -> list[str]:
    """Every key an override can set: the English file's sashLabels keys."""
    return list(_LANGS.get("en", {}).get("sashLabels") or {})


def sash_override_state() -> dict:
    """What the dashboard's editor shows: each language's bundled labels and
    the operator's overrides, as saved.  Overrides for a language or key the
    bundled files no longer have (a hand-written file, or one saved before an
    update renamed them) are left out, so the editor can show and save the
    rest; the next save drops them."""
    keys = sash_vocabulary()
    en = _LANGS.get("en", {}).get("sashLabels") or {}
    builtin = {}
    for code, data in sorted(_LANGS.items()):
        table = data.get("sashLabels") or {}
        builtin[code] = {k: str(table.get(k) or en.get(k) or k) for k in keys}
    return {
        "keys": keys,
        "builtin": builtin,
        "overrides": {
            code: kept for code, table in _read_override_file().items()
            if code in builtin and (kept := {k: v for k, v in table.items() if k in en})
        },
        "max_length": MAX_OVERRIDE_LENGTH,
    }


def _check_placeholders(key: str, text: str, english: str) -> None:
    have = set(_PLACEHOLDER_RE.findall(text))
    want = set(_PLACEHOLDER_RE.findall(english))
    # A release template may name or drop the window ("Streams {month} {day}"
    # says it in words); the date parts and counts must stay.
    optional = {"window"} if key.startswith(("releaseDay", "releaseMonth")) else set()
    missing = want - optional - have
    if missing:
        raise ValueError(f"{key}: keep {', '.join('{' + m + '}' for m in sorted(missing))}")
    unknown = have - want - optional
    if unknown:
        raise ValueError(f"{key}: unknown placeholder {', '.join('{' + u + '}' for u in sorted(unknown))}")


def validate_sash_overrides(raw) -> dict[str, dict[str, str]]:
    """An editor-sent {lang: {key: text}} as the file's contents: blanks and
    entries matching the bundled label dropped.  Raises ValueError on
    anything malformed."""
    if not isinstance(raw, dict):
        raise ValueError("overrides must be an object of languages")
    english = _LANGS.get("en", {}).get("sashLabels") or {}
    out: dict[str, dict[str, str]] = {}
    for code, table in raw.items():
        code = str(code).strip().lower()
        if code not in _LANGS:
            raise ValueError(f"unknown language: {code[:20]}")
        if not isinstance(table, dict):
            raise ValueError(f"{code}: labels must be an object")
        bundled = _LANGS[code].get("sashLabels") or {}
        clean: dict[str, str] = {}
        for key, text in table.items():
            key = str(key)
            if key not in english:
                raise ValueError(f"{code}: unknown sash label {key[:40]}")
            if text is None:
                continue
            if not isinstance(text, str):
                raise ValueError(f"{code}: {key} must be text")
            text = " ".join(text.split())
            if not text or text == bundled.get(key, english[key]):
                continue
            if len(_PLACEHOLDER_RE.sub("", text)) > MAX_OVERRIDE_LENGTH:
                raise ValueError(f"{code}: {key} is longer than {MAX_OVERRIDE_LENGTH} characters")
            _check_placeholders(key, text, english[key])
            clean[key] = text
        if clean:
            out[code] = dict(sorted(clean.items()))
    return out


def sash_override_path() -> str:
    return _OVERRIDE_PATH


def sash_override_writable() -> bool:
    if os.path.exists(_OVERRIDE_PATH):
        return os.access(_OVERRIDE_PATH, os.W_OK)
    return os.access(os.path.dirname(_OVERRIDE_PATH) or ".", os.W_OK)


def save_sash_overrides(overrides: dict[str, dict[str, str]]) -> None:
    """Write validated overrides (validate_sash_overrides) as the whole file,
    atomically, then reload here; other workers pick it up in
    refresh_sash_overrides()."""
    directory = os.path.dirname(_OVERRIDE_PATH) or "."
    tmp = os.path.join(directory, f".{os.path.basename(_OVERRIDE_PATH)}.{os.getpid()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(dict(sorted(overrides.items())), fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp, _OVERRIDE_PATH)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    load_sash_overrides()


def _months_short(lang: str | None) -> tuple[str, ...]:
    for code in _lang_candidates(lang):
        months = _LANGS.get(code, {}).get("monthsShort")
        if isinstance(months, list) and len(months) == 12:
            return tuple(str(m) for m in months)
    return _MONTHS_EN


def _translate_release_date(match: "re.Match[str]", sl: dict, lang: str | None) -> str:
    month_en, rest, window_en = match.group(1), match.group(2), match.group(3)
    if month_en not in _MONTHS_EN:
        return match.group(0)
    month = _months_short(lang)[_MONTHS_EN.index(month_en)]
    is_year = len(rest) == 4
    rest = native_digits(rest, lang)
    season = _SEASON_WINDOW_RE.match(window_en)
    if season:
        # A template rather than a word: most locales put the window before
        # the day, and "Temporada 3 4 mar" runs two numbers together, so
        # they use the short form their TV apps do ("T3 4 mar").
        tmpl = sl.get("seasonWindow")
        window = tmpl.replace("{n}", native_digits(season.group(1), lang)) if tmpl else window_en
    else:
        # A language without its own past tense for a premiere reads its word
        # for one ("Estreia 2 out"); the date says which way it points.
        window = (sl.get(window_en) or (window_en == "Premiered" and sl.get("Premiere"))
                  or window_en)
    if is_year:
        tmpl = sl.get(f"releaseMonth{window_en}") or sl.get("releaseMonth")
        return (tmpl.replace("{month}", month).replace("{year}", rest).replace("{window}", window)
                if tmpl else match.group(0))
    tmpl = sl.get(f"releaseDay{window_en}") or sl.get("releaseDay")
    return (tmpl.replace("{month}", month).replace("{day}", rest).replace("{window}", window)
            if tmpl else match.group(0))


def native_digits(text: str | None, lang: str | None) -> str:
    """*text*'s digits written in the language's own, as its file's "digits"
    gives them (ten, zero first: Arabic's "٠١٢٣٤٥٦٧٨٩"); unchanged for a
    language that writes 0-9.  Used for the trending rank, dates and years;
    scores and ratings keep 0-9, and so do names and codes such as "A24" and
    "4K"."""
    for code in _lang_candidates(lang):
        digits = str(_LANGS.get(code, {}).get("digits") or "")
        if len(digits) == 10:
            return "".join(digits[ord(ch) - 48] if "0" <= ch <= "9" else ch for ch in (text or ""))
    return text or ""


def translate_genre(name: str | None, lang: str | None) -> str:
    """Canonical English genre name → localized, or unchanged if no translation."""
    if not name:
        return name or ""
    return _table(lang, "genreLabels").get(name, name)


def translate_sash(label: str | None, lang: str | None) -> str:
    """Canonical English sash label → localized, or unchanged if no translation.

    Handles two special shapes: the "#<rank> Today" trending template and the
    " • "-joined composite nominee label (each part translated independently).
    Proper nouns and operator-defined labels (studio / director / cast) usually
    aren't in the JSON, so they pass straight through.
    """
    if not label:
        return label or ""
    dated = re.fullmatch(r"(Cinema|Streaming|Airing) · (\d{4}-\d{2}-\d{2})", label)
    if dated:
        try:
            event = date.fromisoformat(dated.group(2))
        except ValueError:
            return translate_sash(dated.group(1), lang)
        code = normalize_locale(lang)
        if code == "en":
            calendar = f"{event.month:02d}/{event.day:02d}/{event.year:04d}"
        else:
            separator = {"nl": "-", "de": "."}.get(code, "/")
            calendar = separator.join((f"{event.day:02d}", f"{event.month:02d}", f"{event.year:04d}"))
        prefix = _table(code, "dateWindowLabels").get(dated.group(1), dated.group(1))
        return f"{prefix} · {calendar}"
    # A language without a file reads the English table, so English's own
    # wording (e.g. "Streams Sep 29") still applies rather than the raw label.
    sl = _sash_table(lang)
    if not sl:
        return label

    m = _TRENDING_RE.match(label)
    if m:
        # The rank in the language's own numerals, where it has them ("#٨").
        tmpl = sl.get("trendingToday")
        return tmpl.replace("{rank}", native_digits(m.group(1), lang)) if tmpl else label

    m = _RELEASE_DATE_RE.match(label)
    if m:
        return _translate_release_date(m, sl, lang)

    if _NOM_SEP in label:
        return _NOM_SEP.join(sl.get(part, part) for part in label.split(_NOM_SEP))

    return sl.get(label, label)


def upper_label(text: str | None, lang: str | None) -> str:
    """Uppercase a translated label the way the language itself would.

    str.upper() is locale-blind, which is wrong for two shipped languages:
    Turkish (and Azerbaijani) dotted i uppercases to İ, not I; and Greek
    drops the tonos in all-caps ("Ταινία" → "ΤΑΙΝΙΑ", not "ΤΑΙΝΊΑ") while
    keeping the diaeresis.
    """
    if not text:
        return text or ""
    base = (_lang_candidates(lang) or [""])[-1]
    if base in ("tr", "az"):
        text = text.replace("i", "İ")
    text = text.upper()
    if base == "el":
        text = unicodedata.normalize(
            "NFC", unicodedata.normalize("NFD", text).replace("́", ""))
    return text


def language_text(lang: str | None) -> str:
    """Every character a language's labels can put on a poster, upper case
    included (the landscape badge uppercases), for checking a font has them.
    For a language with no file, which draws the English labels, only the
    operator's English wording (if any)."""
    parts: list[str] = []
    for code in _lang_candidates(lang):
        data = _LANGS.get(code)
        if data:
            for key in ("genreLabels", "sashLabels"):
                table = data.get(key)
                if isinstance(table, dict):
                    parts.extend(str(v) for v in table.values())
            months = data.get("monthsShort")
            if isinstance(months, list):
                parts.extend(str(m) for m in months)
            parts.append(str(data.get("digits") or ""))
    # The operator's wording for every code the sash labels are read from,
    # English's included when the language has no file and draws English.
    for code in _sash_chain(lang):
        parts.extend(_active_overrides().get(code, {}).values())
    text = "".join(parts)
    return text + upper_label(text, lang)


# Languages written in a script whose letters change shape with their
# neighbours (Arabic joins every letter to the next), which only a shaping
# layout (Pillow's raqm: HarfBuzz + FriBiDi) draws correctly.  fonts sets
# SHAPING around a render of one of them when raqm is available.
_SHAPED_LANGS = frozenset({"ar", "fa", "ur"})
SHAPING: ContextVar[bool] = ContextVar("label_shaping", default=False)


def needs_shaping(lang: str | None) -> bool:
    """Whether *lang* is written in a script that needs shaping."""
    return (_lang_candidates(lang) or [""])[-1] in _SHAPED_LANGS


# Right-to-left scripts: Hebrew, Arabic, Syriac, Thaana, NKo, Samaritan,
# Mandaic, and their presentation forms.
_RTL_RE = re.compile("[\u0590-\u08FF\uFB1D-\uFDFF\uFE70-\uFEFF]")


def visual(text: str | None) -> str:
    """A line of text in the order it is drawn, left to right.

    Pillow here has no bidi layout (no libraqm) and Skia's drawString has none
    either: both draw characters in the order they're stored.  A line holding
    right-to-left script is reordered with the Unicode bidi algorithm, as a
    right-to-left paragraph — so "דרמה · 2024 ★ 87" reads genre first from the
    right, with numbers and Latin names still left to right inside it.  Lines
    with no right-to-left character come back unchanged.

    Apply it to a whole line just before measuring and drawing, never before
    joining or wrapping: reordering is per line.  Hebrew needs nothing more.
    Arabic also needs its letters joined, so its renders lay text out with
    raqm (SHAPING), which reorders as it shapes: there the line comes back
    unchanged, in reading order.
    """
    if not text or SHAPING.get() or not _RTL_RE.search(text):
        return text or ""
    # Joiner controls mean nothing to a layout that doesn't join, and a font
    # may have no glyph for them (fonts.covers lets them through).
    return get_display(text.replace("\u200c", "").replace("\u200d", ""), base_dir="R")


load_sash_overrides()
