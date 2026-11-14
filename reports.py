"""Poster reports: users flag a poster from the configurator's live preview and
the operator reviews them in the dashboard's Reports view.

Off unless the operator turns it on (REPORTS_ENABLED).  Abuse is handled per
reporting address:

  * REPORTS_PER_IP caps the reports one address may file in 24 hours;
  * REPORTS_PURGE_THRESHOLD: an address that sends more than this many
    within 7 days (refused ones included) has every unresolved report it
    filed deleted and is blocked from reporting until the operator unblocks
    it.

A report the operator resolves stops counting against both, so someone whose
reports keep turning out right isn't held back by limits meant for noise.
Dismissing or deleting one gives nothing back; reopening takes it back.

Both depend on seeing each user's own address.  Behind a reverse proxy that
means uvicorn has to trust the proxy's X-Forwarded-For (FORWARDED_ALLOW_IPS);
without that every request comes from the proxy, everyone shares one limit
and one user's flood gets everyone's reports deleted.  forwarding_state()
detects that case, and reports are refused while it holds.

Addresses are never stored: a reporter is an HMAC of the address under a
per-instance secret, so the dashboard can group and block reporters without
the database holding anyone's IP.  IPv6 addresses are keyed by their /64,
which one household or server holds as a whole, so rotating within it
doesn't reset the limits.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import logging
import re
import secrets
import time
from urllib.parse import parse_qsl, urlencode, urlsplit

import config as _cfg
import param_aliases
from cache import _db_lock, get_app_state, get_db, set_app_state

logger = logging.getLogger(__name__)

CATEGORIES = {
    "fake_textless": "Text on a textless poster",
    "wrong_poster":  "Wrong poster or backdrop",
    "wrong_logo":    "Wrong or missing logo",
    "wrong_info":    "Wrong rating, sash or details",
    "other":         "Something else",
}
STATUSES = ("open", "resolved", "dismissed")

MAX_NOTE = 500
MAX_TITLE = 200
MAX_PARAMS = 4000
# However many addresses take part, the table stops growing here.
MAX_OPEN_REPORTS = 10_000
_DAY = 86_400.0
_PURGE_WINDOW = 7 * _DAY
_SALT_KEY = "report_salt"
_FORWARDING_KEY = "report_forwarding"

# Each id on its own terms, ASCII only (str.isdigit also takes "²" and
# other scripts' digits): both end up in the dashboard's markup.
_TMDB_ID_RE = re.compile(r"^[0-9]{1,10}$")
_IMDB_ID_RE = re.compile(r"^tt[0-9]{1,10}$")
# Control characters, zero-width characters and bidi overrides: they can
# make a note or title display as something other than what it says.
_UNSAFE_TEXT_RE = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2060-\u2069\ufeff]")

# Query parameters the stored preview URL keeps out: anything that is a key.
_SECRET_PARAMS = {"access_key", "tmdb_key", "mdblist_key", "fanart_key", "tvdb_key",
                  "simkl_key", "api_key", "key", "token"}


class ReportError(Exception):
    """A refusal the reporter is shown: ``status`` is the HTTP status."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


# ── Addresses ────────────────────────────────────────────────────────────────

def forwarding_state(client_host: str | None, headers) -> str:
    """How this request's client address came about:

      "forwarded"  a proxy sent X-Forwarded-For and uvicorn used it;
      "untrusted"  a proxy sent X-Forwarded-For but uvicorn ignored it, so
                   the address is the proxy's (FORWARDED_ALLOW_IPS unset);
      "private"    no forwarded header and a private peer: a home network,
                   or a proxy that sends no X-Forwarded-For;
      "direct"     no forwarded header and a public peer.
    """
    raw = ",".join(v for k, v in headers.items() if k.lower() == "x-forwarded-for")
    hops = [h.strip() for h in raw.split(",") if h.strip()]
    if hops:
        return "forwarded" if client_host in hops else "untrusted"
    try:
        addr = ipaddress.ip_address(client_host or "")
    except ValueError:
        return "private"
    return "private" if (addr.is_private or addr.is_loopback) else "direct"


def note_forwarding(state: str) -> None:
    """Remember the latest state for the dashboard, across workers."""
    now = time.time()
    last = get_app_state(_FORWARDING_KEY) or ""
    prev_state, _, prev_ts = last.partition("|")
    # The table is written at most once a minute per unchanged state.
    try:
        if prev_state == state and now - float(prev_ts or 0) < 60:
            return
    except ValueError:
        pass
    set_app_state(_FORWARDING_KEY, f"{state}|{now:.0f}")


def last_forwarding() -> tuple[str | None, float | None]:
    last = get_app_state(_FORWARDING_KEY) or ""
    state, _, ts = last.partition("|")
    try:
        return (state or None), (float(ts) if ts else None)
    except ValueError:
        return None, None


def _salt() -> bytes:
    salt = get_app_state(_SALT_KEY)
    if not salt:
        # INSERT OR IGNORE: two workers starting at once agree on one secret.
        with _db_lock:
            get_db().execute("INSERT OR IGNORE INTO app_state (key, value) VALUES (?, ?)",
                             (_SALT_KEY, secrets.token_hex(32)))
            get_db().commit()
        salt = get_app_state(_SALT_KEY) or ""
    return bytes.fromhex(salt)


def reporter_id(client_host: str | None) -> str:
    """The stored stand-in for an address."""
    host = client_host or "?"
    try:
        addr = ipaddress.ip_address(host)
        if addr.version == 6:
            host = str(ipaddress.ip_network(f"{addr}/64", strict=False))
    except ValueError:
        pass
    return hmac.new(_salt(), host.encode("utf-8"), hashlib.sha256).hexdigest()[:24]


# ── Filing ───────────────────────────────────────────────────────────────────

def clean_text(text: str, limit: int, keep_newlines: bool = False) -> str:
    """User text as the dashboard may show it: no control, zero-width or
    bidi-override characters, and (unless keep_newlines) one line."""
    text = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    if not keep_newlines:
        text = text.replace("\n", " ").replace("\t", " ")
    text = _UNSAFE_TEXT_RE.sub("", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()[:limit]


def clean_params(url_or_query: str) -> str:
    """The reported poster's query string with every key taken out."""
    text = (url_or_query or "").strip()
    query = urlsplit(text).query if "?" in text or "://" in text else text
    # Spelled out, so the dashboard shows the settings by name.
    pairs = [(k, v) for k, v in param_aliases.expand(parse_qsl(query, keep_blank_values=True))
             if k.lower() not in _SECRET_PARAMS and not k.lower().endswith("_key")]
    return urlencode(pairs)[:MAX_PARAMS]


def _limits() -> tuple[int, int]:
    return max(1, int(_cfg.REPORTS_PER_IP)), max(0, int(_cfg.REPORTS_PURGE_THRESHOLD))


def submit(*, reporter: str, media_type: str, tmdb_id: str, imdb_id: str, title: str,
           category: str, note: str, params: str) -> dict:
    """File a report, or raise ReportError saying why not."""
    if media_type not in ("movie", "tv"):
        raise ReportError(400, "Unknown media type")
    tmdb_id = (tmdb_id or "").strip()
    imdb_id = (imdb_id or "").strip()
    if not (tmdb_id or imdb_id):
        raise ReportError(400, "Pick a title first")
    if (tmdb_id and not _TMDB_ID_RE.match(tmdb_id)) or (imdb_id and not _IMDB_ID_RE.match(imdb_id)):
        raise ReportError(400, "Bad title id")
    if category not in CATEGORIES:
        raise ReportError(400, "Pick what is wrong")
    note = clean_text(note, MAX_NOTE, keep_newlines=True)
    if category == "other" and not note:
        raise ReportError(400, "Say what is wrong")
    title = clean_text(title, MAX_TITLE)

    per_ip, purge_at = _limits()
    now = time.time()
    db = get_db()
    with _db_lock:
        if db.execute("SELECT 1 FROM report_blocks WHERE reporter = ?", (reporter,)).fetchone():
            raise ReportError(403, "Reports from your address are blocked on this instance")
        db.execute("DELETE FROM report_attempts WHERE ts < ?", (now - _PURGE_WINDOW,))
        attempt = db.execute("INSERT INTO report_attempts (reporter, ts) VALUES (?, ?)",
                             (reporter, now)).lastrowid
        attempts = db.execute("SELECT COUNT(*) FROM report_attempts WHERE reporter = ? AND credited = 0",
                              (reporter,)).fetchone()[0]
        if purge_at and attempts > purge_at:
            removed = db.execute("DELETE FROM poster_reports WHERE reporter = ? AND status != 'resolved'",
                                 (reporter,)).rowcount
            db.execute(
                "INSERT OR REPLACE INTO report_blocks (reporter, blocked_at, reason, removed) VALUES (?, ?, ?, ?)",
                (reporter, now, f"{attempts} reports in 7 days", removed))
            db.commit()
            logger.warning(f"Reports: reporter {reporter[:8]} sent {attempts} reports in 7 days "
                           f"(threshold {purge_at}); deleted its {removed} reports and blocked it")
            raise ReportError(403, "Reports from your address are blocked on this instance")
        # Filed reports, whatever became of them since, bar resolved ones.
        recent = db.execute(
            "SELECT COUNT(*) FROM report_attempts WHERE reporter = ? AND ts >= ? "
            "AND report_id IS NOT NULL AND credited = 0",
            (reporter, now - _DAY)).fetchone()[0]
        if recent >= per_ip:
            db.commit()
            raise ReportError(429, f"You can send {per_ip} report{'s' if per_ip != 1 else ''} a day; try again tomorrow")
        dup = db.execute(
            "SELECT 1 FROM poster_reports WHERE reporter = ? AND media_type = ? AND tmdb_id = ? "
            "AND imdb_id = ? AND category = ? AND status = 'open'",
            (reporter, media_type, tmdb_id, imdb_id, category)).fetchone()
        if dup:
            db.commit()
            raise ReportError(409, "You have already reported this")
        if db.execute("SELECT COUNT(*) FROM poster_reports WHERE status = 'open'").fetchone()[0] >= MAX_OPEN_REPORTS:
            db.commit()
            raise ReportError(503, "This instance is not taking reports right now")
        cur = db.execute(
            "INSERT INTO poster_reports (created_at, reporter, media_type, tmdb_id, imdb_id, title, "
            "category, note, params, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open')",
            (now, reporter, media_type, tmdb_id, imdb_id, title, category, note, params))
        db.execute("UPDATE report_attempts SET report_id = ? WHERE rowid = ?", (cur.lastrowid, attempt))
        db.commit()
    logger.info(f"Reports: {category} for {media_type} {tmdb_id or imdb_id} from {reporter[:8]}")
    return {"id": cur.lastrowid}


# ── Dashboard ────────────────────────────────────────────────────────────────

def list_reports(status: str = "open", limit: int = 500) -> list[dict]:
    where, args = ("", ()) if status == "all" else ("WHERE r.status = ?", (status,))
    rows = get_db().execute(
        f"""SELECT r.id, r.created_at, r.reporter, r.media_type, r.tmdb_id, r.imdb_id, r.title,
                   r.category, r.note, r.params, r.status, r.resolved_at,
                   (SELECT COUNT(*) FROM poster_reports x WHERE x.reporter = r.reporter)
            FROM poster_reports r {where} ORDER BY r.created_at DESC LIMIT ?""",
        (*args, limit)).fetchall()
    keys = ("id", "created_at", "reporter", "media_type", "tmdb_id", "imdb_id", "title",
            "category", "note", "params", "status", "resolved_at", "reporter_total")
    return [dict(zip(keys, row)) for row in rows]


def counts() -> dict:
    out = {s: 0 for s in STATUSES}
    for status, n in get_db().execute("SELECT status, COUNT(*) FROM poster_reports GROUP BY status"):
        out[status] = n
    return out


def set_status(report_ids: list[int], status: str) -> int:
    if status not in STATUSES:
        raise ReportError(400, "Unknown status")
    ids = [int(i) for i in report_ids][:1000]
    if not ids:
        return 0
    marks = ",".join("?" * len(ids))
    with _db_lock:
        n = get_db().execute(
            f"UPDATE poster_reports SET status = ?, resolved_at = ? WHERE id IN ({marks})",
            (status, None if status == "open" else time.time(), *ids)).rowcount
        # Resolved hands the reporter's quota back; anything else takes it.
        get_db().execute(f"UPDATE report_attempts SET credited = ? WHERE report_id IN ({marks})",
                         (1 if status == "resolved" else 0, *ids))
        get_db().commit()
    return n


def delete(report_ids: list[int]) -> int:
    ids = [int(i) for i in report_ids][:1000]
    if not ids:
        return 0
    with _db_lock:
        n = get_db().execute(f"DELETE FROM poster_reports WHERE id IN ({','.join('?' * len(ids))})",
                             ids).rowcount
        get_db().commit()
    return n


def block(reporter: str) -> int:
    """Block a reporter by hand and delete its unresolved reports; returns
    how many."""
    with _db_lock:
        db = get_db()
        removed = db.execute("DELETE FROM poster_reports WHERE reporter = ? AND status != 'resolved'",
                             (reporter,)).rowcount
        db.execute("INSERT OR REPLACE INTO report_blocks (reporter, blocked_at, reason, removed) "
                   "VALUES (?, ?, 'blocked from the dashboard', ?)", (reporter, time.time(), removed))
        db.commit()
    return removed


def unblock(reporter: str) -> bool:
    with _db_lock:
        db = get_db()
        n = db.execute("DELETE FROM report_blocks WHERE reporter = ?", (reporter,)).rowcount
        db.execute("DELETE FROM report_attempts WHERE reporter = ?", (reporter,))
        db.commit()
    return bool(n)


def blocks() -> list[dict]:
    rows = get_db().execute(
        "SELECT reporter, blocked_at, reason, removed FROM report_blocks ORDER BY blocked_at DESC").fetchall()
    return [dict(zip(("reporter", "blocked_at", "reason", "removed"), r)) for r in rows]
