"""Normalised economic event model shared across all fetchers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime


@dataclass(frozen=True, slots=True)
class EconomicEvent:
    """Source-agnostic representation of a single economic calendar event.

    Every fetcher must map its raw API response into this shape so that the
    sync logic never needs to know which data source was used.
    """

    id: str                          # Unique identifier (source-specific)
    name: str                        # e.g. "Non Farm Payrolls"
    country: str                     # Currency code (e.g. "USD", "JPY")
    dt_utc: datetime | None          # Scheduled time in UTC (None → all-day)
    event_date: date | None          # Date of the event (used for all-day events)
    forecast: str                    # Consensus forecast value, or "N/A"
    previous: str                    # Previous period value, or "N/A"
    actual: str                      # Actual (released) value, or "N/A"

    # Optional extra fields – fetchers may leave them as empty strings.
    source_url: str = ""             # Link to the official source
    category: str = ""               # e.g. "Interest Rate"
    currency: str = ""
    unit: str = ""
