#!/usr/bin/env python3
"""
reply_classify.py — classify an inbound vendor/regulator reply on a live return case and
decide the response + which autonomy lane it takes.

This is the "replies auto-analyzed and auto-replied" half of the engine. In production the
classification is a Claude judgment call (reserved for judgment per the cost model); the
heuristic below is the deterministic fallback + the demo harness so the routing is testable
without a live model. Output feeds:
  - a RECORD-ONLY classification comment on the case's Multica issue (wake-agent safe)
  - the response draft + the lane it must go out on (green/yellow/red)

Categories: refund · partial · refused · needs_info · legal_threat · discrimination_signal · other
"""
import re
import sys

CATEGORIES = ["refund", "partial", "refused", "needs_info", "legal_threat",
              "discrimination_signal", "other"]

# heuristic signals (fallback / demo). Live path: Claude classifies with full thread context.
# NOTE: refusal/threat/discrimination are checked BEFORE refund/partial, because a refusal
# sentence often contains the word "refund" ("unable to offer a refund"). Priority order matters.
PRIORITY = ["legal_threat", "discrimination_signal", "refused", "needs_info", "partial", "refund"]
SIGNALS = {
    "legal_threat":         [r"\bcease and desist\b", r"\b(our|refer(red)? to) (legal|counsel|attorney)\b", r"\blitigation\b", r"\bdefamation\b"],
    "discrimination_signal":[r"\bbecause of your\b", r"\bwe don'?t serve\b", r"\bpeople like you\b", r"\brefuse service\b"],
    "refused":              [r"\bdenied?\b", r"\bunable to\b", r"\bcannot (help|assist|refund)\b", r"\bout of warranty\b", r"\bfinal decision\b", r"\bno refund\b", r"\bwe are unable\b"],
    "needs_info":           [r"\bplease (provide|send|confirm)\b", r"\bwe need\b", r"\breceipt\b", r"\bserial\b", r"\border number\b", r"\bmore information\b"],
    "partial":              [r"\bpartial\b", r"\bstore credit\b", r"\bgoodwill\b", r"\b\d+% (off|refund)\b", r"\bone-time\b"],
    "refund":               [r"\brefund (has been |is |will be )?(approved|issued|processed)\b", r"\bfull credit\b", r"\breplacement (is )?on its way\b", r"\bwe will (refund|replace)\b"],
}

# per-category: (next action, lane)  — lane: green auto / yellow veto-window / red explicit-YES
ROUTING = {
    "refund":                ("Confirm receipt of the resolution; move case toward CLOSE (user confirms).", "red"),
    "partial":               ("Draft a reply that evaluates the offer vs the demand; ASK the user whether to accept.", "red"),
    "refused":               ("Draft the next-tier escalation (retention/exec) referencing the refusal.", "yellow"),
    "needs_info":            ("Draft a reply supplying the requested info if we already have it (serial/receipt/photos).", "yellow"),
    "legal_threat":          ("STOP auto-anything. Summarize for the user; no reply without explicit review.", "red"),
    "discrimination_signal": ("Preserve the exact wording as evidence; flag the civil-rights track (Tier 3-D).", "red"),
    "other":                 ("Summarize; ask the user how to proceed.", "red"),
}


def classify(text):
    """Return (category, matched_signal) using the heuristic. Live path swaps in Claude."""
    t = text.lower()
    for cat in PRIORITY:  # refusal/threat/discrimination before refund/partial
        for pat in SIGNALS.get(cat, []):
            if re.search(pat, t):
                return cat, pat
    return "other", None


def route(text):
    cat, sig = classify(text)
    action, lane = ROUTING[cat]
    return {"category": cat, "signal": sig, "next_action": action, "lane": lane}


DEMO = [
    ("T-Mobile", "Unfortunately the device is out of warranty and we are unable to offer a refund. This is our final decision."),
    ("PPG", "Please provide the original receipt and the serial number so we can look into this."),
    ("Vendor", "As a one-time goodwill gesture we can offer you 30% off a replacement."),
    ("Samsung", "Your replacement is on its way and a full refund has been approved."),
    ("Store", "We reserve the right to refuse service to people like you."),
    ("Counsel", "Any further contact will be referred to our legal department for litigation."),
]


def main():
    print("=== reply_classify demo (heuristic fallback; live path uses Claude) ===\n")
    lane_icon = {"green": "🟢", "yellow": "🟡", "red": "🔴"}
    for who, msg in DEMO:
        r = route(msg)
        print(f"{lane_icon[r['lane']]} [{r['category']:>22}]  from {who}")
        print(f"     reply: {msg[:70]}")
        print(f"     → {r['next_action']}  (lane: {r['lane'].upper()})\n")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] != "--demo":
        import json
        print(json.dumps(route(" ".join(sys.argv[1:])), indent=2))
    else:
        main()
