# Before you hand this folder to anyone

SKILL.md §7's distribution model is "hand the folder to anyone, it runs on THEIR Multica with
THEIR identity." That only holds if the folder carries no identity and no secrets.

**Check these every time, before zipping or sharing:**

```bash
find . -name "*token*.json" -o -name "profile.json" -o -name ".env" -o -name "*.pem"
grep -rIl "refresh_token\|client_secret\|BEGIN .*PRIVATE KEY" . 2>/dev/null
```

Both must come back empty. `profile.example.json` is the template that ships; `profile.json` is
the recipient's own and is created by `onboard.py` on their machine.

**Why this file exists.** A live Google OAuth token — `refresh_token`, `client_secret`, `token` —
was once found sitting in `engine/`, giving full read **and send** on the owner's mailbox, next to
a `profile.json` carrying their legal name, phone and mailing address. It was not tracked by git
and had no `.gitignore` protecting it. Credentials now live outside every skill tree and
`profile.json:google_token_file` holds only a pointer.

This file is in the distributable package, so it names no mailbox, no person and no path on
anyone's machine. That is the rule it exists to enforce, applied to itself.

Credentials live **outside** every skill tree. The skill holds a *pointer*, never a secret.
