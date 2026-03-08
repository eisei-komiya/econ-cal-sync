"""Fetcher for Financial Modeling Prep (FMP) economic calendar.

Requires a paid API key set via the ``FMP_API_KEY`` environment variable.
Endpoint: ``https://financialmodelingprep.com/api/v3/economic_calendar``
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import replace
from datetime import datetime, timezone

from ..models import EconomicEvent
from .base import BaseFetcher

_FMP_BASE = "https://financialmodelingprep.com/api/v3/economic_calendar"

# HTTP status codes worth retrying (rate-limit and transient server errors)
_RETRYABLE_STATUS_CODES: frozenset[int] = frozenset({429, 500, 502, 503, 504})
_MAX_RETRIES: int = 3
_BACKOFF_BASE: float = 2.0

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

        url = f"{_FMP_BASE}?from={date_from}&to={date_to}&apikey={api_key}"
        req = urllib.request.Request(url, headers={"Accept": "application/json"})

        data = self._fetch_with_retry(req)
        if data is None:
            return []

        if not isinstance(data, list):
            print(f"[fmp] Unexpected response type: {type(data)}")
            return []

        results: list[EconomicEvent] = []
        seen_ids: dict[str, int] = {}
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
            event = self._normalise(raw, dt, ccy)
            base_id = event.id
            if base_id in seen_ids:
                seen_ids[base_id] += 1
                # Replace the id field with a suffixed version to avoid collision.
                event = replace(event, id=f"{base_id}_{seen_ids[base_id]}")
            else:
                seen_ids[base_id] = 0
            results.append(event)

        return results

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    def _fetch_with_retry(self, req: urllib.request.Request) -> list | None:
        """Execute an HTTP request with exponential back-off on 429/5xx errors.

        Returns the parsed JSON list on success, or ``None`` on failure.
        """
        for attempt in range(_MAX_RETRIES + 1):
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
                    return json.loads(resp.read())
            except urllib.error.HTTPError as exc:
                if exc.code not in _RETRYABLE_STATUS_CODES or attempt == _MAX_RETRIES:
                    print(f"[fmp] API request failed: HTTP {exc.code}")
                    return None
                wait = _BACKOFF_BASE ** attempt
                print(f"  [fmp retry] HTTP {exc.code}, waiting {wait:.0f}s (attempt {attempt + 1}/{_MAX_RETRIES})")
                time.sleep(wait)
            except Exception as exc:  # noqa: BLE001
                print(f"[fmp] API request failed: {exc}")
                return None
        return None  # unreachable but satisfies type checker

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
        eid = f"fmp_{ccy}_{event_name}_{dt_utc.strftime('%Y%m%dT%H%M%S')}"
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
