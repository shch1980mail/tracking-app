from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class Coords:
    lat: float | None
    lng: float | None

    @property
    def ok(self) -> bool:
        return self.lat is not None and self.lng is not None


class GpsProvider(ABC):
    name: str
    url_marker: str

    def matches(self, url: str) -> bool:
        return self.url_marker in url

    @abstractmethod
    async def fetch(self, url: str) -> Coords: ...
