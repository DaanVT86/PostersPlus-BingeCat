# log_store.py
"""
The dashboard's copy of the log, and the queries its Logs view runs on it.

Every record the root logger handles is also appended, one JSON object per
line, to LOG_DIR/postersplus.log: time, level, logger, message (keys
redacted, the same way the console copy is), traceback, the worker's pid and
the id of the HTTP request that logged it.  The console output docker keeps
is untouched; this copy exists so the dashboard can filter, and so a line can
be tied to the request it came from ("show me everything that happened while
serving this poster").

One file for every worker: each line is a single O_APPEND write, which the
kernel keeps whole against the other workers' writes.  Past half of
LOG_VIEWER_MAX_MB the file is renamed to .1 (replacing the previous .1) under
a lock, and every worker notices within a second that the name now points at
a new file and reopens it — so the two files hold at most the configured
size between them.

Access-log lines keep their full, redacted request path here, where the
console copy cuts it at 80 characters: the ids in a poster URL are what an
operator searches for.
"""
from __future__ import annotations

import contextvars
import fcntl
import json
import logging
import os
import re
import secrets
import time
import traceback
from typing import Callable, Iterator

# The request a log line belongs to.  Set by RequestIdMiddleware for every
# HTTP request; tasks the request spawns inherit it, background loops have none.
request_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("log_request_id", default=None)

# The trending addon's "cfg-<base64 query>" path segment carries a user's
# poster settings, and with them any TMDB or MDBList key they typed in, where
# the key=value redaction can't see it.  The console log cuts access paths
# short enough to lose it; this copy keeps full paths, so it drops the blob.
_CFG_RE = re.compile(r"(\bcfg-)[A-Za-z0-9_=-]+")

_MAX_MESSAGE = 8192
_MAX_TRACE = 16384
_CHECK_EVERY = 1.0

_handler: "_JsonFileHandler | None" = None
_disabled_reason = "not started"


class RequestIdMiddleware:
    """Tags every HTTP request with a short random id for the log.  Plain ASGI
    so the endpoint runs in the same task and inherits it.  Never reset: the
    request's task ends with the request, and uvicorn's own "Exception in
    ASGI application" line, logged after the app returns, should carry it."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            request_id.set(secrets.token_hex(4))
        await self.app(scope, receive, send)


class _JsonFileHandler(logging.Handler):
    def __init__(self, path: str, max_bytes: int, redact: Callable[[str], str]):
        super().__init__(level=logging.NOTSET)
        self.path = path
        self.max_bytes = max_bytes
        self.redact = lambda text: _CFG_RE.sub(r"\1…", redact(text))
        self._fd: int | None = None
        self._ino = 0
        self._checked = 0.0
        self._written = 0
        self._open()

    def _open(self) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
        self._fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o640)
        self._ino = os.fstat(self._fd).st_ino
        self._checked = time.monotonic()

    def _check(self) -> None:
        """Reopen when another worker rotated the file; rotate when it is full."""
        self._checked = time.monotonic()
        try:
            st = os.stat(self.path)
        except FileNotFoundError:
            self._open()
            return
        if st.st_ino != self._ino:
            self._open()
            return
        if st.st_size <= self.max_bytes // 2:
            return
        with open(self.path + ".lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                st = os.stat(self.path)
            except FileNotFoundError:
                st = None
            # Another worker got there first when the name no longer points at our file.
            if st is not None and st.st_ino == self._ino and st.st_size > self.max_bytes // 2:
                os.replace(self.path, self.path + ".1")
        self._open()

    def _entry(self, record: logging.LogRecord) -> dict:
        entry: dict = {
            "t": round(record.created, 3),
            "lv": record.levelname,
            "lg": record.name,
            "p": record.process,
        }
        rid = request_id.get()
        if rid:
            entry["rid"] = rid
        args = record.args
        if record.name == "uvicorn.access" and isinstance(args, tuple) and len(args) >= 5:
            client, method, path, _version, status = args[:5]
            entry["m"] = f"{method} {self.redact(str(path))}"[:_MAX_MESSAGE]
            entry["st"] = int(status) if str(status).isdigit() else status
            if client:
                entry["ip"] = str(client).rsplit(":", 1)[0]
            return entry
        try:
            message = record.getMessage()
        except Exception:
            message = str(record.msg)
        entry["m"] = self.redact(message)[:_MAX_MESSAGE]
        trace = record.exc_text
        if not trace and record.exc_info:
            trace = "".join(traceback.format_exception(*record.exc_info))
        if trace:
            entry["x"] = self.redact(trace)[-_MAX_TRACE:]
        return entry

    def emit(self, record: logging.LogRecord) -> None:
        # Never raise and never log from here: this copy failing must not
        # disturb the console log or the request that logged.
        try:
            line = json.dumps(self._entry(record), ensure_ascii=False, separators=(",", ":"))
            data = (line + "\n").encode("utf-8", "replace")
            if time.monotonic() - self._checked > _CHECK_EVERY or self._written > self.max_bytes // 16:
                self._written = 0
                self._check()
            os.write(self._fd, data)
            self._written += len(data)
        except Exception:
            pass


def install(log_dir: str, max_mb: int, redact: Callable[[str], str]) -> None:
    """Start the dashboard copy.  Goes first among the root handlers so it
    sees each record before the console filter shortens access paths."""
    global _handler, _disabled_reason
    if max_mb <= 0:
        _disabled_reason = "LOG_VIEWER_MAX_MB is 0"
        return
    try:
        os.makedirs(log_dir, exist_ok=True)
        _handler = _JsonFileHandler(os.path.join(log_dir, "postersplus.log"), max_mb * 1024 * 1024, redact)
    except OSError as exc:
        _disabled_reason = f"{log_dir} is not writable ({exc.strerror or exc})"
        logging.getLogger(__name__).warning(f"Dashboard log viewer off: {_disabled_reason}")
        return
    logging.getLogger().handlers.insert(0, _handler)


def status() -> dict:
    if _handler is None:
        return {"enabled": False, "reason": _disabled_reason}
    size = 0
    for p in (_handler.path + ".1", _handler.path):
        try:
            size += os.stat(p).st_size
        except OSError:
            pass
    return {"enabled": True, "size": size, "max_bytes": _handler.max_bytes}


# ─────────────────────────────────────────────────────────────────────────────
# Reading.  Both files are small enough (LOG_VIEWER_MAX_MB, 20 MB by default)
# to read whole for a search; a live tail reads only what was written since
# its cursor.  Lines are only JSON-parsed once a cheap byte test says they
# might match.  A line's id is "<inode>.<offset>" in hex, which survives the
# rotation that renames its file.

# (inode, bytes, offset of those bytes in the file), oldest file first.
Segment = tuple[int, bytes, int]


def _segments(after: tuple[int, int] | None = None) -> list[Segment]:
    """The log files, or with *after* only what follows that position: the
    rest of its file and all of any newer one."""
    files = []
    for p in (_handler.path + ".1", _handler.path):
        try:
            files.append((p, os.stat(p).st_ino))
        except OSError:
            pass
    skip_before = None
    if after is not None:
        skip_before = next((i for i, (_, ino) in enumerate(files) if ino == after[0]), None)
    out = []
    for i, (p, _) in enumerate(files):
        if skip_before is not None and i < skip_before:
            continue
        start = after[1] if skip_before is not None and i == skip_before else 0
        try:
            with open(p, "rb") as f:
                ino = os.fstat(f.fileno()).st_ino
                f.seek(start)
                out.append((ino, f.read(), start))
        except OSError:
            pass
    return out


def _parse_cursor(cursor: str | None) -> tuple[int, int] | None:
    if not cursor:
        return None
    ino, _, off = cursor.partition(".")
    try:
        return int(ino, 16), int(off, 16)
    except ValueError:
        return None


def _key(ino: int, off: int) -> str:
    return f"{ino:x}.{off:x}"


def _backwards(segs: list[Segment], before: tuple[int, int] | None) -> Iterator[tuple[int, int, bytes]]:
    """(inode, offset, line) newest first, starting just before *before*."""
    start_seg = len(segs) - 1
    end = None
    if before is not None:
        for i, (ino, _, base) in enumerate(segs):
            if ino == before[0]:
                start_seg, end = i, before[1] - base
                break
        else:
            return      # rotated out of reach: nothing older to give
    for i in range(start_seg, -1, -1):
        ino, data, base = segs[i]
        stop = len(data) if (i != start_seg or end is None) else max(0, min(end, len(data)))
        # Drop a partial last line (a write in progress).
        if i == len(segs) - 1 and stop == len(data) and data and not data.endswith(b"\n"):
            stop = data.rfind(b"\n") + 1
        pos = stop
        while pos > 0:
            nl = data.rfind(b"\n", 0, pos - 1)
            s = nl + 1
            line = data[s:pos - 1] if data[pos - 1:pos] == b"\n" else data[s:pos]
            if line:
                yield ino, base + s, line
            pos = s


def _forwards(segs: list[Segment]) -> Iterator[tuple[int, int, bytes]]:
    for ino, data, base in segs:
        pos = 0
        while pos < len(data):
            nl = data.find(b"\n", pos)
            if nl < 0:
                break           # partial line: the next poll picks it up
            if nl > pos:
                yield ino, base + pos, data[pos:nl]
            pos = nl + 1


def _tail_cursor(segs: list[Segment]) -> str | None:
    if not segs:
        return None
    ino, data, base = segs[-1]
    return _key(ino, base + data.rfind(b"\n") + 1)


def _line_time(raw: bytes) -> float | None:
    """The time of a raw line without parsing it ("t" is always first)."""
    if raw.startswith(b'{"t":'):
        try:
            return float(raw[5:raw.index(b",")])
        except ValueError:
            pass
    return None


# ── Filters ──────────────────────────────────────────────────────────────────

_LEVEL_BYTES = {lv: f'"lv":"{lv}"'.encode() for lv in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")}
_NOISE_PATHS = ("/health", "/static/", "/favicon", "/admin/api/")
_TERM_RE = re.compile(r'(-?)"([^"]*)"|(\S+)')
_RID_RE = re.compile(rb'"rid":"([^"]{1,16})"')


def id_pattern(token: str) -> re.Pattern | None:
    """A title id as it appears in log lines.  IMDb and TVDB ids name their
    namespace; a TMDB id is a bare number, so it only counts in the places the
    code logs one (tmdb_id=, tmdb:, "for 550", movie_550_…), not as any
    number that happens to match (widths, years, vote counts)."""
    token = token.strip().lower()
    if re.fullmatch(r"tt\d{5,10}", token):
        return re.compile(rf"(?<![a-z0-9]){token}(?!\d)", re.I)
    m = re.fullmatch(r"tvdb:(\d{1,10})", token)
    if m:
        return re.compile(rf"tvdb(?:_id)?[=:/ ]\s*{m[1]}(?!\d)", re.I)
    m = re.fullmatch(r"(?:tmdb:|movie:|tv:)?(\d{1,10})", token)
    if m:
        return re.compile(
            rf"(?:tmdb(?:_id)?[=:/]|\bfor |\b(?:movie|tv|series)[_/: ]|tt\d+:|\bposter ){m[1]}(?![\d.])", re.I)
    return None


def tmdb_kind_in(text: str, tmdb_id: str) -> str | None:
    """Whether a line says TMDB id *tmdb_id* is a movie or a series: a cache
    key (movie_550_…, tmdb:1399:1399:tv:…), a tv/1399 path, or a poster URL's
    type=.  admin.html's lgTypeIn reads lines the same way."""
    m = (re.search(rf"\b(movie|tv|series)[_/: ]{tmdb_id}(?![\d.])", text, re.I)
         or re.search(rf"[:/]{tmdb_id}(?::\d+)*:(movie|tv|series)\b", text, re.I))
    if m is None and re.search(rf"(?:tmdb_id=|tmdb:){tmdb_id}(?!\d)", text, re.I):
        m = re.search(r"[?&](?:type|media_type)=(movie|tv|series)\b", text, re.I)
    if m is None:
        return None
    return "movie" if m[1].lower() == "movie" else "tv"


class _IdToken:
    def __init__(self, token: str):
        self.pattern = id_pattern(token)
        # "movie:550" / "tv:550": a TMDB id that knows its kind, so lines
        # about the other kind with the same number can be left out.
        m = re.fullmatch(r"(movie|tv):(\d+)", token)
        self.kind, self.tmdb_id = (m[1], m[2]) if m else (None, None)
        self.raw = re.sub(r"^(?:tmdb|tvdb|movie|tv):", "", token).encode()

    def hits(self, hay: str) -> bool:
        if not self.pattern.search(hay):
            return False
        return not self.kind or tmdb_kind_in(hay, self.tmdb_id) in (None, self.kind)


class Query:
    def __init__(self, *, levels: str = "", q: str = "", ids: str = "", rid: str = "",
                 logger: str = "", kind: str = "all", noise: bool = False,
                 problems: bool = False, since: float = 0.0):
        self.levels = {s.strip().upper() for s in levels.split(",") if s.strip()} or None
        if self.levels and "ERROR" in self.levels:
            self.levels.add("CRITICAL")
        self.rid = rid.strip()[:16] or None
        self.logger = logger.strip() or None
        self.kind = kind if kind in ("app", "access") else "all"
        self.noise = noise
        self.problems = problems
        self.since = since
        self.ids = [_IdToken(t) for t in (t.strip().lower() for t in ids.split(",")) if id_pattern(t)][:8]
        self.typed_ids = [t for t in self.ids if t.kind]
        self.regex = None
        self.include: list[str] = []
        self.exclude: list[str] = []
        q = q.strip()[:300]
        if len(q) > 2 and q.startswith("/") and q.endswith("/"):
            try:
                self.regex = re.compile(q[1:-1], re.I)
            except re.error as exc:
                raise ValueError(f"Bad regular expression: {exc}")
        else:
            for neg, quoted, bare in _TERM_RE.findall(q):
                term = quoted if quoted or neg else bare
                if bare and bare.startswith("-") and len(bare) > 1:
                    neg, term = "-", bare[1:]
                if term:
                    (self.exclude if neg else self.include).append(term.lower())
        # Byte tests that rule a line out before it is parsed.  Only terms that
        # JSON would write unchanged can be tested on the raw bytes.
        self._raw_terms = [t.encode() for t in self.include if t.isascii() and '"' not in t and "\\" not in t]

    @property
    def narrows(self) -> bool:
        """Selects lines by content, as opposed to only hiding some."""
        return bool(self.include or self.regex or self.ids or self.rid)

    def prefilter(self, raw: bytes, check_level: bool = True) -> bool:
        if check_level and self.levels and not self.problems and not any(_LEVEL_BYTES[lv] in raw for lv in self.levels if lv in _LEVEL_BYTES):
            return False
        if self.rid and self.rid.encode() not in raw:
            return False
        # Every form an id is matched in contains its digits (or tt… id).
        if self.ids and not any(t.raw in raw for t in self.ids):
            return False
        if self._raw_terms:
            low = raw.lower()
            if not all(t in low for t in self._raw_terms):
                return False
        return True

    @staticmethod
    def _hay(e: dict) -> str:
        return f"{e.get('m', '')}\n{e.get('x', '')}\n{e.get('lg', '')}"

    def match(self, e: dict, check_level: bool = True) -> bool:
        access = e.get("lg") == "uvicorn.access"
        if self.kind == "app" and access or self.kind == "access" and not access:
            return False
        if access and not self.noise and e.get("m", "").split(" ", 1)[-1].startswith(_NOISE_PATHS):
            return False
        if check_level:
            if self.problems:
                st = e.get("st")
                bad = e.get("lv") in ("WARNING", "ERROR", "CRITICAL") or (isinstance(st, int) and st >= 400)
                if not bad:
                    return False
            if self.levels and e.get("lv") not in self.levels:
                return False
        if self.rid and e.get("rid") != self.rid:
            return False
        if self.logger and e.get("lg") != self.logger:
            return False
        if not (self.include or self.exclude or self.regex or self.ids):
            return True
        hay = self._hay(e)
        if self.ids and not any(t.hits(hay) for t in self.ids):
            return False
        if self.regex is not None:
            return any(self.regex.search(part) for part in (e.get("m", ""), e.get("x", "")) if part)
        low = hay.lower()
        return all(t in low for t in self.include) and not any(t in low for t in self.exclude)

    def wrong_kind(self, e: dict, request: list[dict]) -> bool:
        """A line that matched only a movie:/tv: TMDB id without saying its
        kind ("Poster cache hit for 550"), from a request whose other lines
        say it was the other kind."""
        if not self.typed_ids:
            return False
        hay = self._hay(e)
        hits = [t for t in self.ids if t.hits(hay)]
        if not hits or any(not t.kind for t in hits):
            return False
        for t in hits:
            said = next((k for k in (tmdb_kind_in(self._hay(x), t.tmdb_id) for x in request) if k), None)
            if said in (None, t.kind):
                return False
        return True


def _load(raw: bytes) -> dict | None:
    try:
        e = json.loads(raw)
    except ValueError:
        return None
    return e if isinstance(e, dict) else None


def _out(ino: int, off: int, e: dict, context: bool = False) -> dict:
    e["id"] = _key(ino, off)
    if context:
        e["ctx"] = 1
    return e


def _request_lines(segs: list[Segment], rids: set[str], floor: float) -> dict[str, list[tuple[int, int, dict]]]:
    """Every line of the requests *rids* logged since *floor*, newest first.
    A request's lines sit within seconds of each other, so callers pass a
    minute before the oldest line they have from one."""
    found: dict[str, list[tuple[int, int, dict]]] = {}
    if not rids:
        return found
    for ino, off, raw in _backwards(segs, None):
        m = _RID_RE.search(raw)
        if m is None or m[1].decode() not in rids:
            t = _line_time(raw)
            if t is not None and t < floor:
                break
            continue
        e = _load(raw)
        if e is None:
            continue
        if e.get("t", 0) < floor:
            break
        found.setdefault(e["rid"], []).append((ino, off, e))
    return found


def _with_requests(query: Query, segs: list[Segment], anchors: list[tuple[int, int, dict]],
                   expand: bool) -> list[dict]:
    """The matched lines, less those a movie:/tv: filter rules out by their
    request, plus (with *expand*) the rest of each matched request as ctx."""
    rids = {e["rid"] for _, _, e in anchors if e.get("rid")}
    if not (expand or query.typed_ids) or not rids:
        return [_out(i, o, e) for i, o, e in anchors]
    floor = min(e.get("t", 0) for _, _, e in anchors) - 60
    requests = _request_lines(segs, rids, floor)
    kept = [(i, o, e) for i, o, e in anchors
            if not query.wrong_kind(e, [x for _, _, x in requests.get(e.get("rid"), [])])]
    entries = [_out(i, o, e) for i, o, e in kept]
    if expand:
        seen = {e["id"] for e in entries}
        for rid in {e["rid"] for _, _, e in kept if e.get("rid")}:
            for ino, off, e in requests.get(rid, [])[:1000]:
                if _key(ino, off) not in seen and not _hidden_context(query, e):
                    entries.append(_out(ino, off, e, context=True))
    order = {ino: i for i, (ino, _, _) in enumerate(segs)}

    def position(e: dict) -> tuple:
        ino, off = _parse_cursor(e["id"])
        return order.get(ino, -1), off
    return sorted(entries, key=position, reverse=True)


_LOGGER_RE = re.compile(rb'"lg":"([A-Za-z0-9_.]{1,60})"')


def search(query: Query, *, before: str | None = None, limit: int = 400, expand: bool = False) -> dict:
    """The newest *limit* lines matching *query* older than *before*.  With
    *expand*, a match also brings every other line of its request (marked
    ctx), so an error comes with what led to it."""
    segs = _segments()
    expand = expand and bool(query.narrows or query.levels or query.problems)
    anchors: list[tuple[int, int, dict]] = []
    older = None
    for ino, off, raw in _backwards(segs, _parse_cursor(before)):
        if not query.prefilter(raw):
            # A cheap look at the time so a "last hour" query stops early.
            t = _line_time(raw) if query.since else None
            if t is not None and t < query.since:
                break
            continue
        e = _load(raw)
        if e is None:
            continue
        if query.since and e.get("t", 0) < query.since:
            break
        if query.match(e):
            anchors.append((ino, off, e))
            if len(anchors) >= limit:
                older = _key(ino, off)
                break

    loggers = sorted({m.decode() for _, data, _ in segs for m in _LOGGER_RE.findall(data)})
    return {
        "entries": _with_requests(query, segs, anchors, expand),
        "older": older,
        "tail": _tail_cursor(segs),
        "counts": _counts(segs, query),
        "loggers": loggers,
    }


def _hidden_context(query: Query, e: dict) -> bool:
    """Context lines skip the filters that pick lines by content, but still
    honour "hide health checks" and the app/access split."""
    access = e.get("lg") == "uvicorn.access"
    if query.kind == "app" and access or query.kind == "access" and not access:
        return True
    return access and not query.noise and e.get("m", "").split(" ", 1)[-1].startswith(_NOISE_PATHS)


def _counts(segs: list[Segment], query: Query) -> dict:
    """Warnings and errors among the lines the other filters select — rare
    enough to parse every one."""
    counts = {"WARNING": 0, "ERROR": 0}
    for _, data, _ in segs:
        for lv, key in (("WARNING", b'"lv":"WARNING"'), ("ERROR", b'"lv":"ERROR"'), ("ERROR", b'"lv":"CRITICAL"')):
            pos = data.find(key)
            while pos >= 0:
                s = data.rfind(b"\n", 0, pos) + 1
                n = data.find(b"\n", pos)
                n = len(data) if n < 0 else n
                raw = data[s:n]
                if query.prefilter(raw, check_level=False):
                    e = _load(raw)
                    if e and (not query.since or e.get("t", 0) >= query.since) and query.match(e, check_level=False):
                        counts[lv] += 1
                pos = data.find(key, n)
    return counts


def tail(query: Query, after: str, limit: int = 1000, expand: bool = False) -> dict:
    """Lines written since *after* (a tail cursor), oldest first.  Reads only
    those bytes, unless a match needs its request's earlier lines (*expand*,
    or a movie:/tv: id filter), which may predate the cursor."""
    cursor = _parse_cursor(after)
    new = _segments(cursor)
    anchors = []
    for ino, off, raw in _forwards(new):
        if not query.prefilter(raw):
            continue
        e = _load(raw)
        if e is not None and query.match(e):
            anchors.append((ino, off, e))
    dropped = max(0, len(anchors) - limit)
    anchors = anchors[dropped:]
    expand = expand and bool(query.narrows or query.levels or query.problems)
    if anchors and (expand or query.typed_ids) and any(e.get("rid") for _, _, e in anchors):
        entries = _with_requests(query, _segments(), anchors[::-1], expand)[::-1]
    else:
        entries = [_out(i, o, e) for i, o, e in anchors]
    return {"entries": entries, "tail": _tail_cursor(new) or after, "dropped": dropped}
