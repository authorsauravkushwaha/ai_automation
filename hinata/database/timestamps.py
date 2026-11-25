"""Timezone-aware timestamps in a format SQLite can actually compare.

The database stores every ``created_at``/``updated_at`` with
``datetime('now')``, which is **UTC** in the text format
``YYYY-MM-DD HH:MM:SS``. Python callers, however, naturally produce
``datetime.now().isoformat()`` = ``2026-11-25T17:33:59.001668+00:00``.

Those two formats are compared as *plain strings* by SQLite. ``'T'`` (0x54)
sorts after ``' '`` (0x20), so an **expired** ISO timestamp compares greater
than ``datetime('now')`` and was served as still valid. Fractional seconds and
``+HH:MM`` suffixes are not understood by SQLite's date functions either.

This module is the single place that converts a caller-supplied timestamp into
the canonical SQLite form:

* naive input is assumed to be UTC (the convention used by the schema), never
  the local wall clock, so the meaning does not change when a user moves
  timezone between writing and reading;
* aware input is converted to UTC before formatting;
* already-canonical text is returned unchanged (idempotent);
* unparseable text returns ``None`` so the caller can reject it instead of
  silently storing a value that will never expire correctly.

Epoch seconds must keep using :func:`sqlite_epoch_compare`, because the
``*_epoch`` columns are floats and mixing them with SQLite's text ``now`` is
wrong for a different reason.
"""

from __future__ import annotations

import datetime as dt

#: The exact text format SQLite's ``datetime()`` function produces.
SQLITE_DATETIME = "%Y-%m-%d %H:%M:%S"


def utc_now_sqlite() -> str:
    """Current UTC time in SQLite's canonical text format."""
    return dt.datetime.now(dt.UTC).strftime(SQLITE_DATETIME)


def to_sqlite_utc(value: str | dt.datetime | None) -> str | None:
    """Normalise ``value`` to ``'YYYY-MM-DD HH:MM:SS'`` UTC, or ``None``.

    ``None``/empty input returns ``None``. Text that cannot be parsed returns
    ``None`` -- callers must treat that as "invalid expiry" rather than storing
    a value SQLite will silently miscompare.
    """
    if value is None:
        return None
    moment: dt.datetime | None
    if isinstance(value, dt.datetime):
        moment = value
    else:
        text = str(value).strip()
        if not text:
            return None
        moment = _parse_datetime_text(text)
    if moment is None:
        return None
    if moment.tzinfo is None:
        # Naive values follow the schema convention: they are UTC.
        moment = moment.replace(tzinfo=dt.UTC)
    return moment.astimezone(dt.UTC).strftime(SQLITE_DATETIME)


def _parse_datetime_text(text: str) -> dt.datetime | None:
    """Parse the datetime spellings a Python caller is likely to produce."""
    candidate = text
    if candidate.endswith("Z") or candidate.endswith("z"):
        candidate = candidate[:-1] + "+00:00"
    # ``datetime.fromisoformat`` understands the space separator, 'T', fractional
    # seconds, and a numeric offset from Python 3.11 onwards.
    try:
        return dt.datetime.fromisoformat(candidate)
    except ValueError:
        pass
    for fmt in (SQLITE_DATETIME, "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(candidate, fmt)
        except ValueError:
            continue
    return None


def to_sqlite_utc_or_raise(value: str | dt.datetime, *, field: str = "timestamp") -> str:
    """Like :func:`to_sqlite_utc` but raises ``ValueError`` for invalid input."""
    normalized = to_sqlite_utc(value)
    if normalized is None:
        raise ValueError(f"{field} must be an ISO-8601 date/time; got {value!r}")
    return normalized


def sqlite_epoch_compare(column: str) -> str:
    """SQL expression: is a *float epoch* column still in the future?

    Epoch columns must be compared with ``strftime('%s','now')``. Using the text
    ``datetime('now')`` is silently wrong in the worst possible direction:
    SQLite coerces ``'2026-11-25 17:49:43'`` to its leading digits (``2026.0``),
    so an epoch of 1.76 billion compares as *greater* and every expired lease,
    deadline and message looks permanently live. It is not "always 0" -- it is
    "always true", which fails open.
    """
    return f"(CAST({column} AS REAL) > CAST(strftime('%s','now') AS REAL))"
