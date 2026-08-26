import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.config import MissingAPIKeyError
from src.data_loader import get_store
from src.kb import get_kb

from .checks import RULE_CHECKS, CheckOutcome, deterministic, llm_judge, raises_account_not_found

CASES_DIR = Path(__file__).resolve().parent / "cases"
JSON_OUT = Path(__file__).resolve().parent.parent / "eval_report.json"
MD_OUT = Path(__file__).resolve().parent.parent / "eval_report.md"


def load_cases() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for path in sorted(CASES_DIR.glob("*.json")):
        cases.extend(json.loads(path.read_text(encoding="utf-8")))
    return cases


def run_offline_suite() -> list[dict[str, Any]]:
    results = []

    def record(name: str, passed: bool, detail: str) -> None:
        results.append({"id": f"offline-{name}", "status": "pass" if passed else "fail", "quality": 1.0 if passed else 0.0, "checks": [detail]})

    store = get_store()
    acc = store.resolve_account("ACC-7042")
    record("account-by-id", bool(acc and acc["company"] == "Hooli Corp"), "resolve_account by id")

    orphan = next((t for t in store.tickets if store.resolve_account(t["account_id"]) is None), None)
    ok_fallback = False
    if orphan:
        via_company = store.resolve_account(orphan["company"])
        ok_fallback = bool(via_company and via_company["company"] == orphan["company"])
        n = len(store.tickets_for(via_company)) if via_company else 0
        ok_fallback = ok_fallback and n > 0
    record("company-fallback-join", ok_fallback, f"ticket {orphan['ticket_id'] if orphan else '?'} resolved via company name with tickets attached")

    kb = get_kb()
    hits = kb.search("pipeline stopped ERR_CONNECTION_TIMEOUT after 30s restart", k=3)
    ok = any("databridge" in c.path.lower() or "performance" in c.path.lower() for c, _ in hits)
    record("kb-error-code-retrieval", ok, f"top hit for error-code query: {hits[0][0].location if hits else 'none'}")

    hits = kb.search("SSO_GROUP_NOT_FOUND new users cannot authenticate Okta group mapping", k=3)
    ok = any("authentication" in c.location.lower() or "sso" in c.location.lower() for c, _ in hits)
    record("kb-sso-retrieval", ok, f"top hit for SSO query: {hits[0][0].location if hits else 'none'}")

    record("corpus-integrity", len(kb.chunks) >= 20 and len({c.doc_title for c in kb.chunks}) >= 9, f"{len(kb.chunks)} chunks from {len({c.doc_title for c in kb.chunks})} docs")
    return results


def run_case(case: dict[str, Any], skip_llm: bool) -> dict[str, Any]:
    cid = case.get("id", "?")
    special = case.get("input", {}).get("special")

    if special == "raises_account_not_found":
        outcome = raises_account_not_found(**{k: v for k, v in case["input"].items() if k != "special"})
        return _finish(case, [outcome])

    outcomes: list[CheckOutcome] = []
    result: Any = None

    try:
        if case["task"] == "triage":
            from src.triage import triage_ticket

            result = triage_ticket(case["input"])
        elif case["task"] == "brief":
            from src.brief import generate_account_brief

            result = generate_account_brief(case["input"]["account_ref"])
    except MissingAPIKeyError as err:
        return {"id": cid, "name": case.get("name", ""), "status": "skipped", "quality": None, "checks": [], "note": str(err)}
    except Exception as err:
        return {"id": cid, "name": case.get("name", ""), "status": "fail", "quality": 0.0, "checks": [{"type": "pipeline_error", "passed": False, "score": 0.0, "detail": f"{type(err).__name__}: {err}"}]}

    for criterion in case.get("criteria", []):
        ctype = criterion.get("type", "")
        if ctype == "llm_judge":
            if skip_llm:
                outcomes.append(CheckOutcome(type="llm_judge", passed=True, score=0.0, detail="SKIPPED (--skip-llm)"))
                continue
            try:
                from src.llm import get_llm

                outcomes.append(llm_judge(result.model_dump(), criterion.get("rubric", ""), get_llm()))
            except MissingAPIKeyError as err:
                return {"id": cid, "name": case.get("name", ""), "status": "skipped", "quality": None, "checks": [], "note": str(err)}
            continue
        check_fn = RULE_CHECKS.get(ctype)
        if check_fn is None:
            outcomes.append(CheckOutcome(type=ctype, passed=False, score=0.0, detail=f"unknown check type {ctype!r}"))
            continue
        kwargs = {k: v for k, v in criterion.items() if k != "type"}
        try:
            outcomes.append(check_fn(result, **kwargs))
        except Exception as err:
            outcomes.append(CheckOutcome(type=ctype, passed=False, score=0.0, detail=f"check crashed: {type(err).__name__}: {err}"))

    return _finish(case, outcomes)


def _finish(case: dict[str, Any], outcomes: list[CheckOutcome], note: str = "") -> dict[str, Any]:
    scored = [o for o in outcomes if not o.detail.startswith("SKIPPED")]
    passed_all = all(o.passed for o in scored)
    quality = round(sum(o.score for o in scored) / len(scored), 3) if scored else None
    return {
        "id": case.get("id", "?"),
        "name": case.get("name", ""),
        "status": "pass" if passed_all else "fail",
        "quality": quality,
        "checks": [o.model_dump() for o in outcomes],
        **({"note": note} if note else {}),
    }


def write_reports(results: list[dict[str, Any]]) -> None:
    evaluated = [r for r in results if r["status"] in ("pass", "fail")]
    passed = sum(1 for r in evaluated if r["status"] == "pass")
    skipped = sum(1 for r in results if r["status"] == "skipped")
    qualities = [r["quality"] for r in evaluated if r["quality"] is not None]
    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_cases": len(results),
        "passed": passed,
        "failed": len(evaluated) - passed,
        "skipped": skipped,
        "mean_quality": round(sum(qualities) / len(qualities), 3) if qualities else None,
    }
    report = {"summary": summary, "cases": results}
    JSON_OUT.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    lines = [
        "# Evaluation Report",
        "",
        f"Generated: {summary['generated_at']}",
        "",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Total cases | {summary['total_cases']} |",
        f"| Passed | {passed} |",
        f"| Failed | {summary['failed']} |",
        f"| Skipped (no API key) | {skipped} |",
        f"| Mean quality score | {summary['mean_quality']} |",
        "",
        "| Case | Status | Quality | Failed checks |",
        "|------|--------|---------|---------------|",
    ]
    for r in results:
        failed = [c["type"] for c in r.get("checks", []) if isinstance(c, dict) and not c.get("passed") and not str(c.get("detail", "")).startswith("SKIPPED")]
        lines.append(f"| {r['id']} — {r.get('name','')} | {r['status']} | {r['quality']} | {', '.join(failed) if failed else '—'} |")
    MD_OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the evaluation harness")
    parser.add_argument("--skip-llm", action="store_true", help="Skip LLM-dependent pipeline cases and judge criteria (offline mode)")
    args = parser.parse_args(argv)

    results = run_offline_suite()
    offline_failures = sum(1 for r in results if r["status"] == "fail")

    llm_blocked = False
    for case in load_cases():
        res = run_case(case, skip_llm=args.skip_llm)
        if res["status"] == "skipped":
            llm_blocked = True
        results.append(res)
        status = res["status"]
        print(f"[{status.upper():5}] {res['id']} — {res.get('name','')}" + (f" ({res.get('note','')})" if res.get("note") else ""))

    write_reports(results)

    failures = sum(1 for r in results if r["status"] == "fail")
    total_evaluated = sum(1 for r in results if r["status"] in ("pass", "fail"))
    print(f"\nSummary: {total_evaluated - failures}/{total_evaluated} passed, {failures} failed, {sum(1 for r in results if r['status']=='skipped')} skipped")
    if llm_blocked:
        print("NOTE: some cases were skipped because GROQ_API_KEY is not set. Add it to .env for full runs.")
    report_path = JSON_OUT
    print(f"Reports written: {report_path}, {MD_OUT}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
