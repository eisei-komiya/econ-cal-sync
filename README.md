# econ-cal-sync

Automated weekly sync of high/medium-impact economic calendar events from
[Finnhub](https://finnhub.io/) into Google Calendar, powered by GitHub Actions.

---

## Overview

Every Monday morning (07:00 JST) the workflow fetches the next 4 weeks of
economic events for configurable countries (default: USD / JPY) from the
Finnhub Economic Calendar API and upserts them into a Google Calendar using
a service account.  Duplicate prevention is handled via `extendedProperties`
so repeated runs are idempotent.

---

## Setup

### 1. Finnhub API key

1. Sign up at <https://finnhub.io/> (free tier is sufficient).
2. Copy your API key from the dashboard.

### 2. Google Cloud – Service Account & Calendar API

1. Open the [Google Cloud Console](https://console.cloud.google.com/).
2. Create a new project (or use an existing one).
3. Enable the **Google Calendar API** for the project
   (*APIs & Services → Library → search "Google Calendar API"*).
4. Create a **Service Account**
   (*IAM & Admin → Service Accounts → Create Service Account*).
5. Generate a JSON key for the service account
   (*Keys → Add Key → Create new key → JSON*) and download it.

### 3. Share your Google Calendar with the service account

1. Open [Google Calendar](https://calendar.google.com/) and find the target
   calendar.
2. Go to **Settings → Share with specific people**.
3. Add the service account's email address (ends with
   `@<project>.iam.gserviceaccount.com`) and grant it the **Editor** role.
4. Note the **Calendar ID** shown under *Integrate calendar* (looks like
   `abc123@group.calendar.google.com` or your Gmail address for the primary
   calendar).

### 4. Add GitHub Secrets

In your repository go to **Settings → Secrets and variables → Actions** and
add the following secrets:

| Secret name         | Value                                                    |
|---------------------|----------------------------------------------------------|
| `FINNHUB_API_KEY`   | Your Finnhub API key                                     |
| `GOOGLE_SA_JSON`    | The **full contents** of the service account JSON key file |
| `GOOGLE_CALENDAR_ID`| The Calendar ID from step 3                              |

---

## Manual trigger

Go to **Actions → Sync Economic Calendar → Run workflow** to trigger a run
immediately without waiting for the weekly schedule.

---

## Customisation

Open `src/sync.py` and edit the constants near the top of the file:

```python
# ISO-3166-1 alpha-2 country codes to include
TARGET_COUNTRIES = ["US", "JP"]

# Impact levels to include ("high", "medium", "low")
TARGET_IMPACTS = {"high", "medium"}

# How many weeks ahead to fetch
FETCH_WEEKS = 4
```

Add a matching entry to `COUNTRY_FLAG` if you add a new country so that the
flag emoji appears in the event title.
