"""Deterministic, context-grounded message composer for Vera.

Every number / name in a message is read from the pushed contexts — nothing is invented.
compose() returns a rule-based message; llm.py may optionally polish it.
"""
from __future__ import annotations

import re
from datetime import datetime, date

# ---------------------------------------------------------------- helpers

def g(d, *path, default=None):
    for p in path:
        if isinstance(d, dict):
            d = d.get(p)
        elif isinstance(d, list) and isinstance(p, int) and -len(d) <= p < len(d):
            d = d[p]
        else:
            return default
        if d is None:
            return default
    return d


def pct(x) -> str:
    try:
        return f"{abs(float(x)) * 100:.0f}%"
    except (TypeError, ValueError):
        return str(x)


def num(x) -> str:
    try:
        f = float(x)
        return f"{int(f):,}" if f == int(f) else f"{f:,.1f}"
    except (TypeError, ValueError):
        return str(x)


def human(s) -> str:
    return re.sub(r"(\d+)day\b", r"\g<1>-day", str(s or "").replace("_", " ").strip())


def parse_date(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(str(s)[:10])
        except ValueError:
            return None


def nice_date(s) -> str:
    d = parse_date(s)
    return d.strftime("%-d %b" if False else "%d %b").lstrip("0") if d else str(s)


def months_between(a, b) -> int | None:
    if not a or not b:
        return None
    return max(0, (b.year - a.year) * 12 + b.month - a.month - (1 if b.day < a.day else 0))


def uses_hindi(merchant) -> bool:
    return "hi" in (g(merchant, "identity", "languages", default=[]) or [])


def customer_hinglish(customer) -> bool:
    lp = str(g(customer, "identity", "language_pref", default="")).lower()
    return "hi" in lp


def salutation(merchant, category) -> str:
    first = g(merchant, "identity", "owner_first_name")
    slug = (category or {}).get("slug") or merchant.get("category_slug", "")
    if first:
        return f"Dr. {first}" if slug == "dentists" and not first.startswith("Dr") else first
    return g(merchant, "identity", "name", default="there")


def biz_name(merchant) -> str:
    return g(merchant, "identity", "name", default="your business")


def short_biz(merchant) -> str:
    name = biz_name(merchant)
    return name.split("'s ")[0] + "'s" if "'s " in name else name


def active_offers(merchant) -> list[str]:
    return [o.get("title") for o in merchant.get("offers", []) or [] if o.get("status") == "active" and o.get("title")]


def catalog(category) -> list[str]:
    return [o.get("title") for o in (category or {}).get("offer_catalog", []) or [] if o.get("title")]


def best_offer(merchant, category, keywords=(), strict=False) -> str | None:
    pool = active_offers(merchant) + catalog(category)
    for kw in keywords:
        for t in pool:
            if kw.lower() in t.lower():
                return t
    return None if strict else (active_offers(merchant) or pool or [None])[0]


def digest_item(category, item_id):
    for d in (category or {}).get("digest", []) or []:
        if d.get("id") == item_id:
            return d
    return None


def first_sentence(s: str) -> str:
    s = (s or "").strip()
    for sep in [". ", "; "]:
        if sep in s:
            return s.split(sep)[0].rstrip(".") + "."
    return s


def peer(category, key):
    return g(category, "peer_stats", key)


def locality(merchant) -> str:
    return g(merchant, "identity", "locality", default="") or g(merchant, "identity", "city", default="")


# ---------------------------------------------------------------- per-kind composers
# each returns (body, cta, rationale)

def _research(c, m, t, cu, hi):
    p = t.get("payload", {})
    item = digest_item(c, p.get("top_item_id") or p.get("digest_item_id")) or p.get("top_item") or {}
    s = salutation(m, c)
    title = item.get("title", human(p.get("topic", "this week's research item")))
    src = item.get("source", "")
    bits = [f"{s}, this week's digest has one item worth 2 minutes: {title}"]
    if item.get("trial_n"):
        bits[0] += f" ({num(item['trial_n'])}-patient trial)"
    bits[0] += "."
    summ = first_sentence(item.get("summary", ""))
    if summ:
        bits.append(summ)
    hr = g(m, "customer_aggregate", "high_risk_adult_count")
    seg = item.get("patient_segment", "")
    if hr and "high_risk" in str(seg):
        bits.append(f"Directly relevant — you have {num(hr)} high-risk adults in your roster.")
    elif item.get("actionable"):
        bits.append(item["actionable"] + ".")
    ask = ("Abstract + ek patient-friendly WhatsApp draft bhej doon? Reply YES." if hi
           else "Want me to send the abstract + a patient-friendly WhatsApp draft? Reply YES.")
    bits.append(ask)
    if src:
        bits.append(f"— {src}")
    return " ".join(bits), "binary_yes_no", f"Research digest item '{title}' ({src}) tied to merchant's own cohort; reciprocity + single YES CTA."


def _compliance(c, m, t, cu, hi):
    p = t.get("payload", {})
    item = digest_item(c, p.get("top_item_id") or p.get("digest_item_id")) or {}
    s = salutation(m, c)
    title = item.get("title", human(t.get("kind")))
    dl = p.get("deadline_iso")
    body = f"{s}, heads-up on a compliance change: {title}."
    if item.get("summary"):
        body += " " + ". ".join(item["summary"].split(". ")[:2]).rstrip(".") + "."
    if dl:
        body += f" Deadline: {nice_date(dl)}."
    body += (" Main aapke liye 5-point audit checklist bana doon, SOP note ke saath? Reply YES."
             if hi else " Want a 5-point audit checklist + SOP note drafted for your clinic? Reply YES.")
    if item.get("source"):
        body += f" — {item['source']}"
    return body, "binary_yes_no", f"Regulation change with a hard deadline ({dl}); loss-aversion + effort externalisation."


def _recall(c, m, t, cu, hi):
    p = t.get("payload", {})
    name = g(cu, "identity", "name", default="there")
    hx = customer_hinglish(cu) if cu else hi
    last = parse_date(p.get("last_service_date") or g(cu, "relationship", "last_visit"))
    due = parse_date(p.get("due_date"))
    months = months_between(last, due) if last and due else None
    service = human(p.get("service_due", "check-up")).replace("6 month", "6-month")
    slots = [s.get("label") for s in p.get("available_slots", []) or [] if s.get("label")]
    offer = best_offer(m, c, ("clean", "check"))
    body = f"Hi {name}, {biz_name(m)} here 🦷 Your {service} is due"
    body += f" (last visit {nice_date(last.isoformat())})." if last else "."
    if slots:
        opts = " ya ".join(slots[:2]) if hx else " or ".join(slots[:2])
        body += (f" Aapke liye slots ready hain: {opts}." if hx else f" Slots open for you: {opts}.")
    if offer:
        body += f" {offer}."
    if len(slots) >= 2:
        body += " Reply 1 for the first slot, 2 for the second, or send a time that suits you."
    else:
        body += " Reply YES to book, or tell us a time that suits you."
    return body, "multi_choice_slot" if len(slots) >= 2 else "binary_yes_no", \
        f"Customer recall on merchant's behalf; real slots + catalog price; language pref '{g(cu,'identity','language_pref')}' honoured."


def _perf(c, m, t, cu, hi, up: bool):
    p = t.get("payload", {})
    s = salutation(m, c)
    metric = human(p.get("metric", "views"))
    d = pct(p.get("delta_pct"))
    window = p.get("window", "7d").replace("d", " days")
    base = p.get("vs_baseline")
    if up:
        body = f"{s}, good news — your {metric} are up {d} in the last {window}"
        body += f" (vs a baseline of {num(base)})." if base else "."
        if p.get("likely_driver"):
            body += f" Looks driven by your {human(p['likely_driver'])}."
        body += (" Is momentum ko double karein — same theme pe 2 aur posts draft kar doon? Reply YES."
                 if hi else " Want me to draft 2 more posts on the same theme while it's working? Reply YES.")
        return body, "binary_yes_no", "Perf spike: reinforce what's working; curiosity + effort externalisation."
    body = f"{s}, your {metric} dropped {d} in the last {window}"
    body += f" (baseline {num(base)})." if base else "."
    ctr, pctr = g(m, "performance", "ctr"), peer(c, "avg_ctr")
    if ctr and pctr and ctr < pctr:
        body += f" Your CTR is {ctr*100:.1f}% vs {pctr*100:.1f}% peer average — that's the gap to close."
    stale = next((x for x in m.get("signals", []) if str(x).startswith("stale_posts")), None)
    if stale and ":" in stale:
        body += f" Last Google post was {stale.split(':')[1].replace('d', ' days')} ago."
    offer = active_offers(m)
    if offer:
        body += f" Quick fix: a fresh post pushing '{offer[0]}'."
    body += (" Draft karke bhej doon? Reply YES." if hi else " Shall I draft it now? Reply YES.")
    return body, "binary_yes_no", "Perf dip with peer benchmark; loss aversion + one concrete fix."


def _renewal(c, m, t, cu, hi):
    p = t.get("payload", {})
    s = salutation(m, c)
    days = p.get("days_remaining", g(m, "subscription", "days_remaining"))
    plan = p.get("plan", g(m, "subscription", "plan", default=""))
    amt = p.get("renewal_amount")
    v, calls = g(m, "performance", "views"), g(m, "performance", "calls")
    body = f"{s}, your {plan} plan ends in {days} days."
    if v:
        body += f" Last 30 days on it: {num(v)} profile views, {num(calls)} calls."
    if amt:
        body += f" Renewal is ₹{num(amt)}."
    body += (" Renew kar doon taaki listing boost ruke nahi? Reply YES." if hi
             else " Want me to renew so the listing boost doesn't pause? Reply YES.")
    return body, "binary_yes_no", "Renewal due; value recap from real numbers + loss aversion."


def _festival(c, m, t, cu, hi):
    p = t.get("payload", {})
    s = salutation(m, c)
    fest = p.get("festival", "the festival")
    days = p.get("days_until")
    offer = best_offer(m, c)
    body = f"{s}, {fest} is on {nice_date(p.get('date'))}"
    body += f" — {days} days out." if days is not None else "."
    if days and int(days) > 45:
        body += " Early, but festive search starts 3-4 weeks before; getting your offer listed first is the edge."
    if offer:
        body += f" I'd lead with '{offer}' as the {fest} hook."
    body += (" Festive post + offer banner draft kar doon? Reply YES." if hi
             else " Want me to draft the festive post + offer banner? Reply YES.")
    return body, "binary_yes_no", f"{fest} upcoming; service+price offer from catalog; single CTA."


def _wedding(c, m, t, cu, hi):
    p = t.get("payload", {})
    name = g(cu, "identity", "name", default="there")
    owner = g(m, "identity", "owner_first_name")
    days = p.get("days_to_wedding")
    step = human(p.get("next_step_window_open", "next bridal session"))
    offer = best_offer(m, c, ("skin", "facial", "bridal package", "bridal makeup"), strict=True)
    body = f"Hi {name} 💍 {owner + ' from ' if owner else ''}{biz_name(m)} here."
    if days:
        body += f" {days} days to your wedding ({nice_date(p.get('wedding_date'))})"
    if p.get("trial_completed"):
        body += f" — and your trial on {nice_date(p['trial_completed'])} went well."
    body += f" Now is the right window to start the {step}."
    if offer:
        body += f" {offer}."
    body += " Shall I block a slot for your first session next week? Reply YES."
    return body, "binary_yes_no", "Bridal follow-up on merchant's behalf; wedding countdown + next step."


def _curious(c, m, t, cu, hi):
    s = salutation(m, c)
    body = (f"{s}, quick one — is hafte {short_biz(m)} pe sabse zyada kaunsi service poochi gayi? "
            "Aapke answer se main ek Google post + customers ke liye 4-line WhatsApp reply bana dungi. 5 min ka kaam.") if hi else \
        (f"{s}, quick one — which service got asked about most at {short_biz(m)} this week? "
         "I'll turn your answer into a Google post + a 4-line WhatsApp reply for customers. 5 minutes.")
    return body, "open_ended", "Curiosity/asking-the-merchant lever; reciprocity offered upfront."


def _winback_merchant(c, m, t, cu, hi):
    p = t.get("payload", {})
    s = salutation(m, c)
    body = f"{s}, it's been {p.get('days_since_expiry', '')} days since your plan lapsed."
    if p.get("perf_dip_pct") is not None:
        body += f" Profile views are down {pct(p['perf_dip_pct'])} since."
    if p.get("lapsed_customers_added_since_expiry"):
        body += f" And {p['lapsed_customers_added_since_expiry']} customers have gone inactive in that time."
    body += (" Reactivate karke unke liye winback message draft kar doon? Reply YES." if hi
             else " Want me to reactivate and draft a winback message for them? Reply YES.")
    return body, "binary_yes_no", "Win-back: loss aversion using real lapse numbers."


def _ipl(c, m, t, cu, hi):
    p = t.get("payload", {})
    s = salutation(m, c)
    t_iso = p.get("match_time_iso", "")
    tm = ""
    try:
        tm = datetime.fromisoformat(t_iso).strftime("%I:%M%p").lstrip("0").lower()
    except ValueError:
        pass
    offer = active_offers(m)
    body = f"{s}, {p.get('match', 'IPL match')} at {p.get('venue', '')} tonight{', ' + tm if tm else ''}."
    if p.get("is_weeknight") is False:
        body += " Weekend match — dine-in usually thins as people watch at home, so delivery is the play."
    else:
        body += " Weeknight match — expect a pre-match dine-in rush and post-match delivery spike."
    if offer:
        body += f" Push your '{offer[0]}' as a match-night delivery special."
    body += (" Swiggy/Zomato banner + story draft kar doon? Reply YES." if hi
             else " Want me to draft the delivery banner + story? Reply YES.")
    return body, "binary_yes_no", "IPL match today; channel judgement (dine-in vs delivery) + existing offer."


def _review_theme(c, m, t, cu, hi):
    p = t.get("payload", {})
    s = salutation(m, c)
    theme = human(p.get("theme"))
    body = f"{s}, {p.get('occurrences_30d', 'several')} reviews in the last 30 days mention '{theme}'"
    body += f" and it's {p['trend']}." if p.get("trend") else "."
    if p.get("common_quote"):
        body += f" Typical line: \"{p['common_quote']}\"."
    body += (" Ek polite public reply template + ops fix note draft kar doon? Reply YES." if hi
             else " Want me to draft a public reply template + a quick ops fix note? Reply YES.")
    return body, "binary_yes_no", "Review theme emerging; social proof (negative) + concrete remedy."


def _milestone(c, m, t, cu, hi):
    p = t.get("payload", {})
    s = salutation(m, c)
    metric = human(p.get("metric", "reviews")).replace("review count", "reviews")
    now, goal = p.get("value_now"), p.get("milestone_value")
    body = f"{s}, you're at {num(now)} {metric} — just {num(goal - now) if isinstance(now, (int, float)) and isinstance(goal, (int, float)) else ''} away from {num(goal)}."
    body += (" Pichle happy customers ko ek short review-request WhatsApp bhej doon? Reply YES." if hi
             else " Want me to send a short review-request WhatsApp to recent happy customers? Reply YES.")
    return body, "binary_yes_no", "Milestone within reach; goal-gradient + effort externalisation."


def _planning(c, m, t, cu, hi):
    p = t.get("payload", {})
    s = salutation(m, c)
    topic = human(p.get("intent_topic", "the plan"))
    offers = active_offers(m) + catalog(c)
    body = f"{s}, here's a starter draft for the {topic} — edit freely:\n"
    for o in offers[:3]:
        body += f"• {o}\n"
    if not offers:
        body += "• Core package + intro price\n"
    body += f"• Promote via Google post + WhatsApp broadcast to your {num(g(m, 'customer_aggregate', 'total_unique_ytd', default='') or '')} customers\n".replace("your  customers", "customers")
    body += ("Isko final karke launch post bana doon? Reply YES." if hi else "Shall I finalise this and prep the launch post? Reply YES.")
    return body, "binary_yes_no", f"Merchant already said '{p.get('merchant_last_message', '')}' — action mode, drafted artifact, no re-qualifying."


def _seasonal_dip(c, m, t, cu, hi):
    p = t.get("payload", {})
    s = salutation(m, c)
    metric = human(p.get("metric", "views"))
    body = f"{s}, your {metric} are down {pct(p.get('delta_pct'))} this week — but this is the expected seasonal lull ({human(p.get('season_note', ''))}), not a problem with your listing."
    active = g(m, "customer_aggregate", "total_active_members") or g(m, "customer_aggregate", "active_members")
    if active:
        body += f" Best use of this window: retention for your {num(active)} active members."
    else:
        body += " Best use of this window: retention over acquisition."
    body += (" Ek 4-week attendance challenge draft kar doon? Reply YES." if hi
             else " Want me to draft a 4-week attendance challenge? Reply YES.")
    return body, "binary_yes_no", "Seasonal dip reframed to reduce anxiety; redirect to retention."


def _cust_winback(c, m, t, cu, hi):
    p = t.get("payload", {})
    name = g(cu, "identity", "name", default="there")
    owner = g(m, "identity", "owner_first_name")
    days = p.get("days_since_last_visit")
    focus = human(p.get("previous_focus", ""))
    offer = best_offer(m, c, ("trial", "free", "month"))
    body = f"Hi {name} 👋 {owner + ' from ' if owner else ''}{biz_name(m)} here."
    if days:
        body += f" It's been {days} days — happens to everyone, no judgement."
    if focus:
        body += f" Whenever you're ready to get back to your {focus} goals, we're here."
    if offer:
        body += f" {offer}."
    body += " Want me to hold a spot for you this week? Reply YES — no commitment."
    return body, "binary_yes_no", "Lapsed customer win-back on merchant's behalf; no-shame framing."


def _trial_followup(c, m, t, cu, hi):
    p = t.get("payload", {})
    name = g(cu, "identity", "name", default="there")
    opts = [o.get("label") for o in p.get("next_session_options", []) if o.get("label")]
    body = f"Hi {name}, {biz_name(m)} here 🙂 Thanks for coming in for the trial on {nice_date(p.get('trial_date'))}."
    if opts:
        body += f" Next session: {opts[0]}."
    body += " Shall we book it? Reply YES."
    return body, "binary_yes_no", "Trial follow-up with a concrete next session."


def _supply_alert(c, m, t, cu, hi):
    p = t.get("payload", {})
    s = salutation(m, c)
    item = digest_item(c, p.get("alert_id")) or {}
    batches = ", ".join(p.get("affected_batches", []))
    body = f"{s}, urgent: recall on {p.get('molecule', '')} batches {batches} ({p.get('manufacturer', '')})."
    if item.get("summary"):
        body += " " + first_sentence(item["summary"])
    chronic = g(m, "customer_aggregate", "chronic_rx_count")
    if chronic:
        body += f" Worth checking which of your {num(chronic)} chronic-Rx customers got these batches."
    body += (" Customer WhatsApp note + replacement workflow draft kar doon? Reply YES." if hi
             else " Want me to draft the customer note + replacement workflow? Reply YES.")
    return body, "binary_yes_no", "Supply recall; urgency + batch specifics + end-to-end workflow offer."


def _refill(c, m, t, cu, hi):
    p = t.get("payload", {})
    name = g(cu, "identity", "name", default="")
    mols = ", ".join(p.get("molecule_list", []))
    runout = nice_date(p.get("stock_runs_out_iso"))
    offers = active_offers(m)
    hx = customer_hinglish(cu) if cu else hi
    greet = f"Namaste{' ' + name if name else ''} — {biz_name(m)} here."
    body = greet + (f" Aapki medicines ({mols}) {runout} ko khatam ho rahi hain." if hx
                    else f" Your medicines ({mols}) run out on {runout}.")
    body += " Same dose, same pack ready."
    if offers:
        body += " " + "; ".join(offers[:2]) + "."
    if p.get("delivery_address_saved"):
        body += " Delivery to your saved address."
    body += " Reply CONFIRM to dispatch, or tell us if the dosage changed."
    return body, "binary_confirm_cancel", "Chronic refill due; exact molecules + run-out date + merchant's real offers."


def _category_seasonal(c, m, t, cu, hi):
    p = t.get("payload", {})
    s = salutation(m, c)
    trends = []
    for tr in p.get("trends", []):
        parts = tr.rsplit("_", 1)
        trends.append(f"{human(parts[0])} {parts[1]}%" if len(parts) == 2 else human(tr))
    body = f"{s}, {human(p.get('season', 'season'))} demand shift is here: " + ", ".join(trends) + "."
    body += (" Shelf + Google listing ko iske hisaab se update kar doon? Reply YES." if hi
             else " Want me to update your listing highlights to match? Reply YES.")
    return body, "binary_yes_no", "Seasonal category trend with explicit deltas; actionable restock/listing."


def _gbp_unverified(c, m, t, cu, hi):
    p = t.get("payload", {})
    s = salutation(m, c)
    body = f"{s}, your Google profile is still unverified — verified listings typically see ~{pct(p.get('estimated_uplift_pct', 0.3))} more visibility."
    body += f" Verification is via {human(p.get('verification_path', 'postcard or phone'))}."
    body += (" Steps main guide kar doon, 5 min lagega? Reply YES." if hi
             else " I can walk you through it in 5 minutes. Reply YES.")
    return body, "binary_yes_no", "Unverified GBP; loss aversion + low effort."


def _cde(c, m, t, cu, hi):
    p = t.get("payload", {})
    s = salutation(m, c)
    item = digest_item(c, p.get("digest_item_id")) or {}
    body = f"{s}, {item.get('title', 'a CDE session')}"
    if p.get("credits"):
        body += f" — {p['credits']} CDE credits"
    if p.get("fee"):
        body += f", {human(p['fee'])}"
    body += "."
    if item.get("summary"):
        body += " " + first_sentence(item["summary"])
    body += " Want me to register you? Reply YES."
    return body, "binary_yes_no", "CDE opportunity; credits + fee specifics, single CTA."


def _competitor(c, m, t, cu, hi):
    p = t.get("payload", {})
    s = salutation(m, c)
    body = f"{s}, a new {c.get('display_name', 'clinic').rstrip('s').lower() if c else 'competitor'} — {p.get('competitor_name')} — opened {p.get('distance_km')} km from you"
    body += f" on {nice_date(p.get('opened_date'))}." if p.get("opened_date") else "."
    if p.get("their_offer"):
        body += f" They're leading with '{p['their_offer']}'."
    mine = active_offers(m)
    if mine:
        body += f" Yours is '{mine[0]}' — worth sharpening how you show value."
    body += (" Ek comparison-proof Google post draft kar doon? Reply YES." if hi
             else " Want me to draft a Google post that highlights your edge? Reply YES.")
    return body, "binary_yes_no", "Competitor opened nearby (from trigger payload); loss aversion + concrete response."


def _dormant(c, m, t, cu, hi):
    p = t.get("payload", {})
    s = salutation(m, c)
    v = g(m, "performance", "views")
    body = f"{s}, it's been {p.get('days_since_last_merchant_message', 'a while')} days since we spoke."
    if v:
        body += f" Meanwhile your profile got {num(v)} views in the last 30 days."
    body += (" Ek quick 3-point update bhej doon — kya chal raha hai, kya fix karna hai? Reply YES." if hi
             else " Want a quick 3-point update on what's working and what to fix? Reply YES.")
    return body, "binary_yes_no", "Dormant merchant; reciprocity + curiosity, low-effort CTA."


def _generic(c, m, t, cu, hi):
    p = t.get("payload", {}) or {}
    s = salutation(m, c) if not cu else f"Hi {g(cu, 'identity', 'name', default='there')}"
    facts = []
    for k, v in list(p.items())[:4]:
        if isinstance(v, (str, int, float)) and not str(k).endswith("_id"):
            facts.append(f"{human(k)}: {human(v) if isinstance(v, str) else num(v)}")
    body = f"{s}, quick update on {human(t.get('kind'))}" + (f" — {'; '.join(facts)}." if facts else ".")
    body += " Want me to take care of the next step? Reply YES."
    return body, "binary_yes_no", f"Trigger {t.get('kind')} with payload facts."


KIND_MAP = {
    "research_digest": _research, "category_research_digest_release": _research, "research_digest_release": _research,
    "regulation_change": _compliance, "compliance": _compliance,
    "recall_due": _recall, "customer_lapsed_soft": _recall, "appointment_tomorrow": _recall,
    "perf_dip": lambda *a: _perf(*a, up=False), "perf_spike": lambda *a: _perf(*a, up=True),
    "renewal_due": _renewal, "festival_upcoming": _festival,
    "wedding_package_followup": _wedding, "bridal_followup": _wedding,
    "curious_ask_due": _curious, "scheduled_recurring": _curious,
    "winback_eligible": _winback_merchant, "ipl_match_today": _ipl,
    "review_theme_emerged": _review_theme, "milestone_reached": _milestone,
    "active_planning_intent": _planning, "seasonal_perf_dip": _seasonal_dip,
    "customer_lapsed_hard": _cust_winback, "trial_followup": _trial_followup,
    "supply_alert": _supply_alert, "chronic_refill_due": _refill,
    "category_seasonal": _category_seasonal, "gbp_unverified": _gbp_unverified,
    "cde_opportunity": _cde, "competitor_opened": _competitor, "dormant_with_vera": _dormant,
}


def compose(category: dict, merchant: dict, trigger: dict, customer: dict | None = None) -> dict:
    category = category or {}
    kind = trigger.get("kind", "")
    hi = uses_hindi(merchant)
    fn = KIND_MAP.get(kind)
    if fn is None and trigger.get("scope") == "customer":
        fn = _recall if g(trigger, "payload", "available_slots") else _cust_winback
    fn = fn or _generic
    try:
        body, cta, rationale = fn(category, merchant, trigger, customer, hi)
    except Exception:  # never fail a send because of an odd payload
        body, cta, rationale = _generic(category, merchant, trigger, customer, hi)
    body = " ".join(body.split(" ")).replace("  ", " ").strip()
    for bad in (category.get("voice", {}) or {}).get("vocab_taboo", []) or []:
        body = body.replace(bad, "")
    is_customer = bool(customer) or trigger.get("scope") == "customer"
    return {
        "body": body,
        "cta": cta,
        "send_as": "merchant_on_behalf" if is_customer else "vera",
        "suppression_key": trigger.get("suppression_key") or f"{kind}:{merchant.get('merchant_id')}",
        "rationale": rationale,
    }
