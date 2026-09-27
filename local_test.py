"""Push the seed dataset into the bot in-process and print every composed message + reply scenarios."""
import json, sys
from pathlib import Path
from fastapi.testclient import TestClient
import bot
sys.stdout.reconfigure(encoding="utf-8")
c = TestClient(bot.app)
D = Path("dataset")
for f in (D / "categories").glob("*.json"):
    d = json.loads(f.read_text(encoding="utf-8")); assert c.post("/v1/context", json={"scope": "category", "context_id": d["slug"], "version": 1, "payload": d, "delivered_at": "x"}).status_code == 200
for name, scope, key in [("merchants_seed.json", "merchant", "merchant_id"), ("customers_seed.json", "customer", "customer_id"), ("triggers_seed.json", "trigger", "id")]:
    items = json.loads((D / name).read_text(encoding="utf-8"))[scope + "s"]
    for it in items:
        r = c.post("/v1/context", json={"scope": scope, "context_id": it[key], "version": 1, "payload": it, "delivered_at": "x"}); assert r.status_code == 200, r.text
print(c.get("/v1/healthz").json())
print("dup:", c.post("/v1/context", json={"scope": "category", "context_id": "dentists", "version": 1, "payload": {}, "delivered_at": "x"}).status_code)
trigs = [t["id"] for t in json.loads((D / "triggers_seed.json").read_text(encoding="utf-8"))["triggers"]]
total = 0
for i in range(0, len(trigs), 5):
    acts = c.post("/v1/tick", json={"now": "2026-04-26T10:00:00Z", "available_triggers": trigs[i:i+5]}).json()["actions"]
    for a in acts:
        total += 1
        print(f"\n[{a['trigger_id']}] ({a['send_as']}, {a['cta']})\n{a['body']}")
print("\nTOTAL ACTIONS", total, "of", len(trigs))
def rp(conv, msg, turn=2, mid="m_001_drmeera_dentist_delhi"):
    return c.post("/v1/reply", json={"conversation_id": conv, "merchant_id": mid, "customer_id": None, "from_role": "merchant", "message": msg, "received_at": "x", "turn_number": turn}).json()
for i in range(1, 5): print("AUTO", i, rp(f"conv_auto_{i}", "Thank you for contacting us! Our team will respond shortly."))
print("INTENT", rp("conv_intent_1", "Ok lets do it. Whats next?"))
print("HOSTILE", rp("conv_hostile", "Stop messaging me. This is useless spam."))
print("GST", rp("conv_x", "Btw can you also help me with my GST filing this month?"))
print("HINDI", rp("conv_y", "Haan karo"))
print("Q", rp("conv_z", "How much does it cost?"))
