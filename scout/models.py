from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class WebsiteAnalysis:
    url: str
    status: str = "not_verified"
    https: bool | None = None
    mobile_friendly: bool | None = None
    performance_ms: int | None = None
    quality_score: int | None = None
    phone_number: str | None = None
    whatsapp_url: str | None = None
    has_phone: bool | None = None
    has_whatsapp: bool | None = None
    has_form: bool | None = None
    clear_services: bool | None = None
    has_contact_info: bool | None = None
    has_call_to_action: bool | None = None
    appearance_score: int | None = None
    outdated_signals: list[str] = field(default_factory=list)
    findings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "status": self.status,
            "https": self.https,
            "mobileFriendly": self.mobile_friendly,
            "performanceMs": self.performance_ms,
            "websiteScore": self.quality_score,
            "phoneNumber": self.phone_number,
            "whatsappUrl": self.whatsapp_url,
            "hasPhone": self.has_phone,
            "hasWhatsapp": self.has_whatsapp,
            "hasForm": self.has_form,
            "clearServices": self.clear_services,
            "hasContactInfo": self.has_contact_info,
            "hasCallToAction": self.has_call_to_action,
            "appearanceScore": self.appearance_score,
            "outdatedSignals": self.outdated_signals,
            "findings": self.findings,
        }


@dataclass(slots=True)
class Lead:
    company_name: str
    category: str
    city: str
    state: str
    source: str
    id: str = ""
    phone: str | None = None
    whatsapp: str | None = None
    website: str | None = None
    website_status: str = "not_verified"
    website_score: int | None = None
    website_analysis: WebsiteAnalysis | None = None
    instagram: str | None = None
    address: str | None = None
    opportunity_score: int = 0
    opportunity_level: str = "low"
    reasons: list[str] = field(default_factory=list)
    collected_at: str = ""
    sources: dict[str, str] = field(default_factory=dict)
    signals: dict[str, bool | None] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "companyName": self.company_name,
            "category": self.category,
            "city": self.city,
            "state": self.state,
            "phone": self.phone,
            "whatsapp": self.whatsapp,
            "website": self.website,
            "hasWebsite": False if self.website_status == "not_found" else True if self.website else None,
            "websiteStatus": self.website_status,
            "websiteScore": self.website_score,
            "websiteAnalysis": self.website_analysis.to_dict() if self.website_analysis else None,
            "instagram": self.instagram,
            "address": self.address,
            "source": self.source,
            "sources": self.sources,
            "opportunityScore": self.opportunity_score,
            "opportunityLevel": self.opportunity_level,
            "reasons": self.reasons,
            "collectedAt": self.collected_at,
            "signals": self.signals,
        }