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

**Why this file exists.** On 2026-07-29 a live Google OAuth token — `refresh_token`,
`client_secret`, `token` — was found in `engine/`, giving full read **and send** on the
author's own mailbox, alongside a `profile.json` with a real legal name, phone and mailing
address. It was not tracked by git and had no `.gitignore` protecting it. It has been moved to
`~/.mer/` and `profile.json:google_token_file` repointed there.

**It happened again, to somebody else.** On 2026-07-31 a recipient ran this file's own scan on
their install and it returned two files belonging to a **third party** — a `profile.json` with
that person's legal name, email, phone and city, and a token JSON with `client_id`,
`client_secret`, `refresh_token` and an access token: full read and send on a stranger's
mailbox, sitting on a different stranger's machine. Neither file was in the public repo or
anywhere in its history, so git was not the carrier. That is the point worth keeping: the
`.gitignore` did its job and the leak travelled anyway, because **a folder handed to somebody
is a copy that no `.gitignore` has ever seen.** Run the scan below before any hand-off, not
just before a push. A refresh token stays valid until it is revoked at
`myaccount.google.com/permissions` — time does not expire it and a password change does not
stop it.

Credentials live **outside** every skill tree. The skill holds a *pointer*, never a secret.
