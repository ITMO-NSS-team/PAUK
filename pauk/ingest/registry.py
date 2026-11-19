from __future__ import annotations

from collections.abc import Iterable

from .contracts import PublicationAdapter


class AdapterRegistry:
    def __init__(self, adapters: Iterable[PublicationAdapter] = ()) -> None:
        self._adapters: dict[str, PublicationAdapter] = {}
        for adapter in adapters:
            self.register(adapter)

    def register(self, adapter: PublicationAdapter) -> None:
        source = adapter.source.strip().casefold()
        if not source:
            raise ValueError("adapter source must be non-empty")
        if source in self._adapters:
            raise ValueError(f"an adapter for {source!r} is already registered")
        self._adapters[source] = adapter

    def get(self, source: str) -> PublicationAdapter:
        try:
            return self._adapters[source.strip().casefold()]
        except KeyError as exc:
            known = ", ".join(sorted(self._adapters)) or "none"
            raise ValueError(f"unknown raw provider {source!r}; registered: {known}") from exc

    @property
    def sources(self) -> tuple[str, ...]:
        return tuple(sorted(self._adapters))


def built_in_registry() -> AdapterRegistry:
    # Local import avoids making OpenAlex parsing part of the registry contract.
    from .openalex import OpenAlexAdapter

    return AdapterRegistry([OpenAlexAdapter()])
