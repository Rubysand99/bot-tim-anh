"""
Cấu hình, logging, đối tượng bot dùng chung, kiểm tra quyền/cooldown, cache chủ đề.

(Tách ra từ bot.py cũ — xem README mục Kiến trúc.)
"""

from categories import CATEGORIES, _LEGACY_STATIC_CATEGORIES, get_keywords, keywords_display
from discord import app_commands
import asyncio
from discord.ext import commands, tasks
import crawl_job
from datetime import datetime, timezone, timedelta
import db
import discord
import logging
import os
import time


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


logger = logging.getLogger("bot-tim-anh")


# Giảm bớt log rác từ discord.py (mặc định khá ồn ở mức INFO)
logging.getLogger("discord").setLevel(logging.WARNING)


def _parse_id_set(env_name: str) -> set:
    raw = os.getenv(env_name, "")
    return {int(x) for x in raw.split(",") if x.strip().isdigit()}


# ============================================================
# Kênh log Discord — mọi log ở mức WARNING/ERROR trong bot (lỗi crawl, lỗi
# DB, tác vụ không thực hiện được...) sẽ tự động được gửi vào kênh này,
# ngoài việc vẫn in ra log thường (log của Railway) như trước. Bỏ trống
# LOG_CHANNEL_ID = tắt tính năng này, chỉ log ra Railway như cũ.
# ============================================================

_log_channel_raw = os.getenv("LOG_CHANNEL_ID", "").strip()


LOG_CHANNEL_ID = int(_log_channel_raw) if _log_channel_raw.isdigit() else None


async def _send_log_channel_message(content: str = None, embed: discord.Embed = None):
    if not LOG_CHANNEL_ID:
        return None
    channel = bot.get_channel(LOG_CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(LOG_CHANNEL_ID)
        except Exception:
            return None
    try:
        return await channel.send(content=content, embed=embed)
    except Exception:
        return None


class DiscordAlertHandler(logging.Handler):
    """
    logging.Handler tự động chuyển tiếp mọi log WARNING/ERROR sang kênh
    Discord (LOG_CHANNEL_ID), không cần sửa từng chỗ logger.warning() rải
    rác trong code. Chạy an toàn dù được gọi từ thread khác (executor) nhờ
    asyncio.run_coroutine_threadsafe.
    """

    def __init__(self, level=logging.WARNING):
        super().__init__(level)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if not LOG_CHANNEL_ID:
                return
            loop = getattr(bot, "loop", None)
            if loop is None or not loop.is_running():
                return
            msg = self.format(record)
            if len(msg) > 1900:
                msg = msg[:1900] + "…"
            icon = "🔴" if record.levelno >= logging.ERROR else "🟡"
            content = f"{icon} `{record.name}` {msg}"
            asyncio.run_coroutine_threadsafe(_send_log_channel_message(content=content), loop)
        except Exception:
            pass  # logging handler không bao giờ được phép raise lỗi ra ngoài


# ============================================================
# Cấu hình quyền hạn / giới hạn dùng lệnh
# ============================================================

# Admin: bỏ qua mọi giới hạn (cooldown, kênh, role) + dùng được lệnh quản trị.
# Có thể thêm nhiều admin qua biến môi trường ADMIN_USER_IDS="id1,id2,..."
# trên Railway, không cần sửa code. Mặc định luôn có ID dưới đây.
ADMIN_IDS = _parse_id_set("ADMIN_USER_IDS") or {846332174734983219}


# Nếu set (comma-separated ID trên Railway), lệnh ảnh chỉ dùng được ở các kênh này.
# Để trống (mặc định) = không giới hạn kênh.
ALLOWED_CHANNEL_IDS = _parse_id_set("ALLOWED_CHANNEL_IDS")


# Nếu set, chỉ member có 1 trong các role này mới dùng được lệnh ảnh.
# Để trống (mặc định) = không giới hạn role.
ALLOWED_ROLE_IDS = _parse_id_set("ALLOWED_ROLE_IDS")


COOLDOWN_SECONDS = 8


_last_used_at = {}


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


# Cache cấu hình riêng theo guild (per-server) — tránh mỗi lần dùng lệnh đều
# phải round-trip MongoDB để đọc ALLOWED_CHANNEL_IDS/ALLOWED_ROLE_IDS của
# server đó. /config chủ động xoá cache ngay sau khi sửa nên vẫn có hiệu
# lực tức thì.
_guild_config_cache = {}  # guild_id -> (data, timestamp)


GUILD_CONFIG_CACHE_TTL_SECONDS = 30


def _invalidate_guild_config_cache(guild_id: int) -> None:
    _guild_config_cache.pop(guild_id, None)


async def _get_guild_config_cached(guild_id: int) -> dict:
    now = time.monotonic()
    cached = _guild_config_cache.get(guild_id)
    if cached and (now - cached[1]) < GUILD_CONFIG_CACHE_TTL_SECONDS:
        return cached[0]
    data = await bot.loop.run_in_executor(None, db.get_guild_config, guild_id)
    _guild_config_cache[guild_id] = (data, now)
    return data


async def check_access(user_id: int, guild_id, channel_id: int, roles) -> str:
    """
    Trả về thông báo lỗi nếu bị chặn, chuỗi rỗng nếu được phép dùng lệnh.
    Ưu tiên cấu hình riêng của server (đặt qua /config) nếu server đó đã
    từng cấu hình; nếu chưa, dùng ALLOWED_CHANNEL_IDS/ALLOWED_ROLE_IDS từ
    biến môi trường (mặc định toàn cục, giữ tương thích ngược). Ở DM
    (guild_id=None) luôn dùng biến môi trường vì không có server nào để tra.
    """
    if is_admin(user_id):
        return ""

    if guild_id is None:
        allowed_channels, allowed_roles = ALLOWED_CHANNEL_IDS, ALLOWED_ROLE_IDS
    else:
        cfg = await _get_guild_config_cached(guild_id)
        if cfg.get("configured"):
            # Server đã tự cấu hình qua /config -> dùng RIÊNG, thay thế hoàn toàn
            # mặc định toàn cục (đúng như README). Danh sách rỗng nghĩa là admin
            # chủ động bỏ giới hạn (clear_channels/clear_roles), KHÔNG được quay
            # về ALLOWED_*_IDS của biến môi trường như trước.
            allowed_channels = set(cfg["allowed_channel_ids"])
            allowed_roles = set(cfg["allowed_role_ids"])
        else:
            allowed_channels, allowed_roles = ALLOWED_CHANNEL_IDS, ALLOWED_ROLE_IDS

    if allowed_channels and channel_id not in allowed_channels:
        return "⚠️ Lệnh này chỉ dùng được ở kênh được chỉ định."
    if allowed_roles:
        role_ids = {r.id for r in roles} if roles else set()
        if not (role_ids & allowed_roles):
            return "⚠️ Bạn không có quyền dùng lệnh này."
    return ""


def check_cooldown(user_id: int) -> float:
    """Trả về số giây còn phải chờ (0 nếu được dùng ngay). Admin luôn = 0."""
    if is_admin(user_id):
        return 0
    last = _last_used_at.get(user_id, 0)
    elapsed = time.time() - last
    if elapsed < COOLDOWN_SECONDS:
        return round(COOLDOWN_SECONDS - elapsed, 1)
    return 0


def mark_used(user_id: int) -> None:
    _last_used_at[user_id] = time.time()


def _relative_time_vi(dt) -> str:
    if dt is None:
        return "chưa có"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    seconds = (datetime.now(timezone.utc) - dt).total_seconds()
    if seconds < 60:
        return "vừa xong"
    minutes = seconds / 60
    if minutes < 60:
        return f"{int(minutes)} phút trước"
    hours = minutes / 60
    if hours < 24:
        return f"{round(hours, 1)} giờ trước"
    return f"{round(hours / 24, 1)} ngày trước"


# Khởi tạo Bot với Prefix "!"
intents = discord.Intents.default()


intents.message_content = True


bot = commands.Bot(command_prefix="!", intents=intents)


# Gắn handler để log WARNING/ERROR tự động chuyển tiếp sang kênh Discord
# (bot phải được tạo trước vì handler tham chiếu tới biến `bot`).
_discord_alert_handler = DiscordAlertHandler()


logger.addHandler(_discord_alert_handler)


crawl_job.logger.addHandler(_discord_alert_handler)


# ============================================================
# Heartbeat ping — gửi ngay lúc bot khởi động, sau đó lặp lại mỗi 10 phút,
# vào kênh LOG_CHANNEL_ID. Xoá tin ping CŨ trước khi gửi ping MỚI để tránh
# làm trôi các tin nhắn khác trong kênh log (chỉ giữ 1 tin ping mới nhất).
# ============================================================

def _format_latency_ms():
    """bot.latency có thể là NaN nếu chưa có phép đo heartbeat websocket nào."""
    latency = bot.latency
    if latency != latency:  # NaN != NaN, cách kiểm tra NaN không cần import math
        return "N/A"
    return f"{round(latency * 1000)}ms"


# ============================================================
# Chủ đề (category): gộp CATEGORIES tĩnh (categories.py) với category
# admin thêm qua Discord (lưu trong MongoDB) — cache 30 giây để tránh mỗi
# lần dùng lệnh (kể cả mỗi ký tự gõ trong autocomplete) đều phải round-trip
# MongoDB. /addcategory, /editcategory, /removecategory chủ động xoá cache
# ngay sau khi sửa nên vẫn có hiệu lực tức thì, không phải đợi 30 giây.
# ============================================================

_categories_cache = {"data": None, "at": 0.0}


CATEGORIES_CACHE_TTL_SECONDS = 30


def _invalidate_categories_cache() -> None:
    _categories_cache["at"] = 0.0


def _channel_allows_nsfw(channel) -> bool:
    """
    True nếu kênh đã được Discord đánh dấu Age-Restricted (NSFW). DM hoặc
    kênh không có thuộc tính is_nsfw (hiếm) coi như KHÔNG cho phép, để an
    toàn hơn là mặc định cho phép.
    """
    is_nsfw_fn = getattr(channel, "is_nsfw", None)
    if is_nsfw_fn is None:
        return False
    try:
        return bool(is_nsfw_fn())
    except Exception:
        return False


async def get_all_categories_async() -> dict:
    now = time.monotonic()
    if _categories_cache["data"] is not None and (now - _categories_cache["at"]) < CATEGORIES_CACHE_TTL_SECONDS:
        return _categories_cache["data"]

    def fetch():
        merged = dict(CATEGORIES)
        merged.update(db.get_custom_categories())
        return merged

    try:
        result = await bot.loop.run_in_executor(None, fetch)
    except Exception as e:
        # Từ v1.11.0 toàn bộ chủ đề nằm trong MongoDB, nên KHÔNG được cache kết
        # quả rỗng khi đọc DB lỗi (trước đây cache {} 30 giây -> mọi lệnh báo
        # "chủ đề không tồn tại" chỉ vì Mongo chập chờn 1 lần). Có bản cache cũ
        # thì dùng tạm (và thử lại sớm ở lần gọi sau), chưa có thì trả {} nhưng
        # không lưu vào cache.
        stale = _categories_cache["data"]
        if stale is not None:
            logger.warning(f"Không đọc được custom categories từ DB, dùng tạm bản cache cũ: {e}")
            return stale
        logger.warning(f"Không đọc được custom categories từ DB (chưa có cache): {e}")
        return dict(CATEGORIES)

    _categories_cache["data"] = result
    _categories_cache["at"] = now
    return result


async def category_autocomplete(interaction: discord.Interaction, current: str):
    all_cats = await get_all_categories_async()
    current_lower = current.lower()
    matches = [
        app_commands.Choice(name=info["label"], value=key)
        for key, info in all_cats.items()
        if current_lower in key.lower() or current_lower in info["label"].lower()
    ]
    return matches[:25]
