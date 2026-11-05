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
    TelegramMigrateToChat,
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

DEST_TYPES = {"channel", "group", "supergroup"}
GROUP_TYPES = {"group", "supergroup"}


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


def icon(kind: str) -> str:
    return "📢" if kind == "channel" else "👥"


def bot_can_post(chat_type: str, member) -> bool:
    """Can the bot post in this chat, judging by its own membership record?"""
    status = status_of(member)
    if chat_type == "channel":
        return status in {"administrator", "creator"} and getattr(member, "can_post_messages", None) is not False
    if status in {"administrator", "creator", "member"}:
        return True
    if status == "restricted":
        return bool(getattr(member, "can_send_messages", False))
    return False


def is_admin(message: Message) -> bool:
    return bool(message.from_user and message.from_user.id in ADMIN_IDS)


async def deny(message: Message):
    if message.chat.type != "private":
        return  # stay silent in groups
    uid = message.from_user.id if message.from_user else "?"
    await message.answer(f"Not authorized. Your Telegram user ID: <code>{uid}</code>")


async def notify_admins(text: str):
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text)
        except TelegramAPIError:
            logging.warning("Could not notify admin %s; they may need to start the bot first.", admin_id)


# Errors that mean "the bot can no longer post in this chat" -> remove it, never retry.
LOST_ACCESS_HINTS = (
    "not enough rights to send text", "have no rights to send a message", "need administrator rights",
    "chat_write_forbidden", "chat not found", "bot is not a member", "bot was kicked",
    "channel_private", "chat was deleted", "group chat was deleted",
)


def lost_access(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(hint in text for hint in LOST_ACCESS_HINTS)


async def drop_channel(chat_id: int, title: str, reason: str):
    """Remove a destination completely (no more scheduling, no retries) and tell the admins once."""
    await db(storage.remove_channel, chat_id)
    logging.warning("Destination %s (%s) removed: %s", chat_id, title, reason)
    await notify_admins(
        "<b>Destination removed automatically</b>\n\n"
        f"<b>Chat:</b> {escape(title)}\n"
        f"<b>ID:</b> <code>{chat_id}</code>\n"
        f"<b>Reason:</b> {escape(reason)}\n\n"
        "The bot will not post or retry there. To use it again, add the bot back with permission "
        "to post (channel: admin with 'Post messages')."
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


def parse_target(raw: str):
    """'all' -> 'all', number -> int, anything else -> ValueError."""
    raw = raw.strip()
    return "all" if raw.lower() == "all" else int(raw)


def target_from(message: Message, raw: str):
    """Target from the command argument; inside a group the chat itself is the default."""
    if raw.strip():
        return parse_target(raw)
    if message.chat.type in GROUP_TYPES:
        return message.chat.id
    raise ValueError


# --------------------------------------------- bot added to / removed from chats

@dp.my_chat_member()
async def detect_bot_membership(update: ChatMemberUpdated):
    chat = update.chat
    if chat.type not in DEST_TYPES or storage.is_source(chat.id):
        return  # private chats and the source channel are never destinations
    title = chat.title or str(chat.id)
    registered = any(int(c["chat_id"]) == chat.id for c in await db(storage.list_channels))

    if not bot_can_post(chat.type, update.new_chat_member):
        if registered:
            await drop_channel(chat.id, title, "bot was removed or can no longer post there")
        return
    if registered:
        return  # some other permission changed; nothing to do

    adder = update.from_user.id if update.from_user else None
    if adder not in ADMIN_IDS:
        logging.info("Bot added to %s (%s) by a non-admin; ignored.", chat.id, title)
        return

    await db(storage.register_channel_if_missing, chat.id, title, chat.type)
    logging.info("Registered destination %s (%s, %s)", chat.id, title, chat.type)
    await notify_admins(
        f"<b>{'Channel' if chat.type == 'channel' else 'Group'} added</b>\n\n"
        f"<b>Name:</b> {escape(title)}\n"
        f"<b>ID:</b> <code>{chat.id}</code>\n"
        f"<b>Batch:</b> up to {BATCH_SIZE} posts, then every {DEFAULT_INTERVAL_MINUTES} minutes\n\n"
        "Nothing else to do. Use /channels to see it or /removechannel to remove it."
    )


@dp.message(F.migrate_to_chat_id)
async def group_upgraded(message: Message):
    """Telegram gave an upgraded group a new chat ID; keep its schedule."""
    await db(storage.migrate_channel, message.chat.id, message.migrate_to_chat_id)
    logging.info("Group %s upgraded to %s", message.chat.id, message.migrate_to_chat_id)


# ------------------------------------------------------------------ commands

def help_text() -> str:
    return (
        "<b>Auto Forward Scheduler — Help</b>\n\n"
        "<b>Add a channel or group</b>\n"
        "• Add the bot yourself (admin account) — channel: as admin with 'Post messages'; "
        "group: as a member. It registers itself.\n"
        "• Or in a group/topic send <code>/addhere</code> (posts go to that topic).\n"
        "• Or here: <code>/addchannel -1001234567890</code> or <code>/addchannel @name</code> "
        f"(optional: minutes, <code>topic=ID</code>; default {DEFAULT_INTERVAL_MINUTES} min).\n\n"
        "<b>Remove</b>\n"
        "/removechannel and tap the chat, or /removehere inside a group, or just remove the bot — "
        "it stops posting there automatically.\n\n"
        "<b>Manage</b>\n"
        "/channels — destinations, queue, next run\n"
        "/setinterval CHAT_ID|all | MINUTES\n"
        "/setbatch CHAT_ID|all | POSTS — posts per batch\n"
        "/pause and /resume CHAT_ID|all\n"
        "/sendnow CHAT_ID|all — start a batch now\n"
        "/broadcast — reply to a post to send it everywhere now\n\n"
        "<b>How it works</b>\n"
        f"• Each destination sends up to {BATCH_SIZE} posts, then waits its interval.\n"
        "• New source posts go to one destination (round-robin).\n"
        "• When a queue is empty, random posts are sent without repeats until all were sent once.\n"
        f"• Quiet hours (no sending): {quiet_label()}.\n"
        "• Lost access = destination removed, no retries."
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
        await message.answer("No destinations registered.")
        return

    async def send(channel):
        kwargs = {"chat_id": int(channel["chat_id"]), "from_chat_id": message.chat.id, "message_id": target.message_id}
        if channel["thread_id"]:
            kwargs["message_thread_id"] = channel["thread_id"]
        await bot.copy_message(**kwargs)

    succeeded, failed = [], []
    for channel in channels:
        chat_id = int(channel["chat_id"])
        try:
            await send(channel)
            succeeded.append(chat_id)
        except TelegramForbiddenError:
            await drop_channel(chat_id, channel["title"], "bot has no access to post there")
            failed.append(chat_id)
        except TelegramRetryAfter as exc:
            await asyncio.sleep(int(exc.retry_after) + 1)
            try:
                await send(channel)
                succeeded.append(chat_id)
            except TelegramAPIError:
                logging.exception("Direct broadcast failed for %s", chat_id)
                failed.append(chat_id)
        except TelegramAPIError:
            logging.exception("Direct broadcast failed for %s", chat_id)
            failed.append(chat_id)
        await asyncio.sleep(0.5)

    summary = f"Direct broadcast: sent to {len(succeeded)}/{len(channels)} destination(s)."
    if failed:
        summary += "\nFailed IDs: " + ", ".join(str(c) for c in failed)
    await message.answer(summary)


@dp.message(Command("addchannel"))
async def add_channel_cmd(message: Message):
    if not is_admin(message):
        return await deny(message)
    args = (message.text or "").partition(" ")[2]
    tokens = [t for t in re.split(r"[|\s]+", args) if t]
    if not tokens:
        return await message.answer(
            "Format: <code>/addchannel -1001234567890</code> or <code>/addchannel @name</code>\n"
            f"Optional at the end: minutes (default {DEFAULT_INTERVAL_MINUTES}) and <code>topic=ID</code> "
            "for a group topic. The bot must already be in that chat."
        )
    interval, thread_id = DEFAULT_INTERVAL_MINUTES, None
    for token in tokens[1:]:
        topic = re.fullmatch(r"topic[=:]?(\d+)", token.lower())
        if topic:
            thread_id = int(topic.group(1))
        elif token.isdigit():
            interval = max(1, int(token))
    ref = tokens[0]
    target = ref if ref.startswith("@") else None
    if target is None:
        try:
            target = int(ref)
        except ValueError:
            return await message.answer("Send a numeric chat ID (like -100...) or an @username.")

    try:
        chat = await bot.get_chat(target)
        if chat.type not in DEST_TYPES:
            return await message.answer("That is not a channel or group.")
        if storage.is_source(chat.id):
            return await message.answer("That is your SOURCE channel. Posts are collected from it, never sent to it.")
        me = await bot.get_chat_member(chat.id, (await bot.get_me()).id)
    except TelegramAPIError:
        return await message.answer("I cannot access that chat. Add the bot there first, then try again.")
    if not bot_can_post(chat.type, me):
        return await message.answer("The bot is there but cannot post. Channel: make it admin with 'Post messages'.")

    title = chat.title or str(chat.id)
    await db(storage.add_channel, chat.id, title, interval, chat.type, thread_id)
    topic_note = f", topic {thread_id}" if thread_id else ""
    await message.answer(
        f"Added {icon(chat.type)} <b>{escape(title)}</b> (<code>{chat.id}</code>{topic_note}): "
        f"up to {BATCH_SIZE} posts per batch, then every {interval} minute(s)."
    )


@dp.message(Command("addhere"), F.chat.type.in_(GROUP_TYPES))
async def add_here_cmd(message: Message):
    if not is_admin(message):
        return await deny(message)
    args = (message.text or "").partition(" ")[2].split()
    interval = max(1, int(args[0])) if args and args[0].isdigit() else DEFAULT_INTERVAL_MINUTES
    chat = message.chat
    try:
        me = await bot.get_chat_member(chat.id, (await bot.get_me()).id)
    except TelegramAPIError:
        return await message.answer("I cannot check my permissions here.")
    if not bot_can_post(chat.type, me):
        return await message.answer("I do not have permission to send messages here. Fix that and try again.")
    thread_id = message.message_thread_id if message.is_topic_message else None
    await db(storage.add_channel, chat.id, chat.title or str(chat.id), interval, chat.type, thread_id)
    where = f"this topic (#{thread_id})" if thread_id else "this group"
    await message.answer(f"Added: posts will go to {where}, up to {BATCH_SIZE} per batch, every {interval} minute(s).")


@dp.message(Command("removehere"), F.chat.type.in_(GROUP_TYPES))
async def remove_here_cmd(message: Message):
    if not is_admin(message):
        return await deny(message)
    await db(storage.remove_channel, message.chat.id)
    await message.answer("Removed. No more scheduled posts here.")


@dp.message(Command("setinterval"))
async def set_interval_cmd(message: Message):
    if not is_admin(message):
        return await deny(message)
    parts = [p.strip() for p in (message.text or "").partition(" ")[2].split("|")]
    try:
        if len(parts) == 2:
            target, minutes = parse_target(parts[0]), int(parts[1])
        elif len(parts) == 1 and parts[0].isdigit() and message.chat.type in GROUP_TYPES:
            target, minutes = message.chat.id, int(parts[0])
        else:
            raise ValueError
        if minutes < 1:
            raise ValueError
    except ValueError:
        return await message.answer("Format: /setinterval CHAT_ID | MINUTES   (or: /setinterval all | MINUTES)")
    count = await db(storage.set_interval, target, minutes)
    await message.answer(f"Updated {count} destination(s): next batches wait {minutes} minute(s)." if count
                         else "Destination not found. Check /channels.")


@dp.message(Command("setbatch"))
async def set_batch_cmd(message: Message):
    if not is_admin(message):
        return await deny(message)
    parts = [p.strip() for p in (message.text or "").partition(" ")[2].split("|")]
    try:
        if len(parts) == 2:
            target, size = parse_target(parts[0]), int(parts[1])
        elif len(parts) == 1 and parts[0].isdigit() and message.chat.type in GROUP_TYPES:
            target, size = message.chat.id, int(parts[0])
        else:
            raise ValueError
        if size < 1:
            raise ValueError
    except ValueError:
        return await message.answer("Format: /setbatch CHAT_ID | POSTS   (or: /setbatch all | POSTS)")
    count = await db(storage.set_batch_size, target, size)
    await message.answer(f"Updated {count} destination(s): {size} post(s) per batch." if count
                         else "Destination not found. Check /channels.")


def _simple_target_command(name: str, action, done: str):
    @dp.message(Command(name))
    async def handler(message: Message):
        if not is_admin(message):
            return await deny(message)
        try:
            target = target_from(message, (message.text or "").partition(" ")[2])
        except ValueError:
            return await message.answer(f"Format: /{name} CHAT_ID   (or: /{name} all)")
        count = await db(action, target)
        await message.answer(f"{done}: {count} destination(s)." if count else "Destination not found. Check /channels.")
    return handler


_simple_target_command("pause", lambda t: storage.set_paused(t, True), "Paused")
_simple_target_command("resume", lambda t: storage.set_paused(t, False), "Resumed")
_simple_target_command("sendnow", storage.send_now, "Batch scheduled now (quiet hours still apply)")


@dp.message(Command("channels"))
async def channels_cmd(message: Message):
    if not is_admin(message):
        return await deny(message)
    channels = await db(storage.list_channels)
    library = await db(storage.library_size)
    if not channels:
        return await message.answer("No destinations registered. Add the bot to a channel or group.")
    lines = []
    for c in channels:
        extras = []
        if c["paused"]:
            extras.append("⏸ paused")
        if c["thread_id"]:
            extras.append(f"topic {c['thread_id']}")
        lines.append(
            f"{icon(c['kind'])} <b>{escape(c['title'])}</b> <code>{c['chat_id']}</code>"
            + (f" ({', '.join(extras)})" if extras else "")
            + f"\n   every {c['interval_minutes']} min · batch {c['batch_size'] or BATCH_SIZE} · "
              f"pending {c['pending']} · next {fmt_local(c['next_run'])}"
        )
    await message.answer(
        f"<b>Destinations</b> ({len(channels)})\nLibrary: {library} post(s)\n"
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
            return await message.answer("Send /removechannel alone and tap a chat, or /removechannel CHAT_ID.")
        await db(storage.remove_channel, chat_id)
        return await message.answer(f"Removed <code>{chat_id}</code>. No more posts will be sent there.")

    channels = await db(storage.list_channels)
    if not channels:
        return await message.answer("No destinations registered.")
    rows = [
        [InlineKeyboardButton(text=f"🗑 {icon(c['kind'])} {c['title'][:36]}", callback_data=f"rm:{c['chat_id']}")]
        for c in channels
    ]
    await message.answer("Tap the one you want to remove:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


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
        await message.reply("Post saved to the library and queued for one destination.")
    else:
        await message.reply("This post was already saved earlier.")


# ----------------------------------------------------------------- scheduler

async def process_channel(channel):
    chat_id = int(channel["chat_id"])
    if storage.is_source(chat_id):
        await db(storage.remove_channel, chat_id)
        return
    interval = int(channel["interval_minutes"])
    batch = int(channel["batch_size"] or BATCH_SIZE)
    thread_id = channel["thread_id"]
    sent = 0

    for _ in range(batch):
        post = await db(storage.get_next_post, chat_id)
        if not post:
            break

        kwargs = {
            "chat_id": chat_id,
            "from_chat_id": post["source_chat_id"],
            "message_id": post["message_id"],
        }
        if thread_id:
            kwargs["message_thread_id"] = thread_id
        if post["reply_markup_json"]:
            try:
                kwargs["reply_markup"] = InlineKeyboardMarkup.model_validate_json(post["reply_markup_json"])
            except Exception:
                logging.warning("Could not restore inline keyboard; copying without it")

        try:
            await bot.copy_message(**kwargs)
        except TelegramRetryAfter as exc:
            wait = int(exc.retry_after) + 1
            logging.warning("Rate limited on %s; retrying in %ss", chat_id, wait)
            await db(storage.postpone_channel, chat_id, utc_in(seconds=wait))
            return
        except TelegramMigrateToChat as exc:
            await db(storage.migrate_channel, chat_id, int(exc.migrate_to_chat_id))
            logging.info("Group %s upgraded to %s; schedule moved", chat_id, exc.migrate_to_chat_id)
            return
        except TelegramForbiddenError:
            await drop_channel(chat_id, channel["title"], "bot lost access or permission to post")
            return
        except TelegramBadRequest as exc:
            text = str(exc).lower()
            if "message to copy not found" in text or "message_id_invalid" in text:
                logging.warning("Source post %s no longer exists; removing it from the library", post["id"])
                await db(storage.delete_post, post["id"])
                continue
            if lost_access(exc):
                await drop_channel(chat_id, channel["title"], "chat not found or bot has no posting rights")
                return
            if "thread not found" in text or ("topic" in text and "closed" in text):
                await drop_channel(chat_id, channel["title"], "the target topic no longer exists or is closed")
                return
            logging.exception("Post %s rejected for %s; skipping it", post["id"], chat_id)
            await db(storage.finish_channel_post, chat_id, post["id"])
            continue
        except TelegramAPIError:
            logging.exception("Copy failed for %s; retrying in 1 minute", chat_id)
            await db(storage.postpone_channel, chat_id, utc_in(minutes=1))
            return

        await db(storage.finish_channel_post, chat_id, post["id"])
        sent += 1
        logging.info("Sent post %s/%s to %s", sent, batch, chat_id)
        if sent < batch and POST_DELAY_SECONDS:
            await asyncio.sleep(POST_DELAY_SECONDS)

    if sent:
        await db(storage.postpone_channel, chat_id, utc_in(minutes=interval))
        logging.info("Batch done for %s: %s post(s); next in %s min", chat_id, sent, interval)
    else:
        await db(storage.postpone_channel, chat_id, utc_in(seconds=60))


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
        # An earlier version may have registered the source channel as a destination.
        await db(storage.remove_channel, SOURCE_CHANNEL_ID)
    if not ADMIN_IDS:
        logging.warning("ADMIN_IDS is empty: nobody can use admin commands or auto-register chats.")
    if not SOURCE_CHANNEL_ID:
        logging.warning("SOURCE_CHANNEL_ID is not configured; source channel posts will be ignored.")
    logging.info(
        "Started: batch=%s, default interval=%s min, quiet hours=%s",
        BATCH_SIZE, DEFAULT_INTERVAL_MINUTES, quiet_label(),
    )
    await asyncio.gather(dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types()), scheduler())


if __name__ == "__main__":
    asyncio.run(main())
