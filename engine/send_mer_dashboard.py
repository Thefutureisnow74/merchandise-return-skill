#!/usr/bin/env python3
"""Run mer_dashboard.py and send its output to King via Telegram.

Thin wrapper, not a second copy: reuses send_dashboard.run_and_send() -- the exact same
Telegram-delivery code the whole-business /dashboard uses -- pointed at a different generator.
Used by the merchandise-return-dashboard skill.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import send_dashboard  # noqa: E402

GEN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mer_dashboard.py")

if __name__ == "__main__":
    sys.exit(send_dashboard.run_and_send(GEN))
