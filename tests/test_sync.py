"""Simple tests for the ForexFactory fetcher and sync helpers.

Run with:  python -m pytest tests/ -v
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from src.fetchers.forexfactory import ForexFactoryFetcher
from src.models import EconomicEvent
from src.sync import (
    _EXT_PROP_KEY,
    _IMPORTANCE_COLOR,
    _IMPORTANCE_STARS,
    build_gcal_event,
)

# -----------------------------------------------------------------------
# Fixtures / sample data
# -----------------------------------------------------------------------

SAMPLE_FF_JSON = [
    {
        "title": "Non Farm Payrolls",
        "country": "USD",
        "date": "2026-02-20T08:30:00-05:00",
        "impact": "High",
        "forecast": "180K",
        "previous": "256K",
    },
    {
        "title": "CPI y/y",
        "country": "JPY",
        "date": "2026-02-19T18:30:00-05:00",
        "impact": "High",
        "forecast": "2.0%",
        "previous": "2.4%",
    },
    {
        "title": "Retail Sales m/m",
        "country": "GBP",
        "date": "2026-02-20T02:00:00-05:00",
        "impact": "High",
        "forecast": "0.2%",
        "previous": "0.4%",
    },
    {
        "title": "Empire State Manufacturing Index",
        "country": "USD",
        "date": "2026-02-17T08:30:00-05:00",
        "impact": "Medium",
        "forecast": "6.4",
        "previous": "7.7",
    },
    {
        "title": "Bank Holiday",
        "country": "USD",
        "date": "2026-02-16T08:00:00-05:00",
        "impact": "Holiday",
        "forecast": "",
        "previous": "",
    },
]


@pytest.fixture
def fetcher() -> ForexFactoryFetcher:
    return ForexFactoryFetcher()


# -----------------------------------------------------------------------
# ForexFactoryFetcher tests
# -----------------------------------------------------------------------


class TestForexFactoryFetcher:
    """Tests for ForexFactory fetcher normalisation and filtering."""

    def test_filters_by_country(self, fetcher: ForexFactoryFetcher) -> None:
        """Only events matching target countries should be returned."""
        with patch.object(fetcher, "_fetch_ff_json", return_value=SAMPLE_FF_JSON), \
             patch.object(fetcher, "_fetch_mct", return_value=[]):
            events = fetcher.fetch(
                "2026-02-15", "2026-02-21",
                countries={"USD", "JPY"},
                importance_min=3,
            )
        countries = {ev.country for ev in events}
        assert countries <= {"USD", "JPY"}
        assert "GBP" not in countries

    def test_filters_by_importance(self, fetcher: ForexFactoryFetcher) -> None:
        """Only High impact (>=3) events should pass when importance_min=3."""
        with patch.object(fetcher, "_fetch_ff_json", return_value=SAMPLE_FF_JSON), \
             patch.object(fetcher, "_fetch_mct", return_value=[]):
            events = fetcher.fetch(
                "2026-02-15", "2026-02-21",
                countries={"USD", "JPY"},
                importance_min=3,
            )
        # "Empire State Manufacturing Index" is Medium → excluded
        names = {ev.name for ev in events}
        assert "Non Farm Payrolls" in names
        assert "Empire State Manufacturing Index" not in names
        assert "Bank Holiday" not in names

    def test_filters_by_date_range(self, fetcher: ForexFactoryFetcher) -> None:
        """Events outside the requested date range should be excluded."""
        with patch.object(fetcher, "_fetch_ff_json", return_value=SAMPLE_FF_JSON), \
             patch.object(fetcher, "_fetch_mct", return_value=[]):
            events = fetcher.fetch(
                "2026-02-20", "2026-02-20",
                countries={"USD"},
                importance_min=3,
            )
        # Only NFP on Feb 20 should match
        assert len(events) == 1
        assert events[0].name == "Non Farm Payrolls"

    def test_normalises_to_utc(self, fetcher: ForexFactoryFetcher) -> None:
        """Dates should be converted to UTC."""
        with patch.object(fetcher, "_fetch_ff_json", return_value=SAMPLE_FF_JSON), \
             patch.object(fetcher, "_fetch_mct", return_value=[]):
            events = fetcher.fetch(
                "2026-02-15", "2026-02-21",
                countries={"USD"},
                importance_min=3,
            )
        nfp = [ev for ev in events if ev.name == "Non Farm Payrolls"][0]
        assert nfp.dt_utc is not None
        assert nfp.dt_utc.tzinfo == timezone.utc
        # 08:30 EST (-05:00) → 13:30 UTC
        assert nfp.dt_utc.hour == 13
        assert nfp.dt_utc.minute == 30

    def test_deduplicates(self, fetcher: ForexFactoryFetcher) -> None:
        """Duplicate events from FF JSON and MCT should be merged."""
        with patch.object(fetcher, "_fetch_ff_json", return_value=SAMPLE_FF_JSON), \
             patch.object(fetcher, "_fetch_mct", return_value=SAMPLE_FF_JSON):
            events = fetcher.fetch(
                "2026-02-15", "2026-02-21",
                countries={"USD", "JPY"},
                importance_min=3,
            )
        ids = [ev.id for ev in events]
        assert len(ids) == len(set(ids)), "Duplicate IDs found"

    def test_empty_response(self, fetcher: ForexFactoryFetcher) -> None:
        """Empty API response should return empty list."""
        with patch.object(fetcher, "_fetch_ff_json", return_value=[]), \
             patch.object(fetcher, "_fetch_mct", return_value=[]):
            events = fetcher.fetch(
                "2026-02-15", "2026-02-21",
                countries={"USD"},
                importance_min=3,
            )
        assert events == []

    def test_mct_import_error_graceful(self, fetcher: ForexFactoryFetcher) -> None:
        """If market-calendar-tool is not installed, fetch should still work."""
        with patch.object(fetcher, "_fetch_ff_json", return_value=SAMPLE_FF_JSON), \
             patch.dict("sys.modules", {"market_calendar_tool": None}):
            events = fetcher.fetch(
                "2026-02-15", "2026-02-21",
                countries={"USD", "JPY"},
                importance_min=3,
            )
        assert len(events) > 0


# -----------------------------------------------------------------------
# build_gcal_event tests
# -----------------------------------------------------------------------


class TestBuildGcalEvent:
    """Tests for Google Calendar event body construction."""

    def test_summary_has_flag(self) -> None:
        ev = EconomicEvent(
            id="test_1",
            name="Non Farm Payrolls",
            country="USD",
            dt_utc=datetime(2026, 2, 20, 13, 30, tzinfo=timezone.utc),
            event_date=date(2026, 2, 20),
            forecast="180K",
            previous="256K",
            actual="N/A",
        )
        gcal = build_gcal_event(ev)
        assert "🇺🇸" in gcal["summary"]
        assert "Non Farm Payrolls" in gcal["summary"]

    def test_extended_property_key(self) -> None:
        ev = EconomicEvent(
            id="test_id_123",
            name="CPI",
            country="JPY",
            dt_utc=datetime(2026, 2, 19, 23, 30, tzinfo=timezone.utc),
            event_date=date(2026, 2, 19),
            forecast="2.0%",
            previous="2.4%",
            actual="N/A",
        )
        gcal = build_gcal_event(ev)
        assert gcal["extendedProperties"]["private"][_EXT_PROP_KEY] == "test_id_123"

    def test_allday_when_no_time(self) -> None:
        ev = EconomicEvent(
            id="test_allday",
            name="Bank Holiday",
            country="USD",
            dt_utc=None,
            event_date=date(2026, 2, 16),
            forecast="N/A",
            previous="N/A",
            actual="N/A",
        )
        gcal = build_gcal_event(ev)
        assert "date" in gcal["start"]
        assert "dateTime" not in gcal["start"]
        assert gcal["start"]["date"] == "2026-02-16"
        assert gcal["end"]["date"] == "2026-02-17"

    def test_high_impact_has_stars_and_color(self) -> None:
        ev = EconomicEvent(
            id="test_high",
            name="Non Farm Payrolls",
            country="USD",
            dt_utc=datetime(2026, 2, 20, 13, 30, tzinfo=timezone.utc),
            event_date=date(2026, 2, 20),
            forecast="180K",
            previous="256K",
            actual="N/A",
            importance=3,
        )
        gcal = build_gcal_event(ev)
        assert _IMPORTANCE_STARS[3] in gcal["summary"]
        assert gcal.get("colorId") == _IMPORTANCE_COLOR[3]

    def test_medium_impact_has_stars_and_color(self) -> None:
        ev = EconomicEvent(
            id="test_medium",
            name="Empire State Manufacturing Index",
            country="USD",
            dt_utc=datetime(2026, 2, 17, 13, 30, tzinfo=timezone.utc),
            event_date=date(2026, 2, 17),
            forecast="6.4",
            previous="7.7",
            actual="N/A",
            importance=2,
        )
        gcal = build_gcal_event(ev)
        assert _IMPORTANCE_STARS[2] in gcal["summary"]
        assert gcal.get("colorId") == _IMPORTANCE_COLOR[2]

    def test_low_impact_has_no_stars_no_color(self) -> None:
        ev = EconomicEvent(
            id="test_low",
            name="Some Minor Event",
            country="USD",
            dt_utc=datetime(2026, 2, 17, 13, 30, tzinfo=timezone.utc),
            event_date=date(2026, 2, 17),
            forecast="N/A",
            previous="N/A",
            actual="N/A",
            importance=1,
        )
        gcal = build_gcal_event(ev)
        assert "★" not in gcal["summary"]
        assert "colorId" not in gcal


class TestForexFactoryFetcherImportance:
    """Tests that ForexFactory fetcher stores importance level in EconomicEvent."""

    def test_high_importance_stored(self, fetcher: ForexFactoryFetcher) -> None:
        with patch.object(fetcher, "_fetch_ff_json", return_value=SAMPLE_FF_JSON), \
             patch.object(fetcher, "_fetch_mct", return_value=[]):
            events = fetcher.fetch(
                "2026-02-15", "2026-02-21",
                countries={"USD"},
                importance_min=3,
            )
        nfp = [ev for ev in events if ev.name == "Non Farm Payrolls"][0]
        assert nfp.importance == 3

    def test_medium_importance_stored(self, fetcher: ForexFactoryFetcher) -> None:
        with patch.object(fetcher, "_fetch_ff_json", return_value=SAMPLE_FF_JSON), \
             patch.object(fetcher, "_fetch_mct", return_value=[]):
            events = fetcher.fetch(
                "2026-02-15", "2026-02-21",
                countries={"USD"},
                importance_min=2,
            )
        empire = [ev for ev in events if ev.name == "Empire State Manufacturing Index"][0]
        assert empire.importance == 2

    def test_medium_included_when_importance_min_2(self, fetcher: ForexFactoryFetcher) -> None:
        with patch.object(fetcher, "_fetch_ff_json", return_value=SAMPLE_FF_JSON), \
             patch.object(fetcher, "_fetch_mct", return_value=[]):
            events = fetcher.fetch(
                "2026-02-15", "2026-02-21",
                countries={"USD"},
                importance_min=2,
            )
        names = {ev.name for ev in events}
        assert "Empire State Manufacturing Index" in names
        assert "Non Farm Payrolls" in names
