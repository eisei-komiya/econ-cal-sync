"""Fetcher for ForexFactory economic calendar.

Uses two data sources in combination:
  1. **FF JSON** (``nfs.faireconomy.media/ff_calendar_thisweek.json``)
     – free, no auth, covers the current week only.
  2. **market-calendar-tool** (PyPI package) – scrapes ForexFactory for
     arbitrary date ranges; used as a fallback for weeks beyond "this week".
"""

from __future__ import annotations

import json
import urllib.request
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from datetime import datetime, timezone

from ..models import EconomicEvent
from .base import BaseFetcher

_FF_JSON_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

# FF impact strings → numeric levels
_IMPACT_MAP: dict[str, int] = {
    "holiday": 0,
    "low": 1,
    "medium": 2,
    "high": 3,
}

# Required keys expected in each FF JSON event dict.
_FF_JSON_REQUIRED_KEYS: frozenset[str] = frozenset({"title", "date", "country", "impact"})

# If this fraction or more of sampled events are missing required keys, emit a warning.
_FF_JSON_SCHEMA_WARN_THRESHOLD: float = 0.5

# Timeout (seconds) for the market-calendar-tool scrape call.
_MCT_SCRAPE_TIMEOUT: int = 60


class ForexFactoryFetcher(BaseFetcher):
    """Fetch economic calendar data from ForexFactory."""

    @property
    def name(self) -> str:
        return "forexfactory"

    # ------------------------------------------------------------------ #
    # Public
    # ------------------------------------------------------------------ #

    def fetch(
        self,
        date_from: str,
        date_to: str,
        *,
        countries: set[str],
        importance_min: int,
    ) -> list[EconomicEvent]:
        # --- 1) FF JSON (this week) ------------------------------------
        raw_events = self._fetch_ff_json()

        # --- 2) market-calendar-tool (remaining weeks) -----------------
        mct_events = self._fetch_mct(date_from, date_to)
        raw_events.extend(mct_events)

        # --- 3) de-duplicate by synthetic id ---------------------------
        seen: set[str] = set()
        unique: list[dict] = []
        for ev in raw_events:
            eid = self._make_id(ev)
            if eid not in seen:
                seen.add(eid)
                unique.append(ev)

        # --- 4) filter & normalise -------------------------------------
        results: list[EconomicEvent] = []
        for ev in unique:
            ccy = (ev.get("country") or "").upper()
            if ccy not in countries:
                continue
            impact = _IMPACT_MAP.get((ev.get("impact") or "").lower(), 0)
            if impact < importance_min:
                continue
            dt = self._parse_date(ev.get("date") or "")
            if dt is None:
                continue
            date_str = dt.strftime("%Y-%m-%d")
            if not (date_from <= date_str <= date_to):
                continue
            results.append(self._normalise(ev, dt))

        return results

    # ------------------------------------------------------------------ #
    # Data sources
    # ------------------------------------------------------------------ #

    @staticmethod
    def _validate_ff_json_schema(data: list[dict], *, sample_size: int = 5) -> None:
        """Warn if the FF JSON schema appears to have changed.

        Samples the first *sample_size* events and checks whether each contains
        all required keys (``title``, ``date``, ``country``, ``impact``).
        If at least :data:`_FF_JSON_SCHEMA_WARN_THRESHOLD` of the sample is
        missing one or more keys, a warning is printed so that operators are
        alerted to a potential upstream schema change before events are
        silently dropped.

        This is a best-effort check and does **not** raise; callers must not
        rely on it for control flow.
        """
        if not data:
            return
        sample = [ev for ev in data[:sample_size] if isinstance(ev, dict)]
        if not sample:
            print(
                "[forexfactory] Warning: FF JSON contains non-dict entries; "
                "schema may have changed."
            )
            return
        missing_counts = sum(
            1 for ev in sample if not _FF_JSON_REQUIRED_KEYS.issubset(ev.keys())
        )
        if missing_counts / len(sample) >= _FF_JSON_SCHEMA_WARN_THRESHOLD:
            # Collect the union of all missing keys across the sample for diagnostics.
            missing_keys: set[str] = set()
            for ev in sample:
                missing_keys |= _FF_JSON_REQUIRED_KEYS - ev.keys()
            print(
                f"[forexfactory] Warning: FF JSON schema may have changed. "
                f"Missing keys in {missing_counts}/{len(sample)} sampled events: "
                f"{sorted(missing_keys)}"
            )

    @staticmethod
    def _fetch_ff_json() -> list[dict]:
        """Download the official FF this-week JSON."""
        try:
            req = urllib.request.Request(
                _FF_JSON_URL,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "Mozilla/5.0 (compatible; econ-cal-sync/0.1)",
                },
            )
            with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
                data = json.loads(resp.read())
            if not isinstance(data, list):
                return []
            ForexFactoryFetcher._validate_ff_json_schema(data)
            return data
        except Exception as exc:  # noqa: BLE001
            print(f"[forexfactory] FF JSON fetch failed: {exc}")
            return []

    @staticmethod
    def _fetch_mct(date_from: str, date_to: str) -> list[dict]:
        """Scrape ForexFactory via market-calendar-tool (optional dep)."""
        try:
            from market_calendar_tool import scrape_calendar, clean_calendar_data
        except ImportError:
            print(
                "[forexfactory] market-calendar-tool not installed; "
                "only this-week data available."
            )
            return []

        def _scrape() -> list[dict]:
            raw = scrape_calendar(date_from=date_from, date_to=date_to)
            cleaned = clean_calendar_data(raw)
            df = cleaned.base
            if df is None or df.empty:
                return []
            return df.to_dict(orient="records")  # type: ignore[union-attr]

        try:
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(_scrape)
                return future.result(timeout=_MCT_SCRAPE_TIMEOUT)
        except FuturesTimeoutError:
            print(
                f"[forexfactory] market-calendar-tool scrape timed out "
                f"after {_MCT_SCRAPE_TIMEOUT}s; skipping."
            )
            return []
        except Exception as exc:  # noqa: BLE001
            print(f"[forexfactory] market-calendar-tool scrape failed: {exc}")
            return []

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def _parse_date(date_str: str) -> datetime | None:
        """Parse FF ISO-8601 date (e.g. ``2026-02-20T08:30:00-05:00``).

        Handles:
        - Aware datetimes with offsets (unchanged behavior).
        - Naive datetimes (no offset), which are interpreted as UTC.
        - Date-only strings (``YYYY-MM-DD``), treated as all-day events
          starting at 00:00 UTC.
        """
        if not date_str:
            return None
        if isinstance(date_str, str):
            date_str = date_str.strip()
        try:
            dt = datetime.fromisoformat(date_str)
        except (ValueError, TypeError):
            # Support date-only strings like "2026-02-20" by treating them
            # as all-day events starting at midnight UTC.
            if isinstance(date_str, str) and len(date_str) == 10:
                try:
                    dt = datetime.fromisoformat(f"{date_str}T00:00:00")
                except ValueError:
                    return None
            else:
                return None
        if dt.tzinfo is None:
            # Interpret naive datetimes as UTC so astimezone() does not fail.
            dt = dt.replace(tzinfo=timezone.utc)
        try:
            return dt.astimezone(timezone.utc)
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _make_id(ev: dict) -> str:
        """Synthesise a stable dedup key from title + date + country (+ source ID if available)."""
        title = (ev.get("title") or ev.get("event") or "").strip()
        date = (ev.get("date") or "").strip()
        country = (ev.get("country") or "").strip().upper()
        # If the upstream source provides a stable identifier for the event, prefer to include it.
        source_id = (ev.get("id") or ev.get("event_id") or "").strip()
        parts: list[str] = [p for p in (title, date, country, source_id) if p]
        return "_".join(parts)

    @classmethod
    def _normalise(cls, raw: dict, dt_utc: datetime) -> EconomicEvent:
        title = (raw.get("title") or raw.get("event") or "Economic Event").strip()
        importance = _IMPACT_MAP.get((raw.get("impact") or "").lower(), 0)
        return EconomicEvent(
            id=cls._make_id(raw),
            name=title,
            country=(raw.get("country") or "").upper(),
            dt_utc=dt_utc,
            event_date=dt_utc.date() if dt_utc else None,
            forecast=raw.get("forecast") or "N/A",
            previous=raw.get("previous") or "N/A",
            actual=raw.get("actual") or "N/A",
            importance=importance,
        )
