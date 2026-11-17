# Website support chat (backend part)

`POST /api/public/chat` takes `{"messages":[{"role":"user","content":"..."}]}` and answers from the website text only.
Code: `site_chat.py`. Nothing is stored; the conversation lives in the visitor's browser.

How it knows the website: the frontend build writes `knowledge.txt` (all public page text). This service downloads it from
`KNOWLEDGE_URL` (default `https://thegodstool.com/knowledge.txt`) and refreshes it hourly. If a refresh fails it keeps the last good copy.
After you change a page, the answer changes within an hour of the frontend deploy.

Settings (all optional, set in Railway):
- `ANTHROPIC_API_KEY`: already used by the AI features. Without it the chat says it is unavailable.
- `SITE_CHAT=off`: switches the chat off (the widget then shows an email message).
- `SITE_CHAT_DAILY_LIMIT` (default 400): total messages a day for everyone. Cost ceiling: the knowledge text is about 17,000 tokens and is
  cached by the API, so a message costs roughly 0.2 cent when the cache is warm and up to about 2 cents when it is cold (Haiku pricing).
- `SITE_CHAT_MODEL` (default `claude-haiku-4-5`), `KNOWLEDGE_URL`.

Per visitor (by IP): 20 messages an hour, 60 a day. Messages are cut to 600 characters, 8 messages of history, 450 output tokens.
Message text is never written to logs.
