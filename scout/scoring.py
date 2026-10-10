from dataclasses import dataclass

from scout.models import Lead


@dataclass(frozen=True, slots=True)
class Score:
    value: int
    level: str
    reasons: tuple[str, ...]


def classify_opportunity(score: int) -> str:
    if not 0 <= score <= 100:
        raise ValueError("score deve estar entre 0 e 100")
    if score >= 85:
        return "high"
    if score >= 70:
        return "good"
    if score >= 40:
        return "medium"
    return "low"


def score_lead(lead: Lead) -> Score:
    """Score determinístico; evidências desconhecidas não alteram a pontuação."""
    points = 0
    reasons: list[str] = []

    def add(condition: bool, weight: int, reason: str) -> None:
        nonlocal points
        if condition:
            points += weight
            reasons.append(reason)

    add(lead.website_status == "not_found", 40, "Nenhum site identificado na pesquisa")
    add(lead.website_status == "broken", 35, "Site identificado, mas inacessível")
    add(lead.signals.get("outdatedSite") is True, 12, "Sinais verificáveis de site desatualizado")
    add(lead.signals.get("mobileFriendly") is False, 10, "Site sem boa compatibilidade mobile")
    add(lead.signals.get("hasCallToAction") is False, 8, "Site sem chamada para ação identificada")
    add(lead.signals.get("hasWhatsapp") is False, 4, "WhatsApp não identificado no site")
    add(lead.signals.get("active") is True, 8, "Sinal público de empresa ativa")
    add(lead.signals.get("strongPublicPresence") is True, 6, "Presença pública forte identificada")
    add(lead.signals.get("positiveReputation") is True, 6, "Sinal público de reputação positiva")
    add(lead.signals.get("commercialCategory") is True, 5, "Categoria com potencial comercial")

    value = min(points, 100)
    return Score(value=value, level=classify_opportunity(value), reasons=tuple(reasons))