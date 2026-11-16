"""Streaming services' daily Top 10s, by country (the trending Configure dialog's
Streaming Top 10).

FlixPatrol's charts, as the fp-crawler project publishes them (the same
data AIOMetadata's Top 10 catalogs read): one JSON file per country, each
chart's rows carrying their TMDB id and type, and an availability.json
saying which services each country has.  TRENDING_TOP10_URL points at a
mirror if the project's own copy goes away.

A chart is a source like any other in trending_lists, named by service and
country: "fp_netflix_gb".  Which ones may be picked is read from the
availability index, so the lists an instance builds stay bounded by it.
The index is kept in memory and on disk, and the last good copy is kept
when a refresh fails.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time

import httpx

import config

logger = logging.getLogger(__name__)

FAMILY = "flixpatrol"
# trending_lists source URL of a chart: "top10:<service>:<region slug>".
SCHEME = "top10:"
_PREFIX = "fp_"
_ID_RE = re.compile(r"^fp_([a-z0-9][a-z0-9-]{0,24})_([a-z]{2})$")

# The fp-crawler's region slugs, by ISO code; "ww" is its worldwide chart.
REGIONS: dict[str, str] = {
    "al": "albania", "dz": "algeria", "ag": "antigua-and-barbuda", "ar": "argentina",
    "am": "armenia", "au": "australia", "at": "austria", "az": "azerbaijan", "bs": "bahamas",
    "bh": "bahrain", "bd": "bangladesh", "by": "belarus", "be": "belgium", "bz": "belize",
    "bo": "bolivia", "ba": "bosnia-and-herzegovina", "bw": "botswana", "br": "brazil",
    "bg": "bulgaria", "kh": "cambodia", "ca": "canada", "cl": "chile", "co": "colombia",
    "cr": "costa-rica", "hr": "croatia", "cy": "cyprus", "cz": "czech-republic",
    "dk": "denmark", "dm": "dominica", "do": "dominican-republic", "ec": "ecuador",
    "eg": "egypt", "ee": "estonia", "fi": "finland", "fr": "france", "gm": "gambia",
    "de": "germany", "gh": "ghana", "gr": "greece", "gt": "guatemala", "hn": "honduras",
    "hk": "hong-kong", "hu": "hungary", "is": "iceland", "in": "india", "id": "indonesia",
    "iq": "iraq", "ie": "ireland", "il": "israel", "it": "italy", "jm": "jamaica",
    "jp": "japan", "jo": "jordan", "kz": "kazakhstan", "ke": "kenya", "kw": "kuwait",
    "la": "laos", "lv": "latvia", "lb": "lebanon", "ly": "libya", "lt": "lithuania",
    "lu": "luxembourg", "my": "malaysia", "mt": "malta", "mr": "mauritania", "mu": "mauritius",
    "mx": "mexico", "md": "moldova", "mn": "mongolia", "me": "montenegro", "ma": "morocco",
    "mz": "mozambique", "na": "namibia", "nl": "netherlands", "nz": "new-zealand",
    "ni": "nicaragua", "ne": "niger", "ng": "nigeria", "mk": "north-macedonia",
    "no": "norway", "om": "oman", "pk": "pakistan", "pa": "panama", "py": "paraguay",
    "pe": "peru", "ph": "philippines", "pl": "poland", "pt": "portugal", "qa": "qatar",
    "ro": "romania", "sv": "salvador", "sa": "saudi-arabia", "rs": "serbia", "sg": "singapore",
    "sk": "slovakia", "si": "slovenia", "za": "south-africa", "kr": "south-korea",
    "es": "spain", "lk": "sri-lanka", "se": "sweden", "ch": "switzerland", "tw": "taiwan",
    "tj": "tajikistan", "th": "thailand", "tt": "trinidad-and-tobago", "tn": "tunisia",
    "tr": "turkey", "ug": "uganda", "ua": "ukraine", "ae": "united-arab-emirates",
    "gb": "united-kingdom", "us": "united-states", "uy": "uruguay", "ve": "venezuela",
    "vn": "vietnam", "ye": "yemen", "zw": "zimbabwe", "ww": "global",
}
_REGION_NAMES = {"global": "Worldwide", "salvador": "El Salvador", "united-states": "US",
                 "united-kingdom": "UK", "united-arab-emirates": "UAE"}
SERVICE_NAMES = {
    "netflix": "Netflix", "amazon-prime": "Prime Video", "amazon": "Amazon Store",
    "amazon-channels": "Amazon Channels", "apple-tv": "Apple TV+", "apple-tv-store": "Apple TV Store",
    "hbo-max": "HBO Max", "disney": "Disney+", "paramount": "Paramount+", "hulu": "Hulu",
    "peacock": "Peacock", "google": "Google Play", "rakuten-tv": "Rakuten TV",
    "skyshowtime": "SkyShowtime", "shahid": "Shahid", "claro-video": "Claro video",
    "starz": "Starz", "osn": "OSN+", "crunchyroll": "Crunchyroll", "lionsgate": "Lionsgate+",
    "viaplay": "Viaplay", "mgm": "MGM+", "chili": "Chili", "mubi": "MUBI", "viu": "Viu",
    "voyo": "Voyo", "hayu": "hayu", "shudder": "Shudder", "sony-pictures-core": "Sony Pictures Core",
    "canal": "Canal+", "go3": "Go3", "telia": "Telia", "catchplay": "CATCHPLAY+",
    "discovery-plus": "Discovery+", "vix": "ViX", "amc": "AMC+", "binge": "Binge",
    "sbs": "SBS On Demand", "stan": "Stan", "streamz": "Streamz", "globoplay": "Globoplay",
    "crave": "Crave", "tubi": "Tubi", "pluto": "Pluto TV", "vudu": "Fandango at Home",
    "viki-tv": "Viki", "hotstar": "JioHotstar", "zee5": "ZEE5",
    "abema": "ABEMA", "cda": "CDA Premium", "coupang-play": "Coupang Play", "danime": "dAnime Store",
    "france-tv": "france.tv", "friday": "friDay Video", "hami-video": "Hami Video",
    "hulu-nippon": "Hulu Japan", "infinity": "Infinity+", "ivysilani": "iVysílání", "joyn": "Joyn",
    "lemino": "Lemino", "m6": "M6+", "mewatch": "mewatch", "myvideo": "MyVideo", "neon-tv": "Neon",
    "now": "NOW", "ocs": "OCS", "oneplay": "Oneplay", "player": "Player.pl", "prima": "Prima+",
    "rai": "RaiPlay", "rtl": "RTL+", "telasa": "TELASA", "tf1": "TF1+", "trueid": "TrueID",
    "tv-2-norge": "TV 2 Play", "u-next": "U-NEXT", "universal": "Universal+", "videoland": "Videoland",
    "vidio": "Vidio", "watchit": "WATCH IT!", "wavve": "Wavve", "wow": "WOW",
}
# Anime ranks on its own lists: Crunchyroll's chart is offered for those
# alone, and every other service's for the movie and TV lists.
_ANIME_SERVICES = frozenset({"crunchyroll"})
_KIND = {"movie": "movie", "tv": "tv", "anime": "tv", "anime_movie": "movie"}
_SECTION = {"movie": "movies", "tv": "series", "anime": "series", "anime_movie": "movies"}

# The index changes rarely (a service launching somewhere); the charts daily.
_AVAILABILITY_REFRESH_SECS = 6 * 3600
_CHART_REUSE_SECS = 600
_availability: "dict[str, dict[str, list[str]]] | None" = None
_availability_at = 0.0
_version = 0
# The stored copy's mtime as last read, and when it was last looked at: a
# worker that doesn't run the trending loop picks up the one that does's
# refresh from it.
_stored_mtime = 0.0
_stored_checked = 0.0
_STORED_CHECK_SECS = 300
_charts: dict[str, tuple[float, dict]] = {}
_failed_at: dict[str, float] = {}
_RETRY_SECS = 300


def _base() -> str:
    return (config.TRENDING_TOP10_URL or "").rstrip("/")


def _store_path() -> str:
    return os.path.join(os.path.dirname(config.DB_PATH), "top10_availability.json")


def enabled() -> bool:
    allowed = {f.strip().casefold() for f in str(config.TRENDING_LIST_SOURCES or "").split(",")}
    return bool(config.TRENDING_LIST_CHOICE and FAMILY in allowed and _base())


def version() -> int:
    """Changes whenever the index does, for caches of what it allowed."""
    return _version


def _set_availability(regions: dict) -> None:
    global _availability, _availability_at, _version
    out: dict[str, dict[str, list[str]]] = {}
    for slug, services in (regions or {}).items():
        if slug not in REGIONS.values() or not isinstance(services, dict):
            continue
        out[slug] = {s: sorted(sections) for s, sections in services.items()
                     if isinstance(sections, dict) and re.fullmatch(r"[a-z0-9][a-z0-9-]{0,24}", s)}
    _availability, _availability_at = out, time.time()
    _version += 1


def _load_stored() -> None:
    """The index last fetched, as stored: so a restart can tell picks apart
    before the first refresh, and every worker sees the one refresh."""
    global _stored_mtime, _stored_checked, _availability_at
    now = time.monotonic()
    if _availability is not None and now - _stored_checked < _STORED_CHECK_SECS:
        return
    _stored_checked = now
    try:
        mtime = os.path.getmtime(_store_path())
        if _availability is not None and mtime <= _stored_mtime:
            return
        with open(_store_path(), encoding="utf-8") as fh:
            _set_availability(json.load(fh))
        # As old as the file: a stale copy is refreshed when next due.
        _stored_mtime = _availability_at = mtime
    except (OSError, ValueError):
        pass


def availability() -> "dict[str, dict[str, list[str]]]":
    """{region slug: {service: [sections]}}; {} until first read."""
    _load_stored()
    return _availability or {}


async def refresh_availability(client: httpx.AsyncClient, *, force: bool = False) -> None:
    """Re-read the index when it is due; keep the last good one otherwise."""
    if not enabled():
        return
    _load_stored()
    if not force and _availability is not None and time.time() - _availability_at < _AVAILABILITY_REFRESH_SECS:
        return
    url = f"{_base()}/availability.json"
    try:
        logger.info(f"External API Call: Top 10 availability ({url})")
        resp = await client.get(url, timeout=20.0)
        resp.raise_for_status()
        regions = resp.json().get("regions")
        if not isinstance(regions, dict) or not regions:
            raise ValueError("no regions")
    except Exception as exc:
        logger.warning(f"Top 10 availability not refreshed ({exc}); keeping the last good index")
        return
    _set_availability(regions)

    def _store() -> None:
        tmp = _store_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(regions, fh)
        os.replace(tmp, _store_path())
    try:
        await asyncio.to_thread(_store)
    except OSError as exc:
        logger.warning(f"Top 10 availability not stored: {exc}")


def parse_id(source: str) -> "tuple[str, str] | None":
    """(service, region slug) a "fp_<service>_<iso>" id names, or None."""
    m = _ID_RE.match(source or "")
    if not m or m.group(2) not in REGIONS:
        return None
    return m.group(1), REGIONS[m.group(2)]


def _sections_for(endpoint: str, service: str, sections) -> bool:
    if (service in _ANIME_SERVICES) != (endpoint in ("anime", "anime_movie")):
        return False
    return _SECTION[endpoint] in sections or "overall" in sections


def endpoints(service: str, sections) -> list[str]:
    return [e for e in ("movie", "tv", "anime", "anime_movie") if _sections_for(e, service, sections)]


def offered(endpoint: str, source: str) -> bool:
    """Whether *source* is a chart the index has for *endpoint*'s list."""
    named = parse_id(source)
    if named is None or not enabled():
        return False
    service, region = named
    sections = availability().get(region, {}).get(service)
    return sections is not None and _sections_for(endpoint, service, sections)


def url(source: str) -> str:
    named = parse_id(source)
    return f"{SCHEME}{named[0]}:{named[1]}" if named else ""


def region_name(slug: str) -> str:
    return _REGION_NAMES.get(slug) or " ".join(
        w if w == "and" else w.capitalize() for w in slug.split("-"))


def service_name(service: str) -> str:
    return SERVICE_NAMES.get(service) or " ".join(w.capitalize() for w in service.split("-"))


def name(source: str) -> str:
    named = parse_id(source)
    if not named:
        return source
    return f"{service_name(named[0])} Top 10 ({region_name(named[1])})"


# The US charts the built-in Top 10s (trending_lists' "top10" family, snoak's
# mirrors of these same charts) already offer: left out of the picker while
# those are, so the menu has no two of one chart.
_BUILTIN_US = frozenset({"netflix", "amazon-prime", "apple-tv", "disney", "paramount", "hbo-max"})


def caps() -> "dict | None":
    """The picker's menu for /trending-top10: every region's services and
    the lists each can be a catalog of."""
    if not enabled():
        return None
    regions = availability()
    allowed = {f.strip().casefold() for f in str(config.TRENDING_LIST_SOURCES or "").split(",")}
    out = {}
    for iso, slug in REGIONS.items():
        services = {s: endpoints(s, sections) for s, sections in regions.get(slug, {}).items()
                    if not (iso == "us" and s in _BUILTIN_US and "top10" in allowed)}
        services = {s: e for s, e in services.items() if e}
        if services:
            out[iso] = {"name": region_name(slug), "services": services}
    return {"regions": out,
            "services": {s: service_name(s) for r in out.values() for s in r["services"]}}


async def _region(client: httpx.AsyncClient, slug: str) -> dict:
    held = _charts.get(slug)
    if held is not None and time.monotonic() - held[0] < _CHART_REUSE_SECS:
        return held[1]
    chart_url = f"{_base()}/{slug}.json"
    logger.info(f"External API Call: Top 10 charts ({chart_url})")
    resp = await client.get(chart_url, timeout=20.0)
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict) or not isinstance(data.get("charts"), list):
        raise ValueError("no charts")
    now = time.monotonic()
    # Only reused briefly: drop what can't be any more.
    for old in [s for s, (at, _d) in _charts.items() if now - at >= _CHART_REUSE_SECS]:
        del _charts[old]
    _charts[slug] = (now, data)
    return data


async def fetch_ids(
    client: httpx.AsyncClient, endpoint: str, source_url: str, details_out: dict | None = None,
) -> list[str]:
    """TMDB ids of a chart ("top10:<service>:<region>") in rank order, of
    *endpoint*'s kind: its own movies or series chart, else those of its
    kind on the service's overall one.  [] when it can't be read."""
    service, _, slug = source_url[len(SCHEME):].partition(":")
    cooldown = f"{endpoint}:{source_url}"
    failed = _failed_at.get(cooldown)
    if failed is not None and time.monotonic() - failed < _RETRY_SECS:
        return []
    try:
        data = await _region(client, slug)
    except Exception as exc:
        _failed_at[cooldown] = time.monotonic()
        logger.error(f"Top 10 charts for {slug} unreadable: {exc}")
        return []
    charts = {c.get("catalog_id"): c for c in data["charts"] if isinstance(c, dict)}
    chart = charts.get(f"{service}.{_SECTION[endpoint]}") or charts.get(f"{service}.overall")
    kind = _KIND[endpoint]
    ids: list[str] = []
    for row in sorted((chart or {}).get("entries") or [], key=lambda r: r.get("rank") or 99):
        tmdb = row.get("tmdb") or {}
        entry_id = str(tmdb.get("id") or "")
        if not entry_id.isdigit() or tmdb.get("media_type") != kind or entry_id in ids:
            continue
        ids.append(entry_id)
        if details_out is not None:
            date = str(tmdb.get("release_date") or "")
            details_out[entry_id] = {k: v for k, v in {
                "name": row.get("title"),
                "year": date[:4] if date[:4].isdigit() else None,
            }.items() if v}
    if not ids:
        _failed_at[cooldown] = time.monotonic()
        if chart is None:
            logger.warning(f"Top 10 charts for {slug} have no {service} chart")
        else:
            # A mixed chart that is all films (or all series) today.
            logger.info(f"Top 10 chart {service} in {slug} has no {kind} rows today")
        return []
    _failed_at.pop(cooldown, None)
    logger.info(f"Trending source for {endpoint}: {len(ids)} titles from {service} Top 10 in {slug}")
    return ids
