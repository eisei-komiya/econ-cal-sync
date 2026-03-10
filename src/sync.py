"""Sync high-impact economic events to Google Calendar.

Data source is pluggable via the ``EVENT_SOURCE`` environment variable
(default: ``forexfactory``).  Every source normalises its response
into :class:`models.EconomicEvent` so the sync logic is source-agnostic.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone

from google.oauth2 import service_account
from googleapiclient.discovery import build

from .fetchers import get_fetcher
from .models import EconomicEvent

# ---------------------------------------------------------------------------
# Configuration constants
# ---------------------------------------------------------------------------
# ForexFactory uses currency codes (e.g. "USD", "JPY") as country identifiers.
TARGET_COUNTRIES = {"USD", "JPY"}
IMPORTANCE_MIN = 2                # 1=Low, 2=Medium, 3=High
FETCH_WEEKS = 4                   # How many weeks ahead to fetch
CALENDAR_TIMEZONE = "Asia/Tokyo"
EVENT_DURATION_MINUTES = 30
REMINDER_MINUTES = [40, 10]

COUNTRY_FLAG: dict[str, str] = {
    "USD": "🇺🇸",
    "JPY": "🇯🇵",
}

# Google Calendar colorId: 11=Tomato (red) for High, 5=Banana (yellow) for Medium
_IMPORTANCE_COLOR: dict[int, str] = {
    3: "11",  # High  → Tomato (red)
    2: "5",   # Medium → Banana (yellow)
}

# ★ marks appended to the event title to indicate importance
_IMPORTANCE_STARS: dict[int, str] = {
    3: "★★★",  # High
    2: "★★",   # Medium
}

# Extended property key used for de-duplication in Google Calendar.
_EXT_PROP_KEY = "econ_event_id"

SCOPES = ["https://www.googleapis.com/auth/calendar"]

# ---------------------------------------------------------------------------
# Retry / back-off helper
# ---------------------------------------------------------------------------

_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
_MAX_RETRIES = 4
_BACKOFF_BASE = 2.0  # seconds


def _call_with_retry(request_callable, *, max_retries: int = _MAX_RETRIES) -> dict:
    """Execute a Google API request with exponential back-off on transient errors.

    ``request_callable`` must be a zero-argument callable that invokes
    ``<resource>.execute()`` and returns the response dict.

    Retried on:
    - :class:`googleapiclient.errors.HttpError` with status in
      ``_RETRYABLE_STATUS_CODES`` (429, 500, 502, 503, 504)
    - Network-level transient errors: :class:`socket.timeout`,
      :class:`ConnectionError`, :class:`TimeoutError`, and
      :class:`OSError` (covers ``BrokenPipeError``, ``ConnectionResetError``,
      etc.).

    Raises the last exception when all retries are exhausted.
    """
    import socket

    import googleapiclient.errors  # local import to keep top-level imports clean

    _RETRYABLE_NETWORK_ERRORS = (
        socket.timeout,
        ConnectionError,   # base class for BrokenPipeError, ConnectionResetError, etc.
        TimeoutError,      # built-in; raised by some HTTP clients on connect/read timeout
        OSError,           # catches remaining low-level socket errors
    )

    for attempt in range(max_retries + 1):
        try:
            return request_callable()
        except googleapiclient.errors.HttpError as exc:
            status = exc.status_code if hasattr(exc, "status_code") else exc.resp.status
            if status not in _RETRYABLE_STATUS_CODES or attempt == max_retries:
                raise
            wait = _BACKOFF_BASE ** attempt
            print(f"  [retry] HTTP {status}, waiting {wait:.0f}s (attempt {attempt + 1}/{max_retries})")
            time.sleep(wait)
        except _RETRYABLE_NETWORK_ERRORS as exc:
            if attempt == max_retries:
                raise
            wait = _BACKOFF_BASE ** attempt
            print(
                f"  [retry] network error ({type(exc).__name__}: {exc}), "
                f"waiting {wait:.0f}s (attempt {attempt + 1}/{max_retries})"
            )
            time.sleep(wait)


# ---------------------------------------------------------------------------
# Google Calendar helpers
# ---------------------------------------------------------------------------

def build_calendar_service():
    """Return an authenticated Google Calendar service using a service account."""
    sa_json = os.environ.get("GOOGLE_SA_JSON")
    if not sa_json:
        raise RuntimeError(
            "GOOGLE_SA_JSON environment variable is not set. "
            "Set it to the service account JSON content."
        )
    sa_info = json.loads(sa_json)
    credentials = service_account.Credentials.from_service_account_info(
        sa_info, scopes=SCOPES,
    )
    return build("calendar", "v3", credentials=credentials)


def _event_datetime(
    ev: EconomicEvent, duration_minutes: int,
) -> tuple[dict, dict]:
    """Return (start, end) dicts for a Google Calendar event."""
    if ev.dt_utc is not None:
        start = {"dateTime": ev.dt_utc.isoformat(), "timeZone": CALENDAR_TIMEZONE}
        end_dt = ev.dt_utc + timedelta(minutes=duration_minutes)
        end = {"dateTime": end_dt.isoformat(), "timeZone": CALENDAR_TIMEZONE}
    else:
        # All-day fallback: use event_date if available, otherwise today
        d = ev.event_date or datetime.now(timezone.utc).date()
        start = {"date": d.isoformat()}
        end = {"date": (d + timedelta(days=1)).isoformat()}
    return start, end


def build_gcal_event(ev: EconomicEvent) -> dict:
    """Convert a normalised :class:`EconomicEvent` to a Google Calendar event body."""
    flag = COUNTRY_FLAG.get(ev.country, "")
    stars = _IMPORTANCE_STARS.get(ev.importance, "")
    start, end = _event_datetime(ev, EVENT_DURATION_MINUTES)

    gcal: dict = {
        "summary": " ".join(filter(None, [flag, stars, ev.name])).strip(),
        "description": (
            f"Forecast: {ev.forecast}\n"
            f"Previous: {ev.previous}\n"
            f"Actual: {ev.actual}"
        ),
        "start": start,
        "end": end,
        "reminders": {
            "useDefault": False,
            "overrides": [
                {"method": "popup", "minutes": m} for m in REMINDER_MINUTES
            ],
        },
        "extendedProperties": {
            "private": {
                _EXT_PROP_KEY: ev.id,
            }
        },
    }
    color_id = _IMPORTANCE_COLOR.get(ev.importance)
    if color_id:
        gcal["colorId"] = color_id

    return gcal


_MAX_PAGINATION_PAGES = 50  # safety cap: 50 pages × 2500 events/page = 125,000 events


def get_existing_events(
    service, calendar_id: str, date_from: str, date_to: str,
) -> dict[str, str]:
    """Return a mapping of econ_event_id → Google Calendar event id."""
    mapping: dict[str, str] = {}
    time_min = f"{date_from}T00:00:00Z"
    time_max = f"{date_to}T23:59:59Z"
    page_token = None
    pages_fetched = 0

    while True:
        if pages_fetched >= _MAX_PAGINATION_PAGES:
            print(
                f"[get_existing_events] WARNING: reached pagination limit "
                f"({_MAX_PAGINATION_PAGES} pages). Stopping early."
            )
            break
        try:
            result = _call_with_retry(
                lambda pt=page_token: service.events()
                .list(
                    calendarId=calendar_id,
                    timeMin=time_min,
                    timeMax=time_max,
                    singleEvents=True,
                    pageToken=pt,
                )
                .execute()
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[get_existing_events] API error after retries: {exc}")
            break  # 取得済み分だけで続行（重複作成を最小限に抑える）

        pages_fetched += 1
        for item in result.get("items", []):
            eid = (
                item.get("extendedProperties", {})
                .get("private", {})
                .get(_EXT_PROP_KEY)
            )
            if eid:
                mapping[eid] = item["id"]
        page_token = result.get("nextPageToken")
        if not page_token:
            break

    return mapping


def upsert_event(
    service, calendar_id: str, gcal_event: dict, existing: dict[str, str],
) -> str:
    """Create or update a Google Calendar event.

    Returns ``'created'``, ``'updated'``, or ``'failed'``.  On API error the
    exception is logged and ``'failed'`` is returned so that the caller can
    continue processing the remaining events instead of aborting the entire run.
    """
    eid = gcal_event["extendedProperties"]["private"][_EXT_PROP_KEY]
    try:
        if eid in existing:
            _call_with_retry(
                lambda: service.events().update(
                    calendarId=calendar_id,
                    eventId=existing[eid],
                    body=gcal_event,
                ).execute()
            )
            return "updated"
        else:
            _call_with_retry(
                lambda: service.events().insert(
                    calendarId=calendar_id,
                    body=gcal_event,
                ).execute()
            )
            return "created"
    except Exception as exc:  # noqa: BLE001
        print(f"  [failed] {gcal_event.get('summary', eid)}: {exc}")
        return "failed"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    calendar_id = os.environ.get("GOOGLE_CALENDAR_ID")
    if not calendar_id:
        raise RuntimeError(
            "GOOGLE_CALENDAR_ID environment variable is not set. "
            "Set it to the target Google Calendar ID."
        )
    source_name = os.environ.get("EVENT_SOURCE", "forexfactory")

    fetcher = get_fetcher(source_name)
    print(f"Using data source: {fetcher.name}")

    today = datetime.now(timezone.utc).date()
    date_from = today.strftime("%Y-%m-%d")
    date_to = (today + timedelta(weeks=FETCH_WEEKS)).strftime("%Y-%m-%d")

    print(f"Fetching economic events from {date_from} to {date_to} ...")
    events = fetcher.fetch(
        date_from,
        date_to,
        countries=TARGET_COUNTRIES,
        importance_min=IMPORTANCE_MIN,
    )
    print(f"Found {len(events)} matching events after filtering.")

    if not events:
        print(
            "[sync] No events retrieved from the data source "
            "(e.g. public holiday week or all filtered out). Skipping calendar update."
        )
        return

    service = build_calendar_service()
    existing = get_existing_events(service, calendar_id, date_from, date_to)
    print(f"Found {len(existing)} existing events in Google Calendar.")

    created = updated = failed = 0
    for ev in events:
        gcal_event = build_gcal_event(ev)
        action = upsert_event(service, calendar_id, gcal_event, existing)
        if action == "created":
            created += 1
        elif action == "updated":
            updated += 1
        else:
            failed += 1
        if action != "failed":
            print(f"  [{action}] {gcal_event['summary']}")

    print(f"Done. Created: {created}, Updated: {updated}, Failed: {failed}")
    if failed:
        print(
            f"[sync] WARNING: {failed} event(s) could not be upserted. "
            "Check the logs above for details."
        )


if __name__ == "__main__":
    main()
