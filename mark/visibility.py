from __future__ import annotations

from collections.abc import Iterable

from . import config, db
from .sources import WATCHED_SOURCES

"""Single source of truth for which sessions are user-visible.

A session is filtered out of listings, search results and aggregates when
either of two non-destructive conditions holds:

* the user manually hid it (``sessions.hidden = 1``), or
* its source adapter is currently disabled in config.

Both keep the underlying rows, so an unhide or a re-enabled source brings the
data straight back without a re-scan. Direct access by id (opening a session,
following a deep link) deliberately ignores this so hidden sessions stay
reachable and manageable.
"""


def disabled_sources() -> set[str]:
    """The ``source`` strings whose owning watched adapter is currently disabled.

    Import sources (e.g. ChatGPT exports) have no enable toggle and are always
    visible, so only :data:`WATCHED_SOURCES` are considered.
    """
    out: set[str] = set()
    for source in WATCHED_SOURCES:
        if not config.resolve_source_config(source.default_config()).enabled:
            out.update(source.row_sources)
    return out


def sql_where(alias: str = "", *, only_hidden: bool = False) -> tuple[str, list[str]]:
    """A WHERE predicate selecting visible rows (or only hidden ones).

    ``alias`` is the ``sessions`` table alias used in the query (e.g. ``"s"``);
    pass ``""`` for an unaliased table. Returns ``(clause, params)`` ready to
    splice in with ``AND``. When ``only_hidden`` is set it matches manually
    hidden sessions regardless of source state, so the "manage hidden" view can
    surface everything the user can unhide.
    """
    prefix = f"{alias}." if alias else ""
    if only_hidden:
        return f"{prefix}hidden = 1", []

    clause = f"{prefix}hidden = 0"
    params: list[str] = []
    disabled = disabled_sources()
    if disabled:
        placeholders = ",".join("?" * len(disabled))
        clause += f" AND {prefix}source NOT IN ({placeholders})"
        params = sorted(disabled)
    return clause, params


def filter_visible(ids: Iterable[str]) -> set[str]:
    """Return the subset of ``ids`` that is currently visible.

    Used where ids are gathered outside the SQL layer (e.g. a collection's
    manual members) and still need the same visibility rules applied.
    """
    unique = list(dict.fromkeys(ids))
    if not unique:
        return set()
    clause, params = sql_where()
    placeholders = ",".join("?" * len(unique))
    sql = f"SELECT id FROM sessions WHERE id IN ({placeholders}) AND {clause}"
    with db.cursor() as cur:
        return {r["id"] for r in cur.execute(sql, [*unique, *params])}
