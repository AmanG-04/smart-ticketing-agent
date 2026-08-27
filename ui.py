import json

import streamlit as st

from src.brief import AccountNotFoundError, generate_account_brief
from src.config import MissingAPIKeyError
from src.data_loader import get_store
from src.triage import triage_ticket

st.set_page_config(page_title="Smart Ticketing Agent", page_icon="🎫", layout="wide")


def show_key_error() -> None:
    st.error(
        "GROQ_API_KEY is not configured.\n\n"
        "1. Copy `.env.example` to `.env`\n"
        "2. Paste your key from https://console.groq.com/keys\n"
        "3. Restart this app."
    )


def render_triage() -> None:
    st.subheader("Intelligent Ticket Triage")
    st.caption("Enter a ticket manually or upload a JSON object with `subject` and `body` fields.")
    upload = st.file_uploader(
        "Upload ticket JSON (optional)",
        type=["json"],
        help='Example: {"subject": "Pipeline is down", "body": "..."}',
    )
    uploaded_ticket: dict[str, str] | None = None
    if upload is not None:
        try:
            parsed = json.loads(upload.getvalue().decode("utf-8"))
            if not isinstance(parsed, dict):
                raise ValueError("The file must contain one JSON object.")
            unknown_keys = set(parsed) - {"subject", "body"}
            if unknown_keys:
                raise ValueError(
                    "Only 'subject' and 'body' are accepted. "
                    f"Unexpected fields: {', '.join(sorted(unknown_keys))}."
                )
            subject_value = parsed.get("subject", "")
            body_value = parsed.get("body", "")
            if not isinstance(subject_value, str) or not isinstance(body_value, str):
                raise ValueError("Both 'subject' and 'body' must be strings.")
            if not (subject_value.strip() or body_value.strip()):
                raise ValueError("Provide a non-empty 'subject' or 'body'.")
            uploaded_ticket = {"subject": subject_value, "body": body_value}
            st.success("Valid ticket JSON loaded. The uploaded values will be used for triage.")
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as err:
            st.error(f"Invalid ticket JSON: {err}")

    col1, col2 = st.columns(2)
    with col1:
        subject = st.text_input(
            "Subject",
            value=uploaded_ticket["subject"] if uploaded_ticket else "",
            placeholder="e.g. Webhook from CloudSync not reaching Snowflake",
            disabled=uploaded_ticket is not None,
        )
    with col2:
        body = st.text_area(
            "Ticket body",
            value=uploaded_ticket["body"] if uploaded_ticket else "",
            height=180,
            placeholder="Paste the full customer message here...",
            disabled=uploaded_ticket is not None,
        )
    ticket_payload = uploaded_ticket or {"subject": subject, "body": body}
    if st.button("Run triage", type="primary", disabled=not (ticket_payload["subject"].strip() or ticket_payload["body"].strip())):
        try:
            with st.spinner("Triaging..."):
                result = triage_ticket(ticket_payload)
        except MissingAPIKeyError:
            show_key_error()
            return
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Product", result.product)
        c2.metric("Category", result.category)
        c3.metric("Urgency", result.urgency)
        c4.metric("Confidence", f"{result.confidence:.0%}")
        st.markdown(f"**Responder team:** {result.responder_team}")
        if result.escalation_required:
            st.warning("P1 escalation required — notify the duty manager.")
        st.info(result.draft_first_response, icon="✉️")
        with st.expander("Reasoning"):
            st.write(result.reasoning)
        if result.matched_docs:
            st.markdown("**Matched knowledge-base sections**")
            for doc in result.matched_docs:
                st.markdown(f"- `{doc.location}` — {doc.why_relevant or 'relevant to this issue'}")
        else:
            st.caption("No known-issue match found in the knowledge base.")
        with st.expander("Raw JSON"):
            st.json(json.loads(result.model_dump_json()))


def render_brief() -> None:
    st.subheader("TAM Account Health Brief")
    st.caption("Auto-generates a QBR-ready brief: executive summary, open risks with verbatim ticket quotes, and recommended talking points.")
    store = get_store()
    options = sorted(store.accounts, key=lambda a: a["company"])
    labels = {f"{a['company']} ({a['account_id']}) — {a['health_status']}": a["account_id"] for a in options}
    choice = st.selectbox("Account", list(labels.keys()))
    days = st.slider("Ticket window (days)", 30, 180, 90)
    if st.button("Generate brief", type="primary"):
        try:
            with st.spinner("Analyzing tickets and drafting brief..."):
                brief = generate_account_brief(labels[choice], days=days)
        except AccountNotFoundError as err:
            st.error(str(err))
            return
        except MissingAPIKeyError:
            show_key_error()
            return
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("ARR", f"${brief.arr_usd:,}")
        c2.metric("Health", brief.health_status)
        c3.metric("Usage trend", brief.usage_trend)
        c4.metric("Tickets analyzed", brief.tickets_analyzed)
        st.caption(f"Window: {brief.window_start} → {brief.window_end}")
        st.markdown("### Executive summary")
        st.write(brief.executive_summary)
        if brief.data_gap_note:
            st.caption(brief.data_gap_note)
        st.markdown("### Open risks & flagged issues")
        if not brief.open_risks:
            st.success("No risk signals detected.")
        for risk in brief.open_risks:
            icon = "🏷️" if risk.source == "account" else "🎫"
            with st.expander(f"{icon} {risk.title}  ·  `{risk.quote_ref}`"):
                st.write(risk.detail)
                st.quote(risk.quote)
        st.markdown("### Recommended talking points")
        for i, point in enumerate(brief.recommended_talking_points, start=1):
            st.markdown(f"{i}. {point}")
        with st.expander("Raw JSON"):
            st.json(json.loads(brief.model_dump_json()))


def main() -> None:
    st.title("🎫 Smart Ticketing Agent")
    st.caption("Internal tooling demo for Technical Support & TAM teams · mock dataset")
    mode = st.sidebar.radio("Tool", ["Ticket Triage", "Account Health Brief"])
    if mode == "Ticket Triage":
        render_triage()
    else:
        render_brief()


main()
