from typing import Any, Optional, Union

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from src.config import MissingAPIKeyError
from src.data_loader import get_store
from src.brief import AccountNotFoundError, AccountBrief, generate_account_brief
from src.triage import TriageResult, triage_ticket

app = FastAPI(
    title="Smart Ticketing Agent",
    description="LLM-powered ticket triage and TAM account briefs over the mock dataset.",
    version="1.0.0",
)


class TriageRequest(BaseModel):
    subject: Optional[str] = None
    body: Optional[str] = None
    text: Optional[str] = None


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/triage", response_model=TriageResult)
def triage(req: TriageRequest) -> TriageResult:
    if req.text:
        payload: Union[str, dict[str, Any]] = req.text
    elif req.body or req.subject:
        payload = {"subject": req.subject or "", "body": req.body or ""}
    else:
        raise HTTPException(status_code=422, detail="Provide 'text' or 'subject'/'body'.")
    try:
        return triage_ticket(payload)
    except MissingAPIKeyError as err:
        raise HTTPException(status_code=503, detail=str(err)) from err


@app.get("/accounts")
def list_accounts() -> list[dict[str, Any]]:
    store = get_store()
    return [
        {
            "account_id": a["account_id"],
            "company": a["company"],
            "plan_tier": a["plan_tier"],
            "health_status": a["health_status"],
            "arr_usd": a["arr_usd"],
        }
        for a in sorted(store.accounts, key=lambda x: x["company"])
    ]


@app.get("/accounts/{account_ref}/brief", response_model=AccountBrief)
def account_brief(account_ref: str) -> AccountBrief:
    try:
        return generate_account_brief(account_ref)
    except AccountNotFoundError as err:
        raise HTTPException(status_code=404, detail=str(err)) from err
    except MissingAPIKeyError as err:
        raise HTTPException(status_code=503, detail=str(err)) from err
