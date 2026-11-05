# FFINFO Bot — Render

Telegram bot: `/ffinfo <UID>` fetches Free Fire player info from the WPM API and replies with a formatted card.

## Deploy on Render

Build command: `pip install -r requirements.txt`
Start command: `python main.py`

Environment variables:
- `BOT_TOKEN` — token from BotFather
- `WPM_API_KEY` — your (rotated) API key
- `PLAYER_API_URL` — `https://wpmtopup.com/api/info_main.php`
- `TARGET_GROUP` — optional numeric chat ID to restrict group use to one group (private chats always work)
- `PLAYER_API_REGION_PARAM` — optional; set only if the API accepts a region parameter
- `COOLDOWN_SECONDS` — optional per-user cooldown (default 5)

### Option A — Background Worker (paid, most reliable)
Create a **Background Worker**. No port needed.

### Option B — Web Service (free plan)
Create a **Web Service**. The bot detects Render's `PORT` and starts a tiny health endpoint automatically.
Free web services sleep after ~15 min without HTTP traffic, so point UptimeRobot / cron-job.org at your
`https://<service>.onrender.com` URL every 5–10 minutes.

## Commands
- `/start`
- `/ffinfo <UID>` — e.g. `/ffinfo 123456789` (works in groups and in private chat; region is fixed to Bangladesh)

## Notes
- The API's real JSON shape could not be verified; the parser accepts the common `data.basicInfo.nickname` shape and top-level `nickname`/`name`. If fields show `N/A`, send a sample response and adjust `build_player_message`.
- Run only ONE instance of the bot per token, otherwise Telegram returns a "Conflict" error.

## Security
Never commit `.env` or keys. If the API key was ever pasted in chat or a URL, rotate it and store the new one only in Render Environment Variables.
