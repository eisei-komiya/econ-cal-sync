"""Abstract base class for economic calendar fetchers."""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import EconomicEvent


class BaseFetcher(ABC):
    """Interface that every data-source fetcher must implement."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Short identifier for this source (e.g. ``'trading_economics'``)."""

    @abstractmethod
    def fetch(
        self,
        date_from: str,
        date_to: str,
        *,
        countries: set[str],
        importance_min: int,
    ) -> list[EconomicEvent]:
        """Fetch events in ``[date_from, date_to]`` and return normalised list.

        Parameters
        ----------
        date_from, date_to:
            ISO-8601 date strings (``YYYY-MM-DD``).
        countries:
            Set of full country names to include (e.g. ``{"United States", "Japan"}``).
        importance_min:
            Minimum importance level (1=Low, 2=Medium, 3=High).
        """
