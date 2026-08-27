from typing import Any, List, Literal, Optional, Tuple, Union

from pydantic import BaseModel, Field

from src.config import PRODUCTS, URGENCY_TIERS
from src.kb import KBChunk, get_kb
from src.llm import LLMClient, get_llm
from src.prompts import TRIAGE_SYSTEM, TRIAGE_USER_TEMPLATE

ProductEnum = Literal[
    "DataBridge Pro",
    "CloudSync",
    "AnalyticsHub",
    "SecureVault",
    "WorkflowEngine",
    "Unknown",
]

CategoryEnum = Literal[
    "Bug",
    "Feature Request",
    "How-To",
    "Performance",
    "Billing",
    "Integration",
    "Onboarding",
    "Data Loss",
]

UrgencyEnum = Literal["P1", "P2", "P3", "P4"]


class TicketInput(BaseModel):
    subject: str = ""
    body: str = ""

    @property
    def full_text(self) -> str:
        return f"{self.subject}\n\n{self.body}".strip()


class KBMatch(BaseModel):
    title: str
    location: str
    why_relevant: str = ""


class RawTriage(BaseModel):
    product: ProductEnum
    product_area: str
    category: CategoryEnum
    urgency: UrgencyEnum
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str
    known_issue_match: bool
    matched_docs: List[KBMatch] = Field(default_factory=list)
    draft_first_response: str


class TriageResult(BaseModel):
    ticket_subject: str
    product: str
    product_area: str
    category: str
    urgency: str
    confidence: float
    reasoning: str
    known_issue_match: bool
    matched_docs: List[KBMatch]
    responder_team: str
    escalation_required: bool
    draft_first_response: str


RESPONDER_MAP = {
    "Bug": "Tier-2 Support",
    "Performance": "Tier-2 Support",
    "Integration": "Tier-2 Support (Integrations)",
    "Data Loss": "Tier-2 Support (Data Recovery)",
    "How-To": "Tier-1 Support",
    "Onboarding": "Onboarding Team",
    "Billing": "Billing Support",
    "Feature Request": "Product Management Intake",
}

P1_ESCALATION_TEAM_PREFIX = "P1 Hotline"


def normalize_ticket(raw: Union[str, dict[str, Any], TicketInput]) -> TicketInput:
    if isinstance(raw, TicketInput):
        return raw
    if isinstance(raw, str):
        text = raw.strip()
        if "\n" in text:
            head, _, rest = text.partition("\n")
            return TicketInput(subject=head.strip(), body=rest.strip())
        return TicketInput(subject=text, body="")
    return TicketInput(subject=str(raw.get("subject", "") or ""), body=str(raw.get("body", "") or ""))


def route_responder(category: str, urgency: str) -> Tuple[str, bool]:
    team = RESPONDER_MAP.get(category, "Tier-1 Support")
    escalation = urgency in URGENCY_TIERS and urgency == "P1"
    if escalation:
        team = f"{P1_ESCALATION_TEAM_PREFIX} -> {team}"
    return team, escalation


def _norm_loc(value: str) -> str:
    return " ".join(value.lower().split()).rstrip(".")


def _validate_matched_docs(raw_docs: List[KBMatch], retrieved: List[Tuple[KBChunk, float]]) -> List[KBMatch]:
    valid_locations = {_norm_loc(c.location) for c, _ in retrieved}
    kept: list[KBMatch] = []
    seen: set[str] = set()
    for doc in raw_docs:
        key = _norm_loc(doc.location)
        if key in valid_locations and key not in seen:
            seen.add(key)
            kept.append(doc)
    return kept[:3]


def triage_ticket(
    ticket: Union[str, dict[str, Any], TicketInput],
    llm: Optional[LLMClient] = None,
    k_docs: int = 4,
) -> TriageResult:
    t = normalize_ticket(ticket)
    client = llm or get_llm()

    retrieved = get_kb().search(t.full_text or "support", k=k_docs)
    context = get_kb().build_context(retrieved)

    user_msg = TRIAGE_USER_TEMPLATE.format(
        subject=t.subject.strip() or "(none)",
        body=t.body.strip() or "(none)",
        kb_context=context if context else "(no relevant knowledge-base sections found)",
    )

    raw = client.chat_json(
        messages=[
            {"role": "system", "content": TRIAGE_SYSTEM},
            {"role": "user", "content": user_msg},
        ],
        schema=RawTriage,
    )

    verified_docs = _validate_matched_docs(raw.matched_docs, retrieved)
    # An explicit error-code match is grounded even if the model omitted citations.
    if raw.known_issue_match and not verified_docs and retrieved:
        top_chunk = retrieved[0][0]
        verified_docs = [
            KBMatch(
                title=top_chunk.doc_title,
                location=top_chunk.location,
                why_relevant="Retrieved as the highest-scoring knowledge-base match for this ticket.",
            )
        ]
    known_issue = bool(verified_docs) and raw.known_issue_match
    team, escalation = route_responder(raw.category, raw.urgency)

    return TriageResult(
        ticket_subject=t.subject.strip(),
        product=raw.product,
        product_area=raw.product_area.strip() or "Unknown",
        category=raw.category,
        urgency=raw.urgency,
        confidence=max(0.0, min(1.0, raw.confidence)),
        reasoning=raw.reasoning.strip(),
        known_issue_match=known_issue,
        matched_docs=verified_docs,
        responder_team=team,
        escalation_required=escalation,
        draft_first_response=raw.draft_first_response.strip()[:1600],
    )
