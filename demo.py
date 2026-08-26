import json
import sys

from src.brief import AccountNotFoundError, generate_account_brief
from src.data_loader import get_store
from src.triage import triage_ticket


def pick_risky_account() -> str:
    store = get_store()
    for a in sorted(store.accounts, key=lambda x: (x["health_status"] != "Churning", -int(x.get("arr_usd") or 0))):
        return a["account_id"]
    return ""


def main() -> int:
    store = get_store()
    ticket = store.tickets[0]

    print("=" * 70)
    print(f"TASK 1 DEMO — triage for {ticket['ticket_id']} ({ticket['company']})")
    print("=" * 70)
    result = triage_ticket({"subject": ticket["subject"], "body": ticket["body"]})
    print(json.dumps(result.model_dump(), indent=2))

    account_ref = pick_risky_account()
    print()
    print("=" * 70)
    print(f"TASK 2 DEMO — TAM brief for {account_ref}")
    print("=" * 70)
    try:
        brief = generate_account_brief(account_ref)
        print(json.dumps(brief.model_dump(), indent=2))
    except AccountNotFoundError as err:
        print(f"Account not found: {err}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
