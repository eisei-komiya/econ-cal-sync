"""Sync high/medium-impact economic events from Finnhub to Google Calendar."""

import json
import os
from datetime import datetime, timedelta, timezone

import finnhub
from google.oauth2 import service_account
from googleapiclient.discovery import build

# ---------------------------------------------------------------------------
# Configuration constants
# ---------------------------------------------------------------------------
TARGET_COUNTRIES = ["US", "JP"]   # ISO-3166-1 alpha-2 country codes to keep
TARGET_IMPACTS = {"high", "medium"}
FETCH_WEEKS = 4                   # How many weeks ahead to fetch
CALENDAR_TIMEZONE = "Asia/Tokyo"
EVENT_DURATION_MINUTES = 30
REMINDER_MINUTES = [60, 10]

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
    """Fetch economic calendar events from Finnhub and return filtered list."""
    client = finnhub.Client(api_key=api_key)
    data = client.economic_calendar()
    # The SDK doesn't support date range filtering directly, so we filter manually.
    # Fallback: use the raw endpoint if the SDK returns empty data.
    events = data.get("economicCalendar", []) if isinstance(data, dict) else []

    if not events:
        # Try via raw HTTP as a fallback
        import urllib.parse
        import urllib.request
        params = urllib.parse.urlencode({"from": date_from, "to": date_to, "token": api_key})
        url = f"https://finnhub.io/api/v1/calendar/economic?{params}"
        with urllib.request.urlopen(url) as resp:  # noqa: S310
            payload = json.loads(resp.read())
        events = payload.get("economicCalendar", [])

    filtered = []
    for ev in events:
        country = (ev.get("country") or "").upper()
        impact = (ev.get("impact") or "").lower()
        event_date = ev.get("time") or ev.get("date") or ""
        if country in TARGET_COUNTRIES and impact in TARGET_IMPACTS:
            if len(event_date) >= 10 and date_from <= event_date[:10] <= date_to:
                filtered.append(ev)

    return filtered


def event_datetime(time_str: str, duration_minutes: int) -> tuple[dict, dict]:
    """Return (start, end) dicts for a Google Calendar event."""
    # Finnhub time can be "2025-01-20 13:30:00" or "2025-01-20"
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
    """Convert a Finnhub economic event dict to a Google Calendar event body."""
    country = (ev.get("country") or "").upper()
    flag = COUNTRY_FLAG.get(country, "")
    name = ev.get("event") or ev.get("name") or "Economic Event"
    impact = (ev.get("impact") or "").lower()
    forecast = ev.get("forecast") or "N/A"
    previous = ev.get("prev") or ev.get("previous") or "N/A"
    time_str = ev.get("time") or ev.get("date") or ""

    start, end = event_datetime(time_str, EVENT_DURATION_MINUTES)

    return {
        "summary": f"{flag} {name}".strip(),
        "description": f"Impact: {impact}\nForecast: {forecast}\nPrevious: {previous}",
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
                "finnhub_id": str(ev.get("id") or f"{name}_{time_str}"),
            }
        },
    }


def get_existing_events(service, calendar_id: str, date_from: str, date_to: str) -> dict[str, str]:
    """Return a mapping of finnhub_id -> Google Calendar event id for the date range."""
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
                privateExtendedProperty="finnhub_id",
                singleEvents=True,
                pageToken=page_token,
            )
            .execute()
        )
        for item in result.get("items", []):
            fid = (
                item.get("extendedProperties", {})
                .get("private", {})
                .get("finnhub_id")
            )
            if fid:
                mapping[fid] = item["id"]
        page_token = result.get("nextPageToken")
        if not page_token:
            break

    return mapping


def upsert_event(service, calendar_id: str, gcal_event: dict, existing: dict[str, str]) -> str:
    """Create or update a Google Calendar event. Returns 'created' or 'updated'."""
    fid = gcal_event["extendedProperties"]["private"]["finnhub_id"]
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
    finnhub_api_key = os.environ["FINNHUB_API_KEY"]
    calendar_id = os.environ["GOOGLE_CALENDAR_ID"]

    today = datetime.now(timezone.utc).date()
    date_from = today.strftime("%Y-%m-%d")
    date_to = (today + timedelta(weeks=FETCH_WEEKS)).strftime("%Y-%m-%d")

    print(f"Fetching economic events from {date_from} to {date_to} ...")
    events = fetch_economic_events(finnhub_api_key, date_from, date_to)
    print(f"Found {len(events)} matching events after filtering.")

    if not events:
        print("No events to sync.")
        return

    service = build_calendar_service()
    existing = get_existing_events(service, calendar_id, date_from, date_to)
    print(f"Found {len(existing)} existing Finnhub-sourced events in Google Calendar.")

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
