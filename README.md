# econ-cal-sync

Automated weekly sync of high-impact economic calendar events into
Google Calendar, powered by GitHub Actions.

Data sources are **pluggable** — switch providers by setting a single
environment variable.  The default source
([ForexFactory](https://www.forexfactory.com/)) requires no API key.

---

## Overview

Every Monday morning (07:00 JST) the workflow fetches the next 4 weeks of
economic events for configurable countries (default: `USD`, `JPY`, matching `TARGET_COUNTRIES`) and upserts
them into a Google Calendar using a service account.  Duplicate prevention
is handled via `extendedProperties` so repeated runs are idempotent.

### Supported data sources

| Name                  | Env var `EVENT_SOURCE`  | API key required? |
|-----------------------|-------------------------|-------------------|
| ForexFactory          | `forexfactory` *(default)* | No              |
| Financial Modeling Prep | `fmp`                 | Yes (`FMP_API_KEY`) |

> Adding a new source only requires implementing a small fetcher class in
> `src/fetchers/` — see [Adding a new data source](#adding-a-new-data-source).

---

## Setup

### 1. Google Cloud – Service Account & Calendar API

1. Open the [Google Cloud Console](https://console.cloud.google.com/).
2. Create a new project (or use an existing one).
3. Enable the **Google Calendar API** for the project
   (*APIs & Services → Library → search "Google Calendar API"*).
4. Create a **Service Account**
   (*IAM & Admin → Service Accounts → Create Service Account*).
5. Generate a JSON key for the service account
   (*Keys → Add Key → Create new key → JSON*) and download it.

### 2. Share your Google Calendar with the service account

1. Open [Google Calendar](https://calendar.google.com/) and find the target
   calendar.
2. Go to **Settings → Share with specific people**.
3. Add the service account's email address (ends with
   `@<project>.iam.gserviceaccount.com`) and grant it the **Editor** role.
4. Note the **Calendar ID** shown under *Integrate calendar* (looks like
   `abc123@group.calendar.google.com` or your Gmail address for the primary
   calendar).

### 3. Add GitHub Secrets

In your repository go to **Settings → Secrets and variables → Actions** and
add the following secrets:

| Secret name         | Value                                                    |
|---------------------|----------------------------------------------------------|
| `GOOGLE_SA_JSON`    | The **full contents** of the service account JSON key file |
| `GOOGLE_CALENDAR_ID`| The Calendar ID from step 2                              |

> **Note:** The default data source (ForexFactory) requires no API key.
> If you switch to a source that needs one, add it to Secrets and pass it
> as an environment variable in the workflow.

---

## Switching the data source

Set the `EVENT_SOURCE` environment variable in
`.github/workflows/sync.yml`:

```yaml
env:
  EVENT_SOURCE: forexfactory   # change to another registered name
```

---

## Manual trigger

Go to **Actions → Sync Economic Calendar → Run workflow** to trigger a run
immediately without waiting for the weekly schedule.

---

## Customisation

Open `src/sync.py` and edit the constants near the top of the file:

```python
# Currency codes to include (ForexFactory uses currency codes as country identifiers)
TARGET_COUNTRIES = {"USD", "JPY"}

# Minimum importance level (1=Low, 2=Medium, 3=High)
IMPORTANCE_MIN = 3

# How many weeks ahead to fetch
FETCH_WEEKS = 4
```

Add a matching entry to `COUNTRY_FLAG` if you add a new country so that the
flag emoji appears in the event title.

---

## Adding a new data source

1. Create `src/fetchers/my_source.py` with a class that extends `BaseFetcher`.
2. Implement the `name` property and the `fetch()` method — return a list of
   `EconomicEvent` (defined in `src/models.py`).
3. Register it in `src/fetchers/__init__.py`:
   ```python
   from .my_source import MySourceFetcher
   _FETCHERS["my_source"] = MySourceFetcher
   ```
4. Set `EVENT_SOURCE=my_source` in the workflow.

---

## Project structure

```
src/
├── __init__.py
├── __main__.py          # python -m src entry point
├── sync.py              # main sync logic (source-agnostic)
├── models.py            # EconomicEvent dataclass
└── fetchers/
    ├── __init__.py      # fetcher registry & get_fetcher()
    ├── base.py          # BaseFetcher ABC
    ├── forexfactory.py
    └── fmp.py
```
