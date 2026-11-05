# Telegram Auto Forward Scheduler Bot (Railway)

Saves every post from one source channel into a library and copies posts to your destination channels in timed batches. When a channel's queue runs out, it keeps going with **random posts from the library, without repeating until every post has been sent once**, then starts a new cycle.

## Deploy on Railway
1. Connect this GitHub repository to Railway.
2. **Add a Volume mounted at `/data` and set `DATABASE_PATH=/data/bot.db`** — otherwise the queue and channel list are lost on every redeploy.
3. Add Railway Variables (see below).
4. Add the bot to the source channel (admin recommended) and as admin (post permission) in every destination channel.

## Variables
| Variable | Default | Meaning |
|---|---|---|
| `BOT_TOKEN` | – | Token from BotFather |
| `ADMIN_IDS` | – | Comma-separated Telegram user IDs allowed to use commands |
| `SOURCE_CHANNEL_ID` | – | Source channel ID (starts with `-100`) |
| `DATABASE_PATH` | `./data/bot.db` | Use `/data/bot.db` with a Railway volume |
| `DEFAULT_INTERVAL_MINUTES` | `200` | Wait after each batch (new channels) |
| `BATCH_SIZE` | `12` | Posts per batch |
| `POST_DELAY_SECONDS` | `3` | Pause between posts inside a batch (avoids rate limits) |
| `POLL_SECONDS` | `10` | Scheduler check interval |
| `TIMEZONE` | `Asia/Kolkata` | Timezone for quiet hours |
| `QUIET_START_HOUR` / `QUIET_END_HOUR` | `0` / `7` | No sending from 00:00 until 07:00 (set both equal to disable) |

## Admin commands
- `/channels` — destinations, interval, pending posts, next run (local time)
- `/setinterval CHAT_ID | MINUTES` — change a channel's interval
- `/setinterval all | 200` — set the same interval for every channel
- `/addchannel CHAT_ID | NAME | MINUTES` — add/update a destination
- `/removechannel CHAT_ID` — remove a destination
- Reply to a post with `/broadcast` — send it to all destinations immediately

Destination channels are auto-registered when the bot becomes admin there, and auto-removed if the bot is removed.

## How posting works
1. A new source post is given to **one** destination (true round-robin).
2. Each channel sends its pending posts first, in order.
3. When its queue is empty it picks random posts it has not sent yet in the current cycle. After every post has been sent, a new cycle starts (never the same post twice in a row).
4. After a batch, the channel waits its interval. Nothing is sent during quiet hours.
5. If a source post was deleted, it is removed from the library automatically.

## Limitations
- Albums are handled as separate messages, not as grouped albums.
- Edited source posts are not re-synced.
- Protected content cannot be copied.
- Never commit bot tokens; use Railway Variables.
