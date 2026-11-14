"""Operator-chosen names for the trending addon's catalogs: the Trending
settings' "Catalog names" section in the dashboard.

A client shows the name the manifest gives a catalog, and not every client
lets a user rename a catalog on its side (Nuvio TV doesn't), so the operator
can rename it here, for everyone on the instance, without the name riding in
the install URL.

A name is keyed by list and source ("movie" / "imdb"), not by catalog id:
the id of a list's default source is the list's plain id, so it would move to
another source when the operator sets TRENDING_SOURCE_<type>.  A name stands
in for the whole generated one, the source and language notes included.

Stored as one JSON object in app_state, so every worker reads the same names;
the manifest is fetched rarely enough to read it each time.
"""
from __future__ import annotations

import json
import logging
import re

from cache import get_app_state, set_app_state

logger = logging.getLogger(__name__)

_STATE_KEY = "trending_catalog_names"
MAX_NAME_LENGTH = 60
MAX_NAMES = 500
_SOURCE_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


def load() -> "dict[str, dict[str, str]]":
    """{list: {source: name}}; {} when none are set or the row is unreadable."""
    raw = get_app_state(_STATE_KEY)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        logger.warning("Trending catalog names: stored value is not JSON; ignoring it")
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(e): {str(s): str(n) for s, n in names.items() if str(n).strip()}
            for e, names in data.items() if isinstance(names, dict)}


def validate(names, endpoints) -> "dict[str, dict[str, str]]":
    """The editor's {list: {source: name}} cleaned: blank names dropped,
    whitespace collapsed.  Raises ValueError on anything malformed."""
    if not isinstance(names, dict):
        raise ValueError("names must be an object")
    out: dict[str, dict[str, str]] = {}
    count = 0
    for endpoint, by_source in names.items():
        if endpoint not in endpoints:
            raise ValueError(f"unknown list: {endpoint}")
        if not isinstance(by_source, dict):
            raise ValueError(f"{endpoint}: names must be an object")
        for source, name in by_source.items():
            source = str(source)
            if not _SOURCE_RE.match(source):
                raise ValueError(f"{endpoint}: bad source id")
            if not isinstance(name, str):
                raise ValueError(f"{endpoint}/{source}: name must be text")
            name = " ".join(name.split())
            if not name:
                continue
            if len(name) > MAX_NAME_LENGTH:
                raise ValueError(f"{endpoint}/{source}: longer than {MAX_NAME_LENGTH} characters")
            out.setdefault(endpoint, {})[source] = name
            count += 1
    if count > MAX_NAMES:
        raise ValueError(f"more than {MAX_NAMES} names")
    return out


def save(names: "dict[str, dict[str, str]]") -> None:
    set_app_state(_STATE_KEY, json.dumps(names, ensure_ascii=False, sort_keys=True))
    # set_app_state logs a failed write rather than raising it.
    if load() != names:
        raise OSError("the names were not stored (see the log)")
