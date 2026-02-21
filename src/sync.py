"""Sync high/medium-impact economic events from FMP to Google Calendar."""

import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from google.oauth2 import service_account
from googleapiclient.discovery import build

# ---------------------------------------------------------------------------
# Configuration constants
# ---------------------------------------------------------------------------
TARGET_COUNTRIES = ["US", "JP"]   # ISO-3166-1 alpha-2 country codes to keep
TARGET_IMPACTS = {"high"}
FETCH_WEEKS = 4                   # How many weeks ahead to fetch
CALENDAR_TIMEZONE = "Asia/Tokyo"
EVENT_DURATION_MINUTES = 30
REMINDER_MINUTES = [40, 10]

COUNTRY_FLAG = {
    "US": "🇺🇸",
    "JP": "🇯🇵",
}

SCOPES = ["https://www.googleapis.com/auth/calendar"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def build_calendar_service():
    """Return an authenticated Google Calendar service using a service account."""
    sa_json = os.environ["GOOGLE_SA_JSON"]
    sa_info = json.loads(sa_json)
    credentials = service_account.Credentials.from_service_account_info(
        sa_info, scopes=SCOPES
    )
    return build("calendar", "v3", credentials=credentials)


def fetch_economic_events(api_key: str, date_from: str, date_to: str) -> list[dict]:
    """Fetch economic calendar events from FMP and return filtered list."""
    params = urllib.parse.urlencode({
        "from": date_from,
        "to": date_to,
        "apikey": api_key,
    })
    url = f"https://financialmodelingprep.com/stable/economic-calendar?{params}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req) as resp:  # noqa: S310
        events = json.loads(resp.read())

    if not isinstance(events, list):
        events = []

    filtered = []
    for ev in events:
        country = (ev.get("country") or "").upper()
        impact = (ev.get("impact") or "").lower()
        event_date = ev.get("date") or ""
        if country in TARGET_COUNTRIES and impact in TARGET_IMPACTS:
            if len(event_date) >= 10 and date_from <= event_date[:10] <= date_to:
                filtered.append(ev)

    return filtered


def event_datetime(time_str: str, duration_minutes: int) -> tuple[dict, dict]:
    """Return (start, end) dicts for a Google Calendar event."""
    # FMP date can be "2025-01-20 13:30:00" or "2025-01-20"
    if len(time_str) > 10:
        dt = datetime.strptime(time_str, "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc
        )
        start = {"dateTime": dt.isoformat(), "timeZone": CALENDAR_TIMEZONE}
        end_dt = dt + timedelta(minutes=duration_minutes)
        end = {"dateTime": end_dt.isoformat(), "timeZone": CALENDAR_TIMEZONE}
    else:
        # All-day event
        start = {"date": time_str[:10]}
        end = {"date": time_str[:10]}
    return start, end


def build_gcal_event(ev: dict) -> dict:
    """Convert an FMP economic event dict to a Google Calendar event body."""
    country = (ev.get("country") or "").upper()
    flag = COUNTRY_FLAG.get(country, "")
    name = ev.get("event") or "Economic Event"
    impact = (ev.get("impact") or "").lower()
    estimate = ev.get("estimate") if ev.get("estimate") is not None else "N/A"
    previous = ev.get("previous") if ev.get("previous") is not None else "N/A"
    actual = ev.get("actual") if ev.get("actual") is not None else "N/A"
    time_str = ev.get("date") or ""

    start, end = event_datetime(time_str, EVENT_DURATION_MINUTES)

    return {
        "summary": f"{flag} {name}".strip(),
        "description": f"Impact: {impact}\nEstimate: {estimate}\nPrevious: {previous}\nActual: {actual}",
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
                "fmp_id": f"{name}_{time_str}",
            }
        },
    }


def get_existing_events(service, calendar_id: str, date_from: str, date_to: str) -> dict[str, str]:
    """Return a mapping of fmp_id -> Google Calendar event id for the date range."""
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
                privateExtendedProperty="fmp_id",
                singleEvents=True,
                pageToken=page_token,
            )
            .execute()
        )
        for item in result.get("items", []):
            fid = (
                item.get("extendedProperties", {})
                .get("private", {})
                .get("fmp_id")
            )
            if fid:
                mapping[fid] = item["id"]
        page_token = result.get("nextPageToken")
        if not page_token:
            break

    return mapping


def upsert_event(service, calendar_id: str, gcal_event: dict, existing: dict[str, str]) -> str:
    """Create or update a Google Calendar event. Returns 'created' or 'updated'."""
    fid = gcal_event["extendedProperties"]["private"]["fmp_id"]
    if fid in existing:
        service.events().update(
            calendarId=calendar_id,
            eventId=existing[fid],
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

def main():
    fmp_api_key = os.environ["FMP_API_KEY"]
    calendar_id = os.environ["GOOGLE_CALENDAR_ID"]

    today = datetime.now(timezone.utc).date()
    date_from = today.strftime("%Y-%m-%d")
    date_to = (today + timedelta(weeks=FETCH_WEEKS)).strftime("%Y-%m-%d")

    print(f"Fetching economic events from {date_from} to {date_to} ...")
    events = fetch_economic_events(fmp_api_key, date_from, date_to)
    print(f"Found {len(events)} matching events after filtering.")

    if not events:
        print("No events to sync.")
        return

    service = build_calendar_service()
    existing = get_existing_events(service, calendar_id, date_from, date_to)
    print(f"Found {len(existing)} existing FMP-sourced events in Google Calendar.")

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
