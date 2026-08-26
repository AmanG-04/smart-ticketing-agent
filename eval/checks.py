import hashlib
import json
import re
from typing import Any, Callable, Optional

from pydantic import BaseModel

from src.brief import AccountNotFoundError, AccountBrief, generate_account_brief
from src.config import CATEGORIES, PRODUCTS, URGENCY_TIERS
from src.data_loader import get_store
from src.kb import get_kb
from src.triage import TriageResult


class CheckOutcome(BaseModel):
    type: str
    passed: bool
    score: float
    detail: str = ""


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().lower()


_URGENCY_RANK = {"P1": 1, "P2": 2, "P3": 3, "P4": 4}

_SENTENCE_RE = re.compile(r"[^.!?]+[.!?]")


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_RE.findall(text or "") if s.strip()]


def enum_fields(result: TriageResult, **_: Any) -> CheckOutcome:
    problems = []
    if result.category not in CATEGORIES:
        problems.append(f"category={result.category!r}")
    if result.urgency not in URGENCY_TIERS:
        problems.append(f"urgency={result.urgency!r}")
    if result.product not in PRODUCTS and result.product != "Unknown":
        problems.append(f"product={result.product!r}")
    return CheckOutcome(type="enum_fields", passed=not problems, score=1.0 if not problems else 0.0, detail="; ".join(problems))


def _get_field(result: Any, field: str) -> Any:
    value = getattr(result, field, None)
    if value is None and isinstance(result, dict):
        value = result.get(field)
    return value


def field_equals(result: Any, field: str = "", value: Any = None, **_: Any) -> CheckOutcome:
    actual = _get_field(result, field)
    ok = str(actual).strip().lower() == str(value).strip().lower()
    return CheckOutcome(type="field_equals", passed=ok, score=float(ok), detail=f"{field}={actual!r} expected {value!r}")


def field_in(result: Any, field: str = "", values: Optional[list] = None, **_: Any) -> CheckOutcome:
    values = values or []
    actual = _get_field(result, field)
    ok = str(actual).strip().lower() in {str(v).strip().lower() for v in values}
    return CheckOutcome(type="field_in", passed=ok, score=float(ok), detail=f"{field}={actual!r} expected one of {values}")


def urgency_at_most(result: Any, value: str = "P3", **_: Any) -> CheckOutcome:
    actual = getattr(result, "urgency", None)
    ok = _URGENCY_RANK.get(actual, 99) <= _URGENCY_RANK.get(value, 99)
    return CheckOutcome(type="urgency_at_most", passed=ok, score=float(ok), detail=f"urgency={actual!r} must be at most {value!r}")


def kb_doc_referenced(result: TriageResult, **_: Any) -> CheckOutcome:
    kb = get_kb()
    known_locations = {_norm(c.location) for c in kb.chunks}
    known_titles = {_norm(c.doc_title) for c in kb.chunks}
    if not result.matched_docs:
        return CheckOutcome(type="kb_doc_referenced", passed=False, score=0.0, detail="no matched_docs returned")
    grounded = [
        d for d in result.matched_docs
        if _norm(d.location) in known_locations or _norm(d.title) in known_titles
    ]
    ratio = len(grounded) / len(result.matched_docs)
    ok = bool(grounded) and ratio >= 0.5
    return CheckOutcome(
        type="kb_doc_referenced",
        passed=ok,
        score=ratio,
        detail=f"{len(grounded)}/{len(result.matched_docs)} cited docs exist in the knowledge base",
    )


def known_issue_match_true(result: TriageResult, **_: Any) -> CheckOutcome:
    ok = result.known_issue_match is True
    return CheckOutcome(type="known_issue_match_true", passed=ok, score=float(ok), detail=f"known_issue_match={result.known_issue_match}")


def no_kb_required(result: TriageResult, **_: Any) -> CheckOutcome:
    ok = result.known_issue_match is False
    return CheckOutcome(type="no_kb_required", passed=ok, score=float(ok), detail=f"known_issue_match={result.known_issue_match} expected False for non-technical query")


def draft_mentions(result: Any, any_of: Optional[list] = None, where: str = "draft_first_response", **_: Any) -> CheckOutcome:
    terms = any_of or []
    text = _norm(str(_get_field(result, where)))
    hit = [t for t in terms if t.lower() in text]
    ok = bool(hit)
    return CheckOutcome(type="draft_mentions", passed=ok, score=(len(hit) / len(terms)) if terms else 0.0, detail=f"matched terms: {hit}")


def reasoning_min_length(result: TriageResult, value: int = 40, **_: Any) -> CheckOutcome:
    length = len(result.reasoning.strip())
    ok = length >= value
    return CheckOutcome(type="reasoning_min_length", passed=ok, score=min(1.0, length / max(value, 1)), detail=f"reasoning length={length}")


def responder_contains(result: TriageResult, value: str = "", **_: Any) -> CheckOutcome:
    ok = value.lower() in result.responder_team.lower()
    return CheckOutcome(type="responder_contains", passed=ok, score=float(ok), detail=f"responder_team={result.responder_team!r}")


def escalation_true(result: TriageResult, **_: Any) -> CheckOutcome:
    ok = result.escalation_required is True
    return CheckOutcome(type="escalation_true", passed=ok, score=float(ok), detail=f"escalation_required={result.escalation_required}")


def graceful_output(result: Any, **_: Any) -> CheckOutcome:
    needed = ["product", "category", "urgency", "draft_first_response"]
    missing = [k for k in needed if not str(getattr(result, k, "")).strip()]
    ok = isinstance(result, TriageResult) and not missing
    return CheckOutcome(type="graceful_output", passed=ok, score=1.0 if ok else 0.0, detail=f"missing: {missing}")


def graceful_output_brief(result: AccountBrief, **_: Any) -> CheckOutcome:
    missing = []
    for field in ["company", "executive_summary", "recommended_talking_points"]:
        value = getattr(result, field, None)
        if not value or (isinstance(value, str) and not value.strip()):
            missing.append(field)
    ok = isinstance(result, AccountBrief) and not missing
    return CheckOutcome(type="graceful_output_brief", passed=ok, score=1.0 if ok else 0.0, detail=f"missing: {missing}")


def sections_present(result: AccountBrief, **_: Any) -> CheckOutcome:
    n_sentences = len(_sentences(result.executive_summary))
    points = len(result.recommended_talking_points)
    summary_ok = 3 <= n_sentences <= 5
    points_ok = points >= 3
    score = (1.0 if summary_ok else max(0.0, 1 - abs(3 - min(n_sentences, 3)) * 0.34)) * 0.6 + (1.0 if points_ok else 0.0) * 0.4
    return CheckOutcome(
        type="sections_present",
        passed=summary_ok and points_ok,
        score=round(score, 3),
        detail=f"summary sentences={n_sentences} (need 3-5), talking points={points} (need >=3)",
    )


def risks_min(result: AccountBrief, value: int = 1, **_: Any) -> CheckOutcome:
    ok = len(result.open_risks) >= value
    return CheckOutcome(type="risks_min", passed=ok, score=min(1.0, len(result.open_risks) / max(value, 1)), detail=f"risks={len(result.open_risks)} need >={value}")


def quotes_verified(result: AccountBrief, **_: Any) -> CheckOutcome:
    store = get_store()
    total = verified = 0
    failures: list[str] = []
    for risk in result.open_risks:
        if risk.source != "ticket":
            continue
        total += 1
        ticket = store.ticket_by_id(risk.quote_ref)
        if ticket and _norm(risk.quote) in _norm(ticket["body"]):
            verified += 1
        else:
            failures.append(f"{risk.quote_ref}: {risk.quote[:60]!r}")
    ok = total > 0 and verified == total
    return CheckOutcome(
        type="quotes_verified",
        passed=ok,
        score=(verified / total) if total else 0.0,
        detail=f"{verified}/{total} ticket quotes verbatim" + ("; bad: " + "; ".join(failures) if failures else ""),
    )


def window_consistent(result: AccountBrief, days: int = 90, **_: Any) -> CheckOutcome:
    from datetime import date

    try:
        start = date.fromisoformat(result.window_start)
        end = date.fromisoformat(result.window_end)
    except ValueError as err:
        return CheckOutcome(type="window_consistent", passed=False, score=0.0, detail=f"bad dates: {err}")
    span = (end - start).days
    ok = abs(span - days) <= 1
    return CheckOutcome(type="window_consistent", passed=ok, score=1.0 if ok else 0.0, detail=f"span={span}d expected ~{days}d")


def raises_account_not_found(account_ref: str = "", **_: Any) -> CheckOutcome:
    try:
        generate_account_brief(account_ref)
    except AccountNotFoundError:
        return CheckOutcome(type="raises_account_not_found", passed=True, score=1.0, detail="raised AccountNotFoundError as designed")
    except Exception as err:
        return CheckOutcome(type="raises_account_not_found", passed=False, score=0.0, detail=f"wrong error: {type(err).__name__}: {err}")
    return CheckOutcome(type="raises_account_not_found", passed=False, score=0.0, detail="no error raised for unknown account")


def deterministic(account_ref: str = "", runs: int = 2, **_: Any) -> CheckOutcome:
    digests = []
    for _ in range(max(2, runs)):
        brief = generate_account_brief(account_ref)
        canonical = json.dumps(brief.model_dump(), sort_keys=True, ensure_ascii=False)
        digests.append(hashlib.sha256(canonical.encode()).hexdigest())
    ok = len(set(digests)) == 1
    return CheckOutcome(type="deterministic", passed=ok, score=1.0 if ok else 0.0, detail=f"hashes equal across {len(digests)} runs" if ok else f"divergent hashes: {digests}")


RULE_CHECKS: dict[str, Callable[..., CheckOutcome]] = {
    "enum_fields": enum_fields,
    "field_equals": field_equals,
    "field_in": field_in,
    "urgency_at_most": urgency_at_most,
    "kb_doc_referenced": kb_doc_referenced,
    "known_issue_match_true": known_issue_match_true,
    "no_kb_required": no_kb_required,
    "draft_mentions": draft_mentions,
    "reasoning_min_length": reasoning_min_length,
    "responder_contains": responder_contains,
    "escalation_true": escalation_true,
    "graceful_output": graceful_output,
    "graceful_output_brief": graceful_output_brief,
    "sections_present": sections_present,
    "risks_min": risks_min,
    "quotes_verified": quotes_verified,
    "window_consistent": window_consistent,
}


JUDGE_SYSTEM = """You are a strict, consistent evaluation judge for an AI support system.
Score how well the OUTPUT satisfies the RUBRIC on a scale from 0.0 to 1.0.
Return ONLY JSON: {"score": <float>, "justification": "<one sentence>"}"""


def llm_judge(output: dict[str, Any], rubric: str, llm: Any) -> CheckOutcome:
    user = f"RUBRIC:\n{rubric}\n\nOUTPUT:\n{json.dumps(output, indent=2, default=str)[:6000]}"
    verdict = llm.chat_json(messages=[{"role": "system", "content": JUDGE_SYSTEM}, {"role": "user", "content": user}], schema=_JudgeVerdict)
    score = max(0.0, min(1.0, float(verdict.score)))
    return CheckOutcome(type="llm_judge", passed=score >= 0.7, score=round(score, 3), detail=verdict.justification)


class _JudgeVerdict(BaseModel):
    score: float
    justification: str = ""
