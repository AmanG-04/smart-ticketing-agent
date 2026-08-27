import json
import re
from datetime import date, datetime
from typing import Any, List, Literal, Optional

from pydantic import BaseModel, Field, ValidationError

from src.data_loader import DataStore, get_store
from src.llm import LLMClient, get_llm
from src.prompts import SIGNAL_SYSTEM, SIGNAL_USER_TEMPLATE, SYNTHESIS_SYSTEM, SYNTHESIS_USER_TEMPLATE


class AccountNotFoundError(LookupError):
    pass


class RiskFlag(BaseModel):
    source: Literal["ticket", "account"]
    title: str
    detail: str
    quote: str
    quote_ref: str


class RawSignal(BaseModel):
    ticket_id: str
    title: str
    detail: str
    quote: str


class RawSignalList(BaseModel):
    signals: List[RawSignal] = Field(default_factory=list)


class VerifiedRisk(BaseModel):
    title: str
    detail: str
    quote: str
    quote_ref: str


class RawBrief(BaseModel):
    executive_summary: str
    open_risks: List[VerifiedRisk] = Field(default_factory=list)
    recommended_talking_points: List[str] = Field(min_length=1)


class AccountBrief(BaseModel):
    account_id: str
    company: str
    plan_tier: str
    health_status: str
    usage_trend: str
    arr_usd: int
    tickets_analyzed: int
    window_start: str
    window_end: str
    data_gap_note: Optional[str] = None
    executive_summary: str
    open_risks: List[RiskFlag]
    recommended_talking_points: List[str]


_WS_RE = re.compile(r"\s+")
_brief_cache: dict[tuple[str, int], AccountBrief] = {}


def _norm(text: str) -> str:
    return _WS_RE.sub(" ", (text or "")).strip().lower()


# A brief generally has 5-17 tickets. This cap preserves direct evidence while making
# the signal-extraction call comfortably fit the free-tier token-per-minute budget.
_MAX_TICKET_BODY_CHARS = 360


def _rule_flags(account: dict[str, Any], anchor: datetime) -> list[RiskFlag]:
    flags: list[RiskFlag] = []

    def add(title: str, detail: str, field_name: str) -> None:
        value = account.get(field_name)
        quote = value if isinstance(value, str) else json.dumps(value)
        flags.append(RiskFlag(source="account", title=title, detail=detail, quote=quote, quote_ref=f"account.{field_name}"))

    if account.get("health_status") == "Churning":
        add("Account marked Churning", "CRM health status indicates explicit churn intent.", "health_status")
    elif account.get("health_status") == "At Risk":
        add("Account At Risk", "CRM health status shows risk signals.", "health_status")

    if account.get("usage_trend") == "Inactive":
        add("Usage inactive", "No logins recorded in the last 30 days per usage trend.", "usage_trend")
    elif account.get("usage_trend") == "Declining":
        add("Declining usage", "Seat usage or feature adoption is dropping.", "usage_trend")

    p1 = int(account.get("p1_tickets_last_30d") or 0)
    if p1 > 0:
        add(f"{p1} P1 ticket(s) in last 30 days", "Critical-severity incidents raised recently.", "p1_tickets_last_30d")

    nps = account.get("nps_score")
    if isinstance(nps, (int, float)) and nps <= 6:
        add(f"Low NPS ({nps}/10)", "Net promoter score at or below detractor threshold.", "nps_score")

    licensed = int(account.get("seats_licensed") or 0)
    active = int(account.get("seats_active") or 0)
    if licensed > 0 and active / licensed < 0.6:
        add(
            f"Low seat adoption ({active}/{licensed})",
            "Fewer than 60% of licensed seats were active in the last 30 days.",
            "seats_active",
        )

    renewal = account.get("renewal_date")
    if renewal:
        try:
            rdate = date.fromisoformat(str(renewal))
            days_to_renewal = (rdate - anchor.date()).days
            if 0 <= days_to_renewal <= 60:
                add(
                    f"Renewal in {days_to_renewal} days",
                    "Contract renewal is imminent while risk signals are present.",
                    "renewal_date",
                )
        except ValueError:
            pass

    for idx, note in enumerate(account.get("escalation_notes") or [], start=1):
        flags.append(
            RiskFlag(
                source="account",
                title=f"Escalation note #{idx}",
                detail="Recorded escalation observation from the account team.",
                quote=str(note),
                quote_ref=f"account.escalation_notes[{idx}]",
            )
        )
    return flags


def _verify_and_map(signals: RawSignalList, tickets: list[dict[str, Any]]) -> list[RiskFlag]:
    bodies = {t["ticket_id"]: _norm(t["body"]) for t in tickets}
    verified: list[RiskFlag] = []
    seen_refs: set[str] = set()
    for sig in signals.signals:
        body_norm = bodies.get(sig.ticket_id)
        if body_norm is None:
            continue
        quote_norm = _norm(sig.quote)
        if not quote_norm or quote_norm not in body_norm:
            continue
        key = f"{sig.ticket_id}:{quote_norm[:60]}"
        if key in seen_refs:
            continue
        seen_refs.add(key)
        verified.append(
            RiskFlag(
                source="ticket",
                title=sig.title.strip(),
                detail=sig.detail.strip(),
                quote=sig.quote.strip(),
                quote_ref=sig.ticket_id,
            )
        )
    verified.sort(key=lambda f: f.quote_ref)
    return verified[:8]


def extract_ticket_signals(
    account: dict[str, Any], tickets: list[dict[str, Any]], llm: LLMClient
) -> list[RiskFlag]:
    if not tickets:
        return []
    lines = []
    for t in tickets:
        body = t["body"].strip()
        if len(body) > _MAX_TICKET_BODY_CHARS:
            body = body[:_MAX_TICKET_BODY_CHARS]
        lines.append(
            f"{t['ticket_id']} | {t['created_at'][:10]} | P:{t['urgency']} | status:{t['status']}\n"
            f"Subject: {t['subject']}\nBody: {body}"
        )
    user_msg = SIGNAL_USER_TEMPLATE.format(
        company=account["company"],
        plan_tier=account["plan_tier"],
        health_status=account["health_status"],
        usage_trend=account["usage_trend"],
        tickets="\n\n".join(lines),
    )
    try:
        raw = llm.chat_json(
            messages=[
                {"role": "system", "content": SIGNAL_SYSTEM},
                {"role": "user", "content": user_msg},
            ],
            schema=RawSignalList,
            max_tokens=1100,
        )
    except (ValidationError, ValueError):
        return []
    return _verify_and_map(raw, tickets)


_ACCOUNT_SNAPSHOT_FIELDS = [
    "account_id",
    "company",
    "plan_tier",
    "arr_usd",
    "seats_licensed",
    "seats_active",
    "products",
    "health_status",
    "usage_trend",
    "open_tickets",
    "p1_tickets_last_30d",
    "customer_since",
    "renewal_date",
    "last_qbr_date",
    "primary_contact",
    "nps_score",
    "integrations_active",
    "region",
    "industry",
]


def generate_account_brief(
    account_ref: str,
    llm: Optional[LLMClient] = None,
    store: Optional[DataStore] = None,
    days: int = 90,
) -> AccountBrief:
    st = store or get_store()
    account = st.resolve_account(account_ref)
    if account is None:
        raise AccountNotFoundError(f"No account found matching '{account_ref}'")

    # Reusing an already generated account/window brief keeps repeated QBR requests stable
    # while avoiding avoidable model calls. The cache lasts for this process only.
    cache_key = (account["account_id"], days)
    if store is None and llm is None and cache_key in _brief_cache:
        return _brief_cache[cache_key].model_copy(deep=True)

    client = llm or get_llm()
    tickets, start, end = st.recent_tickets(account, days=days)

    ticket_risks = extract_ticket_signals(account, tickets, client)
    rules = _rule_flags(account, end)

    snapshot = {k: account.get(k) for k in _ACCOUNT_SNAPSHOT_FIELDS}
    user_msg = SYNTHESIS_USER_TEMPLATE.format(
        account_json=json.dumps(snapshot, indent=2),
        window_start=start.date().isoformat(),
        window_end=end.date().isoformat(),
        n_tickets=len(tickets),
        signals_json=json.dumps([r.model_dump() for r in ticket_risks], indent=2),
        rule_flags=json.dumps([r.model_dump() for r in rules], indent=2),
    )

    raw = client.chat_json(
        messages=[
            {"role": "system", "content": SYNTHESIS_SYSTEM},
            {"role": "user", "content": user_msg},
        ],
        schema=RawBrief,
        max_tokens=1100,
    )

    merged: list[RiskFlag] = list(rules)
    llm_keys = {(r.quote_ref, _norm(r.quote)[:80]) for r in ticket_risks}
    for r in raw.open_risks:
        if (r.quote_ref, _norm(r.quote)[:80]) in llm_keys:
            merged.append(RiskFlag(source="ticket", **r.model_dump()))
    merged.sort(key=lambda r: (r.source != "account", r.quote_ref))

    gap_note = None
    if not tickets:
        gap_note = (
            "No support tickets found for this account in the analysis window; "
            "brief is based on account summary data only."
        )

    talking_points = [tp.strip() for tp in raw.recommended_talking_points if tp.strip()][:5]

    brief = AccountBrief(
        account_id=account["account_id"],
        company=account["company"],
        plan_tier=account["plan_tier"],
        health_status=account["health_status"],
        usage_trend=account["usage_trend"],
        arr_usd=int(account.get("arr_usd") or 0),
        tickets_analyzed=len(tickets),
        window_start=start.date().isoformat(),
        window_end=end.date().isoformat(),
        data_gap_note=gap_note,
        executive_summary=raw.executive_summary.strip(),
        open_risks=merged[:10],
        recommended_talking_points=talking_points,
    )
    if store is None and llm is None:
        _brief_cache[cache_key] = brief.model_copy(deep=True)
    return brief
