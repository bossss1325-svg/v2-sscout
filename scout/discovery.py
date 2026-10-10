import copy
import unicodedata
from typing import Protocol

from scout.models import Lead


class SearchProvider(Protocol):
    def search(self, query: str, city: str, state: str, limit: int) -> list[Lead]: ...


def _normalize(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value.casefold())
    return "".join(char for char in normalized if not unicodedata.combining(char))


class MockSearchProvider:
    """Synthetic fixture catalog. It is not an internet search provider."""

    source = "mock://catalog"

    def __init__(self) -> None:
        self._catalog = [
            Lead(
                "Clínica Exemplo Porto Alegre", "dentistas", "Porto Alegre", "RS", self.source,
                sources={"companyName": self.source, "category": self.source, "city": self.source,
                         "state": self.source, "signal:commercialCategory": self.source},
                signals={"commercialCategory": True},
            ),
            Lead(
                "Odonto Demonstração POA", "dentistas", "Porto Alegre", "RS", self.source,
                sources={"companyName": self.source, "category": self.source, "city": self.source,
                         "state": self.source, "signal:commercialCategory": self.source},
                signals={"commercialCategory": True},
            ),
            Lead(
                "Barbearia Demonstração Canoas", "barbearias", "Canoas", "RS", self.source,
                sources={"companyName": self.source, "category": self.source, "city": self.source,
                         "state": self.source, "signal:commercialCategory": self.source},
                signals={"commercialCategory": True},
            ),
            Lead(
                "Restaurante Demonstração Gravataí", "restaurantes", "Gravataí", "RS", self.source,
                sources={"companyName": self.source, "category": self.source, "city": self.source,
                         "state": self.source, "signal:commercialCategory": self.source},
                signals={"commercialCategory": True},
            ),
        ]

    def search(self, query: str, city: str, state: str, limit: int) -> list[Lead]:
        normalized_query = _normalize(query).strip()
        normalized_city = _normalize(city).strip()
        normalized_state = _normalize(state).strip()
        matches = []
        for lead in self._catalog:
            category = _normalize(lead.category)
            name = _normalize(lead.company_name)
            category_matches = normalized_query in category or category in normalized_query or normalized_query in name
            location_matches = _normalize(lead.city) == normalized_city and _normalize(lead.state) == normalized_state
            if category_matches and location_matches:
                matches.append(copy.deepcopy(lead))
        return matches[:limit]