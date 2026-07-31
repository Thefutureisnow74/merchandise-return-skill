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

**It happened again, to somebody else, after this file was written.** A recipient ran the scan
above on their own install and it returned two files belonging to a **third party** — a
`profile.json` with that person's legal name, email, phone and city, and a token JSON with
`client_id`, `client_secret`, `refresh_token` and an access token: full read and send on one
stranger's mailbox, sitting on a different stranger's machine.

Neither file was in the public repo or anywhere in its history. The `.gitignore` did its job and
the credentials travelled anyway, which is the part worth keeping: **a folder handed to somebody
is a copy no `.gitignore` has ever seen.** Run the scan before any hand-off — a zip, a copied
directory, a shared machine, a screen-share — not only before a push.

And if a token has already travelled, deleting the file is not enough. **A Google refresh token
stays valid until it is revoked** at `myaccount.google.com/permissions`. Time does not expire it
and changing the password does not stop it. Revoking is the only thing that does, and only the
account's owner can do it.

Credentials live **outside** every skill tree. The skill holds a *pointer*, never a secret.
