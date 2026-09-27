"""Vera challenge bot — FastAPI server exposing the 5 judge endpoints."""
from __future__ import annotations

import asyncio
import os
import re
import time
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

import composer
import llm

app = FastAPI(title="Vera bot — Team Isha")
START = time.time()

contexts: dict[tuple[str, str], dict] = {}      # (scope, id) -> {"version", "payload"}
conversations: dict[str, dict] = {}             # conversation_id -> state
sent_suppression: set[str] = set()
opted_out: set[str] = set()                     # merchant/customer ids that asked us to stop
autoreply_count: dict[str, int] = {}            # merchant_id -> consecutive auto-replies
SCOPES = ("category", "merchant", "customer", "trigger")


async def _keep_awake():
    """Render's free tier sleeps after 15 min without inbound traffic; ping our own public URL every 10 min."""
    url = os.getenv("RENDER_EXTERNAL_URL")
    if not url:
        return
    while True:
        await asyncio.sleep(600)
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                await client.get(f"{url}/v1/healthz")
        except Exception:
            pass


@app.middleware("http")
async def _collapse_slashes(request: Request, call_next):
    # tolerate "https://host/" + "/v1/..." -> "//v1/..."
    path = request.scope["path"]
    if "//" in path:
        request.scope["path"] = re.sub(r"/{2,}", "/", path)
    return await call_next(request)


@app.on_event("startup")
async def _startup():
    asyncio.create_task(_keep_awake())


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def ctx(scope: str, cid: str | None) -> dict | None:
    if not cid:
        return None
    rec = contexts.get((scope, cid))
    return rec["payload"] if rec else None


# ------------------------------------------------------------------ meta endpoints

@app.get("/")
async def root():
    return {"status": "ok", "service": "vera-bot", "endpoints": ["/v1/context", "/v1/tick", "/v1/reply", "/v1/healthz", "/v1/metadata"]}


@app.get("/v1/healthz")
async def healthz():
    counts = {s: 0 for s in SCOPES}
    for (scope, _) in contexts:
        counts[scope] = counts.get(scope, 0) + 1
    return {"status": "ok", "uptime_seconds": int(time.time() - START), "contexts_loaded": counts}


@app.get("/v1/metadata")
async def metadata():
    return {
        "team_name": "Team Isha",
        "team_members": ["Isha Tiwari"],
        "model": llm.MODEL if llm.API_KEY else "rule-based (no LLM key)",
        "approach": "Deterministic per-trigger-kind composer grounded only in pushed contexts, with LLM polish "
                    "(temperature 0, fact-locked, validated, rule fallback) + rule-first reply router "
                    "(auto-reply detection, intent->action, opt-out, off-topic redirect).",
        "contact_email": "imisha1703@gmail.com",
        "version": "1.0.0",
        "submitted_at": "2026-09-27T00:00:00Z",
    }


@app.post("/v1/teardown")
async def teardown():
    contexts.clear(); conversations.clear(); sent_suppression.clear(); opted_out.clear(); autoreply_count.clear()
    return {"ok": True}


# ------------------------------------------------------------------ context

@app.post("/v1/context")
async def push_context(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"accepted": False, "reason": "invalid_json"})
    scope, cid, version, payload = body.get("scope"), body.get("context_id"), body.get("version"), body.get("payload")
    if scope not in SCOPES:
        return JSONResponse(status_code=400, content={"accepted": False, "reason": "invalid_scope", "details": str(scope)})
    if not cid or not isinstance(payload, dict):
        return JSONResponse(status_code=400, content={"accepted": False, "reason": "missing_fields"})
    try:
        version = int(version)
    except (TypeError, ValueError):
        version = 1
    cur = contexts.get((scope, cid))
    if cur and cur["version"] >= version:
        return JSONResponse(status_code=409, content={"accepted": False, "reason": "stale_version", "current_version": cur["version"]})
    contexts[(scope, cid)] = {"version": version, "payload": payload}
    return {"accepted": True, "ack_id": f"ack_{cid}_v{version}", "stored_at": now_iso()}


# ------------------------------------------------------------------ tick

def _expired(trg: dict, now: str) -> bool:
    exp = trg.get("expires_at")
    if not exp or not now:
        return False
    try:
        return datetime.fromisoformat(exp.replace("Z", "+00:00")) < datetime.fromisoformat(now.replace("Z", "+00:00"))
    except ValueError:
        return False


def _template_name(kind: str, customer: bool) -> str:
    return f"{'merchant' if customer else 'vera'}_{re.sub(r'[^a-z0-9]+', '_', kind.lower())}_v1"


TICK_BUDGET = 7.0    # seconds; judge budget is 10s — always answer in time with the deterministic draft
REPLY_BUDGET = 7.0


async def _build_action(trg_id: str, trg: dict, merchant: dict, category: dict, customer: dict | None,
                        deadline: float) -> dict:
    draft = composer.compose(category, merchant, trg, customer)
    try:
        msg = await asyncio.wait_for(llm.polish(draft, category, merchant, trg, customer),
                                     timeout=max(0.1, deadline - time.monotonic()))
    except Exception:
        msg = draft
    mid = merchant.get("merchant_id") or trg.get("merchant_id")
    cid = customer.get("customer_id") if customer else None
    conv_id = f"conv_{(cid or mid)}_{trg_id}"[:120]
    conversations[conv_id] = {"merchant_id": mid, "customer_id": cid, "trigger_id": trg_id,
                              "history": [{"from": "bot", "body": msg["body"]}], "sent": {msg["body"]},
                              "autoreplies": 0, "last_merchant_msgs": [], "ended": False}
    name = (customer or {}).get("identity", {}).get("name") or composer.salutation(merchant, category)
    return {
        "conversation_id": conv_id, "merchant_id": mid, "customer_id": cid,
        "send_as": msg["send_as"], "trigger_id": trg_id,
        "template_name": _template_name(trg.get("kind", "generic"), bool(customer)),
        "template_params": [name, msg["body"][:200], msg["cta"]],
        "body": msg["body"], "cta": msg["cta"], "suppression_key": msg["suppression_key"],
        "rationale": msg["rationale"],
    }


@app.post("/v1/tick")
async def tick(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    now = body.get("now", now_iso())
    candidates = []
    for trg_id in body.get("available_triggers", []) or []:
        trg = ctx("trigger", trg_id)
        if not trg or _expired(trg, now):
            continue
        sk = trg.get("suppression_key") or trg_id
        if sk in sent_suppression:
            continue
        mid = trg.get("merchant_id") or (trg.get("payload") or {}).get("merchant_id")
        merchant = ctx("merchant", mid)
        if not merchant:
            continue
        cust_id = trg.get("customer_id") or (trg.get("payload") or {}).get("customer_id")
        customer = ctx("customer", cust_id) if cust_id else None
        if trg.get("scope") == "customer" and not customer:
            continue  # never message a customer we have no consented context for
        if mid in opted_out or (cust_id and cust_id in opted_out):
            continue
        category = ctx("category", merchant.get("category_slug")) or {}
        candidates.append((-(trg.get("urgency") or 0), trg_id, trg, merchant, category, customer, sk))

    # restraint: at most two messages per recipient per tick, highest urgency first
    candidates.sort(key=lambda x: x[0])
    chosen, seen = [], {}
    for c in candidates:
        recipient = (c[5] or {}).get("customer_id") or c[3].get("merchant_id")
        if seen.get(recipient, 0) >= 2:
            continue
        seen[recipient] = seen.get(recipient, 0) + 1
        chosen.append(c)
        if len(chosen) >= 20:
            break

    deadline = time.monotonic() + TICK_BUDGET

    async def build(c):
        _, trg_id, trg, merchant, category, customer, sk = c
        try:
            return await _build_action(trg_id, trg, merchant, category, customer, deadline), sk
        except Exception:
            return None, sk

    # each build falls back to its rule-based draft at the deadline, so gather never loses actions
    results = await asyncio.gather(*(build(c) for c in chosen))
    actions = []
    for a, sk in results:
        if a:
            actions.append(a)
            sent_suppression.add(sk)
    return {"actions": actions}


# ------------------------------------------------------------------ reply

AUTO_REPLY = re.compile(
    r"thank(s| you) for (contacting|reaching|your message)|will (respond|reply|get back)|our team will|"
    r"automated (assistant|message|reply)|auto[- ]?reply|we are (currently )?(closed|unavailable|away)|"
    r"business hours|jaankari ke liye|team tak pahuncha|message received|abhi uplabdh nahi", re.I)
OPT_OUT = re.compile(
    r"\bstop\b|not interested|unsubscribe|don'?t (message|text|contact)|do not (message|contact)|"
    r"leave me alone|spam|useless|bothering|band karo|mat bhejo|nahi chahiye|no thanks|remove me|block", re.I)
ABUSE = re.compile(r"\b(idiot|stupid|nonsense|bakwas|shut up|fraud|scam|bloody|damn)\b", re.I)
STRONG_COMMIT = re.compile(
    r"let'?s do it|lets do|go ahead|\bproceed\b|\bconfirm(ed)?\b|please do|do it|kar do|karo|chalo|"
    r"i want to join|want to join|join karna|judna|sign me up|start (it|now)|send (it|me)|book it|"
    r"let'?s go|let'?s start", re.I)
WEAK_COMMIT = re.compile(r"^\s*(yes|yes please|yeah|yep|haan( ji)?|ha|ok(ay)?|sure|done|theek hai|thik hai|sounds good|great|perfect)\b", re.I)
SLOT_PICK = re.compile(r"^\s*[12]\s*$|^\s*(wed|thu|sat|first|second)\b", re.I)
LATER = re.compile(r"\blater\b|busy|baad mein|kal\b|tomorrow|call me|not now|abhi nahi|in a meeting", re.I)
OFF_TOPIC = re.compile(r"\bgst\b|income tax|\bitr\b|\btax(es)? filing|loan|insurance|visa|passport|electricity bill|"
                       r"aadhaar|pan card|legal notice|accountant|\bca\b", re.I)
QUESTION = re.compile(r"\?|\bhow\b|\bwhat\b|\bwhy\b|\bkya\b|\bkaise\b|\bkitna\b|\bprice\b|\bcost\b", re.I)
HINDI = re.compile(r"\b(hai|haan|nahi|kya|karo|kar|mujhe|aap|hum|chahiye|kaise|bhai|ji|theek|accha|acha|kab|kal)\b", re.I)


def _state(body: dict) -> dict:
    cid = body.get("conversation_id") or f"conv_{body.get('merchant_id')}"
    st = conversations.get(cid)
    if not st:
        st = {"merchant_id": body.get("merchant_id"), "customer_id": body.get("customer_id"), "trigger_id": None,
              "history": [], "sent": set(), "autoreplies": 0, "last_merchant_msgs": [], "ended": False}
        conversations[cid] = st
    st["merchant_id"] = st.get("merchant_id") or body.get("merchant_id")
    return st


def _unique(st: dict, text: str, alt: str) -> str:
    if text in st["sent"]:
        text = alt if alt not in st["sent"] else text + " 🙂"
    st["sent"].add(text)
    st["history"].append({"from": "bot", "body": text})
    return text


def _action_body(st: dict, hi: bool, merchant: dict, trg: dict | None) -> str:
    kind = (trg or {}).get("kind", "")
    customer_side = bool(st.get("customer_id"))
    if customer_side:
        return ("Done — booking confirmed ✅ Aapko ek din pehle reminder bhej denge." if hi
                else "Done — you're booked ✅ We'll send a reminder the day before.")
    what = {
        "research_digest": "the abstract + a patient-ed WhatsApp draft",
        "regulation_change": "the audit checklist + SOP note",
        "renewal_due": "your renewal link and invoice",
        "festival_upcoming": "the festive post + offer banner",
        "perf_dip": "a fresh Google post to lift calls",
        "perf_spike": "2 follow-up posts on the same theme",
        "review_theme_emerged": "the review-reply template + ops note",
        "milestone_reached": "the review-request WhatsApp",
        "active_planning_intent": "the final package + launch post",
        "supply_alert": "the customer note + replacement workflow",
        "competitor_opened": "the Google post highlighting your edge",
        "ipl_match_today": "the delivery banner + story",
        "curious_ask_due": "the Google post + WhatsApp reply",
        "gbp_unverified": "the verification steps",
        "cde_opportunity": "your registration",
    }.get(kind, "the draft")
    name = composer.salutation(merchant, None) if merchant else ""
    if hi:
        return (f"Done{', ' + name if name else ''} — {what} abhi bana rahi hoon, 10 min mein yahin bhej dungi. "
                "Review karke bas CONFIRM likh dena, main live kar dungi.")
    return (f"Done{', ' + name if name else ''} — sending {what} here in the next 10 minutes. "
            "Review it and reply CONFIRM, and I'll publish it right away.")


@app.post("/v1/reply")
async def reply(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"action": "end", "rationale": "malformed request"})
    msg = (body.get("message") or "").strip()
    st = _state(body)
    mid = st.get("merchant_id")
    merchant = ctx("merchant", mid) or {}
    trg = ctx("trigger", st.get("trigger_id"))
    hi = bool(HINDI.search(msg)) or (composer.uses_hindi(merchant) and not re.fullmatch(r"[A-Za-z0-9 ,.'!?-]+", msg or "x"))
    st["history"].append({"from": body.get("from_role", "merchant"), "body": msg})
    who = st.get("customer_id") or mid

    if st.get("ended"):
        return {"action": "end", "rationale": "Conversation already closed; not re-engaging."}

    # 1) hostile / opt-out -> exit gracefully
    if OPT_OUT.search(msg):
        st["ended"] = True
        if who:
            opted_out.add(who)
        return {"action": "end",
                "rationale": "Merchant signalled opt-out/frustration; closing politely and suppressing future sends."}
    if ABUSE.search(msg) and not OFF_TOPIC.search(msg):
        text = ("Maaf kijiye agar messages zyada lage. Main sirf wahi bhejungi jo aapke business ke kaam ka ho — "
                "aage continue karein ya yahin rok doon? Reply STOP to pause." if hi else
                "Sorry if these felt like too much. I'll only send what's useful for your business — "
                "reply STOP and I'll pause right away.")
        return {"action": "send", "body": _unique(st, text, "Understood — reply STOP anytime and I'll pause."),
                "cta": "binary_yes_no", "rationale": "Frustration without explicit opt-out: one calm apology + clear exit path, no pitch."}

    # 2) auto-reply detection (pattern or verbatim repeat, tracked per merchant across conversations)
    repeat = msg and st["last_merchant_msgs"].count(msg) >= 1
    st["last_merchant_msgs"].append(msg)
    key = mid or body.get("conversation_id")
    if AUTO_REPLY.search(msg) or repeat:
        autoreply_count[key] = autoreply_count.get(key, 0) + 1
        n = autoreply_count[key]
        if n == 1:
            text = _unique(st, "Lagta hai yeh auto-reply hai 🙂 Owner/manager dekhein toh bas 'YES' reply kar dein — baaki main sambhal lungi."
                           if composer.uses_hindi(merchant) else
                           "Looks like an auto-reply 🙂 When the owner sees this, just reply YES and I'll take it from there.",
                           "Owner ke liye: just reply YES whenever convenient.")
            return {"action": "send", "body": text, "cta": "binary_yes_no",
                    "rationale": "Detected WhatsApp Business auto-reply; one short flag for the owner, no pitch repeated."}
        if n == 2:
            return {"action": "wait", "wait_seconds": 86400,
                    "rationale": "Second consecutive auto-reply — owner not at phone; backing off 24h instead of burning turns."}
        st["ended"] = True
        return {"action": "end", "rationale": f"Auto-reply {n}x with no human response; closing conversation to avoid spam."}
    autoreply_count[key] = 0

    # 3) off-topic -> polite decline + redirect
    if OFF_TOPIC.search(msg):
        topic = "GST/tax" if re.search(r"gst|tax|itr|\bca\b", msg, re.I) else "that"
        back = (f"wapas {composer.human(trg.get('kind'))} pe aate hain" if trg and hi else
                f"back to the {composer.human(trg.get('kind'))} item" if trg else
                ("aapki Google listing pe wapas aate hain" if hi else "back to growing your listing"))
        text = (f"{topic} ke liye aapke CA best rahenge — woh mere scope se bahar hai. Chaliye {back} — main agla step shuru karoon? Reply YES."
                if hi else
                f"{topic} is best handled by your CA — that's outside what I can do. Coming {back} — shall I start the next step? Reply YES.")
        return {"action": "send", "body": _unique(st, text, text.replace("Reply YES", "Just say YES")),
                "cta": "binary_yes_no", "rationale": "Out-of-scope request declined politely; redirected to the original mission."}

    # 4) explicit intent / commitment -> action mode immediately (no re-qualifying)
    stripped = re.sub(r"what'?s next\??|ab kya\??|next step\??", "", msg, flags=re.I)
    if (STRONG_COMMIT.search(msg) or (WEAK_COMMIT.search(msg) and not QUESTION.search(stripped))
            or (st.get("customer_id") and SLOT_PICK.search(msg))):
        text = _action_body(st, hi, merchant, trg)
        return {"action": "send", "body": _unique(st, text, text.replace("10 minutes", "a few minutes").replace("10 min", "kuch der")),
                "cta": "binary_confirm_cancel",
                "rationale": "Merchant committed; switched from pitch to execution with a concrete deliverable and single CONFIRM."}

    # 5) not now -> wait
    if LATER.search(msg):
        return {"action": "wait", "wait_seconds": 3600 * 4,
                "rationale": "Merchant asked for time; backing off 4h before a light follow-up."}

    # 6) anything else -> LLM grounded reply, fallback rule
    category = ctx("category", merchant.get("category_slug")) or {}
    context = {"merchant": {k: merchant.get(k) for k in ("identity", "performance", "offers", "signals", "customer_aggregate")},
               "category_voice": category.get("voice"), "trigger": trg,
               "customer": ctx("customer", st.get("customer_id"))}
    out = None
    try:
        out = await asyncio.wait_for(llm.reply(context, st["history"], msg), timeout=REPLY_BUDGET)
    except Exception:
        out = None
    if out:
        text = out["body"]
        rationale = out.get("rationale", "Grounded continuation toward one next step.")
    else:
        text = ("Samajh gayi. Main isse aapke profile data ke saath check karke ek short draft bana deti hoon — bhej doon? Reply YES."
                if hi else
                "Got it. I'll check this against your profile data and put together a short draft — shall I send it? Reply YES.")
        rationale = "Acknowledged merchant's message; offered a concrete low-effort next step."
    return {"action": "send", "body": _unique(st, text, text + " (no rush)"), "cta": "open_ended", "rationale": rationale}
