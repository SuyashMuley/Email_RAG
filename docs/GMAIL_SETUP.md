# Connecting Gmail

One-time setup, about 10 minutes. The app only requests the read-only Gmail scope
(`gmail.readonly`) and nothing is sent anywhere except to Google to read your mail.

## 1. Create OAuth credentials

1. Open <https://console.cloud.google.com/> and create a project (e.g. `email-rag`).
2. **APIs & Services > Library**: enable the **Gmail API**. For the Drive bonus, also enable the **Google Drive API**.
3. **APIs & Services > OAuth consent screen** (or *Google Auth Platform*):
   - User type: **External**. Leave the app in **Testing**.
   - Add the scope `https://www.googleapis.com/auth/gmail.readonly`.
   - Under **Test users**, add every Gmail address you want to index. For the multi-user demo, add both accounts.
4. **APIs & Services > Credentials > Create credentials > OAuth client ID**, application type **Desktop app**.
5. Download the JSON and save it as `secrets/credentials.json` in this repo (the `secrets/` folder is git-ignored).

## 2. Authorize and sync

```bash
.venv/bin/rag gmail-auth                      # opens a browser; prints the authorized address
.venv/bin/rag gmail-sync -u you@gmail.com     # first run indexes the whole mailbox
.venv/bin/rag gmail-sync -u you@gmail.com     # later runs fetch only new / deleted mail
```

Useful options for `gmail-sync`:

| Option | Effect |
|---|---|
| `--query "newer_than:90d"` | Index only mail matching a Gmail search. Good for a first test. |
| `--max 200` | Stop after 200 messages. |
| `--full` | Ignore the saved cursor and re-list everything. Unchanged mail is skipped. |

`--query` and `--max` runs are partial, so they do not advance the incremental-sync cursor.
The next plain `gmail-sync` does a complete pass.

Run each account once with `gmail-auth` and `gmail-sync`; each gets its own token file
(`secrets/token_<address>.json`) and its own isolated set of documents.

## Notes

- **Testing mode tokens expire after 7 days.** If `gmail-sync` reports an auth error, run `gmail-auth` again.
  Publishing the consent screen removes the limit but is unnecessary for a demo.
- You will see an "unverified app" warning during sign-in. Choose **Advanced > Go to (app name)**.
- Spam and trash are not indexed. Messages deleted in Gmail are removed from the index on the next sync.
- Email links in answers open the message in Gmail (`https://mail.google.com/mail/u/<address>/#all/<id>`), so you
  must be signed in to that Google account in your browser.
