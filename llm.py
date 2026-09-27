"""Optional LLM polish layer (OpenAI-compatible: Groq or Gemini). Falls back silently."""
from __future__ import annotations

import asyncio
import json
import os
import re
import time

import httpx

API_KEY = os.getenv("LLM_API_KEY", "").strip()
if API_KEY.startswith("AIza"):
    BASE_URL = os.getenv("LLM_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai")
    MODEL = os.getenv("LLM_MODEL", "gemini-2.0-flash")
else:
    BASE_URL = os.getenv("LLM_BASE_URL", "https://api.groq.com/openai/v1")
    MODEL = os.getenv("LLM_MODEL", "openai/gpt-oss-120b")

TIMEOUT = float(os.getenv("LLM_TIMEOUT", "9"))
_sem = asyncio.Semaphore(int(os.getenv("LLM_CONCURRENCY", "4")))
_cooldown_until = 0.0
URL_RE = re.compile(r"https?://|www\.", re.I)

COMPOSE_SYSTEM = """You are Vera, magicpin's merchant-growth assistant on WhatsApp for Indian local businesses.
You are given a DRAFT message plus the exact context facts. Rewrite the draft into the best possible WhatsApp message.

HARD RULES:
- Use ONLY facts, numbers, names, dates, prices and sources present in FACTS or DRAFT. Never invent data, competitors, citations or offers.
- Keep every concrete number/date/price that the draft uses, and if the draft ends with a source citation ("— <source>"), keep it verbatim at the end.
- Voice: match the category tone (dentists: peer-clinical, address as "Dr. <name>"; salons: warm-practical; restaurants: operator-to-operator; gyms: coach; pharmacies: precise, trustworthy). No hype, no "AMAZING", no taboo words.
- If merchant languages include "hi" (or customer language_pref mentions hi), use natural Hindi-English code-mix (Roman script). Otherwise English.
- No URLs. No long preamble, no self-introduction beyond one short clause. Concise (under ~420 characters unless the draft is a list/plan).
- Exactly ONE call-to-action and it must be the last sentence (keep the draft's CTA type, e.g. "Reply YES").
- Customer-facing messages (send_as=merchant_on_behalf) speak as the merchant's business, never as Vera.
Return ONLY JSON: {"body": "...", "rationale": "<one sentence: why this message, why now, which levers>"}"""

REPLY_SYSTEM = """You are Vera, magicpin's merchant-growth assistant on WhatsApp. Continue the conversation.
RULES: use only facts in CONTEXT (never invent numbers, names, offers). Be brief (1-3 sentences), helpful, peer tone.
If the merchant asked a question, answer it from context or say honestly you'll check. Move toward one concrete next step.
Do not re-introduce yourself. Do not repeat a previous message. No URLs. Match the merchant's language (Hindi-English mix if they use Hindi).
End with a single clear question or CTA.
Return ONLY JSON: {"body": "...", "rationale": "<one sentence>"}"""


def enabled() -> bool:
    return bool(API_KEY) and time.time() >= _cooldown_until


async def _chat(system: str, user: str) -> dict | None:
    global _cooldown_until
    if not enabled():
        return None
    async with _sem:
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                r = await client.post(
                    f"{BASE_URL}/chat/completions",
                    headers={"Authorization": f"Bearer {API_KEY}"},
                    json={"model": MODEL, "temperature": 0, "max_tokens": 1500,
                          **({"reasoning_effort": "low"} if "gpt-oss" in MODEL else {}),
                          "response_format": {"type": "json_object"},
                          "messages": [{"role": "system", "content": system},
                                       {"role": "user", "content": user}]},
                )
            if r.status_code == 429:
                _cooldown_until = time.time() + 30
                return None
            r.raise_for_status()
            text = r.json()["choices"][0]["message"]["content"]
            m = re.search(r"\{[\s\S]*\}", text)
            return json.loads(m.group()) if m else None
        except Exception:
            return None


def _ok(body: str, taboos: list[str]) -> bool:
    if not body or len(body) < 30 or len(body) > 1200 or URL_RE.search(body):
        return False
    low = body.lower()
    return not any(t.lower() in low for t in taboos or [])


def _facts(category, merchant, trigger, customer) -> dict:
    cat = category or {}
    item_ids = {v for k, v in (trigger.get("payload") or {}).items() if k.endswith("item_id") or k == "alert_id"}
    return {
        "category": {"slug": cat.get("slug"), "voice": cat.get("voice"), "peer_stats": cat.get("peer_stats"),
                     "relevant_digest": [d for d in cat.get("digest", []) if d.get("id") in item_ids]},
        "merchant": {k: merchant.get(k) for k in ("identity", "subscription", "performance", "offers",
                                                  "customer_aggregate", "signals", "review_themes")},
        "recent_conversation": (merchant.get("conversation_history") or [])[-3:],
        "trigger": trigger,
        "customer": customer,
    }


async def polish(draft: dict, category, merchant, trigger, customer) -> dict:
    user = json.dumps({"send_as": draft["send_as"], "DRAFT": draft["body"],
                       "FACTS": _facts(category, merchant, trigger, customer)}, ensure_ascii=False, default=str)
    out = await _chat(COMPOSE_SYSTEM, user[:14000])
    taboos = ((category or {}).get("voice") or {}).get("vocab_taboo", [])
    if out and _ok(out.get("body", ""), taboos):
        return {**draft, "body": out["body"].strip(),
                "rationale": (out.get("rationale") or draft["rationale"]).strip()}
    return draft


async def reply(context: dict, history: list, message: str) -> dict | None:
    user = json.dumps({"CONTEXT": context, "HISTORY": history[-8:], "MERCHANT_MESSAGE": message},
                      ensure_ascii=False, default=str)
    out = await _chat(REPLY_SYSTEM, user[:14000])
    if out and _ok(out.get("body", ""), []):
        return out
    return None
