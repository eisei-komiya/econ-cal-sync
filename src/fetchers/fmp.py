"""Fetcher for Financial Modeling Prep (FMP) economic calendar.

Requires a paid API key set via the ``FMP_API_KEY`` environment variable.
Endpoint: ``https://financialmodelingprep.com/api/v3/economic_calendar``
"""

from __future__ import annotations

import json
import os
import urllib.request
from datetime import datetime, timezone

from ..models import EconomicEvent
from .base import BaseFetcher

_FMP_BASE = "https://financialmodelingprep.com/api/v3/economic_calendar"

# FMP uses 2-letter country codes; map to currency codes used by sync.py.
_COUNTRY_TO_CCY: dict[str, str] = {
    "US": "USD",
    "JP": "JPY",
    "GB": "GBP",
    "EU": "EUR",
    "CA": "CAD",
    "AU": "AUD",
    "NZ": "NZD",
    "CH": "CHF",
}

_IMPACT_MAP: dict[str, int] = {
    "low": 1,
    "medium": 2,
    "high": 3,
}


class FMPFetcher(BaseFetcher):
    """Fetch economic calendar data from Financial Modeling Prep."""

    @property
    def name(self) -> str:
        return "fmp"

    def fetch(
        self,
        date_from: str,
        date_to: str,
        *,
        countries: set[str],
        importance_min: int,
    ) -> list[EconomicEvent]:
        api_key = os.environ.get("FMP_API_KEY", "")
        if not api_key:
            print("[fmp] FMP_API_KEY not set – skipping.")
            return []

        url = f"{_FMP_BASE}?from={date_from}&to={date_to}"
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "Accept": "application/json",
                    "apikey": api_key,
                },
            )
            with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
                data = json.loads(resp.read())
        except Exception as exc:  # noqa: BLE001
            print(f"[fmp] API request failed: {exc}")
            return []

        if not isinstance(data, list):
            print(f"[fmp] Unexpected response type: {type(data)}")
            return []

        results: list[EconomicEvent] = []
        for raw in data:
            ccy = _COUNTRY_TO_CCY.get((raw.get("country") or "").upper(), "")
            if ccy not in countries:
                continue
            impact = _IMPACT_MAP.get((raw.get("impact") or "").lower(), 0)
            if impact < importance_min:
                continue
            dt = self._parse_date(raw.get("date") or "")
            if dt is None:
                continue
            results.append(self._normalise(raw, dt, ccy))

        return results

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def _parse_date(date_str: str) -> datetime | None:
        if not date_str:
            return None
        try:
            dt = datetime.fromisoformat(date_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _normalise(raw: dict, dt_utc: datetime, ccy: str) -> EconomicEvent:
        event_name = (raw.get("event") or "Economic Event").strip()
        eid = f"fmp_{event_name}_{dt_utc.strftime('%Y%m%dT%H%M%S')}"
        return EconomicEvent(
            id=eid,
            name=event_name,
            country=ccy,
            dt_utc=dt_utc,
            event_date=dt_utc.date() if dt_utc else None,
            forecast=str(raw.get("estimate") or raw.get("consensus") or "N/A"),
            previous=str(raw.get("previous") or "N/A"),
            actual=str(raw.get("actual") or "N/A"),
            currency=ccy,
        )
