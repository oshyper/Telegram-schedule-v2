import os
import re


def _int(name: str, default: int, minimum: int | None = None, maximum: int | None = None) -> int:
    raw = os.getenv(name, "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        value = default
    if minimum is not None:
        value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}
SOURCE_CHANNEL_ID = _int("SOURCE_CHANNEL_ID", 0)
# One or more source channels. SOURCE_CHANNEL_ID (single) and SOURCE_CHANNEL_IDS (comma separated) both work.
SOURCE_IDS = {int(x) for x in re.findall(r"-?\d+", os.getenv("SOURCE_CHANNEL_IDS", "") + "," + os.getenv("SOURCE_CHANNEL_ID", ""))}
SOURCE_IDS.discard(0)
DATABASE_PATH = os.getenv("DATABASE_PATH", "./data/bot.db")

POLL_SECONDS = _int("POLL_SECONDS", 10, minimum=5)
BATCH_SIZE = _int("BATCH_SIZE", 12, minimum=1)
DEFAULT_INTERVAL_MINUTES = _int("DEFAULT_INTERVAL_MINUTES", 200, minimum=1)
POST_DELAY_SECONDS = _int("POST_DELAY_SECONDS", 3, minimum=0)

# Quiet hours: no posts are sent from QUIET_START_HOUR up to (not including) QUIET_END_HOUR.
# Default: 00:00 - 07:00 India time.
TIMEZONE = os.getenv("TIMEZONE", "Asia/Kolkata").strip() or "Asia/Kolkata"
QUIET_START_HOUR = _int("QUIET_START_HOUR", 0, minimum=0, maximum=23)
QUIET_END_HOUR = _int("QUIET_END_HOUR", 7, minimum=0, maximum=23)

# Alerts and reports
LOW_POST_THRESHOLD = _int("LOW_POST_THRESHOLD", 10, minimum=0)      # 0 = no low-post alert
DAILY_REPORT_HOUR = _int("DAILY_REPORT_HOUR", 8, minimum=-1, maximum=23)  # local hour; -1 = no daily report
MAX_IMPORT_RANGE = _int("MAX_IMPORT_RANGE", 5000, minimum=1)
