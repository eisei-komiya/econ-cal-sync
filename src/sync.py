"""Sync high-impact economic events to Google Calendar.

Data source is pluggable via the ``EVENT_SOURCE`` environment variable
(default: ``forexfactory``).  Every source normalises its response
into :class:`models.EconomicEvent` so the sync logic is source-agnostic.
"""

from __future__ import annotations

import json
import os
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
# Google Calendar helpers
# ---------------------------------------------------------------------------

def build_calendar_service():
    """Return an authenticated Google Calendar service using a service account."""
    sa_json = os.environ["GOOGLE_SA_JSON"]
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
        "summary": f"{flag} {stars} {ev.name}".strip(),
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


def get_existing_events(
    service, calendar_id: str, date_from: str, date_to: str,
) -> dict[str, str]:
    """Return a mapping of econ_event_id → Google Calendar event id."""
    mapping: dict[str, str] = {}
    time_min = f"{date_from}T00:00:00Z"
    time_max = f"{date_to}T23:59:59Z"
    page_token = None

    while True:
        result = (
            service.events()
            .list(
                calendarId=calendar_id,
                timeMin=time_min,
                timeMax=time_max,
                singleEvents=True,
                pageToken=page_token,
            )
            .execute()
        )
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
    """Create or update a Google Calendar event.  Returns ``'created'`` or ``'updated'``."""
    eid = gcal_event["extendedProperties"]["private"][_EXT_PROP_KEY]
    if eid in existing:
        service.events().update(
            calendarId=calendar_id,
            eventId=existing[eid],
            body=gcal_event,
        ).execute()
        return "updated"
    else:
        service.events().insert(
            calendarId=calendar_id,
            body=gcal_event,
        ).execute()
        return "created"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    calendar_id = os.environ["GOOGLE_CALENDAR_ID"]
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
        raise RuntimeError(
            "No events retrieved from the data source. "
            "Check source connectivity and filters."
        )

    service = build_calendar_service()
    existing = get_existing_events(service, calendar_id, date_from, date_to)
    print(f"Found {len(existing)} existing events in Google Calendar.")

    created = updated = 0
    for ev in events:
        gcal_event = build_gcal_event(ev)
        action = upsert_event(service, calendar_id, gcal_event, existing)
        if action == "created":
            created += 1
        else:
            updated += 1
        print(f"  [{action}] {gcal_event['summary']}")

    print(f"Done. Created: {created}, Updated: {updated}")


if __name__ == "__main__":
    main()
