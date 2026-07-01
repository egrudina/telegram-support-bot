# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the bot

**Locally:**
```bash
pip install -r requirements.txt
python main.py
```

**Docker:**
```bash
docker compose up --build
```

Both require a `.env` file in the root directory (see below).

## Required environment variables

```
TELEGRAM_TOKEN=              # bot token from @BotFather
TELEGRAM_SUPPORT_CHAT_ID=   # group chat ID where messages are forwarded (must be integer, negative for groups)
PERSONAL_ACCOUNT_CHAT_ID=   # personal Telegram user ID (always required, even if not used as forwarding target)
FORWARD_MODE=support_chat   # "support_chat" or "personal_account"
```

Optional: `WELCOME_MESSAGE`, `HEROKU_APP_NAME`, `REPLY_TO_THIS_MESSAGE`, `WRONG_REPLY`.

`settings.py` raises at startup if `TELEGRAM_TOKEN`, `TELEGRAM_SUPPORT_CHAT_ID`, or `PERSONAL_ACCOUNT_CHAT_ID` are missing or non-numeric.

## Architecture

Three files do all the work:

- **`settings.py`** — loads and validates env vars; raises `Exception` on missing required values.
- **`handlers.py`** — three async handlers using `python-telegram-bot` v21 (async API):
  - `start` — replies with `WELCOME_MESSAGE` on `/start`
  - `forward_to_group` — forwards user messages to the support chat or personal account; stores `forwarded_msg.message_id → user_id` in `context.bot_data` (in-memory, lost on restart)
  - `forward_to_user` — when support replies to a forwarded message, looks up the original user via `context.bot_data` and sends the reply back
- **`main.py`** — wires up the `Application`, registers handlers with filters, starts polling, and handles graceful shutdown via `asyncio` signals. Uses `asyncio.get_event_loop().run_until_complete()` instead of `asyncio.run()` for Python 3.8 compatibility.

**Message routing logic:** `main.py` registers `forward_to_group` for all non-command text messages *not* from `TELEGRAM_SUPPORT_CHAT_ID`, and `forward_to_user` for reply messages *from* `TELEGRAM_SUPPORT_CHAT_ID`. The `PERSONAL_ACCOUNT_CHAT_ID` filter is commented out in both handlers — currently only `support_chat` mode routes replies back to users.

**State:** `context.bot_data` (a plain dict on the `Application` object) maps forwarded message IDs to originating user IDs. This state is ephemeral — bot restarts lose all pending reply mappings.

**UI language:** Error and status messages sent to support operators are in Ukrainian.
