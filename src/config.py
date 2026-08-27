import os
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
STARTER_DIR = ROOT_DIR / "starter-repo-20260826T101459Z-1-001" / "starter-repo"
DATA_DIR = STARTER_DIR / "data"
KB_DIR = STARTER_DIR / "knowledge-base"

TICKETS_PATH = DATA_DIR / "tickets.json"
ACCOUNTS_PATH = DATA_DIR / "accounts.json"

load_dotenv(ROOT_DIR / ".env")

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
# Compound Mini has a 70K token/minute free-tier limit (vs 8K for GPT-OSS), which
# keeps the interactive demo responsive while retaining reliable tool-free JSON output.
GROQ_MODEL = os.getenv("GROQ_MODEL", "groq/compound-mini").strip()
LLM_TEMPERATURE = 0.0
LLM_SEED = 42

PRODUCTS = [
    "DataBridge Pro",
    "CloudSync",
    "AnalyticsHub",
    "SecureVault",
    "WorkflowEngine",
]

CATEGORIES = [
    "Bug",
    "Feature Request",
    "How-To",
    "Performance",
    "Billing",
    "Integration",
    "Onboarding",
    "Data Loss",
]

URGENCY_TIERS = ["P1", "P2", "P3", "P4"]


class MissingAPIKeyError(RuntimeError):
    pass


def require_api_key() -> str:
    if not GROQ_API_KEY:
        raise MissingAPIKeyError(
            "GROQ_API_KEY is not set. Copy .env.example to .env and add your key "
            "from https://console.groq.com/keys"
        )
    return GROQ_API_KEY
