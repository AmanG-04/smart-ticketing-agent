# Smart Ticketing Agent

LLM-powered internal tooling for **Technical Support** and **TAM** teams, built on the provided mock
dataset (500 synthetic tickets, 50 synthetic accounts, 9 knowledge-base markdown docs).

| Task | What it does | Entry point |
|------|--------------|-------------|
| **1 · Ticket triage agent** | Classifies any raw ticket into product / product area / category / urgency with reasoning, matches known issues in the knowledge base (RAG), routes to a responder team, drafts the first response | `POST /triage` |
| **2 · TAM account health summariser** | Generates a deterministic QBR brief for an account: executive summary, open risks flagged with **verbatim ticket quotes**, recommended talking points | `GET /accounts/{id}/brief` |
| **3 · Evaluation harness** | Rule-based checks + LLM-as-judge over both tasks, adversarial cases included; writes `eval_report.json` / `eval_report.md` | `python -m eval.run_evals` |
| **4 · Design note** | Failure modes, latency trade-offs, PII handling, scaling | [DESIGN.md](DESIGN.md) |

Bonus features included: **Streamlit UI** (`ui.py`) and a **GitHub Actions workflow** that runs the
eval harness on every push (`.github/workflows/eval.yml`).

---

## Setup

```bash
pip install -r requirements.txt
copy .env.example .env        # then paste your Groq API key inside .env
```

Get a free key at [console.groq.com/keys](https://console.groq.com/keys). The default current
Groq model is `groq/compound-mini`; it has a 70K token/minute free-tier limit and can be overridden with `GROQ_MODEL`. The key is read from
`.env` and is never committed.

## Sample runs

### Task 1 — triage (single command demo of Tasks 1+2)

```bash
python demo.py
```

### REST API

```bash
uvicorn src.api:app --reload
```

```bash
# Triage a ticket
curl -X POST http://localhost:8000/triage -H "Content-Type: application/json" -d "{\"subject\": \"Webhook from CloudSync not reaching Snowflake\", \"body\": \"Failed deliveries since last week: 9175. ERR_CONNECTION_TIMEOUT in logs.\"}"

# Account brief (works with account id OR company name)
curl http://localhost:8000/accounts/ACC-7042/brief
curl "http://localhost:8000/accounts/Hooli%20Corp/brief"

# List all accounts
curl http://localhost:8000/accounts
```

### Task 2 — account brief from Python

```python
from src.brief import generate_account_brief

brief = generate_account_brief("ACC-7042")
print(brief.executive_summary)
for risk in brief.open_risks:
    print(f"[{risk.source}] {risk.title}: \"{risk.quote}\" ({risk.quote_ref})")
```

### Streamlit UI

```bash
streamlit run ui.py
```

### Evaluation harness

```bash
python -m eval.run_evals              # full suite (needs GROQ_API_KEY)
python -m eval.run_evals --skip-llm   # zero LLM calls: data joins and retrieval quality only
```

The latest committed report lives in [`eval_report.md`](eval_report.md) /
[`eval_report.json`](eval_report.json). CI runs zero-cost offline data and retrieval checks on every
push. To preserve the Groq free-tier request quota, live LLM cases run only when the **Eval harness**
workflow is manually dispatched with `run_llm_evals` enabled and `GROQ_API_KEY` configured as a
repository secret.

---

## Project structure

```
├── src/
│   ├── config.py          # paths, enums, env loading
│   ├── data_loader.py     # JSON loading + account resolution (id → company fallback)
│   ├── kb.py              # markdown chunking (on ---), heading metadata, BM25 + error-code boosting
│   ├── llm.py             # Groq wrapper: temperature=0, fixed seed, JSON mode, retry/backoff, schema repair
│   ├── prompts.py         # versioned prompt templates (PROMPT_VERSION = 1.0.0)
│   ├── triage.py          # Task 1 pipeline + routing rules
│   ├── brief.py           # Task 2 pipeline: signal extraction → quote verification → synthesis
│   └── api.py             # FastAPI endpoints
├── eval/
│   ├── cases/             # test cases incl. adversarial ones
│   ├── checks.py          # rule-based checks + LLM-as-judge
│   └── run_evals.py       # runner → eval_report.json / eval_report.md
├── ui.py                  # Streamlit demo
├── demo.py                # one-command sample run
└── starter-repo-.../      # provided mock dataset (unmodified)
```

## Design decisions worth knowing

- **Account join trap:** only 4 of 500 tickets share an `account_id` with `accounts.json`, but every
  ticket `company` matches an account. The loader resolves by id first, then falls back to company
  name, so briefs work across the whole dataset.
- **90-day window:** anchored to the newest ticket timestamp *in the dataset* (2026-05-22), not wall-clock
  time — otherwise a static dataset would always produce an empty window.
- **Quote integrity:** every ticket-sourced risk quote is verified as a verbatim substring of its
  source ticket body before being included; unverifiable quotes are dropped. This is enforced again
  by the eval harness (`quotes_verified`).
- **Determinism:** temperature 0 + fixed seed + sorted/normalized outputs. The harness re-runs a brief
  twice and compares SHA-256 hashes (`brief-05-determinism`).
- **Anti-hallucination for KB citations:** triage may only cite doc sections that were actually
  retrieved for that ticket; fabricated locations are filtered out before responding.

All data is synthetic. See the provided starter README for schema details.
