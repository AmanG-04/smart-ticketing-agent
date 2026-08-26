# Design Note

## Failure modes in production

**1. Retrieval misses the right KB section.** BM25 over chunked markdown is strong on keyword overlap
(error codes, product names) but weak when a customer describes a symptom in different words than the
docs ("money taken twice" vs "duplicate invoice"). *Detect:* the eval harness already scores
retrieval hit-rate; in production I would log `known_issue_match=false` rates per product area and
alert on drift. *Mitigate:* hybrid retrieval (BM25 + embeddings), query rewriting by the LLM, and a
feedback loop where support engineers flag wrong citations.

**2. Silent schema drift / hallucinated fields.** The pipeline depends on the model returning valid
structured JSON; a model upgrade or prompt edit can subtly break it (e.g., urgency drifting to P2 for
everything). *Detect:* every response passes Pydantic validation, and the harness re-runs
classification cases on each commit — CI catches regressions before deploy. *Mitigate:* enum-typed
schemas with one repair retry, then deterministic fallback routing; prompt versions are tracked so a
regression bisects quickly.

**3. Quote/grounding fabrication.** A summariser that paraphrases a customer quote into something they
never said destroys TAM trust and creates real business risk. *Detect:* quotes are mechanically
verified as verbatim substrings of source tickets before display — unverifiable quotes are dropped
rather than shown. *Mitigate:* this same check runs as an always-on guardrail (`quotes_verified`),
not just at eval time.

## Latency vs quality

Task 1 makes two sequential decisions that could be one: retrieval happens *before* classification
(the classifier sees only retrieved docs), so a bad retrieval caps answer quality. Merging them
(ask the model to both search and classify) would cut latency ~40% but produced worse grounding in
experiments-style reasoning: without pre-filtered context the model invents doc references. If
latency were the hard constraint I would keep two stages but cache retrievals by ticket-product
similarity, shrink context to the top-2 chunks, and stream the draft response so agents see output
in under a second while classification completes.

## Data sensitivity

Real ticket data contains PII (names, emails, contract details). The design isolates all outbound
calls behind `src/llm.py`, giving one choke point to enforce: redaction of email addresses, phone
numbers and account ids before dispatch; a self-hosted or VPC-deployed model as an alternative
provider; zero logging of raw bodies (logs carry ticket IDs only); and API responses scoped to
internal auth tokens. The mock dataset is synthetic, but nothing in the code path assumes that —
the same guarantees would hold with production data. No API key is ever committed; `.env` is
gitignored and `.env.example` documents required variables.

## Scaling to 10× volume

First bottleneck: **LLM call cost/rate limits**, since every ticket triggers at least one Groq
request and briefs trigger two-plus. Mitigations: batch triage calls, route obvious P4 how-to
tickets to a smaller fine-tuned model, cache brief sections between QBR cycles. Second:
**BM25 is in-memory but single-process** — at 50k tickets the account scan in `DataStore` (linear
per-account filtering) degrades; I would index tickets by resolved account once at startup (already
done) and move to SQLite/DuckDB for persistence. Third: the quote verifier does normalized substring
search per flag — cheap now, but worth replacing with token-level diffing if tickets average several
KB. The stateless FastAPI layer itself scales horizontally.
