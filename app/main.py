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
    BotCommand, BotCommandScopeAllChatAdministrators, BotCommandScopeChat, BotCommandScopeDefault,
    BufferedInputFile, CallbackQuery, ChatMemberUpdated, InlineKeyboardButton, InlineKeyboardMarkup,
    Message, MessageEntity,
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
    BOT_TOKEN, ADMIN_IDS, POLL_SECONDS, BATCH_SIZE, DEFAULT_INTERVAL_MINUTES, POST_DELAY_SECONDS,
    TIMEZONE, QUIET_START_HOUR, QUIET_END_HOUR, LOW_POST_THRESHOLD, DAILY_REPORT_HOUR, MAX_IMPORT_RANGE,
)
from . import storage
from .media import watermark_image

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing. Set it in Railway Variables.")

TZ = ZoneInfo(TIMEZONE)
bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

DEST_TYPES = {"channel", "group", "supergroup"}
GROUP_TYPES = {"group", "supergroup"}
CAPTION_TYPES = {"photo", "video", "animation", "audio", "document", "voice"}
IMPORTING: dict = {}  # admin user id -> running forward-import session


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


# ---- who is allowed: owners (ADMIN_IDS variable) + admins added with /addadmin
def all_admin_ids() -> set:
    return set(ADMIN_IDS) | storage.extra_admin_ids()


def is_admin(message: Message) -> bool:
    return bool(message.from_user and message.from_user.id in all_admin_ids())


def is_owner(message: Message) -> bool:
    return bool(message.from_user and message.from_user.id in ADMIN_IDS)


async def deny(message: Message):
    if message.chat.type != "private":
        return  # stay silent in groups
    uid = message.from_user.id if message.from_user else "?"
    await message.answer(f"Not authorized. Your Telegram user ID: <code>{uid}</code>")


async def notify_admins(text: str):
    for admin_id in all_admin_ids():
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


def origin_ref(message: Message):
    """Where to copy a saved post from. A post forwarded from one of our source channels is referenced by
    its ORIGINAL channel and ID (no duplicates, survives deleting the chat message); anything else is
    referenced by this private chat."""
    origin = getattr(message, "forward_origin", None)
    if origin is not None and getattr(origin, "type", None) == "channel":
        chat = getattr(origin, "chat", None)
        if chat is not None and storage.is_source(chat.id):
            return chat.id, origin.message_id
    return message.chat.id, message.message_id


def queue_post(message: Message, assign: bool = True):
    entities = [e.model_dump(exclude_none=True) for e in (message.entities or [])]
    caption_entities = [e.model_dump(exclude_none=True) for e in (message.caption_entities or [])]
    payload = json.dumps({"text": entities, "caption": caption_entities}, ensure_ascii=False)
    content_type = str(getattr(message.content_type, "value", message.content_type))
    file_id = message.photo[-1].file_id if message.photo else None
    source_chat, source_message = origin_ref(message)
    return storage.add_post(
        source_chat, source_message, message.caption or message.text, payload, markup_json(message),
        content_type, file_id, message.media_group_id, assign,
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


def command_args(message: Message) -> str:
    return (message.text or "").partition(" ")[2].strip()


# --------------------------------------------- bot added to / removed from chats

@dp.my_chat_member()
async def detect_bot_membership(update: ChatMemberUpdated):
    chat = update.chat
    if chat.type not in DEST_TYPES or storage.is_source(chat.id):
        return  # private chats and source channels are never destinations
    title = chat.title or str(chat.id)
    registered = any(int(c["chat_id"]) == chat.id for c in await db(storage.list_channels))

    if not bot_can_post(chat.type, update.new_chat_member):
        if registered:
            await drop_channel(chat.id, title, "bot was removed or can no longer post there")
        return
    if registered:
        return  # some other permission changed; nothing to do

    adder = update.from_user.id if update.from_user else None
    if adder not in all_admin_ids():
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


# ------------------------------------------------------------------ command menu

# The "Menu" button next to the message box. Admins see everything; other people only see /start.
ADMIN_MENU = [
    ("help", "Show all commands and how they work"),
    ("channels", "Destinations: schedule, pending, next run"),
    ("stats", "Post counts: library, used, remaining"),
    ("addchannel", "Add a channel/group by ID or @username"),
    ("removechannel", "Remove a destination (tap to choose)"),
    ("setinterval", "Set wait time between batches"),
    ("setbatch", "Set posts per batch"),
    ("pause", "Pause a destination"),
    ("resume", "Resume a paused destination"),
    ("sendnow", "Send a batch now (even in quiet hours)"),
    ("broadcast", "Reply to a post: send to all now"),
    ("sources", "List source channels"),
    ("addsource", "Add a source channel"),
    ("removesource", "Remove a source channel"),
    ("import_history", "Import old posts (forward or ID range)"),
    ("done", "Finish forward-import"),
    ("setfooter", "Set footer text under posts"),
    ("clearfooter", "Remove footer"),
    ("setwatermark", "Set watermark text for photos"),
    ("clearwatermark", "Remove watermark"),
    ("branding", "Show footer and watermark settings"),
    ("addadmin", "Give someone admin access (owner only)"),
    ("removeadmin", "Remove an admin (owner only)"),
    ("admins", "List owners and admins"),
]
GROUP_MENU = [
    ("addhere", "Send scheduled posts to this group/topic"),
    ("removehere", "Stop scheduled posts here"),
    ("pause", "Pause posting here"),
    ("resume", "Resume posting here"),
    ("sendnow", "Send a batch here now"),
    ("setinterval", "Set minutes between batches here"),
    ("setbatch", "Set posts per batch here"),
]
PUBLIC_MENU = [("start", "Start the bot")]


def _commands(items):
    return [BotCommand(command=name, description=text) for name, text in items]


async def set_admin_menu(user_id: int):
    """Show the full command menu in one admin's private chat."""
    try:
        await bot.set_my_commands(_commands(ADMIN_MENU), scope=BotCommandScopeChat(chat_id=user_id))
    except TelegramAPIError:
        logging.info("Could not set the menu for %s yet (they must open the bot and send /start).", user_id)


async def clear_admin_menu(user_id: int):
    try:
        await bot.delete_my_commands(scope=BotCommandScopeChat(chat_id=user_id))
    except TelegramAPIError:
        pass


async def setup_command_menu():
    try:
        await bot.set_my_commands(_commands(PUBLIC_MENU), scope=BotCommandScopeDefault())
        await bot.set_my_commands(_commands(GROUP_MENU), scope=BotCommandScopeAllChatAdministrators())
    except TelegramAPIError:
        logging.exception("Could not set the default command menu")
    for admin_id in all_admin_ids():
        await set_admin_menu(admin_id)


# ------------------------------------------------------------------ help

def help_text() -> str:
    return (
        "<b>Auto Forward Scheduler — Help</b>\n"
        "Tap the <b>Menu</b> button next to the message box to see every command.\n\n"
        "<b>Destinations (channels / groups)</b>\n"
        "Add the bot with an admin account (channel: as admin with 'Post messages'; group: as member) "
        "and it registers itself. Or: /addchannel -100… | @name, or /addhere inside a group/topic.\n"
        "/channels · /removechannel · /removehere\n"
        "/setinterval · /setbatch · /pause · /resume  (CHAT_ID or all)\n\n"
        "<b>Sources &amp; old posts</b>\n"
        "/sources · /addsource -100… · /removesource -100…\n"
        "/import_history — forward old posts to the bot, then /done\n"
        "/import_history SOURCE_ID FIRST_ID LAST_ID — add a range of old post IDs\n\n"
        "<b>Counts &amp; alerts</b>\n"
        "/stats — posts, used, remaining, sent in 24h\n"
        f"Low-post alert at {LOW_POST_THRESHOLD} left; alert when all posts are used; "
        + (f"daily report at {DAILY_REPORT_HOUR:02d}:00." if DAILY_REPORT_HOUR >= 0 else "daily report off.") + "\n\n"
        "<b>Footer &amp; watermark</b>\n"
        "/setfooter all | text — added under every post\n"
        "/setwatermark all | text — drawn on photos\n"
        "/clearfooter · /clearwatermark · /branding\n"
        "Use a chat ID instead of <code>all</code> for one destination, or <code>-</code> as text to switch it off there.\n\n"
        "<b>Send now</b>\n"
        "/sendnow all|CHAT_ID [count] — sends immediately, <b>even in quiet hours</b>\n"
        "/broadcast — reply to a post to send it everywhere now\n\n"
        "<b>Admins</b> (owner only)\n"
        "/addadmin USER_ID · /removeadmin USER_ID · /admins\n\n"
        "<b>How it works</b>\n"
        f"Each destination sends up to {BATCH_SIZE} posts, then waits its interval. New posts go to one "
        "destination (round-robin); when its queue is empty it sends random posts from the library without "
        f"repeats until all were used. Quiet hours: {quiet_label()}."
    )


@dp.message(CommandStart())
async def start(message: Message):
    if not is_admin(message):
        return await deny(message)
    if message.chat.type == "private":
        await set_admin_menu(message.from_user.id)
    await message.answer(help_text())


@dp.message(Command("help"))
async def help_cmd(message: Message):
    if not is_admin(message):
        return await deny(message)
    if message.chat.type == "private":
        await set_admin_menu(message.from_user.id)
    await message.answer(help_text())


# ------------------------------------------------------------------ broadcast

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


# ------------------------------------------------------- destinations: add / remove

async def resolve_chat(ref: str):
    """Chat from a numeric ID or @username, or None."""
    try:
        return await bot.get_chat(ref if ref.startswith("@") else int(ref))
    except (ValueError, TelegramAPIError):
        return None


@dp.message(Command("addchannel"))
async def add_channel_cmd(message: Message):
    if not is_admin(message):
        return await deny(message)
    tokens = [t for t in re.split(r"[|\s]+", command_args(message)) if t]
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

    chat = await resolve_chat(tokens[0])
    if chat is None:
        return await message.answer("I cannot access that chat. Send a valid ID (-100…) or @username, and add the bot there first.")
    if chat.type not in DEST_TYPES:
        return await message.answer("That is not a channel or group.")
    if storage.is_source(chat.id):
        return await message.answer("That is one of your SOURCE channels. Posts are collected from it, never sent to it.")
    try:
        me = await bot.get_chat_member(chat.id, (await bot.get_me()).id)
    except TelegramAPIError:
        return await message.answer("I cannot check my permissions there. Add the bot first.")
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
    args = command_args(message).split()
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


@dp.message(Command("removechannel"))
async def remove_channel_cmd(message: Message):
    if not is_admin(message):
        return await deny(message)
    args = command_args(message)
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
    if query.from_user.id not in all_admin_ids():
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


# ------------------------------------------------------- schedule settings

@dp.message(Command("setinterval"))
async def set_interval_cmd(message: Message):
    if not is_admin(message):
        return await deny(message)
    parts = [p.strip() for p in command_args(message).split("|")]
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
    parts = [p.strip() for p in command_args(message).split("|")]
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
            target = target_from(message, command_args(message))
        except ValueError:
            return await message.answer(f"Format: /{name} CHAT_ID   (or: /{name} all)")
        count = await db(action, target)
        await message.answer(f"{done}: {count} destination(s)." if count else "Destination not found. Check /channels.")
    return handler


_simple_target_command("pause", lambda t: storage.set_paused(t, True), "Paused")
_simple_target_command("resume", lambda t: storage.set_paused(t, False), "Resumed")


@dp.message(Command("sendnow"))
async def send_now_cmd(message: Message):
    """Start a batch immediately, even during quiet hours. Optional post count: /sendnow all 3"""
    if not is_admin(message):
        return await deny(message)
    target, count = None, None
    for token in command_args(message).replace("|", " ").split():
        if token.lower() == "all" or token.lstrip("-").isdigit() and token.startswith("-"):
            target = parse_target(token)
        elif token.isdigit():
            count = max(1, int(token))
    if target is None and message.chat.type in GROUP_TYPES:
        target = message.chat.id
    if target is None:
        return await message.answer("Format: /sendnow CHAT_ID [posts]   or   /sendnow all [posts]")
    n = await db(storage.send_now, target, count)
    if not n:
        return await message.answer("Destination not found. Check /channels.")
    await message.answer(
        f"Sending now to {n} destination(s)"
        + (f": {count} post(s) each" if count else ": a full batch each")
        + ". Works even during quiet hours."
    )


# ------------------------------------------------------- list and counts

def build_stats_text(title: str = "Statistics") -> str:
    data = storage.stats()
    lines = [f"<b>📊 {title}</b>", f"Library: <b>{data['total']}</b> post(s)"]
    for s in data["sources"]:
        lines.append(f"  • source {escape(s['title'])}: {s['posts']}")
    if data["manual"]:
        lines.append(f"  • added manually / forwarded: {data['manual']}")
    lines.append("")
    if not data["destinations"]:
        lines.append("No destinations registered.")
    for d in data["destinations"]:
        flag = " ⏸ paused" if d["paused"] else ""
        lines.append(
            f"{icon(d['kind'])} <b>{escape(d['title'])}</b>{flag}\n"
            f"   sent 24h: {d['sent_24h']} · used {d['used']}/{data['total']} · "
            f"remaining {d['remaining']} · pending {d['pending']} · cycles {d['cycles']}"
        )
    return "\n".join(lines)


@dp.message(Command("stats"))
async def stats_cmd(message: Message):
    if not is_admin(message):
        return await deny(message)
    await message.answer(await db(build_stats_text))


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


# ------------------------------------------------------- sources and old posts

@dp.message(Command("sources"))
async def sources_cmd(message: Message):
    if not is_admin(message):
        return await deny(message)
    sources = await db(storage.list_sources)
    if not sources:
        return await message.answer("No source channels yet. Use /addsource -100… (the bot must be admin there).")
    lines = [f"📥 <b>{escape(s['title'])}</b> <code>{s['chat_id']}</code> — {s['posts']} post(s)" for s in sources]
    await message.answer("<b>Source channels</b>\n" + "\n".join(lines))


@dp.message(Command("addsource"))
async def add_source_cmd(message: Message):
    if not is_admin(message):
        return await deny(message)
    ref = command_args(message).split()[0] if command_args(message) else ""
    if not ref:
        return await message.answer(
            "Format: <code>/addsource -1001234567890</code> or <code>/addsource @name</code>\n"
            "Make the bot admin in that channel first. New posts there are collected automatically."
        )
    chat = await resolve_chat(ref)
    if chat is None or chat.type != "channel":
        return await message.answer("I cannot access that channel. Add the bot as admin there and try again.")
    try:
        me = await bot.get_chat_member(chat.id, (await bot.get_me()).id)
    except TelegramAPIError:
        return await message.answer("I cannot check my permissions there. Make the bot admin first.")
    if status_of(me) not in {"administrator", "creator"}:
        return await message.answer("The bot must be an admin of that channel to read its posts.")
    title = chat.title or str(chat.id)
    await db(storage.add_source, chat.id, title)
    await message.answer(
        f"Source added: <b>{escape(title)}</b> (<code>{chat.id}</code>). New posts are collected automatically.\n"
        f"To bring in old posts use <code>/import_history {chat.id} FIRST_ID LAST_ID</code> "
        "or forward them with /import_history.\n"
        "It is no longer a destination, if it was one."
    )


@dp.message(Command("removesource"))
async def remove_source_cmd(message: Message):
    if not is_admin(message):
        return await deny(message)
    try:
        chat_id = int(command_args(message))
    except ValueError:
        return await message.answer("Format: /removesource CHAT_ID   (see /sources)")
    if await db(storage.remove_source, chat_id):
        await message.answer(
            "Source removed: its new posts are no longer collected. Posts already in the library stay.\n"
            "Note: a source listed in the SOURCE_CHANNEL_ID(S) variable comes back after a restart."
        )
    else:
        await message.answer("That ID is not a source. Check /sources.")


@dp.message(Command("import_history"))
async def import_history_cmd(message: Message):
    """Bring old posts into the library. Telegram bots cannot read a channel's history by themselves, so:
    (1) forward old posts to the bot, or (2) give a range of post IDs of a source channel."""
    if not is_admin(message):
        return await deny(message)
    if message.chat.type != "private":
        return await message.answer("Please use /import_history in a private chat with the bot.")
    args = command_args(message).split()
    uid = message.from_user.id

    if not args:
        IMPORTING[uid] = {"added": 0, "dup": 0}
        return await message.answer(
            "<b>Import mode ON</b>\n\n"
            "Forward your old posts to me now. Tip: in the channel long-press a post, tap more posts to select "
            "many, then Forward → this bot.\n"
            "• Posts from a source channel are matched by their original ID, so duplicates are skipped.\n"
            "• Do not delete the forwarded messages from this chat unless they came from a source channel.\n"
            "• Imported posts join the library and are sent in the random rotation.\n\n"
            "Send /done when finished.\n\n"
            "<b>Whole range instead?</b> <code>/import_history SOURCE_ID FIRST_ID LAST_ID</code>\n"
            "Post IDs: open a post, copy its link, the number at the end is the ID."
        )

    if len(args) != 3 or not args[1].isdigit() or not args[2].isdigit():
        return await message.answer("Format: <code>/import_history SOURCE_ID FIRST_ID LAST_ID</code>  (or just /import_history)")
    chat = await resolve_chat(args[0])
    if chat is None or not storage.is_source(chat.id):
        return await message.answer("That channel is not a source yet. Add it first with /addsource.")
    first, last = int(args[1]), int(args[2])
    if first < 1 or last < first:
        return await message.answer("FIRST_ID must be at least 1 and not bigger than LAST_ID.")
    if last - first + 1 > MAX_IMPORT_RANGE:
        return await message.answer(f"Too many at once. Maximum {MAX_IMPORT_RANGE} IDs per command; split the range.")
    added, existed = await db(storage.add_posts_range, chat.id, first, last)
    library = await db(storage.library_size)
    await message.answer(
        f"Imported IDs {first}-{last} of <b>{escape(chat.title or str(chat.id))}</b>: "
        f"{added} added, {existed} already there. Library: {library} post(s).\n\n"
        "IDs that do not exist (deleted posts, service messages) are cleaned out automatically the first "
        "time the bot tries to send them. Footer works on these posts only when the post is a photo/video "
        "that the bot saw live or you forwarded; watermark needs a post saved live or forwarded."
    )


@dp.message(Command("done"))
async def done_cmd(message: Message):
    if not is_admin(message):
        return await deny(message)
    session = IMPORTING.pop(message.from_user.id, None)
    if session is None:
        return await message.answer("No import is running. Start one with /import_history.")
    library = await db(storage.library_size)
    await message.answer(
        f"<b>Import finished</b>\nAdded: {session['added']}\nAlready saved (skipped): {session['dup']}\n"
        f"Library now: {library} post(s)."
    )


# ------------------------------------------------------- footer and watermark

MAX_FOOTER = 300
MAX_WATERMARK = 40


def _branding_setter(kind: str, command: str, limit: int, label: str):
    @dp.message(Command(command))
    async def handler(message: Message):
        if not is_admin(message):
            return await deny(message)
        head, sep, body = command_args(message).partition("|")
        body = body.strip()
        try:
            target = parse_target(head) if head.strip() else None
        except ValueError:
            target = None
        if target is None and message.chat.type in GROUP_TYPES and not sep:
            target, body = message.chat.id, head.strip()
        if target is None or not body:
            return await message.answer(
                f"Format: <code>/{command} all | your text</code>\n"
                f"or <code>/{command} CHAT_ID | your text</code>  (use <code>-</code> as text to switch it off there)."
            )
        if len(body) > limit:
            return await message.answer(f"Too long: {len(body)} characters, maximum {limit}.")
        await db(storage.set_branding, kind, target, body)
        where = "every destination" if target == "all" else f"<code>{target}</code>"
        shown = "switched off" if body == "-" else f"set to:\n{escape(body)}"
        await message.answer(f"{label} for {where} {shown}")
    return handler


def _branding_clearer(kind: str, command: str, label: str):
    @dp.message(Command(command))
    async def handler(message: Message):
        if not is_admin(message):
            return await deny(message)
        try:
            target = target_from(message, command_args(message))
        except ValueError:
            return await message.answer(f"Format: /{command} all   or   /{command} CHAT_ID")
        await db(storage.clear_branding, kind, target)
        await message.answer(f"{label} cleared for {'every destination' if target == 'all' else target}.")
    return handler


_branding_setter("footer", "setfooter", MAX_FOOTER, "Footer")
_branding_setter("watermark", "setwatermark", MAX_WATERMARK, "Watermark")
_branding_clearer("footer", "clearfooter", "Footer")
_branding_clearer("watermark", "clearwatermark", "Watermark")


@dp.message(Command("branding"))
async def branding_cmd(message: Message):
    if not is_admin(message):
        return await deny(message)
    rows = await db(storage.list_branding)
    if not rows:
        return await message.answer("No footer or watermark set. Use /setfooter and /setwatermark.")
    lines = []
    for r in rows:
        kind, _, target = r["key"].partition(":")
        where = "all destinations" if target == "all" else target
        lines.append(f"<b>{kind}</b> ({where}): {escape(r['value'])}")
    await message.answer(
        "\n".join(lines)
        + "\n\nFooter works on text posts and on photo/video/file captions. Watermark is drawn on photos "
          "the bot saved live or that you forwarded. A destination's own setting beats the 'all' one."
    )


# ------------------------------------------------------- admin management (owner only)

@dp.message(Command("addadmin"))
async def add_admin_cmd(message: Message):
    if not is_admin(message):
        return await deny(message)
    if not is_owner(message):
        return await message.answer("Only the owner (ADMIN_IDS) can add admins.")
    raw = command_args(message)
    user_id = None
    if raw.isdigit():
        user_id = int(raw)
    elif message.reply_to_message and message.reply_to_message.from_user and not raw:
        user_id = message.reply_to_message.from_user.id
    if not user_id:
        return await message.answer(
            "Format: <code>/addadmin USER_ID</code>, or reply to the person's message with /addadmin.\n"
            "Anyone can find their own ID by sending /start to this bot."
        )
    if user_id in ADMIN_IDS or user_id in storage.extra_admin_ids():
        return await message.answer("That user is already an admin.")
    await db(storage.add_admin, user_id, message.from_user.id)
    await set_admin_menu(user_id)
    await message.answer(
        f"Admin added: <code>{user_id}</code>. They must open this bot and send /start once to receive alerts."
    )


@dp.message(Command("removeadmin"))
async def remove_admin_cmd(message: Message):
    if not is_admin(message):
        return await deny(message)
    if not is_owner(message):
        return await message.answer("Only the owner (ADMIN_IDS) can remove admins.")
    raw = command_args(message)
    if not raw.isdigit():
        return await message.answer("Format: /removeadmin USER_ID   (see /admins)")
    if int(raw) in ADMIN_IDS:
        return await message.answer("Owners are set in the ADMIN_IDS variable and cannot be removed here.")
    if await db(storage.remove_admin, int(raw)):
        IMPORTING.pop(int(raw), None)
        await clear_admin_menu(int(raw))
        await message.answer(f"Admin removed: <code>{raw}</code>.")
    else:
        await message.answer("That user is not an added admin.")


@dp.message(Command("admins"))
async def admins_cmd(message: Message):
    if not is_admin(message):
        return await deny(message)
    owners = "\n".join(f"👑 <code>{i}</code>" for i in sorted(ADMIN_IDS)) or "none"
    extra = "\n".join(f"🛠 <code>{i}</code>" for i in sorted(storage.extra_admin_ids())) or "none"
    await message.answer(f"<b>Owners</b>\n{owners}\n\n<b>Added admins</b>\n{extra}")


# ------------------------------------------------------- collecting posts

@dp.channel_post()
async def capture_source_channel_post(message: Message):
    if not storage.is_source(message.chat.id):
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
        logging.info("Queued post %s from source %s", message.message_id, message.chat.id)
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
    session = IMPORTING.get(message.from_user.id)
    # While importing: save to the library only (no single destination queue) and stay quiet until /done.
    qid = await db(queue_post, message, session is None)
    if session is not None:
        session["added" if qid else "dup"] += 1
        return
    if qid:
        await message.reply("Post saved to the library and queued for one destination.")
    else:
        await message.reply("This post was already saved earlier.")


# ----------------------------------------------------------------- sending

def load_entities(post, kind: str):
    try:
        payload = json.loads(post["entities_json"] or "{}")
    except ValueError:
        return None
    result = []
    for raw in payload.get(kind) or []:
        try:
            result.append(MessageEntity.model_validate(raw))
        except Exception:
            continue
    return result or None


async def deliver(chat_id: int, thread_id, post, footer, watermark):
    """Send one saved post. Plain copy by default; with a footer/watermark the post is rebuilt:
    text posts are re-sent with the footer, media gets a new caption, photos can be watermarked."""
    markup = None
    if post["reply_markup_json"]:
        try:
            markup = InlineKeyboardMarkup.model_validate_json(post["reply_markup_json"])
        except Exception:
            logging.warning("Could not restore inline keyboard; sending without it")

    ctype = post["content_type"]
    text = post["caption"] or ""
    base = {"chat_id": chat_id}
    if thread_id:
        base["message_thread_id"] = thread_id

    # Text post + footer: a text message cannot be edited while copying, so send it again with the footer.
    if ctype == "text" and footer and text and len(text) + 2 + len(footer) <= 4096:
        await bot.send_message(
            **base, text=f"{text}\n\n{footer}", entities=load_entities(post, "text"), parse_mode=None,
            reply_markup=markup,
        )
        return

    new_caption = None
    if ctype in CAPTION_TYPES:
        # Album items usually have no caption of their own: no footer there, so it is not repeated per photo.
        if footer and (text or not post["media_group_id"]):
            candidate = f"{text}\n\n{footer}" if text else footer
            if len(candidate) <= 1024:
                new_caption = candidate

        if watermark and ctype == "photo" and post["file_id"]:
            marked = None
            try:
                raw = (await bot.download(post["file_id"])).read()
                marked = await asyncio.to_thread(watermark_image, raw, watermark)
            except Exception:
                logging.exception("Watermark failed; sending without it")
            if marked:
                caption = new_caption if new_caption is not None else (text or None)
                await bot.send_photo(
                    **base, photo=BufferedInputFile(marked, "post.jpg"), caption=caption,
                    caption_entities=load_entities(post, "caption") if caption else None,
                    parse_mode=None, reply_markup=markup,
                )
                return

    kwargs = {**base, "from_chat_id": post["source_chat_id"], "message_id": post["message_id"]}
    if markup:
        kwargs["reply_markup"] = markup
    if new_caption is not None:
        kwargs.update(caption=new_caption, caption_entities=load_entities(post, "caption"), parse_mode=None)
    await bot.copy_message(**kwargs)


async def run_alerts(chat_id: int, title: str):
    try:
        alerts = await db(storage.pop_alerts, chat_id, LOW_POST_THRESHOLD)
    except Exception:
        logging.exception("Could not check alerts for %s", chat_id)
        return
    for kind, value in alerts:
        if kind == "low":
            await notify_admins(
                f"⚠️ <b>Low posts</b> for {escape(title)} (<code>{chat_id}</code>)\n"
                f"Only <b>{value}</b> unused post(s) left before it starts repeating. "
                "Add or import more posts (/import_history)."
            )
        elif kind == "all_used":
            await notify_admins(
                f"🔁 <b>All posts used</b> on {escape(title)} (<code>{chat_id}</code>)\n"
                f"Every post has been sent once. It now repeats posts randomly (cycle #{value}). "
                "Add or import new posts for fresh content."
            )


async def process_channel(channel):
    chat_id = int(channel["chat_id"])
    if storage.is_source(chat_id):
        await db(storage.remove_channel, chat_id)
        return
    interval = int(channel["interval_minutes"])
    forced = int(channel["force_now"] or 0)
    batch = forced if forced > 0 else int(channel["batch_size"] or BATCH_SIZE)
    if forced:
        await db(storage.clear_force, chat_id)  # /sendnow handled: do it once
    thread_id = channel["thread_id"]
    footer = await db(storage.get_branding, "footer", chat_id)
    watermark = await db(storage.get_branding, "watermark", chat_id)
    sent = attempts = 0
    max_attempts = batch * 3 + 50  # posts that no longer exist are skipped without counting as sent

    while sent < batch and attempts < max_attempts:
        attempts += 1
        post = await db(storage.get_next_post, chat_id)
        if not post:
            break

        try:
            await deliver(chat_id, thread_id, post, footer, watermark)
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
            if "message to copy not found" in text or "message_id_invalid" in text or "can't be copied" in text:
                logging.info("Source post %s no longer exists / cannot be copied; removing it", post["id"])
                await db(storage.delete_post, post["id"])
                continue
            if lost_access(exc):
                await drop_channel(chat_id, channel["title"], "chat not found or bot has no posting rights")
                return
            if "thread not found" in text or ("topic" in text and "closed" in text):
                await drop_channel(chat_id, channel["title"], "the target topic no longer exists or is closed")
                return
            logging.exception("Post %s rejected for %s; skipping it", post["id"], chat_id)
            await db(storage.finish_channel_post, chat_id, post["id"], False)
            continue
        except TelegramAPIError:
            logging.exception("Send failed for %s; retrying in 1 minute", chat_id)
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
    await run_alerts(chat_id, channel["title"])


async def maybe_daily_report(now_local: datetime):
    if DAILY_REPORT_HOUR < 0 or now_local.hour != DAILY_REPORT_HOUR:
        return
    today = now_local.strftime("%Y-%m-%d")
    if await db(storage.get_setting, "report_date") == today:
        return
    await db(storage.set_setting, "report_date", today)
    text = await db(build_stats_text, f"Daily report {now_local.strftime('%d %b')}")
    await notify_admins(text)
    await db(storage.prune_send_log)


async def scheduler():
    while True:
        try:
            now_local = datetime.now(TZ)
            quiet = in_quiet_hours(now_local)
            channel = await db(storage.next_due_channel, datetime.now(timezone.utc).isoformat(), quiet)
            if channel:
                await process_channel(channel)
            await maybe_daily_report(now_local)
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("Scheduler error; continuing in 30 seconds")
            await asyncio.sleep(30)
        await asyncio.sleep(POLL_SECONDS)


async def main():
    await db(storage.init_db)
    await setup_command_menu()
    if not ADMIN_IDS:
        logging.warning("ADMIN_IDS is empty: nobody can use admin commands or auto-register chats.")
    if not storage.source_ids():
        logging.warning("No source channel yet: set SOURCE_CHANNEL_ID(S) or use /addsource.")
    logging.info(
        "Started: batch=%s, default interval=%s min, quiet hours=%s, sources=%s",
        BATCH_SIZE, DEFAULT_INTERVAL_MINUTES, quiet_label(), sorted(storage.source_ids()),
    )
    await asyncio.gather(dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types()), scheduler())


if __name__ == "__main__":
    asyncio.run(main())
