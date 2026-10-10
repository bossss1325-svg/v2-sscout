import logging
import time
from dataclasses import dataclass

from scout.discovery import SearchProvider
from scout.models import Lead
from scout.research import CompanyResearcher
from scout.storage import LeadStore

logger = logging.getLogger("scout")


@dataclass(frozen=True, slots=True)
class SearchResult:
    results: list[Lead]
    total: int
    errors: int = 0


class ScoutService:
    def __init__(self, provider: SearchProvider, researcher: CompanyResearcher | None = None,
                 store: LeadStore | None = None) -> None:
        self.provider = provider
        self.researcher = researcher or CompanyResearcher()
        self.store = store or LeadStore()

    def search(self, query: str, city: str, state: str, limit: int = 20) -> SearchResult:
        started = time.monotonic()
        logger.info("Pesquisa iniciada: query=%r city=%r state=%r limit=%d", query, city, state, limit)
        discovered = self.provider.search(query, city, state, limit)
        logger.info("Empresas encontradas: %d", len(discovered))
        analyzed: list[Lead] = []
        errors = 0
        inaccessible = 0
        for lead in discovered:
            try:
                researched = self.researcher.research(lead)
                analyzed.append(researched)
                self.store.save(researched)
                if researched.website_status == "broken":
                    inaccessible += 1
                    logger.warning("Site inacessível: lead_id=%s", researched.id)
            except Exception:
                errors += 1
                logger.exception("Erro ao analisar empresa: source=%s", lead.source)

        analyzed.sort(key=lambda item: (-item.opportunity_score, item.company_name.casefold(), item.id))
        elapsed = time.monotonic() - started
        logger.info("Empresas analisadas: %d; erros: %d; sites inacessíveis: %d", len(analyzed), errors, inaccessible)
        logger.info("Pesquisa concluída em %.3fs", elapsed)
        return SearchResult(results=analyzed, total=len(analyzed), errors=errors)