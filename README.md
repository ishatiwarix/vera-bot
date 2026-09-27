# Vera Bot — Team Isha

HTTP bot for the magicpin Vera challenge. Endpoints: `POST /v1/context`, `POST /v1/tick`, `POST /v1/reply`, `GET /v1/healthz`, `GET /v1/metadata` (+ optional `POST /v1/teardown`).

**Approach**
- `composer.py` — deterministic composer dispatched by `trigger.kind` (25+ kinds + generic fallback). Every number, name, date, price and citation is read from the pushed category/merchant/trigger/customer contexts — nothing is invented. Hindi-English code-mix when the merchant speaks Hindi / customer prefers hi-en. Single CTA as the last sentence.
- `llm.py` — optional polish via an OpenAI-compatible LLM (Groq `openai/gpt-oss-120b` or Gemini `gemini-2.0-flash`, temperature 0). Output is validated (no URLs, no taboo words, length) and falls back to the deterministic draft on any error/timeout/rate-limit, so every call stays well under 30s.
- `bot.py` — versioned context store (409 on stale), tick with expiry + suppression dedup + max 2 sends per recipient per tick, and a rule-first reply router: opt-out → end; auto-reply (pattern or verbatim repeat) → one nudge → wait 24h → end; commitment ("let's do it", "haan karo") → action mode immediately; off-topic (GST etc.) → polite decline + redirect; "later" → wait; everything else → grounded LLM reply.

**Run locally**: `pip install -r requirements.txt && uvicorn bot:app --port 8080` · test: `python local_test.py`
**Env**: `LLM_API_KEY` (Groq `gsk_...` or Gemini `AIza...`; optional — bot works rule-based without it).
