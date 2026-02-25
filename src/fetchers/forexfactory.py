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
    def _fetch_ff_json() -> list[dict]:
        """Download the official FF this-week JSON."""
        try:
            req = urllib.request.Request(
                _FF_JSON_URL, headers={"Accept": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
                data = json.loads(resp.read())
            return data if isinstance(data, list) else []
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

        try:
            raw = scrape_calendar(date_from=date_from, date_to=date_to)
            cleaned = clean_calendar_data(raw)
            df = cleaned.base
            if df is None or df.empty:
                return []
            return df.to_dict(orient="records")  # type: ignore[union-attr]
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
