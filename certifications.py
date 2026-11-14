"""Age certificates by country, for the graphic badges' "cert" slot.

TMDB's details call, with /release_dates (films) or /content_ratings (TV)
appended, carries every country's certificate at once; fetch_badge_facts keeps
them all (see certs_from_release_dates / certs_from_content_ratings), so a
country picked in badge_cert_country costs no further call.

A country's certificates are user-entered on TMDB, so only the ones its
rating board issues are drawn: OFFICIAL, TMDB's /certification/{movie,tv}/list
(fetched 2026-10-11) without the not-a-rating entries (NR, Unrated, Exempt...).
A country TMDB lists no board for takes any short, plain value.
"""
from __future__ import annotations

import os
import re

import fonts

DEFAULT_COUNTRY = "US"

# Country -> (film certificates, TV certificates), each in its board's order.
OFFICIAL: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "AR": (('ATP', '+13', '+16', '+18', 'C'),
           ('ATP', 'SAM 13', 'SAM 16', 'SAM 18')),
    "AT": (('AA', '6', '8', '10', '12', '14', '16'),
           ('AA', '6', '8', '10', '12', '14', '16')),
    "AU": (('G', 'PG', 'M', 'MA 15+', 'R 18+', 'X 18+'),
           ('P', 'C', 'G', 'PG', 'M', 'MA 15+', 'R 18+')),
    "BE": (('AL', '6', '9', '12', '14', '16', '18'),
           ('AL', '6', '9', '12', '14', '16', '18')),
    "BG": (('A', 'B', 'C', 'D', 'X'),
           ('12', '14', '16', '18')),
    "BR": (('L', '10', '12', '14', '16', '18'),
           ('L', '10', '12', '14', '16', '18')),
    "CA": (('G', 'PG', '14A', '18A', 'R', 'A'),
           ('C', 'C8', 'G', 'PG', '14+', '18+')),
    "CH": (('0', '6', '8', '10', '12', '14', '16', '18'),
           ('0', '6', '12', '16', '18')),
    "CL": (('TE', 'TE+7', '14', '18'),
           ('I', 'I7', 'I10', 'I12', 'F', 'R', 'A')),
    "CZ": (('U', '12+', '15+', '18+'),
           ('0+', '5+', '7+', '9+', '12+', '15+', '18+')),
    "DE": (('0', '6', '12', '16', '18'),
           ('0', '6', '12', '16', '18')),
    "DK": (('A', '7', '11', '15', 'F'),
           ('A', '7', '11', '15')),
    "ES": (('A', 'Ai', '7', '7i', '12', '16', '18', 'X'),
           ('ERI', 'TP', '7', '10', '12', '13', '16', '18')),
    "FI": (('S', 'K-7', 'K-12', 'K-16', 'K-18', 'KK'),
           ('S', 'K7', 'K12', 'K16', 'K18')),
    "FR": (('TP', '12', '16', '18'),
           ('TP', '10', '12', '16', '18')),
    "GB": (('U', 'PG', '12A', '12', '15', '18', 'R18'),
           ('U', 'PG', '12A', '12', '15', '18', 'R18')),
    "GR": (('K', 'K12', 'K15', 'K18'),
           ('K', '8', '12', '16', '18')),
    "HK": (('I', 'IIA', 'IIB', 'III'),
           ()),
    "HU": (('KN', '6', '12', '16', '18', 'X'),
           ('Children', '6', '12', '16', '18')),
    "ID": (('SU', '13+', '17+', '21+'),
           ('SU', 'P', 'A', 'R', 'D')),
    "IE": (('G', 'PG', '12A', '12', '15A', '15', '16', '18'),
           ('G', 'PG', '12', '15', '16', '18')),
    "IL": (('All', '12', '14', '16', '18'),
           ('E', 'G', '12+', '15+', '18+')),
    "IN": (('U', 'U/A 7+', 'UA', 'U/A 13+', 'U/A 16+', 'A', 'S'),
           ('U', 'U/A 7+', 'U/A 13+', 'U/A 16+', 'A')),
    "IT": (('T', '6+', '10+', '14+', '18+'),
           ('T', 'BA', 'VM12', 'VM14', 'VM18')),
    "JP": (('G', 'PG12', 'R15+', 'R18+'),
           ()),
    "KR": (('ALL', '12', '15', '19', 'Restricted Screening'),
           ('ALL', '7', '12', '15', '19')),
    "LT": (('V', 'N-7', 'N-13', 'N-16', 'N-18'),
           ('N-7', 'N-14', 'S')),
    "LU": (('A', '6', '12', '16', '18'),
           ('Tous', '10', '12', '16', '18')),
    "LV": (('U', '7+', '12+', '16+', '18+'),
           ()),
    "MA": ((),
           ('10', '12', '16')),
    "MO": (('A', 'B', 'C', 'D'),
           ()),
    "MX": (('AA', 'A', 'B', 'B-15', 'C', 'D'),
           ('AA', 'A', 'B', 'B-15', 'C', 'D')),
    "MY": (('U', 'P13', '18SG', '18SX', '18PA', '18PL'),
           ('U', 'P13', '18')),
    "NL": (('AL', '6', '9', '12', '14', '16', '18'),
           ('AL', '6', '9', '12', '14', '16', '18')),
    "NO": (('A', '6', '9', '12', '15', '18'),
           ('A', '6', '9', '12', '15', '18')),
    "NZ": (('G', 'PG', 'M', 'R13', 'RP13', 'R15', 'R16', 'RP16', 'R18', 'RP18', 'R'),
           ('G', 'PG', 'M', '16', '18')),
    "PH": (('G', 'PG', 'R-13', 'R-16', 'R-18', 'X'),
           ('G', 'PG', 'SPG', 'X')),
    "PL": (('AL', '7', '12', '16', '18'),
           ('AL', '0', '7', '12', '16', '18')),
    "PR": (('G', 'PG', 'PG-13', 'R', 'NC-17'),
           ('TV-Y', 'TV-Y7', 'TV-G', 'TV-PG', 'TV-14', 'TV-MA')),
    "PT": (('Públicos', 'M/3', 'M/6', 'M/12', 'M/14', 'M/16', 'M/18', 'P'),
           ('T', '10', '12', '16')),
    "RO": (('AP', '12', '15', '18', '18+', 'IC'),
           ('AP', '12', '15', '18')),
    "RU": (('0+', '6+', '12+', '16+', '18+'),
           ('0+', '6+', '12+', '16+', '18+')),
    "SE": (('Btl', '7', '11', '15'),
           ('Btl', 'Från 7 år', 'Från 11 år', 'Från 15 år')),
    "SG": (('G', 'PG', 'PG13', 'NC16', 'M18', 'R21'),
           ('G', 'PG', 'PG13', 'NC16', 'M18', 'R21')),
    "SK": (('U', '7', '12', '15', '18'),
           ('U', '7', '12', '15', '18')),
    "TH": (('P', 'G', '13', '15', '18', '20'),
           ('ส', 'ท', 'น 13+', 'น 15+', 'น 18+', 'ฉ 20-')),
    "TR": (('Genel İzleyici Kitlesi', '6A', '6+', '10A', '10+', '13A', '13+', '16+', '18+'),
           ('Genel İzleyici', '7+', '13+', '18+')),
    "TW": (('0+', '6+', '12+', '15+', '18+'),
           ('0+', '6+', '12+', '15+', '18+')),
    "UA": (('0+', '12+', '16+', '18+'),
           ('0+', '9+', '12+', '16+', '18+')),
    "US": (('G', 'PG', 'PG-13', 'R', 'NC-17'),
           ('TV-Y', 'TV-Y7', 'TV-G', 'TV-PG', 'TV-14', 'TV-MA')),
    "VI": (('G', 'PG', 'PG-13', 'R', 'NC-17'),
           ('TV-Y', 'TV-Y7', 'TV-G', 'TV-PG', 'TV-14', 'TV-MA')),
    "ZA": (('A', 'PG', '7-9PG', '10-12PG', '13', '16', '18', 'X18', 'XX'),
           ('All', 'PG', '13', '16', '18', 'X18')),
}

# Theatrical releases carry the certificate a title is known by; premieres and
# TV airings can carry a different one, or an empty string.
_RELEASE_ORDER = (3, 2, 4, 5, 6, 1)
# Longest certificate drawn: the box is a badge, not a label.
_MAX_LEN = 8
_UNLISTED_OK = re.compile(r"^[0-9A-Za-z][0-9A-Za-z+/ -]{0,5}$")
_NOT_A_RATING = {"NR", "UR", "NA", "N/A", "TBC", "TBA"}
_INTER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts", "Inter-Bold.ttf")
# Sweden's TV ratings are spelt out; its film ratings are the bare age.
_SPELT_AGE = re.compile(r"^Från (\d+) år$")


def country(value: str | None) -> str:
    """badge_cert_country as given, upper-cased; the default if not a code."""
    v = (value or "").strip().upper()
    return v if re.fullmatch(r"[A-Z]{2}", v) else DEFAULT_COUNTRY


def display(iso: str, cert: str | None) -> str | None:
    """*cert* as drawn for *iso*, or None if it isn't one to draw."""
    c = (cert or "").strip()
    if not c:
        return None
    listed = OFFICIAL.get(iso)
    if listed is not None:
        # Matched in any case, drawn in the board's own spelling ("pg-13").
        c = next((x for x in (*listed[0], *listed[1]) if x.casefold() == c.casefold()), None)
        if c is None:
            return None
    elif not _UNLISTED_OK.match(c) or c.upper() in _NOT_A_RATING:
        return None
    m = _SPELT_AGE.match(c)
    if m:
        c = m.group(1)
    if len(c) > _MAX_LEN or not fonts.covers(_INTER, c):
        return None
    return c


_ALL_LISTED = {c for film, tv in OFFICIAL.values() for c in (*film, *tv)}


def known(cert: str) -> bool:
    """Whether *cert* is something some country's badge could draw — the
    check for a certificate already resolved (see display)."""
    if not cert:
        return False
    if cert in _ALL_LISTED:
        return len(cert) <= _MAX_LEN and fonts.covers(_INTER, cert)
    return display("", cert) is not None


def certs_from_release_dates(payload: dict) -> dict[str, str]:
    """Every country's drawable certificate from a /movie/{id}/release_dates
    body: the first, by release type, its board issues."""
    out: dict[str, str] = {}
    for entry in payload.get("results") or []:
        iso = entry.get("iso_3166_1")
        if not iso:
            continue
        dates = sorted(entry.get("release_dates") or [],
                       key=lambda d: _RELEASE_ORDER.index(d.get("type"))
                       if d.get("type") in _RELEASE_ORDER else len(_RELEASE_ORDER))
        for d in dates:
            shown = display(iso, d.get("certification"))
            if shown:
                out[iso] = shown
                break
    return out


def certs_from_content_ratings(payload: dict) -> dict[str, str]:
    """Every country's drawable rating from a /tv/{id}/content_ratings body."""
    out: dict[str, str] = {}
    for entry in payload.get("results") or []:
        iso = entry.get("iso_3166_1")
        shown = display(iso, entry.get("rating")) if iso else None
        if shown:
            out[iso] = shown
    return out
