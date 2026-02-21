"""Economic calendar data fetchers."""

from __future__ import annotations

from .base import BaseFetcher
from .fmp import FMPFetcher
from .forexfactory import ForexFactoryFetcher

__all__ = ["BaseFetcher", "FMPFetcher", "ForexFactoryFetcher", "get_fetcher"]

# Registry of available fetchers – add new sources here.
_FETCHERS: dict[str, type[BaseFetcher]] = {
    "forexfactory": ForexFactoryFetcher,
    "fmp": FMPFetcher,
}


def get_fetcher(name: str) -> BaseFetcher:
    """Return a fetcher instance by name.

    Raises ``KeyError`` if *name* is not registered.
    Available names: forexfactory, fmp
    """
    try:
        cls = _FETCHERS[name]
    except KeyError:
        available = ", ".join(sorted(_FETCHERS))
        raise KeyError(
            f"Unknown fetcher '{name}'. Available: {available}"
        ) from None
    return cls()
