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


class TestReminders:
    """Tests that Google Calendar events include popup reminders and optional attendee."""

    def _make_ev(self) -> "EconomicEvent":
        return EconomicEvent(
            id="test_reminder",
            name="Non Farm Payrolls",
            country="USD",
            dt_utc=datetime(2026, 2, 20, 13, 30, tzinfo=timezone.utc),
            event_date=date(2026, 2, 20),
            forecast="180K",
            previous="256K",
            actual="N/A",
            importance=3,
        )

    def test_reminders_not_default(self) -> None:
        from src.sync import build_gcal_event
        gcal = build_gcal_event(self._make_ev())
        assert gcal["reminders"]["useDefault"] is False

    def test_popup_reminders_present(self) -> None:
        from src.sync import build_gcal_event, REMINDER_MINUTES
        gcal = build_gcal_event(self._make_ev())
        overrides = gcal["reminders"]["overrides"]
        popup_minutes = {r["minutes"] for r in overrides if r["method"] == "popup"}
        assert popup_minutes == set(REMINDER_MINUTES)

    def test_no_attendees_without_owner_email(self) -> None:
        """Without owner_email, attendees should not be set."""
        from src.sync import build_gcal_event
        gcal = build_gcal_event(self._make_ev())
        assert "attendees" not in gcal

    def test_attendee_added_with_owner_email(self) -> None:
        """With owner_email, owner should appear as an accepted attendee."""
        from src.sync import build_gcal_event
        gcal = build_gcal_event(self._make_ev(), owner_email="owner@example.com")
        assert "attendees" in gcal
        assert gcal["attendees"][0]["email"] == "owner@example.com"
        assert gcal["attendees"][0]["responseStatus"] == "accepted"

    def test_reminders_still_present_with_owner_email(self) -> None:
        """Popup reminders must be set even when owner_email is provided."""
        from src.sync import build_gcal_event, REMINDER_MINUTES
        gcal = build_gcal_event(self._make_ev(), owner_email="owner@example.com")
        assert gcal["reminders"]["useDefault"] is False
        popup_minutes = {
            r["minutes"] for r in gcal["reminders"]["overrides"] if r["method"] == "popup"
        }
        assert popup_minutes == set(REMINDER_MINUTES)


class TestUpsertEvent:
    """Tests for upsert_event error handling."""

    def _make_gcal_event(self, eid: str = "test_event_id") -> dict:
        return {
            "summary": "Test Event",
            "extendedProperties": {"private": {"econ_event_id": eid}},
        }

    def test_upsert_creates_new_event(self) -> None:
        """When the event id is not in existing, insert is called."""
        from src.sync import upsert_event
        service = MagicMock()

        result = upsert_event(service, "cal_id", self._make_gcal_event(), existing={})

        assert result == "created"
        service.events().insert.assert_called_once_with(
            calendarId="cal_id",
            body=self._make_gcal_event(),
        )

    def test_upsert_updates_existing_event(self) -> None:
        """When the event id is in existing, update is called."""
        from src.sync import upsert_event
        service = MagicMock()

        result = upsert_event(
            service, "cal_id", self._make_gcal_event(), existing={"test_event_id": "gcal_123"}
        )

        assert result == "updated"
        service.events().update.assert_called_once_with(
            calendarId="cal_id",
            eventId="gcal_123",
            body=self._make_gcal_event(),
        )

    def test_upsert_returns_failed_on_api_error(self) -> None:
        """When the API call raises an exception, 'failed' is returned (not re-raised)."""
        from src.sync import upsert_event
        service = MagicMock()
        service.events().insert().execute.side_effect = Exception("API quota exceeded")

        result = upsert_event(service, "cal_id", self._make_gcal_event(), existing={})

        assert result == "failed"

    def test_upsert_update_returns_failed_on_api_error(self) -> None:
        """When update raises, 'failed' is returned without re-raising."""
        from src.sync import upsert_event
        service = MagicMock()
        service.events().update().execute.side_effect = RuntimeError("503 Service Unavailable")

        result = upsert_event(
            service, "cal_id", self._make_gcal_event(), existing={"test_event_id": "gcal_123"}
        )

        assert result == "failed"


class TestGetExistingEvents:
    """Tests for get_existing_events error handling and pagination."""

    _EXT_KEY = "econ_event_id"

    def _make_item(self, eid: str, gcal_id: str) -> dict:
        return {
            "id": gcal_id,
            "extendedProperties": {"private": {self._EXT_KEY: eid}},
        }

    def _make_service(self, pages: list[list[dict]], error_on_page: int | None = None) -> MagicMock:
        """Build a mock service that returns paginated results.

        If error_on_page is set, the call for that page index (0-based) raises an exception.
        """
        service = MagicMock()
        responses = []
        for i, items in enumerate(pages):
            if error_on_page is not None and i == error_on_page:
                responses.append(Exception(f"API error on page {i}"))
            else:
                next_token = f"token_{i+1}" if i < len(pages) - 1 else None
                resp = {"items": items}
                if next_token:
                    resp["nextPageToken"] = next_token
                responses.append(resp)

        call_count = {"n": 0}
        original_execute = service.events().list().execute

        def side_effect_execute(*args, **kwargs):
            n = call_count["n"]
            call_count["n"] += 1
            r = responses[n]
            if isinstance(r, Exception):
                raise r
            return r

        service.events().list().execute.side_effect = side_effect_execute
        return service

    def test_single_page_returns_mapping(self) -> None:
        """Single page with two events → both are in the mapping."""
        from src.sync import get_existing_events

        items = [self._make_item("ev1", "gcal1"), self._make_item("ev2", "gcal2")]
        service = self._make_service([items])

        result = get_existing_events(service, "cal_id", "2026-01-01", "2026-01-31")

        assert result == {"ev1": "gcal1", "ev2": "gcal2"}

    def test_multiple_pages_returns_all_events(self) -> None:
        """Multi-page response → events from all pages are collected."""
        from src.sync import get_existing_events

        page1 = [self._make_item("ev1", "gcal1")]
        page2 = [self._make_item("ev2", "gcal2")]
        service = self._make_service([page1, page2])

        result = get_existing_events(service, "cal_id", "2026-01-01", "2026-01-31")

        assert result == {"ev1": "gcal1", "ev2": "gcal2"}

    def test_api_error_on_first_page_returns_empty(self) -> None:
        """API error on the first page → returns empty dict without raising."""
        from src.sync import get_existing_events

        service = self._make_service([[], []], error_on_page=0)

        result = get_existing_events(service, "cal_id", "2026-01-01", "2026-01-31")

        assert result == {}

    def test_api_error_on_second_page_returns_partial(self) -> None:
        """API error mid-pagination → returns whatever was collected before the error."""
        from src.sync import get_existing_events

        page1 = [self._make_item("ev1", "gcal1")]
        service = self._make_service([page1, []], error_on_page=1)

        result = get_existing_events(service, "cal_id", "2026-01-01", "2026-01-31")

        assert result == {"ev1": "gcal1"}

    def test_items_without_ext_prop_are_ignored(self) -> None:
        """Events missing the extendedProperties key are silently skipped."""
        from src.sync import get_existing_events

        items = [
            {"id": "gcal1"},  # no extendedProperties at all
            self._make_item("ev2", "gcal2"),
        ]
        service = self._make_service([items])

        result = get_existing_events(service, "cal_id", "2026-01-01", "2026-01-31")

        assert result == {"ev2": "gcal2"}


class TestValidateFFJsonSchema:
    """Tests for ForexFactoryFetcher._validate_ff_json_schema."""

    def test_no_warning_when_data_is_empty(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Empty list → no output."""
        ForexFactoryFetcher._validate_ff_json_schema([])
        assert capsys.readouterr().out == ""

    def test_no_warning_when_all_required_keys_present(self, capsys: pytest.CaptureFixture[str]) -> None:
        """All events have required keys → no warning."""
        data = [
            {"title": "CPI", "date": "2026-01-01", "country": "USD", "impact": "High"},
            {"title": "NFP", "date": "2026-01-02", "country": "USD", "impact": "High"},
        ]
        ForexFactoryFetcher._validate_ff_json_schema(data)
        assert capsys.readouterr().out == ""

    def test_warning_when_all_events_missing_keys(self, capsys: pytest.CaptureFixture[str]) -> None:
        """All sampled events missing required keys → warning is printed."""
        data = [{"foo": "bar"}, {"baz": "qux"}]
        ForexFactoryFetcher._validate_ff_json_schema(data)
        out = capsys.readouterr().out
        assert "Warning" in out
        assert "FF JSON schema may have changed" in out

    def test_warning_includes_missing_key_names(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Warning message lists the specific missing key names."""
        data = [{"foo": "bar"}]  # all required keys absent
        ForexFactoryFetcher._validate_ff_json_schema(data)
        out = capsys.readouterr().out
        for key in ("title", "date", "country", "impact"):
            assert key in out

    def test_no_warning_below_threshold(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Only 1 of 4 events missing keys → below 50% threshold, no warning."""
        valid = {"title": "CPI", "date": "2026-01-01", "country": "USD", "impact": "High"}
        data = [valid, valid, valid, {"foo": "bar"}]
        ForexFactoryFetcher._validate_ff_json_schema(data)
        assert capsys.readouterr().out == ""

    def test_warning_exactly_at_threshold(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Exactly 50% missing → warning is emitted (>= threshold)."""
        valid = {"title": "CPI", "date": "2026-01-01", "country": "USD", "impact": "High"}
        data = [valid, {"foo": "bar"}]  # 1/2 = 50%
        ForexFactoryFetcher._validate_ff_json_schema(data)
        assert "Warning" in capsys.readouterr().out

    def test_non_dict_entries_emit_different_warning(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Non-dict entries (e.g. strings) in data → schema-change warning."""
        data = ["not a dict", 42, None]  # type: ignore[list-item]
        ForexFactoryFetcher._validate_ff_json_schema(data)
        assert "Warning" in capsys.readouterr().out

    def test_fetch_ff_json_calls_validate_on_success(self) -> None:
        """_fetch_ff_json() calls _validate_ff_json_schema() when data is a list."""
        data = [{"title": "CPI", "date": "2026-01-01", "country": "USD", "impact": "High"}]
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(data).encode()
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)

        with (
            patch("urllib.request.urlopen", return_value=mock_response),
            patch.object(ForexFactoryFetcher, "_validate_ff_json_schema") as mock_validate,
        ):
            result = ForexFactoryFetcher._fetch_ff_json()

        mock_validate.assert_called_once_with(data)
        assert result == data

    def test_fetch_ff_json_returns_empty_on_non_list_response(self) -> None:
        """_fetch_ff_json() returns [] when JSON root is not a list."""
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({"error": "bad"}).encode()
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_response):
            result = ForexFactoryFetcher._fetch_ff_json()

        assert result == []


class TestForexFactoryFetcherMCTTimeout:
    """Tests for the timeout guard around market-calendar-tool scrape."""

    def test_fetch_mct_returns_empty_on_timeout(self, capsys) -> None:
        """_fetch_mct should return [] and log a warning when scrape times out."""
        from src.fetchers.forexfactory import ForexFactoryFetcher
        from concurrent.futures import Future, TimeoutError as FuturesTimeoutError

        # Simulate a future that raises TimeoutError when result() is called.
        mock_future = MagicMock(spec=Future)
        mock_future.result.side_effect = FuturesTimeoutError()

        mock_executor = MagicMock()
        mock_executor.__enter__ = lambda s: s
        mock_executor.__exit__ = MagicMock(return_value=False)
        mock_executor.submit = MagicMock(return_value=mock_future)

        mock_mct = MagicMock()
        mock_mct.scrape_calendar = MagicMock()
        mock_mct.clean_calendar_data = MagicMock()

        with (
            patch.dict("sys.modules", {"market_calendar_tool": mock_mct}),
            patch("src.fetchers.forexfactory.ThreadPoolExecutor", return_value=mock_executor),
        ):
            result = ForexFactoryFetcher._fetch_mct("2026-01-01", "2026-01-07")

        assert result == []
        captured = capsys.readouterr()
        assert "timed out" in captured.out

class TestFMPFetcherNormalise:
    """Tests for FMPFetcher._normalise and ID collision handling."""

    def test_normalise_id_includes_ccy(self) -> None:
        """ID should include the currency code to distinguish same-name events."""
        from src.fetchers.fmp import FMPFetcher

        raw = {"event": "CPI", "country": "US", "impact": "High", "date": "2026-01-01T12:00:00"}
        dt = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        event = FMPFetcher._normalise(raw, dt, "USD")
        assert "USD" in event.id
        assert event.id == "fmp_USD_CPI_20260101T120000"

    def test_fetch_deduplicates_ids_with_suffix(self) -> None:
        """Duplicate IDs within same fetch → suffixed with _1, _2, etc."""
        from src.fetchers.fmp import FMPFetcher

        # Two events: same name, same time, same country
        data = [
            {"event": "CPI", "country": "US", "impact": "high", "date": "2026-01-01T12:00:00"},
            {"event": "CPI", "country": "US", "impact": "high", "date": "2026-01-01T12:00:00"},
        ]
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(data).encode()
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)

        with (
            patch("urllib.request.urlopen", return_value=mock_response),
            patch.dict("os.environ", {"FMP_API_KEY": "test_key"}),
        ):
            fetcher = FMPFetcher()
            results = fetcher.fetch("2026-01-01", "2026-01-07", countries={"USD"}, importance_min=1)

        assert len(results) == 2
        ids = [e.id for e in results]
        assert len(set(ids)) == 2, f"IDs should be unique, got: {ids}"
        assert ids[0] == "fmp_USD_CPI_20260101T120000"
        assert ids[1] == "fmp_USD_CPI_20260101T120000_1"

    def test_fetch_sends_apikey_as_query_param(self) -> None:
        """API key must be passed as query parameter, not as a request header."""
        from src.fetchers.fmp import FMPFetcher

        captured_requests: list = []

        def mock_urlopen(req, timeout=None):
            captured_requests.append(req)
            mock_response = MagicMock()
            mock_response.read.return_value = b"[]"
            mock_response.__enter__ = lambda s: s
            mock_response.__exit__ = MagicMock(return_value=False)
            return mock_response

        with (
            patch("urllib.request.urlopen", side_effect=mock_urlopen),
            patch.dict("os.environ", {"FMP_API_KEY": "secret123"}),
        ):
            FMPFetcher().fetch("2026-01-01", "2026-01-07", countries={"USD"}, importance_min=1)

        assert len(captured_requests) == 1
        req = captured_requests[0]
        assert "apikey=secret123" in req.full_url, (
            f"API key should be in query params, got URL: {req.full_url}"
        )
        assert "apikey" not in (req.headers or {}), (
            "API key should NOT be sent as a header"
        )


class TestEnvVarValidation:
    """Tests for explicit error messages when required env vars are missing."""

    def test_build_calendar_service_raises_on_missing_sa_json(self) -> None:
        """build_calendar_service should raise RuntimeError with helpful message."""
        import os
        from src.sync import build_calendar_service

        env = {k: v for k, v in os.environ.items() if k != "GOOGLE_SA_JSON"}
        with patch.dict("os.environ", env, clear=True):
            with pytest.raises(RuntimeError, match="GOOGLE_SA_JSON"):
                build_calendar_service()

    def test_main_raises_on_missing_calendar_id(self) -> None:
        """main() should raise RuntimeError with helpful message when GOOGLE_CALENDAR_ID is absent."""
        import os
        from src.sync import main

        env = {k: v for k, v in os.environ.items() if k != "GOOGLE_CALENDAR_ID"}
        env["GOOGLE_SA_JSON"] = "{}"  # prevent SA_JSON error first
        with patch.dict("os.environ", env, clear=True):
            with pytest.raises(RuntimeError, match="GOOGLE_CALENDAR_ID"):
                main()


class TestCallWithRetry:
    """Tests for the _call_with_retry helper."""

    def test_succeeds_on_first_attempt(self) -> None:
        from src.sync import _call_with_retry

        call_count = 0

        def success():
            nonlocal call_count
            call_count += 1
            return {"ok": True}

        result = _call_with_retry(success)
        assert result == {"ok": True}
        assert call_count == 1

    def test_retries_on_429_then_succeeds(self) -> None:
        """Should retry on 429 and eventually succeed."""
        import googleapiclient.errors
        from unittest.mock import MagicMock
        from src.sync import _call_with_retry

        call_count = 0

        def flaky():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                resp = MagicMock()
                resp.status = 429
                raise googleapiclient.errors.HttpError(resp=resp, content=b"rate limited")
            return {"ok": True}

        with patch("time.sleep"):  # don't actually sleep in tests
            result = _call_with_retry(flaky, max_retries=4)

        assert result == {"ok": True}
        assert call_count == 3

    def test_raises_after_max_retries_exhausted(self) -> None:
        """Should raise the last exception when retries are exhausted."""
        import googleapiclient.errors
        from unittest.mock import MagicMock
        from src.sync import _call_with_retry

        resp = MagicMock()
        resp.status = 503

        def always_fails():
            raise googleapiclient.errors.HttpError(resp=resp, content=b"unavailable")

        with patch("time.sleep"):
            with pytest.raises(googleapiclient.errors.HttpError):
                _call_with_retry(always_fails, max_retries=2)

    def test_does_not_retry_on_non_retryable_status(self) -> None:
        """Should not retry on 4xx errors that are not in the retryable set."""
        import googleapiclient.errors
        from unittest.mock import MagicMock
        from src.sync import _call_with_retry

        call_count = 0

        def not_found():
            nonlocal call_count
            call_count += 1
            resp = MagicMock()
            resp.status = 404
            raise googleapiclient.errors.HttpError(resp=resp, content=b"not found")

        with pytest.raises(googleapiclient.errors.HttpError):
            _call_with_retry(not_found, max_retries=3)

        assert call_count == 1  # no retry


class TestMainEmptyEvents:
    """Tests for main() behavior when no events are retrieved."""

    def test_main_returns_gracefully_when_no_events(self, capsys) -> None:
        """main() should log a warning and return (not raise) when fetcher returns []."""
        import os
        from src.sync import main

        env = {
            "GOOGLE_CALENDAR_ID": "test@group.calendar.google.com",
            "EVENT_SOURCE": "forexfactory",
            "GOOGLE_SA_JSON": "{}",
        }

        mock_fetcher = MagicMock()
        mock_fetcher.name = "ForexFactory"
        mock_fetcher.fetch.return_value = []

        with (
            patch.dict("os.environ", env, clear=True),
            patch("src.sync.get_fetcher", return_value=mock_fetcher),
        ):
            # Should NOT raise RuntimeError
            main()

        captured = capsys.readouterr()
        assert "No events retrieved" in captured.out or "Skipping" in captured.out


# ---------------------------------------------------------------------------
# FMPFetcher retry tests
# ---------------------------------------------------------------------------

class TestFMPFetcherRetry:
    """Tests for FMPFetcher._fetch_with_retry() exponential back-off."""

    def test_success_on_first_attempt(self) -> None:
        """Returns parsed JSON list when request succeeds immediately."""
        from src.fetchers.fmp import FMPFetcher
        import urllib.request
        import io

        fetcher = FMPFetcher()
        payload = b'[{"event": "CPI", "country": "US"}]'

        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = payload

        with patch("urllib.request.urlopen", return_value=mock_resp):
            req = urllib.request.Request("https://example.com")
            result = fetcher._fetch_with_retry(req)

        assert result == [{"event": "CPI", "country": "US"}]

    def test_retries_on_429_then_succeeds(self, capsys) -> None:
        """Retries on HTTP 429 and returns data on subsequent success."""
        from src.fetchers.fmp import FMPFetcher
        import urllib.error
        import urllib.request

        fetcher = FMPFetcher()
        payload = b'[{"event": "NFP", "country": "US"}]'

        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = payload

        call_count = 0

        def urlopen_side_effect(req, timeout):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise urllib.error.HTTPError(None, 429, "Too Many Requests", {}, None)
            return mock_resp

        with (
            patch("urllib.request.urlopen", side_effect=urlopen_side_effect),
            patch("time.sleep"),
        ):
            req = urllib.request.Request("https://example.com")
            result = fetcher._fetch_with_retry(req)

        assert result == [{"event": "NFP", "country": "US"}]
        assert call_count == 2

    def test_returns_none_after_all_retries_exhausted(self, capsys) -> None:
        """Returns None when all retries fail with 429."""
        from src.fetchers.fmp import FMPFetcher
        import urllib.error
        import urllib.request

        fetcher = FMPFetcher()

        def urlopen_side_effect(req, timeout):
            raise urllib.error.HTTPError(None, 429, "Too Many Requests", {}, None)

        with (
            patch("urllib.request.urlopen", side_effect=urlopen_side_effect),
            patch("time.sleep"),
        ):
            req = urllib.request.Request("https://example.com")
            result = fetcher._fetch_with_retry(req)

        assert result is None

    def test_non_retryable_http_error_returns_none_immediately(self, capsys) -> None:
        """Returns None immediately on 401 without retrying."""
        from src.fetchers.fmp import FMPFetcher
        import urllib.error
        import urllib.request

        fetcher = FMPFetcher()
        call_count = 0

        def urlopen_side_effect(req, timeout):
            nonlocal call_count
            call_count += 1
            raise urllib.error.HTTPError(None, 401, "Unauthorized", {}, None)

        with (
            patch("urllib.request.urlopen", side_effect=urlopen_side_effect),
            patch("time.sleep"),
        ):
            req = urllib.request.Request("https://example.com")
            result = fetcher._fetch_with_retry(req)

        assert result is None
        assert call_count == 1  # no retry on 401


# ---------------------------------------------------------------------------
# ForexFactoryFetcher retry tests
# ---------------------------------------------------------------------------

class TestForexFactoryFetcherRetry:
    """Tests for ForexFactoryFetcher._fetch_with_retry() exponential back-off."""

    def test_success_on_first_attempt(self) -> None:
        """Returns parsed JSON list when request succeeds immediately."""
        from src.fetchers.forexfactory import ForexFactoryFetcher
        import urllib.request

        payload = b'[{"title": "CPI", "country": "USD", "impact": "high", "date": "2026-03-08T08:30:00+00:00"}]'

        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = payload

        with patch("urllib.request.urlopen", return_value=mock_resp):
            req = urllib.request.Request("https://example.com")
            result = ForexFactoryFetcher._fetch_with_retry(req)

        assert isinstance(result, list)
        assert result[0]["title"] == "CPI"

    def test_retries_on_429_then_succeeds(self) -> None:
        """Retries on HTTP 429 and returns data on subsequent success."""
        from src.fetchers.forexfactory import ForexFactoryFetcher
        import urllib.error
        import urllib.request

        payload = b'[{"title": "NFP", "country": "USD", "impact": "high", "date": "2026-03-08T08:30:00+00:00"}]'

        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = payload

        call_count = 0

        def urlopen_side_effect(req, timeout):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise urllib.error.HTTPError(None, 429, "Too Many Requests", {}, None)
            return mock_resp

        with (
            patch("urllib.request.urlopen", side_effect=urlopen_side_effect),
            patch("time.sleep"),
        ):
            req = urllib.request.Request("https://example.com")
            result = ForexFactoryFetcher._fetch_with_retry(req)

        assert result is not None
        assert call_count == 2

    def test_returns_none_after_all_retries_exhausted(self) -> None:
        """Returns None when all retries fail with 429."""
        from src.fetchers.forexfactory import ForexFactoryFetcher
        import urllib.error
        import urllib.request

        def urlopen_side_effect(req, timeout):
            raise urllib.error.HTTPError(None, 429, "Too Many Requests", {}, None)

        with (
            patch("urllib.request.urlopen", side_effect=urlopen_side_effect),
            patch("time.sleep"),
        ):
            req = urllib.request.Request("https://example.com")
            result = ForexFactoryFetcher._fetch_with_retry(req)

        assert result is None

    def test_non_retryable_http_error_returns_none_immediately(self) -> None:
        """Returns None immediately on 401 without retrying."""
        from src.fetchers.forexfactory import ForexFactoryFetcher
        import urllib.error
        import urllib.request

        call_count = 0

        def urlopen_side_effect(req, timeout):
            nonlocal call_count
            call_count += 1
            raise urllib.error.HTTPError(None, 401, "Unauthorized", {}, None)

        with (
            patch("urllib.request.urlopen", side_effect=urlopen_side_effect),
            patch("time.sleep"),
        ):
            req = urllib.request.Request("https://example.com")
            result = ForexFactoryFetcher._fetch_with_retry(req)

        assert result is None
        assert call_count == 1  # no retry on 401


# ---------------------------------------------------------------------------
# Issue #56 — failed events should not raise RuntimeError
# ---------------------------------------------------------------------------

class TestMainFailedEventsNoRaise:
    """Tests for main() behavior when some upsert_event calls return 'failed'."""

    def test_main_does_not_raise_when_some_events_fail(self, capsys) -> None:
        """main() should log a WARNING (not raise RuntimeError) when some events fail."""
        from src.sync import main

        env = {
            "GOOGLE_CALENDAR_ID": "test@group.calendar.google.com",
            "EVENT_SOURCE": "forexfactory",
            "GOOGLE_SA_JSON": json.dumps({"type": "service_account"}),
        }

        sample_event = {
            "title": "CPI",
            "country": "US",
            "impact": 3,
            "dt": "2026-03-10T08:30:00+00:00",
            "forecast": "0.3%",
            "previous": "0.2%",
            "source": "ff",
        }

        mock_fetcher = MagicMock()
        mock_fetcher.name = "ForexFactory"
        mock_fetcher.fetch.return_value = [sample_event]

        mock_gcal_event = {"summary": "CPI", "start": {}, "end": {}}

        with (
            patch.dict("os.environ", env, clear=True),
            patch("src.sync.get_fetcher", return_value=mock_fetcher),
            patch("src.sync.build_calendar_service", return_value=MagicMock()),
            patch("src.sync.get_existing_events", return_value={}),
            patch("src.sync.build_gcal_event", return_value=mock_gcal_event),
            patch("src.sync.upsert_event", return_value="failed"),
        ):
            # Should NOT raise RuntimeError
            main()

        captured = capsys.readouterr()
        assert "WARNING" in captured.out
        assert "1" in captured.out


# ---------------------------------------------------------------------------
# Issue #55 — get_existing_events() pagination should have a safety cap
# ---------------------------------------------------------------------------

class TestGetExistingEventsPaginationLimit:
    """Tests for the MAX_PAGINATION_PAGES safety cap in get_existing_events()."""

    def _make_page(self, n: int, has_next: bool) -> dict:
        return {
            "items": [
                {
                    "id": f"gcal-{n}-{i}",
                    "extendedProperties": {"private": {"econ_event_id": f"eid-{n}-{i}"}},
                }
                for i in range(2)
            ],
            **({"nextPageToken": f"token-{n+1}"} if has_next else {}),
        }

    def test_stops_at_max_pages(self, capsys) -> None:
        """get_existing_events() must stop and warn after MAX_PAGINATION_PAGES pages."""
        from src.sync import get_existing_events, _MAX_PAGINATION_PAGES

        call_count = 0

        def fake_call(fn):
            nonlocal call_count
            call_count += 1
            # Always return a page with nextPageToken so the loop would run forever
            return {
                "items": [
                    {
                        "id": f"gcal-{call_count}",
                        "extendedProperties": {
                            "private": {"econ_event_id": f"eid-{call_count}"}
                        },
                    }
                ],
                "nextPageToken": f"token-{call_count+1}",
            }

        with patch("src.sync._call_with_retry", side_effect=fake_call):
            result = get_existing_events(
                MagicMock(), "cal@example.com", "2026-01-01", "2026-12-31"
            )

        assert call_count == _MAX_PAGINATION_PAGES
        captured = capsys.readouterr()
        assert "WARNING" in captured.out
        assert "pagination limit" in captured.out
        # All fetched events should be present
        assert len(result) == _MAX_PAGINATION_PAGES
