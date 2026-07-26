#!/usr/bin/env python3
"""
fresh_profile_check.py — M41, the automatable half: does the engine actually run as SOMEONE ELSE?

The product promise (BLUEPRINT §1) is that anyone can install this skill and run it under their
own name, mailbox and Multica board. Until that is *tested*, it is an assumption — and the whole
point of §12 is that an untested guarantee is not a guarantee.

This check plants a profile for a fictional user, then asks the engine's identity-bearing modules
who they think they are. Every answer must be the fictional user's. If ANY identity-shaped value
that is not that profile's own survives into a resolved value — a built-in default, a leftover
literal, a previous operator — portability is broken and this fails.

It also pins the M41 contract from the other side: with NO profile and no env override, the send
and read paths must raise a clear, catchable error naming the fix, never quietly resolve to
somebody's address.

Each probe runs in its OWN subprocess. That is deliberate: `mer_config` caches the profile in a
process-level singleton, so importing twice in one interpreter would test the cache rather than
the resolution path.

What this canNOT prove (the other half of M41, which needs a real board + mailbox):
  * that a fresh Multica board can actually be provisioned  (M35 / onboard.py)
  * that a case can be driven to a Tier-1 send end-to-end under that profile

Run:  python fresh_profile_check.py          (offline, no network, writes only to a temp dir)
"""
import json
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------------------------------------
# THE NEGATIVE CONTROL, without naming anybody.
#
# This used to be a list of the author's real email, name, phone and Multica workspace UUID —
# the very values the check exists to keep out of other people's installs. Publishing them to
# prove they don't leak is self-defeating, so the control is now expressed as SHAPES: any
# address-, phone-, UUID- or token-file-shaped value that appears in a resolved answer must be
# one of the installed profile's OWN values. Anything else — a leftover default, a hardcoded
# literal, a previous operator's identity — is a leak by construction, whoever it belongs to.
# This is strictly stronger than the old list, which could only catch one specific person.
# ---------------------------------------------------------------------------------------------
IDENTITY_SHAPES = [
    ("email address", re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")),
    ("phone number", re.compile(r"\+?\d[\d\s().-]{8,}\d")),
    ("uuid", re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
                        r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")),
    ("token file", re.compile(r"[\w./-]*_token\.json")),
]

STRANGER = {
    "legal_name": "Priya N. Raman",
    "email": "priya.raman@example.net",
    "phone": "+15125550143",
    "mailing_address": "88 Wharf Street\nProvidence, RI 02903",
    "state": "Rhode Island",
    "county": "Providence",
    "multica_workspace_id": "ffffffff-0000-4444-8888-cccccccccccc",
    "multica_project_id": None,
    "notify": {"channel": "telegram", "telegram_chat_id": "555000111"},
    "signature_block": "Priya N. Raman\npriya.raman@example.net",
    "google_token_file": "/srv/returns/priya_google_token.json",
}

# Each probe: (label, python expression printing the resolved value, must-equal)
PROBES = [
    ("mer_config.email()",
     "import mer_config;print(mer_config.email())",
     STRANGER["email"]),
    ("mer_config.legal_name()",
     "import mer_config;print(mer_config.legal_name())",
     STRANGER["legal_name"]),
    ("mer_config.state()/county()",
     "import mer_config;print(mer_config.state()+'/'+mer_config.county())",
     "Rhode Island/Providence"),
    ("mer_config.multica_workspace_id()",
     "import mer_config;print(mer_config.multica_workspace_id())",
     STRANGER["multica_workspace_id"]),
    # The send choke point — this is the one that actually decides whose name mail leaves under.
    ("gmail_transport.DEFAULT_SENDER",
     "import gmail_transport as g;print(g.DEFAULT_SENDER)",
     STRANGER["email"]),
    ("gmail_transport.DEFAULT_TOKEN_FILE",
     "import gmail_transport as g;print(g.DEFAULT_TOKEN_FILE)",
     STRANGER["google_token_file"]),
    # How an outbound letter is actually signed.
    ("nudge.sender_signature()",
     "import nudge;print(nudge.sender_signature().replace(chr(10),' | '))",
     "Priya N. Raman | priya.raman@example.net"),
]


def _strings(v):
    """Every string anywhere inside the profile dict."""
    if isinstance(v, str):
        yield v
    elif isinstance(v, dict):
        for x in v.values():
            yield from _strings(x)
    elif isinstance(v, (list, tuple)):
        for x in v:
            yield from _strings(x)


# The loose phone shape above is right for a short resolved VALUE but far too greedy for a whole
# source file or traceback (it happily matches a date or a line number), so scanning bulk text
# uses the strict subset: an address, an E.164 number, a UUID, a token-file path.
STRICT_SHAPES = [(n, rx) for n, rx in IDENTITY_SHAPES if n != "phone number"]
STRICT_SHAPES.append(("phone number", re.compile(r"\+\d{10,15}")))


def _shaped(text, shapes=None):
    """Every identity-shaped token in a piece of text, as (shape, value) pairs."""
    for shape, rx in (shapes or IDENTITY_SHAPES):
        for m in rx.findall(text or ""):
            yield shape, m.strip()


# Everything the INSTALLED profile is allowed to say about itself.
OWN_VALUES = set()
for _s in _strings(STRANGER):
    OWN_VALUES.add(_s.strip())
    for _shape, _v in _shaped(_s):
        OWN_VALUES.add(_v)


def foreign_identity_in(text):
    """Any identity-shaped value in `text` that does NOT belong to the installed profile.

    That is the whole property under test: with a profile installed, nothing identifying may
    come from anywhere else — not a built-in default, not a leftover literal, not a previous
    operator. Expressed by shape so it holds for every name, not just one.
    """
    return sorted({v for _shape, v in _shaped(text) if v not in OWN_VALUES})


def run(expr, env):
    p = subprocess.run([sys.executable, "-c", "import sys;sys.path.insert(0,%r);%s" % (HERE, expr)],
                       capture_output=True, text=True, env=env, timeout=120)
    return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()


def main():
    results = []

    def check(label, ok, detail=""):
        results.append((label, ok, detail))
        print("  %-4s %-42s %s" % ("PASS" if ok else "FAIL", label, detail))

    tmp = tempfile.mkdtemp(prefix="mer_fresh_")
    profile_path = os.path.join(tmp, "profile.json")
    with open(profile_path, "w", encoding="utf-8") as f:
        json.dump(STRANGER, f, indent=2)

    env = dict(os.environ)
    env["MER_PROFILE"] = profile_path
    env["PYTHONIOENCODING"] = "utf-8"
    env["MER_ENGINE_SEND"] = "off"          # belt and braces: nothing here may ever send
    # Scrub the env overrides so we prove the PROFILE is doing the work, not a leftover variable.
    for k in ("MER_SENDER", "MER_GOOGLE_TOKEN_FILE"):
        env.pop(k, None)

    print("=== M41 fresh-profile check — can the engine run as someone who is not King? ===")
    print("profile: %s\n" % profile_path)

    for label, expr, expected in PROBES:
        rc, out, err = run(expr, env)
        if rc != 0:
            check(label, False, "process exited %d: %s" % (rc, err.splitlines()[-1] if err else "?"))
            continue
        if out != expected:
            check(label, False, "got %r, expected %r" % (out, expected))
            continue
        leaked = foreign_identity_in(out)
        check(label, not leaked,
              "LEAKED a value that is not this profile's: %s" % leaked if leaked else out[:46])

    # ------------------------------------------------------------------------------------------
    # THE NO-PROFILE CONTRACT (M41). This probe used to assert the opposite — that with no
    # profile the engine silently resolved to the author's own address, so the config layer was
    # safe to drop onto his running box. A real profile is installed now and the engine ships to
    # strangers, so that behaviour is a defect: an unconfigured install must FAIL LOUDLY and name
    # the fix, never quietly send as whoever wrote it.
    # ------------------------------------------------------------------------------------------
    env2 = dict(os.environ)
    env2["PYTHONIOENCODING"] = "utf-8"
    env2["MER_ENGINE_SEND"] = "off"
    for k in ("MER_PROFILE", "MER_SENDER", "MER_GOOGLE_TOKEN_FILE"):
        env2.pop(k, None)
    # Run from an EMPTY dir — the planted ./profile.json must not be discoverable either.
    bare = os.path.join(tmp, "bare")
    os.makedirs(bare, exist_ok=True)
    rc, out, err = run(
        "import os;os.chdir(%r);import gmail_transport as g;print(g.DEFAULT_SENDER)" % bare, env2)
    blob = out + "\n" + err
    check("no profile -> the send path REFUSES to resolve a sender", rc != 0 and not out,
          "resolved %r instead of raising" % out if out else "raised")
    check("no profile -> the error is MerConfigError (catchable)",
          "MerConfigError" in blob, blob.splitlines()[-1][:60] if blob.strip() else "(no output)")
    for needle, why in (("onboard.py", "names onboard.py"),
                        ("MER_PROFILE", "names $MER_PROFILE"),
                        ("NO built-in identity", "says there is no built-in identity"),
                        ("profile.example.json", "points at the schema")):
        check("no-profile error %s" % why, needle in blob)
    check("no profile -> no identity-shaped value is emitted at all",
          not [v for _s, v in _shaped(blob, STRICT_SHAPES) if "example." not in v],
          str([v for _s, v in _shaped(blob, STRICT_SHAPES)])[:60])

    # The read side must refuse for the same reason, with the same error.
    rc, out, err = run(
        "import os;os.chdir(%r);import gmail_fetch as f;print(f.DEFAULT_TOKEN_FILE)" % bare, env2)
    check("no profile -> the read path REFUSES to resolve a mailbox",
          rc != 0 and not out and "MerConfigError" in (out + err),
          "resolved %r" % out if out else "raised")

    # Belt and braces for the public repo: the three identity-bearing modules must contain no
    # real-looking personal literal at all. Obvious fiction (example.* domains, 555 numbers,
    # all-f/0 UUIDs) is allowed — that is documentation, not a person.
    def _fiction(v):
        return ("example." in v or "555" in v or "_token.json" in v
                or set(v.lower()) <= set("f0-") or "0000-4000" in v or "0000-0000" in v)

    for fname in ("mer_config.py", "gmail_transport.py", "gmail_fetch.py"):
        with open(os.path.join(HERE, fname), encoding="utf-8") as fh:
            src = fh.read()
        found = sorted({v for _s, v in _shaped(src, STRICT_SHAPES) if not _fiction(v)})
        check("%s carries no personal literal" % fname, not found, "found %s" % found)

    failed = [r for r in results if not r[1]]
    print("\n%s — %d/%d probes passed" %
          ("PASS" if not failed else "FAIL", len(results) - len(failed), len(results)))
    if failed:
        print("Portability is NOT proven: %s" % ", ".join(l for l, _, _ in failed))
        return 1
    print("The engine answers as the profile's owner at every identity-bearing call site,\n"
          "including the send choke point and the letter signature.")
    print("\nStill unproven by this check (needs a real board + mailbox): workspace provisioning\n"
          "(M35/onboard.py) and driving a case to a Tier-1 send under that profile.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
