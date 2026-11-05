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

## Destinations: channels AND groups
The source channel (`SOURCE_CHANNEL_ID`) is never a destination: the bot only collects posts from it.
Posts can go to channels, groups and supergroups (including a specific **topic** of a forum group).

**Add**
- **Easiest:** add the bot with an account listed in `ADMIN_IDS` — channel: as admin with *Post messages*; group: as a member. It registers itself and messages the admins. (If someone else adds the bot, it is ignored, so strangers cannot make it post in their groups.)
- **Inside a group/topic:** send `/addhere` (optional minutes). Posts go to that topic. If the command is not delivered, use `/addhere@YourBotUsername` or make the bot admin.
- **By ID/username:** `/addchannel -1001234567890`, `/addchannel @name`, optional minutes and `topic=ID` (e.g. `/addchannel -1001234567890 200 topic=7`).

**Remove**
- `/removechannel` -> tap the chat -> confirm, or `/removehere` inside a group, or `/removechannel CHAT_ID`.
- **Automatic:** if the bot is removed, demoted, loses the permission to post, or the chat/topic is gone, the destination is removed immediately. No retries; the admins get one notice. A group upgraded to a supergroup keeps its schedule (ID is moved automatically).

## Manage
- `/channels` — all destinations, interval, batch size, pending posts, next run
- `/setinterval CHAT_ID|all | MINUTES` — wait between batches
- `/setbatch CHAT_ID|all | POSTS` — posts per batch (default `BATCH_SIZE`)
- `/pause` / `/resume` `CHAT_ID|all` — stop/restart a destination without removing it
- `/sendnow CHAT_ID|all` — start a batch right away (quiet hours still apply)
- Reply to a post with `/broadcast` — send it to every destination now
In a group, `/pause`, `/resume`, `/sendnow`, `/setinterval 200` and `/setbatch 3` work without an ID.

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
- One destination per chat (a forum group can target one topic).
- Never commit bot tokens; use Railway Variables.
