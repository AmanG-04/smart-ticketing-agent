import json
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from src.config import ACCOUNTS_PATH, TICKETS_PATH


def _parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def load_tickets() -> list[dict[str, Any]]:
    with open(TICKETS_PATH, encoding="utf-8") as f:
        return json.load(f)


def load_accounts() -> list[dict[str, Any]]:
    with open(ACCOUNTS_PATH, encoding="utf-8") as f:
        return json.load(f)


def dataset_time_anchor(tickets: Optional[list[dict]] = None) -> datetime:
    tickets = tickets if tickets is not None else load_tickets()
    return max(_parse_ts(t["created_at"]) for t in tickets)


class DataStore:
    def __init__(self) -> None:
        self.tickets = load_tickets()
        self.accounts = load_accounts()
        self._by_account_id = {a["account_id"]: a for a in self.accounts}
        self._by_company = {a["company"].strip().lower(): a for a in self.accounts}
        self._tickets_by_account: dict[str, list[dict]] = {}
        for t in self.tickets:
            self._tickets_by_account.setdefault(t["account_id"], []).append(t)

    def resolve_account(self, account_ref: str) -> Optional[dict[str, Any]]:
        ref = (account_ref or "").strip()
        if not ref:
            return None
        acc = self._by_account_id.get(ref)
        if acc:
            return acc
        return self._by_company.get(ref.lower())

    def tickets_for(self, account: dict[str, Any]) -> list[dict[str, Any]]:
        acc_id = account["account_id"]
        direct = self._tickets_by_account.get(acc_id, [])
        if direct:
            return sorted(direct, key=lambda t: t["created_at"])
        company = account["company"].strip().lower()
        matched = [
            t for t in self.tickets if t.get("company", "").strip().lower() == company
        ]
        return sorted(matched, key=lambda t: t["created_at"])

    def recent_tickets(
        self,
        account: dict[str, Any],
        days: int = 90,
        anchor: Optional[datetime] = None,
    ) -> tuple[list[dict], datetime, datetime]:
        end = anchor or dataset_time_anchor(self.tickets)
        start = end - timedelta(days=days)
        in_window = [t for t in self.tickets_for(account) if _parse_ts(t["created_at"]) > start]
        return in_window, start, end

    def ticket_by_id(self, ticket_id: str) -> Optional[dict[str, Any]]:
        for t in self.tickets:
            if t["ticket_id"] == ticket_id:
                return t
        return None


_store: Optional[DataStore] = None


def get_store() -> DataStore:
    global _store
    if _store is None:
        _store = DataStore()
    return _store
