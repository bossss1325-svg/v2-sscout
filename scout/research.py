import hashlib
from datetime import datetime, timezone

from scout.models import Lead
from scout.scoring import score_lead
from scout.website import WebsiteAnalyzer

OPTIONAL_FIELDS = ("phone", "whatsapp", "website", "instagram", "address")
REQUIRED_SOURCES = ("companyName", "category", "city", "state")


class CompanyResearcher:
    def __init__(self, website_analyzer: WebsiteAnalyzer | None = None) -> None:
        self.website_analyzer = website_analyzer or WebsiteAnalyzer()

    def research(self, lead: Lead) -> Lead:
        for field in REQUIRED_SOURCES:
            if not lead.sources.get(field):
                raise ValueError(f"campo obrigatório sem fonte: {field}")

        for field in OPTIONAL_FIELDS:
            if getattr(lead, field) and not lead.sources.get(field):
                setattr(lead, field, None)

        lead.signals = {
            key: value for key, value in lead.signals.items()
            if lead.sources.get(f"signal:{key}")
        }
        lead.collected_at = datetime.now(timezone.utc).isoformat()
        identity = "|".join((lead.source, lead.company_name, lead.city, lead.state)).casefold()
        lead.id = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]

        if lead.website:
            analysis = self.website_analyzer.analyze(lead.website)
            lead.website_analysis = analysis
            lead.website_status = analysis.status
            lead.website_score = analysis.quality_score
            if analysis.phone_number and not lead.phone:
                lead.phone = analysis.phone_number
                lead.sources["phone"] = lead.website
            if analysis.whatsapp_url and not lead.whatsapp:
                lead.whatsapp = analysis.whatsapp_url
                lead.sources["whatsapp"] = lead.website
            if analysis.mobile_friendly is not None:
                lead.signals["mobileFriendly"] = analysis.mobile_friendly
            if analysis.has_whatsapp is not None:
                lead.signals["hasWhatsapp"] = analysis.has_whatsapp
            if analysis.has_call_to_action is not None:
                lead.signals["hasCallToAction"] = analysis.has_call_to_action
            if analysis.outdated_signals:
                lead.signals["outdatedSite"] = True
        elif lead.website_status != "not_found":
            lead.website_status = "not_verified"

        result = score_lead(lead)
        lead.opportunity_score = result.value
        lead.opportunity_level = result.level
        lead.reasons = list(result.reasons)
        return lead