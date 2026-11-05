import asyncio
import json
import logging
import re
from html import escape
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery, ChatMemberUpdated, InlineKeyboardButton, InlineKeyboardMarkup, Message,
)
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


LOST_ACCESS_HINTS = (
    "not enough rights", "have no rights", "need administrator rights", "chat_write_forbidden",
    "chat not found", "bot is not a member", "bot was kicked", "channel_private",
)


def lost_access(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(hint in text for hint in LOST_ACCESS_HINTS)


async def drop_channel(channel_id: int, title: str, reason: str):
    """Remove a channel completely (no more scheduling, no retries) and tell the admins once."""
    await db(storage.remove_channel, channel_id)
    logging.warning("Channel %s (%s) removed: %s", channel_id, title, reason)
    await notify_admins(
        "<b>Channel removed automatically</b>\n\n"
        f"<b>Channel:</b> {escape(title)}\n"
        f"<b>ID:</b> <code>{channel_id}</code>\n"
        f"<b>Reason:</b> {escape(reason)}\n\n"
        "The bot will not post or retry there. To use it again, add the bot back as admin "
        "with 'Post messages' permission — it registers automatically."
    )


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
    if update.chat.type != "channel" or storage.is_source(update.chat.id):
        return  # the source channel is never a destination
    new_status = status_of(update.new_chat_member)
    title = update.chat.title or str(update.chat.id)
    can_post = getattr(update.new_chat_member, "can_post_messages", None)

    # Bot removed, demoted from admin, or lost the 'Post messages' permission -> forget the channel.
    if new_status in {"left", "kicked", "member", "restricted"} or (new_status == "administrator" and can_post is False):
        if any(int(c["chat_id"]) == update.chat.id for c in await db(storage.list_channels)):
            await drop_channel(update.chat.id, title, "bot was removed or lost the 'Post messages' permission")
        return

    # Bot became admin with posting rights -> register automatically (once).
    if new_status == "administrator":
        if any(int(c["chat_id"]) == update.chat.id for c in await db(storage.list_channels)):
            return  # already registered; some other admin right changed
        await db(storage.register_channel_if_missing, update.chat.id, title)
        logging.info("Registered destination channel %s (%s)", update.chat.id, title)
        await notify_admins(
            "<b>Channel added</b>\n\n"
            f"<b>Channel:</b> {escape(title)}\n"
            f"<b>ID:</b> <code>{update.chat.id}</code>\n"
            f"<b>Batch:</b> up to {BATCH_SIZE} posts, then every {DEFAULT_INTERVAL_MINUTES} minutes\n\n"
            "Nothing else to do. Use /channels to see it or /removechannel to remove it."
        )


# ------------------------------------------------------------------ commands

def help_text() -> str:
    return (
        "<b>Auto Forward Scheduler — Help</b>\n\n"
        "<b>Add a channel (easiest)</b>\n"
        "Make the bot admin in the channel with 'Post messages' permission. Done — it registers itself.\n"
        "Or: <code>/addchannel -1001234567890</code> or <code>/addchannel @channelname</code> "
        "(optional minutes at the end, default "
        f"{DEFAULT_INTERVAL_MINUTES}).\n\n"
        "<b>Remove a channel</b>\n"
        "Send /removechannel and tap the channel. Or just remove the bot from the channel — "
        "it stops posting there automatically.\n\n"
        "<b>Other commands</b>\n"
        "/channels — channels, queue and next run\n"
        "/setinterval CHAT_ID | MINUTES — wait between batches\n"
        "/setinterval all | MINUTES — same for every channel\n"
        "/broadcast — reply to a post to send it to all channels now\n\n"
        "<b>How it works</b>\n"
        f"• Each channel sends up to {BATCH_SIZE} posts, then waits its interval.\n"
        "• New source posts go to one channel (round-robin).\n"
        "• When a queue is empty, random posts are sent without repeats until all were sent once.\n"
        f"• Quiet hours (no sending): {quiet_label()}.\n"
        "• If the bot loses access to a channel, that channel is removed — no retries."
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
        except TelegramForbiddenError:
            await drop_channel(channel_id, channel["title"], "bot has no access to post there")
            failed.append(channel_id)
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
    args = (message.text or "").partition(" ")[2]
    tokens = [t for t in re.split(r"[|\s]+", args) if t]
    if not tokens:
        return await message.answer(
            "Format: <code>/addchannel -1001234567890</code> or <code>/addchannel @channelname</code>\n"
            f"Optional minutes at the end (default {DEFAULT_INTERVAL_MINUTES}). "
            "The bot must already be admin in that channel."
        )
    interval = DEFAULT_INTERVAL_MINUTES
    if len(tokens) > 1 and tokens[-1].isdigit():
        interval = max(1, int(tokens[-1]))
    ref = tokens[0]
    target = ref if ref.startswith("@") else None
    if target is None:
        try:
            target = int(ref)
        except ValueError:
            return await message.answer("Send a numeric channel ID (starts with -100) or an @username.")

    try:
        chat = await bot.get_chat(target)
        if chat.type != "channel":
            return await message.answer("That is not a channel.")
        if storage.is_source(chat.id):
            return await message.answer("That is your SOURCE channel. Posts are collected from it, never sent to it.")
        me = await bot.get_chat_member(chat.id, (await bot.get_me()).id)
    except TelegramAPIError:
        return await message.answer(
            "I cannot access that channel. Add the bot as admin there (with 'Post messages') and try again."
        )
    if status_of(me) != "administrator" or getattr(me, "can_post_messages", None) is False:
        return await message.answer("The bot is in the channel but is not admin with 'Post messages' permission.")

    title = chat.title or str(chat.id)
    await db(storage.add_channel, chat.id, title, interval)
    await message.answer(
        f"Added <b>{escape(title)}</b> (<code>{chat.id}</code>): up to {BATCH_SIZE} posts per batch, "
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
    args = (message.text or "").partition(" ")[2].strip()
    if args:
        try:
            chat_id = int(args)
        except ValueError:
            return await message.answer("Send /removechannel alone and tap a channel, or /removechannel CHAT_ID.")
        await db(storage.remove_channel, chat_id)
        return await message.answer(f"Removed <code>{chat_id}</code>. No more posts will be sent there.")

    channels = await db(storage.list_channels)
    if not channels:
        return await message.answer("No channels registered.")
    rows = [[InlineKeyboardButton(text=f"🗑 {c['title'][:40]}", callback_data=f"rm:{c['chat_id']}")] for c in channels]
    await message.answer("Tap the channel you want to remove:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@dp.callback_query(F.data.startswith("rm"))
async def remove_channel_buttons(query: CallbackQuery):
    if query.from_user.id not in ADMIN_IDS:
        return await query.answer("Not authorized", show_alert=True)
    action, _, raw_id = (query.data or "").partition(":")
    if action == "rmn":
        await query.message.edit_text("Cancelled.")
        return await query.answer()
    try:
        chat_id = int(raw_id)
    except ValueError:
        return await query.answer()
    if action == "rm":
        title = next((c["title"] for c in await db(storage.list_channels) if int(c["chat_id"]) == chat_id), str(chat_id))
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Yes, remove", callback_data=f"rmy:{chat_id}"),
            InlineKeyboardButton(text="Cancel", callback_data="rmn"),
        ]])
        await query.message.edit_text(f"Remove <b>{escape(title)}</b>?", reply_markup=keyboard)
    elif action == "rmy":
        await db(storage.remove_channel, chat_id)
        await query.message.edit_text(f"Removed <code>{chat_id}</code>. No more posts will be sent there.")
    await query.answer()


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
    if storage.is_source(channel_id):
        await db(storage.remove_channel, channel_id)
        return
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
            await drop_channel(channel_id, channel["title"], "bot lost access or the 'Post messages' permission")
            return
        except TelegramBadRequest as exc:
            text = str(exc).lower()
            if "message to copy not found" in text or "message_id_invalid" in text:
                logging.warning("Source post %s no longer exists; removing it from the library", post["id"])
                await db(storage.delete_post, post["id"])
                continue
            if lost_access(exc):
                await drop_channel(channel_id, channel["title"], "channel not found or bot has no posting rights")
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
    if SOURCE_CHANNEL_ID:
        # Clean up: an earlier version may have registered the source channel as a destination.
        await db(storage.remove_channel, SOURCE_CHANNEL_ID)
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
