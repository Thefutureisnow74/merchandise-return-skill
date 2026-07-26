#!/usr/bin/env python3
"""
idempotency.py — the skill-level send guard (Blueprint M22).

Makes a double-send structurally impossible, regardless of which code path triggers a send.
This is the generalized fix for the 2026-07-18 bank-double-email incident: instead of relying on
a per-cron flag file (which only protects one script), EVERY outbound in the engine must pass
through reserve() first. If the same logical send was already made, reserve() returns False and
the caller must NOT send.

A "logical send" is keyed by (case, action, recipient, body-fingerprint) so that:
  - re-running the tick / a retry / a second code path with the same message → blocked
  - a genuinely different message (new tier, new recipient, changed body) → allowed

Ledger: a single JSON file, atomically updated. Persists across runs and restarts.
"""
import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone

LEDGER = os.environ.get("MER_SENT_LEDGER", "/opt/data/scripts/mer_sent_ledger.json")


def _load():
    try:
        with open(LEDGER, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _atomic_save(data):
    d = os.path.dirname(LEDGER) or "."
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1, ensure_ascii=False)
        os.replace(tmp, LEDGER)  # atomic on POSIX
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def send_key(case, action, recipient, body):
    """Stable key for a logical send. Body is fingerprinted (whitespace-normalized)."""
    body_fp = hashlib.sha256(" ".join((body or "").split()).encode("utf-8")).hexdigest()[:12]
    raw = "|".join([str(case), str(action), (recipient or "").lower().strip(), body_fp])
    return "%s|%s|%s" % (case, action, hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16])


def reserve(case, action, recipient, body, meta=None):
    """
    Atomic check-and-set. Returns (ok_to_send, key):
      - (True, key)  → this send is NEW; caller may send. Recorded now so a concurrent/later
                        identical attempt is blocked.
      - (False, key) → already sent before; caller MUST NOT send.
    """
    key = send_key(case, action, recipient, body)
    data = _load()
    if key in data:
        return False, key
    data[key] = {
        "case": case, "action": action, "recipient": recipient,
        "at": datetime.now(timezone.utc).isoformat(),
        **(meta or {}),
    }
    _atomic_save(data)
    return True, key


def was_sent(case, action, recipient, body):
    return send_key(case, action, recipient, body) in _load()


if __name__ == "__main__":
    # self-test: the same logical send is allowed once, blocked the second time
    os.environ.setdefault("MER_SENT_LEDGER", os.path.join(tempfile.gettempdir(), "mer_ledger_test.json"))
    LEDGER = os.environ["MER_SENT_LEDGER"]
    if os.path.exists(LEDGER):
        os.remove(LEDGER)
    ok1, k1 = reserve("CASE-3", "tier1", "consumer.affairs@ppg.com", "Dear PPG, please refund...")
    ok2, k2 = reserve("CASE-3", "tier1", "consumer.affairs@ppg.com", "Dear PPG, please refund...")  # dup
    ok3, k3 = reserve("CASE-3", "tier2", "contact@ppgac.com", "Escalating to your executives...")   # new
    print("1st identical send  -> ok_to_send =", ok1, "(expect True)")
    print("2nd identical send  -> ok_to_send =", ok2, "(expect False — blocked)")
    print("different send      -> ok_to_send =", ok3, "(expect True)")
    assert ok1 and not ok2 and ok3, "IDEMPOTENCY SELF-TEST FAILED"
    print("PASS — double-send is structurally blocked; distinct sends pass.")
