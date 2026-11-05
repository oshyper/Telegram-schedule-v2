import asyncio
import json
import logging
from html import escape
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, InlineKeyboardMarkup, ChatMemberUpdated
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from .config import (
    BOT_TOKEN, ADMIN_IDS, SOURCE_CHANNEL_ID, POLL_SECONDS, BATCH_SIZE,
    DEFAULT_INTERVAL_MINUTES, POST_DELAY_SECONDS, TIMEZONE,
    QUIET_START_HOUR, QUIET_END_HOUR,
)
from . import storage

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing. Set it in Railway Variables.")

TZ = ZoneInfo(TIMEZONE)
bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()


# ------------------------------------------------------------------ helpers

async def db(fn, *args):
    """Run a (blocking) storage function in a thread so the bot never freezes."""
    return await asyncio.to_thread(fn, *args)


def utc_in(**delta) -> str:
    return (datetime.now(timezone.utc) + timedelta(**delta)).isoformat()


def in_quiet_hours(now_local: datetime) -> bool:
    start, end, hour = QUIET_START_HOUR, QUIET_END_HOUR, now_local.hour
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end  # window crossing midnight, e.g. 22 -> 6


def quiet_label() -> str:
    if QUIET_START_HOUR == QUIET_END_HOUR:
        return "off"
    return f"{QUIET_START_HOUR:02d}:00-{QUIET_END_HOUR:02d}:00 {TIMEZONE}"


def fmt_local(iso) -> str:
    if not iso:
        return "now"
    try:
        return datetime.fromisoformat(iso).astimezone(TZ).strftime("%d %b, %H:%M")
    except ValueError:
        return "?"


def status_of(member) -> str:
    return str(getattr(member.status, "value", member.status))


def is_admin(message: Message) -> bool:
    return bool(message.from_user and message.from_user.id in ADMIN_IDS)


async def deny(message: Message):
    uid = message.from_user.id if message.from_user else "?"
    await message.answer(f"Not authorized. Your Telegram user ID: <code>{uid}</code>")


async def notify_admins(text: str):
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text)
        except TelegramAPIError:
            logging.warning("Could not notify admin %s; they may need to start the bot first.", admin_id)


def markup_json(message: Message):
    if not message.reply_markup:
        return None
    try:
        return message.reply_markup.model_dump_json(exclude_none=True)
    except Exception:
        return None


def queue_post(message: Message):
    entities = [e.model_dump(exclude_none=True) for e in (message.entities or [])]
    caption_entities = [e.model_dump(exclude_none=True) for e in (message.caption_entities or [])]
    payload = json.dumps({"text": entities, "caption": caption_entities}, ensure_ascii=False)
    return storage.add_post(
        message.chat.id, message.message_id, message.caption or message.text, payload, markup_json(message)
    )


# ------------------------------------------------------- channel registration

@dp.my_chat_member()
async def detect_bot_channel_membership(update: ChatMemberUpdated):
    if update.chat.type != "channel":
        return
    old_status = status_of(update.old_chat_member)
    new_status = status_of(update.new_chat_member)
    title = update.chat.title or str(update.chat.id)

    if new_status in {"left", "kicked"}:
        await db(storage.remove_channel, update.chat.id)
        logging.info("Bot removed from channel %s (%s); channel unregistered", update.chat.id, title)
        await notify_admins(
            f"<b>Bot removed from a channel</b>\n\n<b>Channel:</b> {escape(title)}\n"
            f"<b>Channel ID:</b> <code>{update.chat.id}</code>\nIt was removed from the scheduler."
        )
        return

    became_admin = new_status == "administrator" and old_status != "administrator"
    was_added = new_status in {"administrator", "member"} and old_status in {"left", "kicked"}
    if became_admin or was_added:
        await db(storage.register_channel_if_missing, update.chat.id, title)
        logging.info("Auto-registered destination channel %s (%s)", update.chat.id, title)
        await notify_admins(
            "<b>Bot added to a channel</b>\n\n"
            f"<b>Channel:</b> {escape(title)}\n"
            f"<b>Channel ID:</b> <code>{update.chat.id}</code>\n"
            f"<b>Status:</b> {new_status}\n"
            f"<b>Batch:</b> up to {BATCH_SIZE} posts\n"
            f"<b>Interval:</b> {DEFAULT_INTERVAL_MINUTES} minutes\n\n"
            "Use /channels to view it or /setinterval to change its schedule."
        )


# ------------------------------------------------------------------ commands

def help_text() -> str:
    return (
        "<b>Auto Forward Scheduler — Help</b>\n\n"
        "<b>Admin commands</b> (ADMIN_IDS only)\n"
        "/channels — destination channels, queue and next run\n"
        "/setinterval CHAT_ID | MINUTES — wait between batches\n"
        "/setinterval all | MINUTES — same for every channel\n"
        "/addchannel CHAT_ID | NAME | MINUTES — add/update a destination\n"
        "/removechannel CHAT_ID — remove a destination\n"
        "/broadcast — reply to a post (in this chat) to send it to all destinations now\n\n"
        "<b>How it works</b>\n"
        "• Add the bot as channel admin to auto-register it.\n"
        f"• Each channel sends up to {BATCH_SIZE} posts, then waits its interval "
        f"(default {DEFAULT_INTERVAL_MINUTES} min).\n"
        "• New source posts go to one channel (round-robin).\n"
        "• When a channel's queue is empty it sends random posts from the library, "
        "without repeating until every post has been sent once.\n"
        f"• Quiet hours (no sending): {quiet_label()}.\n\n"
        "<b>Examples</b>\n"
        "<code>/setinterval -1001234567890 | 200</code>\n"
        "<code>/setinterval all | 200</code>\n"
        "<code>/addchannel -1001234567890 | News Channel | 200</code>"
    )


@dp.message(CommandStart())
async def start(message: Message):
    if not is_admin(message):
        return await deny(message)
    await message.answer(help_text())


@dp.message(Command("help"))
async def help_cmd(message: Message):
    if not is_admin(message):
        return await deny(message)
    await message.answer(help_text())


async def direct_broadcast(message: Message):
    target = message.reply_to_message
    if not target:
        await message.answer("Reply to the post you want to broadcast, then send /broadcast.")
        return
    channels = await db(storage.list_channels)
    if not channels:
        await message.answer("No destination channels registered. Add the bot as admin to a channel.")
        return

    succeeded, failed = [], []
    for channel in channels:
        channel_id = int(channel["chat_id"])
        try:
            await bot.copy_message(chat_id=channel_id, from_chat_id=message.chat.id, message_id=target.message_id)
            succeeded.append(channel_id)
        except TelegramRetryAfter as exc:
            await asyncio.sleep(int(exc.retry_after) + 1)
            try:
                await bot.copy_message(chat_id=channel_id, from_chat_id=message.chat.id, message_id=target.message_id)
                succeeded.append(channel_id)
            except TelegramAPIError:
                logging.exception("Direct broadcast failed for channel %s", channel_id)
                failed.append(channel_id)
        except TelegramAPIError:
            logging.exception("Direct broadcast failed for channel %s", channel_id)
            failed.append(channel_id)
        await asyncio.sleep(0.5)

    summary = f"Direct broadcast: sent to {len(succeeded)}/{len(channels)} channel(s)."
    if failed:
        summary += "\nFailed channel IDs: " + ", ".join(str(c) for c in failed)
    await message.answer(summary)


@dp.message(Command("addchannel"))
async def add_channel_cmd(message: Message):
    if not is_admin(message):
        return await deny(message)
    parts = [p.strip() for p in (message.text or "").partition(" ")[2].split("|")]
    if len(parts) != 3:
        return await message.answer("Format: /addchannel CHAT_ID | NAME | INTERVAL_MINUTES")
    try:
        chat_id, title, interval = int(parts[0]), parts[1], int(parts[2])
        if not title or interval < 1:
            raise ValueError
    except ValueError:
        return await message.answer("CHAT_ID and interval must be valid; interval must be at least 1 minute.")
    await db(storage.add_channel, chat_id, title, interval)
    await message.answer(
        f"Saved channel <code>{chat_id}</code> ({escape(title)}): up to {BATCH_SIZE} posts per batch, "
        f"then every {interval} minute(s)."
    )


@dp.message(Command("setinterval"))
async def set_interval_cmd(message: Message):
    if not is_admin(message):
        return await deny(message)
    parts = [p.strip() for p in (message.text or "").partition(" ")[2].split("|")]
    if len(parts) != 2:
        return await message.answer("Format: /setinterval CHAT_ID | MINUTES   (or: /setinterval all | MINUTES)")
    try:
        interval = int(parts[1])
        if interval < 1:
            raise ValueError
        target_all = parts[0].lower() == "all"
        chat_id = None if target_all else int(parts[0])
    except ValueError:
        return await message.answer("CHAT_ID must be numeric (or 'all') and MINUTES must be at least 1.")

    if target_all:
        count = await db(storage.set_all_intervals, interval)
        return await message.answer(f"Updated {count} channel(s): next batches wait {interval} minute(s).")
    if not await db(storage.set_channel_interval, chat_id, interval):
        return await message.answer("Channel not found. Make the bot admin there first, or use /addchannel.")
    await message.answer(f"Updated <code>{chat_id}</code>: next batches will wait {interval} minute(s).")


@dp.message(Command("channels"))
async def channels_cmd(message: Message):
    if not is_admin(message):
        return await deny(message)
    channels = await db(storage.list_channels)
    library = await db(storage.library_size)
    if not channels:
        return await message.answer("No destination channels registered. Add the bot as admin to a channel.")
    lines = [
        f"<code>{c['chat_id']}</code> — {escape(c['title'])}\n"
        f"   every {c['interval_minutes']} min · pending {c['pending']} · next {fmt_local(c['next_run'])}"
        for c in channels
    ]
    await message.answer(
        f"<b>Destination channels</b>\nLibrary: {library} post(s) · batch up to {BATCH_SIZE}\n"
        f"Quiet hours: {quiet_label()}\n\n" + "\n".join(lines)
    )


@dp.message(Command("removechannel"))
async def remove_channel_cmd(message: Message):
    if not is_admin(message):
        return await deny(message)
    try:
        chat_id = int((message.text or "").split(maxsplit=1)[1])
    except (ValueError, IndexError):
        return await message.answer("Format: /removechannel CHAT_ID")
    await db(storage.remove_channel, chat_id)
    await message.answer(f"Removed channel <code>{chat_id}</code> from scheduler if it existed.")


@dp.channel_post()
async def capture_source_channel_post(message: Message):
    if not SOURCE_CHANNEL_ID or message.chat.id != SOURCE_CHANNEL_ID:
        return

    content = (message.text or message.caption or "").strip()
    entities = message.entities or message.caption_entities or []
    command_entity = any(
        e.type == "bot_command" and content[e.offset:e.offset + e.length].split("@")[0] == "/broadcast"
        for e in entities
    )
    command_text = content.split(maxsplit=1)[0].split("@")[0] if content else ""
    if command_text == "/broadcast" or command_entity:
        await direct_broadcast(message)
        return

    qid = await db(queue_post, message)
    if qid:
        logging.info("Queued source-channel post %s", message.message_id)
    else:
        logging.info("Source post %s already saved; ignored.", message.message_id)


@dp.message(Command("broadcast"), F.chat.type == "private")
async def broadcast_private_cmd(message: Message):
    if not is_admin(message):
        return await deny(message)
    await direct_broadcast(message)


@dp.message(F.chat.type == "private")
async def capture_admin_direct_post(message: Message):
    if not is_admin(message) or (message.text and message.text.startswith("/")):
        return
    qid = await db(queue_post, message)
    if qid:
        await message.reply("Post saved to the library and queued for one destination channel.")
    else:
        await message.reply("This post was already saved earlier.")


# ----------------------------------------------------------------- scheduler

async def process_channel(channel):
    channel_id = int(channel["chat_id"])
    interval = int(channel["interval_minutes"])
    sent = 0

    for _ in range(BATCH_SIZE):
        post = await db(storage.get_next_post, channel_id)
        if not post:
            break

        kwargs = {
            "chat_id": channel_id,
            "from_chat_id": post["source_chat_id"],
            "message_id": post["message_id"],
        }
        if post["reply_markup_json"]:
            try:
                kwargs["reply_markup"] = InlineKeyboardMarkup.model_validate_json(post["reply_markup_json"])
            except Exception:
                logging.warning("Could not restore inline keyboard; copying without it")

        try:
            await bot.copy_message(**kwargs)
        except TelegramRetryAfter as exc:
            wait = int(exc.retry_after) + 1
            logging.warning("Rate limited on channel %s; retrying in %ss", channel_id, wait)
            await db(storage.postpone_channel, channel_id, utc_in(seconds=wait))
            return
        except TelegramForbiddenError:
            logging.exception("No permission to post in channel %s", channel_id)
            await db(storage.postpone_channel, channel_id, utc_in(minutes=60))
            await notify_admins(
                f"<b>Cannot post</b> in <code>{channel_id}</code>: the bot lost access or "
                "the 'post messages' permission. Will retry in 60 minutes."
            )
            return
        except TelegramBadRequest as exc:
            text = str(exc).lower()
            if "message to copy not found" in text or "message_id_invalid" in text:
                logging.warning("Source post %s no longer exists; removing it from the library", post["id"])
                await db(storage.delete_post, post["id"])
                continue
            if "chat not found" in text:
                await db(storage.postpone_channel, channel_id, utc_in(minutes=60))
                await notify_admins(f"<b>Channel not found:</b> <code>{channel_id}</code>. Will retry in 60 minutes.")
                return
            logging.exception("Post %s rejected for channel %s; skipping it", post["id"], channel_id)
            await db(storage.finish_channel_post, channel_id, post["id"])
            continue
        except TelegramAPIError:
            logging.exception("Copy failed for channel %s; retrying in 1 minute", channel_id)
            await db(storage.postpone_channel, channel_id, utc_in(minutes=1))
            return

        await db(storage.finish_channel_post, channel_id, post["id"])
        sent += 1
        logging.info("Sent post %s/%s to channel %s", sent, BATCH_SIZE, channel_id)
        if sent < BATCH_SIZE and POST_DELAY_SECONDS:
            await asyncio.sleep(POST_DELAY_SECONDS)

    if sent:
        await db(storage.postpone_channel, channel_id, utc_in(minutes=interval))
        logging.info("Batch done for %s: %s post(s); next in %s min", channel_id, sent, interval)
    else:
        await db(storage.postpone_channel, channel_id, utc_in(seconds=60))


async def scheduler():
    while True:
        try:
            if not in_quiet_hours(datetime.now(TZ)):
                channel = await db(storage.next_due_channel, datetime.now(timezone.utc).isoformat())
                if channel:
                    await process_channel(channel)
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("Scheduler error; continuing in 30 seconds")
            await asyncio.sleep(30)
        await asyncio.sleep(POLL_SECONDS)


async def main():
    await db(storage.init_db)
    if not ADMIN_IDS:
        logging.warning("ADMIN_IDS is empty: nobody can use admin commands.")
    if not SOURCE_CHANNEL_ID:
        logging.warning("SOURCE_CHANNEL_ID is not configured; source channel posts will be ignored.")
    logging.info(
        "Started: batch=%s, default interval=%s min, quiet hours=%s",
        BATCH_SIZE, DEFAULT_INTERVAL_MINUTES, quiet_label(),
    )
    await asyncio.gather(dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types()), scheduler())


if __name__ == "__main__":
    asyncio.run(main())
