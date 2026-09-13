import asyncio
import concurrent.futures
import logging
import os
import random
import sys
import time
from datetime import datetime, timezone, timedelta

import discord
import requests
from discord import app_commands
from discord.ext import commands, tasks
from keep_alive import keep_alive
from categories import CATEGORIES, get_keywords, keywords_display
import db
import crawl_job
from version import __version__, __description__

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
# ngoài việc vẫn in ra log thường (Render logs) như trước. Bỏ trống
# LOG_CHANNEL_ID = tắt tính năng này, chỉ log ra Render như cũ.
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
# trên Render, không cần sửa code. Mặc định luôn có ID dưới đây.
ADMIN_IDS = _parse_id_set("ADMIN_USER_IDS") or {846332174734983219}

# Nếu set (comma-separated ID trên Render), lệnh ảnh chỉ dùng được ở các kênh này.
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
        allowed_channels = set(cfg["allowed_channel_ids"]) if cfg["allowed_channel_ids"] else ALLOWED_CHANNEL_IDS
        allowed_roles = set(cfg["allowed_role_ids"]) if cfg["allowed_role_ids"] else ALLOWED_ROLE_IDS

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

_persistent_view_ready = False
_last_ping_message_id = None


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


@tasks.loop(minutes=10)
async def heartbeat_ping():
    global _last_ping_message_id
    if not LOG_CHANNEL_ID:
        return

    channel = bot.get_channel(LOG_CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(LOG_CHANNEL_ID)
        except Exception as e:
            logger.warning(f"Không lấy được kênh log (LOG_CHANNEL_ID={LOG_CHANNEL_ID}): {e}")
            return

    if _last_ping_message_id:
        try:
            old_msg = await channel.fetch_message(_last_ping_message_id)
            await old_msg.delete()
        except discord.NotFound:
            pass  # tin cũ đã bị xoá thủ công từ trước, bỏ qua
        except Exception as e:
            logger.warning(f"Không xoá được tin ping cũ: {e}")

    embed = discord.Embed(
        title="✅ Bot đang hoạt động",
        description=f"Độ trễ hiện tại: **{_format_latency_ms()}**",
        color=discord.Color.green(),
        timestamp=datetime.now(timezone.utc),
    )
    try:
        msg = await channel.send(embed=embed)
        _last_ping_message_id = msg.id
    except Exception as e:
        logger.warning(f"Không gửi được heartbeat ping vào kênh log: {e}")


@heartbeat_ping.before_loop
async def _before_heartbeat_ping():
    await bot.wait_until_ready()


# ============================================================
# Self-test soak: tự đăng 1 embed ảnh vào LOG_CHANNEL_ID lúc khởi động, rồi
# mỗi 10 giây tự lấy ảnh tiếp theo (đúng hàm db.get_next_image mà nút
# Trước/Sau thật dùng) và edit lại tin nhắn — KHÔNG qua interaction Discord
# (bot không thể tự "bấm" nút của chính nó, message.edit() không có giới
# hạn 3 giây như interaction). Mục đích: chạy dài hạn để phát hiện nếu
# MongoDB thỉnh thoảng chậm/lỗi bất thường mà chỉ số liệu 1 lần không thấy
# được — nếu có bất thường sẽ tự log WARNING (tự động hiện trong kênh này
# luôn nhờ DiscordAlertHandler, không cần gửi tay).
# ============================================================

SELF_TEST_INTERVAL_SECONDS = 10
SELF_TEST_SLOW_THRESHOLD_SECONDS = 1.0
_self_test_message_id = None


@tasks.loop(seconds=SELF_TEST_INTERVAL_SECONDS)
async def self_test_loop():
    global _self_test_message_id
    if not LOG_CHANNEL_ID:
        return

    channel = bot.get_channel(LOG_CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(LOG_CHANNEL_ID)
        except Exception as e:
            logger.warning(f"[self-test] Không lấy được kênh log: {e}")
            return

    all_cats = await get_all_categories_async()
    if not all_cats:
        return
    category_key, info = random.choice(list(all_cats.items()))

    t0 = time.monotonic()
    try:
        # peek_random_image (KHÔNG phải get_next_image) — không đánh dấu
        # last_sent_at, tránh tự động "dùng hết" ảnh thật của user. Xem
        # ghi chú trong db.py.
        doc = await bot.loop.run_in_executor(None, db.peek_random_image, category_key)
    except Exception as e:
        logger.warning(f"[self-test] Lỗi đọc MongoDB cho '{category_key}': {e}")
        return
    fetch_elapsed = time.monotonic() - t0

    if fetch_elapsed >= SELF_TEST_SLOW_THRESHOLD_SECONDS:
        logger.warning(f"[self-test] Đọc MongoDB cho '{category_key}' chậm bất thường: {fetch_elapsed:.2f}s")

    if not doc:
        # Category này đang không có ảnh nào -> bỏ qua vòng này, thử category
        # khác ở lần lặp sau, không phải lỗi thật.
        return
    url = doc["image_url"]

    if not db.is_valid_image_url(url):
        # Tự chữa lành: URL xấu này sẽ luôn crash bất kỳ ai bốc trúng nó
        # (kể cả user thật qua /img) -> xoá luôn khỏi DB ngay khi self-test
        # phát hiện ra, không cần đợi tới khi Discord từ chối embed.
        logger.warning(
            f"[self-test] Phát hiện + tự xoá URL ảnh không hợp lệ trong '{category_key}' "
            f"(dài {len(url)} ký tự)."
        )
        await bot.loop.run_in_executor(None, db.delete_images_by_url, [url])
        return

    embed = _build_image_embed(f"[self-test] {info['label']}", url)
    embed.set_footer(text=f"Nguồn: kho ảnh đã crawl sẵn · fetch {fetch_elapsed*1000:.0f}ms")

    t1 = time.monotonic()
    try:
        if _self_test_message_id:
            try:
                msg = await channel.fetch_message(_self_test_message_id)
                await msg.edit(embed=embed)
            except discord.NotFound:
                msg = await channel.send(embed=embed)
                _self_test_message_id = msg.id
        else:
            msg = await channel.send(embed=embed)
            _self_test_message_id = msg.id
    except Exception as e:
        logger.warning(f"[self-test] Lỗi gửi/sửa tin nhắn: {e}")
        return
    edit_elapsed = time.monotonic() - t1

    if edit_elapsed >= SELF_TEST_SLOW_THRESHOLD_SECONDS:
        logger.warning(f"[self-test] Gửi/sửa tin nhắn Discord chậm bất thường: {edit_elapsed:.2f}s")


@self_test_loop.before_loop
async def _before_self_test_loop():
    await bot.wait_until_ready()


# custom_id -> hàm xử lý thật (đã tách khỏi callback trong từng View — xem
# _do_epaginator_save, _do_showcase_start, và _paginator_navigate).
_COMPONENT_DISPATCH_TABLE = {
    "paginator:prev": lambda i: _paginator_navigate(i, -1, PAGINATOR_VIEW),
    "paginator:next": lambda i: _paginator_navigate(i, +1, PAGINATOR_VIEW),
    "epaginator:prev": lambda i: _paginator_navigate(i, -1, EPHEMERAL_PAGINATOR_VIEW),
    "epaginator:next": lambda i: _paginator_navigate(i, +1, EPHEMERAL_PAGINATOR_VIEW),
    "epaginator:save": lambda i: _do_epaginator_save(i),
    "showcase:start": lambda i: _do_showcase_start(i),
}


@bot.event
async def on_interaction(interaction: discord.Interaction):
    """
    Dispatch thủ công cho MỌI nút bấm (component interaction), bỏ qua hẳn
    cơ chế add_view()/ViewStore nội bộ của discord.py.

    LÝ DO: đã xác nhận qua log thực tế (log [RAW interaction] thêm tạm để
    chẩn đoán) rằng Discord luôn gửi đúng interaction tới tiến trình này —
    kể cả khi đã tạo project Railway mới (gateway mới hoàn toàn) và reset
    token (loại bỏ khả năng có tiến trình khác dùng chung token). Vậy
    interaction ĐẾN đúng nơi, nhưng cơ chế add_view()/ViewStore của
    discord.py 2.7.1 không khớp được item -> tự huỷ âm thầm, không log gì
    (xem discord/ui/view.py, ViewStore.dispatch_view(): "If 3 lookups
    failed at this point then just discard it"). Chưa xác định được lý do
    sâu xa vì sao khớp thất bại (nghi vấn liên quan _get_snapshot_diff() bị
    gọi lại nhiều lần do store_view() tự động chạy mỗi lần gửi/sửa tin
    nhắn có view), nhưng vì on_interaction() ở đây LUÔN nhận được sự kiện
    đúng, nên né hẳn ViewStore và tự gọi thẳng hàm xử lý là chắc ăn nhất.

    An toàn với @discord.ui.button cũ: sự kiện 'interaction' do
    ConnectionState.dispatch() bắn ra SONG SONG, không thay thế, cơ chế
    ViewStore — nên nếu 1 ngày nào đó bản vá discord.py mới làm ViewStore
    hoạt động lại, callback trong View (giờ chỉ còn gọi qua cùng 1 hàm dùng
    chung) có thể chạy thêm 1 lần nữa. _timed_defer() đã tự bắt lỗi
    "interaction đã được phản hồi" trong trường hợp đó (chỉ log warning,
    không crash) nên không nguy hiểm, nhưng để né hẳn, kiểm tra
    is_done() trước khi tự dispatch.
    """
    if interaction.type != discord.InteractionType.component or interaction.response.is_done():
        return
    custom_id = interaction.data.get("custom_id") if interaction.data else None
    handler = _COMPONENT_DISPATCH_TABLE.get(custom_id)
    if handler is None:
        return
    try:
        await handler(interaction)
    except Exception as e:
        logger.warning(f"[manual dispatch] Lỗi khi xử lý nút '{custom_id}': {e}")


@bot.event
async def on_ready():
    global _persistent_view_ready
    logger.info(f"Bot đã đăng nhập thành công với tên: {bot.user}")
    logger.info(f"[diag] discord.py version: {discord.__version__} | file: {discord.__file__}")

    # Làm nóng kết nối MongoDB ngay lúc khởi động, chạy NỀN (không await ở
    # đây, không chặn các bước bên dưới). Lý do: get_db() lần gọi ĐẦU TIÊN
    # sau khi restart phải làm TLS handshake + tra cứu SRV DNS của Atlas,
    # từng đo được mất tới 3+ giây (xem log "[Mongo chậm] get_db mất 3068ms").
    # Nếu không làm nóng trước, chi phí 3 giây này sẽ rơi đúng vào request
    # Mongo đầu tiên nào đó — thường là ngay khi có người bấm nút ngay sau
    # khi vừa deploy xong — khiến dù code đã defer() đúng thứ tự vẫn có thể
    # timeout, vì bản thân việc lấy connection (chạy trong executor thread)
    # có thể làm event loop khựng lại đủ lâu để defer() gửi không kịp.
    async def _warmup_mongo():
        start = time.monotonic()
        try:
            await bot.loop.run_in_executor(None, db.get_db)
            elapsed = time.monotonic() - start
            logger.info(f"Đã làm nóng kết nối MongoDB lúc khởi động ({elapsed:.1f}s).")
        except Exception as e:
            logger.warning(f"Lỗi khi làm nóng kết nối MongoDB lúc khởi động: {e}")

    asyncio.create_task(_warmup_mongo())

    try:
        synced = await bot.tree.sync()
        logger.info(f"Đã đồng bộ {len(synced)} slash command(s).")
    except Exception as e:
        logger.warning(f"Lỗi khi đồng bộ slash command: {e}")
    if not _persistent_view_ready:
        bot.add_view(PAGINATOR_VIEW)
        bot.add_view(EPHEMERAL_PAGINATOR_VIEW)
        bot.add_view(SHOWCASE_VIEW)
        _persistent_view_ready = True
        logger.info("Đã đăng ký các persistent view (nút ảnh + showcase) — hoạt động cả sau khi bot restart.")
    if not heartbeat_ping.is_running():
        heartbeat_ping.start()  # tự gửi ping ngay lần đầu, sau đó lặp lại mỗi 10 phút
    if not self_test_loop.is_running():
        # ĐÃ TẮT theo yêu cầu — đã dùng self-test để chẩn đoán xong nguyên
        # nhân "không phản hồi kịp thời" (xác nhận: MongoDB ổn định, ~3.1%
        # lệnh gọi Discord API chậm bất thường là baseline hạ tầng bình
        # thường, không phải bug). Bỏ comment dòng dưới nếu cần bật lại để
        # điều tra thêm trong tương lai.
        # self_test_loop.start()
        pass
    logger.info("------------------------------------------")


# ============================================================
# Lệnh ping — kiểm tra độ trễ của bot
# ============================================================

@bot.tree.command(name="ping", description="Kiểm tra độ trễ của bot")
async def ping_slash(interaction: discord.Interaction):
    await interaction.response.send_message(f"🏓 Pong! Độ trễ: **{_format_latency_ms()}**")


@bot.command(name="ping", help="Kiểm tra độ trễ của bot")
async def ping_prefix(ctx):
    await ctx.send(f"🏓 Pong! Độ trễ: **{_format_latency_ms()}**")


# ============================================================
# Lệnh version — xem số bản hiện tại + mô tả ngắn (sửa trong version.py)
# ============================================================

def _version_embed() -> discord.Embed:
    return discord.Embed(
        title=f"🏷️ Phiên bản v{__version__}",
        description=__description__,
        color=discord.Color.blurple(),
    )


@bot.tree.command(name="version", description="Xem số phiên bản hiện tại của bot")
async def version_slash(interaction: discord.Interaction):
    await interaction.response.send_message(embed=_version_embed())


@bot.command(name="version", help="Xem số phiên bản hiện tại của bot")
async def version_prefix(ctx):
    await ctx.send(embed=_version_embed())


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
        try:
            merged.update(db.get_custom_categories())
        except Exception as e:
            logger.warning(f"Không đọc được custom categories từ DB: {e}")
        return merged

    result = await bot.loop.run_in_executor(None, fetch)
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


# ============================================================
# Lấy ảnh: CHỈ đọc từ MongoDB (kho ảnh đã crawl sẵn qua crawl_job.py, chạy
# định kỳ mỗi 2 tiếng qua GitHub Actions cron). KHÔNG cào Pinterest trực
# tiếp lúc user đang chờ phản hồi nữa — đây từng là nguồn gây timeout "không
# phản hồi kịp thời" khó lường (worst case cũ có thể tới 36s/17s tuỳ bản).
# Nếu category hết ảnh khả dụng, báo ngay cho user thay vì bắt chờ cào —
# đợi tối đa 2 tiếng tới lần crawl job kế tiếp là sẽ có ảnh mới.
# ============================================================

SLOW_DB_THRESHOLD_SECONDS = 3
CRAWL_INTERVAL_HOURS = 2  # phải khớp lịch cron trong .github/workflows/main.yml
DEFER_SLOW_THRESHOLD_SECONDS = 1.0


async def _timed_defer(interaction: discord.Interaction, ephemeral: bool = False) -> bool:
    """
    Wrapper dùng chung cho MỌI lệnh gọi interaction.response.defer() trong
    bot — đo thời gian THẬT SỰ của chính lệnh gọi này (khác với self-test
    soak trước đây, vốn chỉ đo tốc độ đọc MongoDB + message.edit() thường,
    KHÔNG đo được defer() thật vì bot không thể tự tạo interaction để gọi).
    defer() dùng API endpoint riêng (POST /interactions/.../callback), có
    thể có đặc tính tốc độ khác hẳn message.edit() (PATCH /messages/...).

    Log MỌI lần gọi (không chỉ lúc lỗi) ở mức INFO, và WARNING nếu vượt
    DEFER_SLOW_THRESHOLD_SECONDS — để có dữ liệu thống kê thật từ production,
    xác nhận defer() có thực sự là nguồn gây "không phản hồi kịp thời" hay
    không, thay vì suy luận gián tiếp qua self-test.

    Trả về True nếu defer() thành công, False nếu lỗi (caller nên return
    ngay khi nhận False, vì interaction có thể đã không còn dùng được).
    """
    t0 = time.monotonic()
    try:
        await interaction.response.defer(ephemeral=ephemeral)
    except discord.NotFound:
        elapsed = time.monotonic() - t0
        logger.warning(f"[defer] Interaction đã hết hạn trước khi kịp defer() (chờ {elapsed:.2f}s).")
        return False
    except Exception as e:
        elapsed = time.monotonic() - t0
        logger.warning(f"[defer] Lỗi không xác định khi defer() (sau {elapsed:.2f}s): {e}")
        return False

    elapsed = time.monotonic() - t0
    if elapsed >= DEFER_SLOW_THRESHOLD_SECONDS:
        logger.warning(f"[defer] Chậm bất thường: {elapsed:.2f}s")
    else:
        logger.info(f"[defer] OK: {elapsed*1000:.0f}ms")
    return True


async def _next_crawl_eta_text() -> str:
    """
    Ước tính thời gian tới lần crawl_job.py kế tiếp, dựa vào last_crawl_time
    (lưu trong DB mỗi khi crawl job chạy xong) + chu kỳ CRAWL_INTERVAL_HOURS.
    Dùng để báo user biết cần chờ bao lâu khi 1 category hết ảnh khả dụng,
    thay vì chỉ nói chung chung "thử lại sau".
    """
    last_crawl = await bot.loop.run_in_executor(None, db.get_last_crawl_time)
    if not last_crawl:
        return "chưa rõ lịch crawl (chưa từng chạy job crawl nào)"

    if last_crawl.tzinfo is None:
        last_crawl = last_crawl.replace(tzinfo=timezone.utc)
    next_crawl = last_crawl + timedelta(hours=CRAWL_INTERVAL_HOURS)
    remaining_seconds = (next_crawl - datetime.now(timezone.utc)).total_seconds()

    if remaining_seconds <= 0:
        return "sắp có (đang chờ job crawl chạy)"

    minutes = int(remaining_seconds // 60)
    if minutes < 1:
        return "chưa đầy 1 phút nữa"
    if minutes < 60:
        return f"khoảng {minutes} phút nữa"
    hours, mins = divmod(minutes, 60)
    if mins == 0:
        return f"khoảng {hours} tiếng nữa"
    return f"khoảng {hours} tiếng {mins} phút nữa"


async def _fetch_next_image_url(category_key: str, exclude_urls: list):
    """
    Lấy 1 URL ảnh khả dụng cho category, tự chữa lành nếu bốc trúng URL
    không hợp lệ (vd dài hơn 2048 ký tự — Discord sẽ từ chối cả embed với
    lỗi 400 "Invalid Form Body", phát hiện qua self-test soak). Nếu gặp URL
    xấu: xoá luôn khỏi DB (không bao giờ bị bốc trúng lại lần nữa) và thử
    lấy ảnh khác, tối đa 3 lần thử để tránh vòng lặp vô hạn nếu DB có nhiều
    URL xấu liên tiếp.
    """
    exclude_urls = list(exclude_urls)  # tránh sửa list gốc của caller

    def fetch_db():
        try:
            doc = db.get_next_image(category_key, exclude_urls)
            return doc["image_url"] if doc else None
        except Exception as e:
            logger.warning(f"Lỗi đọc MongoDB khi lấy ảnh cho '{category_key}': {e}")
            return None

    for attempt in range(3):
        t0 = time.monotonic()
        url = await bot.loop.run_in_executor(None, fetch_db)
        db_elapsed = time.monotonic() - t0
        if db_elapsed > SLOW_DB_THRESHOLD_SECONDS:
            logger.warning(f"Đọc MongoDB cho '{category_key}' chậm bất thường: {db_elapsed:.1f}s")
        else:
            logger.info(f"[perf] Đọc MongoDB cho '{category_key}': {db_elapsed:.2f}s")

        if not url:
            return None

        if db.is_valid_image_url(url):
            return url

        logger.warning(
            f"Bốc trúng URL ảnh không hợp lệ trong '{category_key}' (dài {len(url)} ký tự) "
            f"-> tự xoá khỏi DB và thử ảnh khác."
        )
        await bot.loop.run_in_executor(None, db.delete_images_by_url, [url])
        exclude_urls.append(url)

    logger.warning(f"'{category_key}': bốc trúng URL xấu 3 lần liên tiếp, tạm dừng thử thêm.")
    return None


def _build_image_embed(label: str, url: str) -> discord.Embed:
    embed = discord.Embed(title=f"🖼️ {label}", color=discord.Color.red())
    embed.set_image(url=url)
    embed.set_footer(text="Nguồn: kho ảnh đã crawl sẵn")
    return embed


def _schedule_prefetch(message_id: str, category_key: str, exclude_urls: list) -> None:
    """
    Tải trước (prefetch) 1 ảnh kế tiếp trong nền, KHÔNG chặn phản hồi hiện
    tại — mục đích để lần bấm "Sau" TIẾP THEO có ảnh sẵn trong bộ đệm, khỏi
    phải chờ Mongo lúc đang bấm (đây là nguồn gây độ trễ nhận thấy được khi
    chuyển ảnh mà trước đây phải fetch đồng bộ ngay lúc bấm).

    Chạy qua bot.loop.create_task() (fire-and-forget) + tự bắt mọi exception
    bên trong, để lỗi tải trước (nếu có) không bao giờ ảnh hưởng tới phản hồi
    chính đang gửi cho user, và không bị asyncio cảnh báo "Task exception was
    never retrieved".
    """
    async def _do_prefetch():
        try:
            new_url = await _fetch_next_image_url(category_key, exclude_urls)
            if new_url:
                await bot.loop.run_in_executor(None, db.append_image_to_session, message_id, new_url)
        except Exception as e:
            logger.warning(f"[prefetch] Lỗi tải trước ảnh cho session {message_id}: {e}")

    bot.loop.create_task(_do_prefetch())


def _maybe_schedule_prefetch(message_id: str, category_key: str, images: list, index: int) -> None:
    """Chỉ tải trước khi đang đứng ở ẢNH CUỐI của bộ đệm hiện tại — nghĩa là
    lần bấm "Sau" kế tiếp chắc chắn sẽ cần 1 ảnh chưa từng có sẵn."""
    if index >= len(images) - 1:
        _schedule_prefetch(message_id, category_key, list(images))


async def _send_image_result(send_func, category_key: str, label: str, keywords_text: str, url: str, author_id: int, view=None):
    """
    send_func: async callable(embed, view) -> discord.Message
    Gửi ảnh + lưu phiên xem (paginator session) vào MongoDB theo message.id,
    để nút Trước/Sau hoạt động vĩnh viễn (không phụ thuộc RAM của bot).
    view: mặc định PAGINATOR_VIEW (2 nút công khai); truyền EPHEMERAL_PAGINATOR_VIEW
    (3 nút, có Lưu ảnh) khi gửi qua showcase board.
    keywords_text: chuỗi hiển thị các từ khóa của category (chỉ để lưu vào
    session cho mục đích tra cứu/hiển thị — KHÔNG dùng để truy vấn ảnh).
    """
    if view is None:
        view = PAGINATOR_VIEW
    embed = _build_image_embed(label, url)
    message = await send_func(embed=embed, view=view)
    message_id = str(message.id)
    await bot.loop.run_in_executor(
        None, db.save_paginator_session, message_id, category_key, label, keywords_text, [url], 0, author_id
    )
    # Tải trước ngay ảnh thứ 2 — session vừa tạo chỉ có đúng 1 ảnh (index 0,
    # cũng là ảnh cuối bộ đệm) nên chắc chắn cần tải trước.
    _maybe_schedule_prefetch(message_id, category_key, [url], 0)


async def _paginator_navigate(interaction: discord.Interaction, direction: int, view: discord.ui.View):
    """Logic điều hướng dùng chung cho mọi nơi hiển thị ảnh có nút Trước/Sau (/img, /random, showcase)."""
    # QUAN TRỌNG: defer() phải là việc ĐẦU TIÊN — trước cả db.get_paginator_session()
    # bên dưới. Cùng 1 lỗi từng gặp ở nút "Bắt đầu": nếu Mongo chậm đúng lúc gọi
    # get_paginator_session (trước khi kịp defer), tổng thời gian có thể vượt quá
    # 3 giây Discord cho phép, gây "không phản hồi kịp thời" dù ảnh cuối cùng vẫn
    # lấy được (thấy rõ trong log: ảnh gửi thành công lúc 13:42 nhưng lỗi timeout
    # xảy ra ngay sau đó — khả năng cao là lúc bấm "Sau" tiếp theo).
    # Vì đã defer() không điều kiện ngay từ đầu, mọi nhánh bên dưới đều phải dùng
    # edit_original_response() thay vì response.edit_message() (không thể gọi
    # response.edit_message() sau khi đã defer()).
    if not await _timed_defer(interaction):
        return

    message_id = str(interaction.message.id)
    session = await bot.loop.run_in_executor(None, db.get_paginator_session, message_id)

    if not session:
        await interaction.followup.send(
            "⚠️ Không tìm thấy dữ liệu phiên xem ảnh này nữa. Dùng lại `/img` để bắt đầu phiên mới.",
            ephemeral=True,
        )
        return
    if interaction.user.id != session["author_id"]:
        await interaction.followup.send(
            "⚠️ Bạn không thể điều khiển kết quả tìm kiếm của người khác.", ephemeral=True
        )
        return

    if session.get("mode") == "random":
        await _paginator_navigate_random(interaction, direction, view, session, message_id)
        return

    images = session["images"]
    index = session["index"]
    category_key = session["category_key"]

    if direction < 0:
        index = max(0, index - 1)
        await interaction.edit_original_response(embed=_build_image_embed(session["label"], images[index]), view=view)
        await bot.loop.run_in_executor(None, db.update_paginator_index, message_id, index)
        return

    # direction > 0 ("Sau"): còn ảnh đệm sẵn (nhờ prefetch chạy trước đó) -> chuyển luôn, không cần chờ Mongo
    if index < len(images) - 1:
        index += 1
        # Trả ảnh cho Discord TRƯỚC, ghi index vào Mongo SAU — user thấy ảnh
        # ngay lập tức, không phải chờ thêm round-trip Mongo mới thấy ảnh đổi
        # (lệch index tạm thời nếu ghi Mongo lỗi chỉ khiến 1 lần bấm kế tiếp
        # hiện lại đúng ảnh này, không mất dữ liệu, tự sửa ở lần bấm sau).
        await interaction.edit_original_response(embed=_build_image_embed(session["label"], images[index]), view=view)
        await bot.loop.run_in_executor(None, db.update_paginator_index, message_id, index)
        _maybe_schedule_prefetch(message_id, category_key, images, index)
        return

    # Hết ảnh đệm (prefetch chưa kịp xong, hoặc lần đầu chưa từng chạy) ->
    # lấy ảnh mới đồng bộ ngay tại đây, có thể mất vài giây (đã defer() từ
    # đầu hàm nên không còn bị giới hạn 3 giây ở đây nữa)
    new_url = await _fetch_next_image_url(category_key, images)
    if not new_url:
        eta = await _next_crawl_eta_text()
        await interaction.followup.send(
            f"❌ Hết ảnh khả dụng cho chủ đề này rồi. Kho ảnh tự làm mới mỗi "
            f"{CRAWL_INTERVAL_HOURS} tiếng — lần crawl kế tiếp {eta}.",
            ephemeral=True,
        )
        return

    index += 1
    await interaction.edit_original_response(embed=_build_image_embed(session["label"], new_url), view=view)
    await bot.loop.run_in_executor(None, db.append_image_and_set_index, message_id, new_url, index)
    _maybe_schedule_prefetch(message_id, category_key, images + [new_url], index)


async def _paginator_navigate_random(interaction: discord.Interaction, direction: int, view: discord.ui.View,
                                      session: dict, message_id: str):
    """
    Bản dành riêng cho phiên /random: khác phiên /img ở chỗ MỖI ảnh trong
    "items" có thể thuộc 1 category khác nhau (random thật trên toàn kho,
    không cố định 1 category cho cả phiên như /img) — nên embed phải lấy
    label từ CHÍNH ảnh đang xem (item["label"]), không dùng 1 label chung
    cho cả session. Logic tải trước/đổi thứ tự ghi Mongo giống hệt
    _paginator_navigate ở trên, chỉ khác nguồn lấy ảnh mới (random toàn kho
    qua _pick_random_image thay vì next_image của đúng 1 category).
    """
    items = session["items"]
    index = session["index"]
    allowed_keys = session.get("allowed_keys") or []
    all_cats = await get_all_categories_async()

    if direction < 0:
        index = max(0, index - 1)
        item = items[index]
        await interaction.edit_original_response(embed=_build_image_embed(item["label"], item["url"]), view=view)
        await bot.loop.run_in_executor(None, db.update_paginator_index, message_id, index)
        return

    if index < len(items) - 1:
        index += 1
        item = items[index]
        await interaction.edit_original_response(embed=_build_image_embed(item["label"], item["url"]), view=view)
        await bot.loop.run_in_executor(None, db.update_paginator_index, message_id, index)
        _maybe_schedule_random_prefetch(message_id, all_cats, allowed_keys, items, index)
        return

    exclude_urls = [i["url"] for i in items]
    result = await _pick_random_image(all_cats, allowed_keys, exclude_urls)
    if not result:
        eta = await _next_crawl_eta_text()
        await interaction.followup.send(
            f"❌ Hết ảnh khả dụng rồi. Kho ảnh tự làm mới mỗi "
            f"{CRAWL_INTERVAL_HOURS} tiếng — lần crawl kế tiếp {eta}.",
            ephemeral=True,
        )
        return

    category_key, label, url = result
    index += 1
    new_item = {"url": url, "category_key": category_key, "label": label}
    await interaction.edit_original_response(embed=_build_image_embed(label, url), view=view)
    await bot.loop.run_in_executor(None, db.append_random_item_and_set_index, message_id, new_item, index)
    _maybe_schedule_random_prefetch(message_id, all_cats, allowed_keys, items + [new_item], index)


class PersistentImagePaginator(discord.ui.View):
    """
    View "vĩnh viễn": không timeout, đăng ký 1 lần lúc bot khởi động qua
    bot.add_view() nên nút vẫn bấm được kể cả sau khi bot restart hoặc
    tin nhắn đã gửi từ rất lâu — vì trạng thái (ảnh nào, category nào...)
    được đọc từ MongoDB theo message.id thay vì lưu trong RAM.
    """

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="◀ Trước", style=discord.ButtonStyle.secondary, custom_id="paginator:prev")
    async def previous_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _paginator_navigate(interaction, -1, self)

    @discord.ui.button(label="Sau ▶", style=discord.ButtonStyle.secondary, custom_id="paginator:next")
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _paginator_navigate(interaction, +1, self)


PAGINATOR_VIEW = PersistentImagePaginator()


def _try_get_image_info(url: str):
    """
    Lấy nhanh định dạng + dung lượng ảnh qua HEAD request (không tải cả ảnh).
    Trả về chuỗi mô tả, hoặc None nếu không lấy được (không coi là lỗi —
    chỗ gọi hàm này sẽ tự dùng ghi chú thường nếu trả về None).
    """
    try:
        resp = requests.head(url, timeout=4, allow_redirects=True)
        content_type = resp.headers.get("Content-Type", "")
        content_length = resp.headers.get("Content-Length")

        parts = []
        if content_type:
            parts.append(content_type)
        if content_length and content_length.isdigit():
            size_kb = int(content_length) / 1024
            parts.append(f"{size_kb / 1024:.1f} MB" if size_kb >= 1024 else f"{size_kb:.0f} KB")
        return " · ".join(parts) if parts else None
    except Exception:
        return None


class EphemeralImagePaginator(discord.ui.View):
    """
    Giống PersistentImagePaginator nhưng có thêm nút "💾 Lưu ảnh" — dùng cho
    phiên xem riêng tư (ephemeral) mở ra sau khi bấm "Bắt đầu" ở showcase
    board. custom_id khác PAGINATOR_VIEW để Discord phân biệt được 2 view
    khi cùng đăng ký persistent.

    "Lưu ảnh" = bot gửi ảnh qua tin nhắn riêng (DM) cho người bấm kèm ghi
    chú (có thêm định dạng/dung lượng nếu lấy được). Không lưu vào DB, không
    có danh sách xem lại — chỉ đơn thuần gửi DM ngay lúc đó.
    """

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary, custom_id="epaginator:prev")
    async def previous_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _paginator_navigate(interaction, -1, self)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary, custom_id="epaginator:next")
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _paginator_navigate(interaction, +1, self)

    @discord.ui.button(label="💾 Lưu ảnh", style=discord.ButtonStyle.success, custom_id="epaginator:save")
    async def save_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _do_epaginator_save(interaction)


async def _do_epaginator_save(interaction: discord.Interaction):
    """
    Logic thật của nút "💾 Lưu ảnh" — tách riêng khỏi callback trong View để
    có thể gọi trực tiếp từ on_interaction() (dispatch thủ công), phòng
    trường hợp ViewStore nội bộ của discord.py không khớp được item (xem
    ghi chú dài trong on_interaction() phía trên đầu file).
    """
    # Cùng nguyên tắc: defer() trước, mọi DB call sau — xem ghi chú ở
    # _paginator_navigate() phía trên. Dùng _timed_defer() dùng chung
    # để có timing thống nhất trên mọi lệnh gọi defer() trong bot.
    if not await _timed_defer(interaction, ephemeral=True):
        return

    message_id = str(interaction.message.id)
    session = await bot.loop.run_in_executor(None, db.get_paginator_session, message_id)
    if not session:
        await interaction.followup.send("⚠️ Không tìm thấy dữ liệu ảnh này nữa.", ephemeral=True)
        return
    if interaction.user.id != session["author_id"]:
        await interaction.followup.send("⚠️ Bạn không thể lưu ảnh của người khác.", ephemeral=True)
        return

    url = session["images"][session["index"]]
    label = session["label"]

    # Cố gắng lấy thêm thông tin ảnh (định dạng, dung lượng) qua HEAD request.
    # Không bắt buộc phải thành công — nếu lỗi/timeout thì bỏ qua, chỉ dùng ghi chú thường.
    info_text = await bot.loop.run_in_executor(None, _try_get_image_info, url)

    dm_embed = discord.Embed(title=f"💾 Ảnh đã lưu — {label}", description="✅ Gửi theo yêu cầu lưu ảnh của bạn.", color=discord.Color.green())
    dm_embed.set_image(url=url)
    if info_text:
        dm_embed.add_field(name="Thông tin ảnh", value=info_text, inline=False)

    dm_ok = False
    dm_error_text = None
    try:
        await interaction.user.send(embed=dm_embed)
        dm_ok = True
    except discord.Forbidden:
        dm_error_text = "Tài khoản của bạn đang tắt nhận tin nhắn riêng (DM) từ thành viên server này."
    except discord.HTTPException as e:
        dm_error_text = f"Lỗi khi gửi tin nhắn riêng qua Discord: {e}"
    except Exception as e:
        dm_error_text = f"Lỗi không xác định khi gửi tin nhắn riêng: {e}"

    if dm_ok:
        await interaction.followup.send("✅ Đã gửi ảnh vào tin nhắn riêng (DM) của bạn.", ephemeral=True)
        return

    logger.warning(f"Không gửi được DM lưu ảnh cho user {interaction.user.id}: {dm_error_text}")

    fail_embed = discord.Embed(
        title="⚠️ Không gửi được tin nhắn riêng (DM)",
        description=f"**Lý do:** {dm_error_text}",
        color=discord.Color.orange(),
    )
    fail_embed.set_image(url=url)
    if info_text:
        fail_embed.add_field(name="Thông tin ảnh", value=info_text, inline=False)
    await interaction.followup.send(embed=fail_embed, ephemeral=True)


EPHEMERAL_PAGINATOR_VIEW = EphemeralImagePaginator()


class ShowcaseStartView(discord.ui.View):
    """
    Nút "Bắt đầu" trên showcase board (đăng bởi admin qua /showcase). Persistent,
    custom_id cố định — tra category_key theo message.id trong DB (giống
    paginator_sessions) thay vì nhúng vào custom_id, để dùng chung 1 view cho
    mọi board dù có bao nhiêu chủ đề đi nữa.
    """

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🎲 Bắt đầu", style=discord.ButtonStyle.primary, custom_id="showcase:start")
    async def start_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _do_showcase_start(interaction)


async def _do_showcase_start(interaction: discord.Interaction):
    """
    Logic thật của nút "🎲 Bắt đầu" — tách riêng khỏi callback trong View để
    có thể gọi trực tiếp từ on_interaction() (dispatch thủ công), phòng
    trường hợp ViewStore nội bộ của discord.py không khớp được item (xem
    ghi chú dài trong on_interaction() phía trên đầu file).
    """
    # QUAN TRỌNG: defer() phải là việc ĐẦU TIÊN, trước mọi DB call/check
    # bên dưới. Discord chỉ cho 3 giây để phản hồi ban đầu (kể cả defer);
    # nếu Mongo chậm/chập chờn, 4 bước check phía dưới (get_showcase_board,
    # get_all_categories_async, check_access...) cộng dồn có thể vượt 3
    # giây, khiến Discord huỷ interaction ("không phản hồi kịp thời")
    # trước khi code kịp defer. Defer trước rồi mới check sẽ không còn
    # giới hạn 3 giây nữa (chỉ còn giới hạn 15 phút của followup).
    #
    # Bọc try/except quanh defer() qua _timed_defer() dùng chung — đo
    # timing thống nhất trên mọi lệnh gọi defer() trong bot, giúp xác
    # nhận defer() có thực sự là nguồn gây timeout hay không (khác với
    # self-test soak trước đây, chỉ đo được message.edit() thường, không
    # đo được đúng API defer() thật).
    if not await _timed_defer(interaction, ephemeral=True):
        return

    message_id = str(interaction.message.id)
    board = await bot.loop.run_in_executor(None, db.get_showcase_board, message_id)
    if not board:
        await interaction.followup.send(
            "❌ Bảng giới thiệu này thiếu dữ liệu (có thể tạo từ bản bot cũ), không dùng được nữa.",
            ephemeral=True,
        )
        return

    category_key = board["category_key"]
    all_cats = await get_all_categories_async()
    info = all_cats.get(category_key)
    if not info:
        await interaction.followup.send("❌ Chủ đề này không còn tồn tại nữa.", ephemeral=True)
        return
    if info.get("nsfw") and not _channel_allows_nsfw(interaction.channel):
        await interaction.followup.send(
            "🔞 Chủ đề này chỉ dùng được ở kênh đã đánh dấu Age-Restricted (NSFW).", ephemeral=True
        )
        return

    roles = getattr(interaction.user, "roles", None)
    access_err = await check_access(interaction.user.id, interaction.guild_id, interaction.channel_id, roles)
    if access_err:
        await interaction.followup.send(access_err, ephemeral=True)
        return
    wait = check_cooldown(interaction.user.id)
    if wait:
        await interaction.followup.send(f"⏳ Chờ thêm {wait}s rồi thử lại nhé.", ephemeral=True)
        return
    mark_used(interaction.user.id)

    url = await _fetch_next_image_url(category_key, [])
    if not url:
        eta = await _next_crawl_eta_text()
        await interaction.followup.send(
            f"❌ Không tìm thấy ảnh nào cho chủ đề: **{info['label']}**. "
            f"Lần crawl kế tiếp {eta}.",
            ephemeral=True,
        )
        return

    async def send_func(embed, view):
        return await interaction.followup.send(embed=embed, view=view, ephemeral=True, wait=True)

    await _send_image_result(
        send_func, category_key, info["label"], keywords_display(info), url, interaction.user.id,
        view=EPHEMERAL_PAGINATOR_VIEW,
    )


SHOWCASE_VIEW = ShowcaseStartView()


# ============================================================
# Lệnh /img và !img — lấy ảnh theo chủ đề, CHỈ đọc từ MongoDB (kho ảnh đã
# crawl sẵn qua crawl_job.py, random trong category, không theo thứ tự).
# Không còn fallback cào Pinterest trực tiếp lúc user đang chờ phản hồi —
# đã bỏ hẳn cơ chế này (xem ghi chú chi tiết ở đầu khu vực "Lấy ảnh" phía
# trên và trong crawl_job.py).
# ============================================================

@bot.tree.command(name="img", description="Lấy ảnh theo chủ đề (đã crawl sẵn từ Pinterest)")
@app_commands.describe(chu_de="Chọn chủ đề ảnh (gõ để tìm)")
@app_commands.autocomplete(chu_de=category_autocomplete)
async def img_slash(interaction: discord.Interaction, chu_de: str):
    roles = getattr(interaction.user, "roles", None)
    access_err = await check_access(interaction.user.id, interaction.guild_id, interaction.channel_id, roles)
    if access_err:
        await interaction.response.send_message(access_err, ephemeral=True)
        return
    wait = check_cooldown(interaction.user.id)
    if wait:
        await interaction.response.send_message(f"⏳ Chờ thêm {wait}s rồi thử lại nhé.", ephemeral=True)
        return
    mark_used(interaction.user.id)

    if not await _timed_defer(interaction):
        return

    all_cats = await get_all_categories_async()
    info = all_cats.get(chu_de)
    if not info:
        await interaction.followup.send(f"❌ Chủ đề không hợp lệ: **{chu_de}**")
        return
    if info.get("nsfw") and not _channel_allows_nsfw(interaction.channel):
        await interaction.followup.send(
            f"🔞 Chủ đề **{info['label']}** chỉ dùng được ở kênh đã đánh dấu Age-Restricted (NSFW)."
        )
        return

    url = await _fetch_next_image_url(chu_de, [])
    if not url:
        eta = await _next_crawl_eta_text()
        await interaction.followup.send(
            f"❌ Không tìm thấy ảnh nào cho chủ đề: **{info['label']}**. "
            f"Lần crawl kế tiếp {eta}."
        )
        return

    async def send_func(embed, view):
        return await interaction.followup.send(embed=embed, view=view, wait=True)

    await _send_image_result(send_func, chu_de, info["label"], keywords_display(info), url, interaction.user.id)


@bot.command(name="img", help="Lấy ảnh theo chủ đề. Vd: !img meo")
async def img_prefix(ctx, chu_de: str = None):
    roles = getattr(ctx.author, "roles", None)
    access_err = await check_access(ctx.author.id, ctx.guild.id if ctx.guild else None, ctx.channel.id, roles)
    if access_err:
        await ctx.send(access_err)
        return

    all_cats = await get_all_categories_async()
    if not chu_de or chu_de.lower() not in all_cats:
        options_text = "\n".join(f"`{k}` — {v['label']}" for k, v in all_cats.items())
        await ctx.send(f"⚠️ Vui lòng chọn 1 chủ đề hợp lệ:\n{options_text}")
        return

    wait = check_cooldown(ctx.author.id)
    if wait:
        await ctx.send(f"⏳ Chờ thêm {wait}s rồi thử lại nhé.")
        return
    mark_used(ctx.author.id)

    category_key = chu_de.lower()
    info = all_cats[category_key]
    if info.get("nsfw") and not _channel_allows_nsfw(ctx.channel):
        await ctx.send(f"🔞 Chủ đề **{info['label']}** chỉ dùng được ở kênh đã đánh dấu Age-Restricted (NSFW).")
        return
    await ctx.typing()

    url = await _fetch_next_image_url(category_key, [])
    if not url:
        eta = await _next_crawl_eta_text()
        await ctx.send(
            f"❌ Không tìm thấy ảnh nào cho chủ đề: **{info['label']}**. "
            f"Lần crawl kế tiếp {eta}."
        )
        return

    async def send_func(embed, view):
        return await ctx.send(embed=embed, view=view)

    await _send_image_result(send_func, category_key, info["label"], keywords_display(info), url, ctx.author.id)


# ============================================================
# Lệnh /random và !random — lấy 1 ảnh NGẪU NHIÊN TRÊN TOÀN BỘ KHO
# (không phải random category rồi mới chọn ảnh trong đó).
# ============================================================

async def _pick_random_image(all_cats: dict, allowed_keys: list, exclude_urls: list):
    """
    Lõi chọn 1 ảnh random dùng CHUNG cho: lệnh /random lần đầu, mỗi lần bấm
    "Sau" trong phiên /random (hết ảnh đệm), và tác vụ tải trước (prefetch)
    của phiên /random. Trả về (category_key, label, url) hoặc None nếu hết
    ảnh khả dụng trong phạm vi allowed_keys.
    """
    if not allowed_keys:
        return None
    doc = await bot.loop.run_in_executor(None, db.get_random_image, exclude_urls, allowed_keys)
    if doc:
        category_key = doc["category"]
        info = all_cats.get(category_key, {"label": category_key, "keywords": [category_key]})
        return category_key, info["label"], doc["image_url"]

    # DB trống hoàn toàn ảnh khả dụng (trong phạm vi allowed_keys) -> thử
    # random 1 chủ đề khác (vẫn trong allowed_keys) qua _fetch_next_image_url
    # (chỉ đọc DB, không cào Pinterest trực tiếp nữa — xem ghi chú đầu file).
    category_key = random.choice(allowed_keys)
    info = all_cats[category_key]
    url = await _fetch_next_image_url(category_key, exclude_urls)
    if not url:
        return None
    return category_key, info["label"], url


async def _get_random_image_result(channel):
    """Trả về (category_key, label, url, allowed_keys) hoặc (None, None, None, None) nếu hết ảnh.
    allowed_keys được trả về luôn để lưu vào phiên /random — dùng lại cho
    prefetch trong nền, không cần tính lại lần nữa."""
    all_cats = await get_all_categories_async()
    channel_nsfw_ok = _channel_allows_nsfw(channel)

    # Chỉ random trong các category được phép ở kênh này (loại NSFW nếu kênh
    # chưa đánh dấu Age-Restricted) để tránh /random vô tình đưa ảnh nhạy
    # cảm vào kênh thường.
    allowed_keys = [k for k, info in all_cats.items() if channel_nsfw_ok or not info.get("nsfw")]
    if not allowed_keys:
        return None, None, None, None

    result = await _pick_random_image(all_cats, allowed_keys, [])
    if not result:
        return None, None, None, None
    category_key, label, url = result
    return category_key, label, url, allowed_keys


def _schedule_random_prefetch(message_id: str, all_cats: dict, allowed_keys: list, exclude_urls: list) -> None:
    """Bản dành cho phiên /random của _schedule_prefetch — mỗi ảnh tải trước
    có thể thuộc 1 category khác với ảnh hiện tại, nên lưu kèm cả
    category_key/label riêng cho từng ảnh (xem save_random_paginator_session)."""
    async def _do_prefetch():
        try:
            result = await _pick_random_image(all_cats, allowed_keys, exclude_urls)
            if result:
                category_key, label, url = result
                item = {"url": url, "category_key": category_key, "label": label}
                await bot.loop.run_in_executor(None, db.append_random_item_to_session, message_id, item)
        except Exception as e:
            logger.warning(f"[prefetch-random] Lỗi tải trước ảnh cho session {message_id}: {e}")

    bot.loop.create_task(_do_prefetch())


def _maybe_schedule_random_prefetch(message_id: str, all_cats: dict, allowed_keys: list, items: list, index: int) -> None:
    if index >= len(items) - 1:
        _schedule_random_prefetch(message_id, all_cats, allowed_keys, [i["url"] for i in items])


async def _send_random_image_result(send_func, category_key: str, label: str, url: str, allowed_keys: list, author_id: int):
    """Gửi ảnh cho /random + lưu phiên xem RIÊNG cho random (mỗi ảnh trong
    phiên có thể thuộc category khác nhau — xem save_random_paginator_session).
    /random luôn công khai (không ephemeral) nên luôn dùng PAGINATOR_VIEW."""
    embed = _build_image_embed(label, url)
    message = await send_func(embed=embed, view=PAGINATOR_VIEW)
    message_id = str(message.id)
    item = {"url": url, "category_key": category_key, "label": label}
    await bot.loop.run_in_executor(
        None, db.save_random_paginator_session, message_id, [item], allowed_keys, 0, author_id
    )
    all_cats = await get_all_categories_async()
    _maybe_schedule_random_prefetch(message_id, all_cats, allowed_keys, [item], 0)


@bot.tree.command(name="random", description="Lấy 1 ảnh ngẫu nhiên bất kỳ trong toàn bộ kho")
async def random_slash(interaction: discord.Interaction):
    roles = getattr(interaction.user, "roles", None)
    access_err = await check_access(interaction.user.id, interaction.guild_id, interaction.channel_id, roles)
    if access_err:
        await interaction.response.send_message(access_err, ephemeral=True)
        return
    wait = check_cooldown(interaction.user.id)
    if wait:
        await interaction.response.send_message(f"⏳ Chờ thêm {wait}s rồi thử lại nhé.", ephemeral=True)
        return
    mark_used(interaction.user.id)

    if not await _timed_defer(interaction):
        return

    category_key, label, url, allowed_keys = await _get_random_image_result(interaction.channel)
    if not url:
        eta = await _next_crawl_eta_text()
        await interaction.followup.send(f"❌ Kho ảnh hiện đang trống. Lần crawl kế tiếp {eta}.")
        return

    async def send_func(embed, view):
        return await interaction.followup.send(embed=embed, view=view, wait=True)

    await _send_random_image_result(send_func, category_key, label, url, allowed_keys, interaction.user.id)


@bot.command(name="random", help="Lấy 1 ảnh ngẫu nhiên bất kỳ trong toàn bộ kho")
async def random_prefix(ctx):
    roles = getattr(ctx.author, "roles", None)
    access_err = await check_access(ctx.author.id, ctx.guild.id if ctx.guild else None, ctx.channel.id, roles)
    if access_err:
        await ctx.send(access_err)
        return
    wait = check_cooldown(ctx.author.id)
    if wait:
        await ctx.send(f"⏳ Chờ thêm {wait}s rồi thử lại nhé.")
        return
    mark_used(ctx.author.id)

    await ctx.typing()

    category_key, label, url, allowed_keys = await _get_random_image_result(ctx.channel)
    if not url:
        eta = await _next_crawl_eta_text()
        await ctx.send(f"❌ Kho ảnh hiện đang trống. Lần crawl kế tiếp {eta}.")
        return

    async def send_func(embed, view):
        return await ctx.send(embed=embed, view=view)

    await _send_random_image_result(send_func, category_key, label, url, allowed_keys, ctx.author.id)


# ============================================================
# Lệnh /stats và !stats — thống kê chi tiết kho ảnh theo chủ đề
# ============================================================

async def _build_stats_embed() -> discord.Embed:
    all_cats = await get_all_categories_async()

    def fetch():
        stats = {}
        for key in all_cats:
            try:
                stats[key] = db.get_category_stats(key)
            except Exception as e:
                logger.warning(f"Lỗi lấy stats category '{key}': {e}")
                stats[key] = None
        last_crawl = db.get_last_crawl_time()
        return stats, last_crawl

    stats, last_crawl = await bot.loop.run_in_executor(None, fetch)

    embed = discord.Embed(title="📊 Kho ảnh chi tiết theo chủ đề", color=discord.Color.blurple())

    total_all = 0
    available_all = 0
    for key, info in all_cats.items():
        s = stats.get(key)
        if s is None:
            embed.add_field(name=f"{info['label']} · `{key}`", value="⚠️ lỗi đọc DB", inline=False)
            continue
        total_all += s["total"]
        available_all += s["available"]
        nsfw_text = "Có 🔞" if info.get("nsfw") else "Không"
        value = (
            f"Từ khóa: `{keywords_display(info)}`\n"
            f"NSFW: {nsfw_text}\n"
            f"Tổng: **{s['total']}** · Khả dụng: **{s['available']}**\n"
            f"TB gửi: {s['avg_sent_count']} lần/ảnh (cao nhất {s['max_sent_count']})\n"
            f"Ảnh mới nhất: {_relative_time_vi(s['newest_created_at'])}"
        )
        embed.add_field(name=f"{info['label']} · `{key}`", value=value, inline=True)

    custom_count = len(all_cats) - len(CATEGORIES)
    embed.description = (
        f"Tổng cộng: **{total_all}** ảnh · Khả dụng ngay: **{available_all}**\n"
        f"Chủ đề: {len(CATEGORIES)} có sẵn trong code + {custom_count} thêm qua Discord\n"
        f"Lần crawl định kỳ gần nhất: {_relative_time_vi(last_crawl)}"
    )
    return embed


@bot.tree.command(name="stats", description="Xem thống kê chi tiết kho ảnh theo từng chủ đề")
async def stats_slash(interaction: discord.Interaction):
    if not await _timed_defer(interaction):
        return
    embed = await _build_stats_embed()
    await interaction.followup.send(embed=embed)


@bot.command(name="stats", help="Xem thống kê chi tiết kho ảnh theo từng chủ đề")
async def stats_prefix(ctx):
    await ctx.typing()
    embed = await _build_stats_embed()
    await ctx.send(embed=embed)


# ============================================================
# Lệnh admin: /addcategory, /editcategory, /removecategory — quản lý
# chủ đề qua Discord thay vì sửa categories.py + deploy lại. Chỉ
# ADMIN_IDS dùng được. Category thêm qua đây lưu trong MongoDB
# (custom_categories), có hiệu lực ngay lập tức.
# ============================================================

def _parse_keywords_input(raw: str) -> list:
    """Cho phép nhập nhiều từ khóa cách nhau bằng dấu phẩy, vd: 'cá heo, cá mập'."""
    return [k.strip() for k in raw.split(",") if k.strip()]


async def _maybe_crawl_new_category_now(slug: str, keywords: list) -> str:
    """
    Nếu lần crawl định kỳ gần nhất đã >= 1 tiếng trước (hoặc chưa từng crawl),
    crawl ngay category mới thêm để không phải chờ tới chu kỳ crawl tiếp theo.
    Trả về 1 câu mô tả kết quả để nối vào tin nhắn phản hồi.
    keywords: danh sách từ khóa (crawl_category tự lặp qua từng từ khóa).
    """
    last_crawl = await bot.loop.run_in_executor(None, db.get_last_crawl_time)
    now = datetime.now(timezone.utc)
    should_crawl_now = (last_crawl is None) or ((now - last_crawl) >= timedelta(hours=1))

    if not should_crawl_now:
        return " Chủ đề sẽ được crawl ở lần chạy định kỳ tiếp theo (crawl gần đây vừa mới chạy xong)."

    def do_crawl():
        return crawl_job.crawl_category(slug, keywords)

    try:
        inserted, skipped, had_error = await bot.loop.run_in_executor(None, do_crawl)
    except Exception as e:
        logger.warning(f"Lỗi crawl ngay category mới '{slug}': {e}")
        return " ⚠️ Crawl ngay bị lỗi, sẽ tự thử lại ở lần crawl định kỳ tiếp theo."

    if had_error:
        return " ⚠️ Crawl ngay bị lỗi, sẽ tự thử lại ở lần crawl định kỳ tiếp theo."
    return f" Đã crawl ngay **{inserted}** ảnh cho chủ đề này."


@bot.tree.command(name="addcategory", description="[Admin] Thêm chủ đề ảnh mới")
@app_commands.describe(
    slug="Mã chủ đề, không dấu/không khoảng trắng (vd: hoahong)",
    label="Tên hiển thị trong Discord",
    keywords="Từ khóa tìm kiếm trên Pinterest — nhiều từ khóa cách nhau bằng dấu phẩy (vd: cá heo, cá mập)",
    nsfw="Chủ đề nhạy cảm, chỉ dùng được ở kênh Age-Restricted? (mặc định: Không)",
)
async def addcategory_slash(interaction: discord.Interaction, slug: str, label: str, keywords: str, nsfw: bool = False):
    if not is_admin(interaction.user.id):
        await interaction.response.send_message("⚠️ Chỉ admin mới dùng được lệnh này.", ephemeral=True)
        return

    slug = slug.strip().lower()
    if not slug or " " in slug:
        await interaction.response.send_message("⚠️ Slug không hợp lệ (không dấu cách).", ephemeral=True)
        return
    if slug in CATEGORIES:
        await interaction.response.send_message(
            f"⚠️ `{slug}` là chủ đề có sẵn trong code (categories.py), không thể ghi đè qua lệnh.",
            ephemeral=True,
        )
        return
    keywords_list = _parse_keywords_input(keywords)
    if not keywords_list:
        await interaction.response.send_message("⚠️ Cần ít nhất 1 từ khóa.", ephemeral=True)
        return

    if not await _timed_defer(interaction):
        return
    await bot.loop.run_in_executor(None, db.add_custom_category, slug, label, keywords_list, nsfw)
    _invalidate_categories_cache()
    extra = await _maybe_crawl_new_category_now(slug, keywords_list)
    nsfw_note = " 🔞 (đánh dấu NSFW)" if nsfw else ""
    kw_text = ", ".join(keywords_list)
    await interaction.followup.send(
        f"✅ Đã thêm chủ đề **{label}** (`{slug}`, từ khóa: `{kw_text}`){nsfw_note}." + extra +
        f"\nDùng ngay được với `/img` (gõ để autocomplete) hoặc `!img {slug}`."
    )


@bot.command(name="addcategory", help="[Admin] !addcategory slug | Label hiển thị | từ khóa Pinterest (cách nhau bằng dấu phẩy nếu nhiều) [| nsfw]")
async def addcategory_prefix(ctx, *, args: str = None):
    if not is_admin(ctx.author.id):
        await ctx.send("⚠️ Chỉ admin mới dùng được lệnh này.")
        return
    if not args or args.count("|") not in (2, 3):
        await ctx.send("⚠️ Cú pháp: `!addcategory slug | Label hiển thị | từ khóa Pinterest (vd: cá heo, cá mập) [| nsfw]`")
        return

    parts = [p.strip() for p in args.split("|")]
    slug, label, keywords_raw = parts[0], parts[1], parts[2]
    nsfw = len(parts) == 4 and parts[3].lower() in ("nsfw", "true", "1", "có")
    slug = slug.lower()
    if not slug or " " in slug:
        await ctx.send("⚠️ Slug không hợp lệ (không dấu cách).")
        return
    if slug in CATEGORIES:
        await ctx.send(f"⚠️ `{slug}` là chủ đề có sẵn trong code, không thể ghi đè qua lệnh.")
        return
    keywords_list = _parse_keywords_input(keywords_raw)
    if not keywords_list:
        await ctx.send("⚠️ Cần ít nhất 1 từ khóa.")
        return

    await ctx.typing()
    await bot.loop.run_in_executor(None, db.add_custom_category, slug, label, keywords_list, nsfw)
    _invalidate_categories_cache()
    extra = await _maybe_crawl_new_category_now(slug, keywords_list)
    nsfw_note = " 🔞 (đánh dấu NSFW)" if nsfw else ""
    kw_text = ", ".join(keywords_list)
    await ctx.send(f"✅ Đã thêm chủ đề **{label}** (`{slug}`, từ khóa: `{kw_text}`){nsfw_note}." + extra)


@bot.tree.command(name="editcategory", description="[Admin] Sửa label/keywords/nsfw của 1 chủ đề đã thêm qua lệnh")
@app_commands.describe(
    slug="Mã chủ đề cần sửa",
    label="Tên hiển thị mới (bỏ trống nếu giữ nguyên)",
    keywords="Từ khóa Pinterest mới — nhiều từ khóa cách nhau bằng dấu phẩy, THAY THẾ toàn bộ danh sách cũ (bỏ trống nếu giữ nguyên)",
    nsfw="Đánh dấu NSFW? (bỏ trống nếu giữ nguyên)",
)
async def editcategory_slash(interaction: discord.Interaction, slug: str, label: str = None,
                              keywords: str = None, nsfw: bool = None):
    if not is_admin(interaction.user.id):
        await interaction.response.send_message("⚠️ Chỉ admin mới dùng được lệnh này.", ephemeral=True)
        return

    slug = slug.strip().lower()
    if slug in CATEGORIES:
        await interaction.response.send_message(
            f"⚠️ `{slug}` là chủ đề có sẵn trong code, không sửa được qua lệnh.", ephemeral=True
        )
        return
    keywords_list = _parse_keywords_input(keywords) if keywords else None
    if not label and not keywords_list and nsfw is None:
        await interaction.response.send_message(
            "⚠️ Cần cung cấp ít nhất 1 trong 3: label, keywords hoặc nsfw để sửa.", ephemeral=True
        )
        return

    if not await _timed_defer(interaction, ephemeral=True):
        return
    ok = await bot.loop.run_in_executor(None, db.edit_custom_category, slug, label, keywords_list, nsfw)
    _invalidate_categories_cache()
    if ok:
        await interaction.followup.send(f"✅ Đã cập nhật chủ đề `{slug}`.")
    else:
        await interaction.followup.send(f"❌ Không tìm thấy chủ đề `{slug}` (chưa từng thêm qua `/addcategory`).")


@bot.command(name="editcategory", help="[Admin] !editcategory slug | Label mới | keyword mới (cách nhau bằng dấu phẩy nếu nhiều, THAY THẾ toàn bộ danh sách cũ) | nsfw mới (để trống phần nào nếu giữ nguyên)")
async def editcategory_prefix(ctx, *, args: str = None):
    if not is_admin(ctx.author.id):
        await ctx.send("⚠️ Chỉ admin mới dùng được lệnh này.")
        return
    if not args or args.count("|") not in (2, 3):
        await ctx.send("⚠️ Cú pháp: `!editcategory slug | Label mới | keyword mới [| nsfw mới]` (để trống phần nào nếu muốn giữ nguyên)")
        return

    parts = [p.strip() for p in args.split("|")]
    slug, label, keywords_raw = parts[0], parts[1], parts[2]
    nsfw_raw = parts[3] if len(parts) == 4 else ""
    nsfw = None
    if nsfw_raw:
        nsfw = nsfw_raw.lower() in ("nsfw", "true", "1", "có")

    slug = slug.lower()
    if slug in CATEGORIES:
        await ctx.send(f"⚠️ `{slug}` là chủ đề có sẵn trong code, không sửa được qua lệnh.")
        return
    keywords_list = _parse_keywords_input(keywords_raw) if keywords_raw else None
    if not label and not keywords_list and nsfw is None:
        await ctx.send("⚠️ Cần cung cấp ít nhất 1 trong 3: label, keyword hoặc nsfw để sửa.")
        return

    await ctx.typing()
    ok = await bot.loop.run_in_executor(None, db.edit_custom_category, slug, label or None, keywords_list, nsfw)
    _invalidate_categories_cache()
    if ok:
        await ctx.send(f"✅ Đã cập nhật chủ đề `{slug}`.")
    else:
        await ctx.send(f"❌ Không tìm thấy chủ đề `{slug}` (chưa từng thêm qua `!addcategory`).")


@bot.tree.command(name="removecategory", description="[Admin] Xoá chủ đề ảnh đã thêm qua lệnh")
@app_commands.describe(slug="Mã chủ đề cần xoá")
async def removecategory_slash(interaction: discord.Interaction, slug: str):
    if not is_admin(interaction.user.id):
        await interaction.response.send_message("⚠️ Chỉ admin mới dùng được lệnh này.", ephemeral=True)
        return

    slug = slug.strip().lower()
    if slug in CATEGORIES:
        await interaction.response.send_message(
            f"⚠️ `{slug}` là chủ đề có sẵn trong code (categories.py), không thể xoá qua lệnh — cần sửa code.",
            ephemeral=True,
        )
        return

    if not await _timed_defer(interaction, ephemeral=True):
        return
    removed = await bot.loop.run_in_executor(None, db.remove_custom_category, slug)
    _invalidate_categories_cache()
    if removed:
        await interaction.followup.send(f"✅ Đã xoá chủ đề `{slug}`.")
    else:
        await interaction.followup.send(f"❌ Không tìm thấy chủ đề `{slug}` trong danh sách đã thêm.")


@bot.command(name="removecategory", help="[Admin] !removecategory <slug>")
async def removecategory_prefix(ctx, slug: str = None):
    if not is_admin(ctx.author.id):
        await ctx.send("⚠️ Chỉ admin mới dùng được lệnh này.")
        return
    if not slug:
        await ctx.send("⚠️ Cú pháp: `!removecategory <slug>`")
        return

    slug = slug.strip().lower()
    if slug in CATEGORIES:
        await ctx.send(f"⚠️ `{slug}` là chủ đề có sẵn trong code, không thể xoá qua lệnh.")
        return

    await ctx.typing()
    removed = await bot.loop.run_in_executor(None, db.remove_custom_category, slug)
    _invalidate_categories_cache()
    if removed:
        await ctx.send(f"✅ Đã xoá chủ đề `{slug}`.")
    else:
        await ctx.send(f"❌ Không tìm thấy chủ đề `{slug}` trong danh sách đã thêm.")


async def _do_merge_category(from_slug: str, to_slug: str) -> str:
    """Logic thật của /mergecategory, dùng chung cho slash + prefix — trả về
    câu trả lời hoàn chỉnh để gửi cho user."""
    all_cats = await get_all_categories_async()
    moved = await bot.loop.run_in_executor(None, db.merge_category_images, from_slug, to_slug)

    # Gộp từ khóa của from_slug vào to_slug — CHỈ khi to_slug là chủ đề
    # custom (thêm qua lệnh). Chủ đề có sẵn trong code (categories.py) không
    # sửa được qua lệnh, phải tự thêm từ khóa vào code nếu muốn.
    if to_slug not in CATEGORIES:
        from_keywords = get_keywords(all_cats[from_slug])
        to_keywords = get_keywords(all_cats[to_slug])
        merged_keywords = to_keywords + [k for k in from_keywords if k not in to_keywords]
        if merged_keywords != to_keywords:
            await bot.loop.run_in_executor(None, db.edit_custom_category, to_slug, None, merged_keywords, None)
        keyword_note = f"\nTừ khóa đã gộp vào `{to_slug}`: `{', '.join(merged_keywords)}`"
    else:
        keyword_note = (
            f"\n⚠️ `{to_slug}` là chủ đề có sẵn trong code — muốn gộp thêm từ khóa của "
            f"`{from_slug}` thì tự thêm vào `categories.py`."
        )

    _invalidate_categories_cache()

    if from_slug not in CATEGORIES:
        remove_note = f"\n`{from_slug}` giờ đã hết ảnh (0 ảnh còn lại) — dùng `/removecategory {from_slug}` nếu muốn xoá luôn chủ đề này."
    else:
        remove_note = f"\n⚠️ `{from_slug}` là chủ đề có sẵn trong code — vẫn còn hiện trong danh sách, tự xoá khỏi `categories.py` nếu không cần nữa."

    return f"✅ Đã chuyển **{moved}** ảnh từ `{from_slug}` sang `{to_slug}`." + keyword_note + remove_note


@bot.tree.command(name="mergecategory", description="[Admin] Gộp toàn bộ ảnh đã crawl của 1 chủ đề vào chủ đề khác")
@app_commands.describe(
    from_slug="Chủ đề NGUỒN — ảnh sẽ được CHUYỂN ĐI khỏi đây (chủ đề này sẽ hết ảnh sau khi gộp)",
    to_slug="Chủ đề ĐÍCH — ảnh sẽ được gộp VÀO đây",
)
async def mergecategory_slash(interaction: discord.Interaction, from_slug: str, to_slug: str):
    if not is_admin(interaction.user.id):
        await interaction.response.send_message("⚠️ Chỉ admin mới dùng được lệnh này.", ephemeral=True)
        return

    from_slug = from_slug.strip().lower()
    to_slug = to_slug.strip().lower()
    if from_slug == to_slug:
        await interaction.response.send_message("⚠️ 2 chủ đề phải khác nhau.", ephemeral=True)
        return

    # QUAN TRỌNG: defer() TRƯỚC get_all_categories_async() — nguyên tắc
    # xuyên suốt cả bot (xem _paginator_navigate, _do_showcase_start...).
    # Lỗi thực tế đã xảy ra: get_all_categories_async() đọc cache category
    # trong RAM, nhưng NẾU cache vừa bị _invalidate_categories_cache() xoá
    # (vd do vừa chạy /removecategory hoặc /addcategory ngay trước đó), lần
    # gọi kế tiếp phải đọc lại MongoDB từ đầu — nếu đúng lúc Mongo hơi chậm,
    # tổng thời gian trước khi kịp defer() có thể vượt quá 3 giây Discord
    # cho phép, gây "Interaction đã hết hạn trước khi kịp defer()" (đã thấy
    # trong log thực tế ngày 13/09 lúc dùng /mergecategory ngay sau
    # /removecategory).
    if not await _timed_defer(interaction, ephemeral=True):
        return

    all_cats = await get_all_categories_async()
    if from_slug not in all_cats:
        await interaction.followup.send(f"❌ Không tìm thấy chủ đề nguồn `{from_slug}`.")
        return
    if to_slug not in all_cats:
        await interaction.followup.send(f"❌ Không tìm thấy chủ đề đích `{to_slug}`.")
        return

    await interaction.followup.send(await _do_merge_category(from_slug, to_slug))


@bot.command(name="mergecategory", help="[Admin] !mergecategory slug_nguồn | slug_đích — gộp toàn bộ ảnh của slug_nguồn vào slug_đích")
async def mergecategory_prefix(ctx, *, args: str = None):
    if not is_admin(ctx.author.id):
        await ctx.send("⚠️ Chỉ admin mới dùng được lệnh này.")
        return
    if not args or args.count("|") != 1:
        await ctx.send("⚠️ Cú pháp: `!mergecategory slug_nguồn | slug_đích`")
        return

    from_slug, to_slug = [p.strip().lower() for p in args.split("|")]
    if from_slug == to_slug:
        await ctx.send("⚠️ 2 chủ đề phải khác nhau.")
        return

    all_cats = await get_all_categories_async()
    if from_slug not in all_cats:
        await ctx.send(f"❌ Không tìm thấy chủ đề nguồn `{from_slug}`.")
        return
    if to_slug not in all_cats:
        await ctx.send(f"❌ Không tìm thấy chủ đề đích `{to_slug}`.")
        return

    await ctx.typing()
    await ctx.send(await _do_merge_category(from_slug, to_slug))


# ============================================================
# Lệnh admin: /cleanup, !cleanup — dọn ảnh lỗi link (404...) hoặc bị gửi
# quá nhiều lần trong 1 category. Chỉ ADMIN_IDS dùng được.
# ============================================================

CLEANUP_OVERUSED_THRESHOLD = 20  # ảnh bị gửi >= N lần thì coi là "cũ", xoá bớt
CLEANUP_MAX_CHECK = 100          # tối đa số ảnh kiểm tra link mỗi lần chạy


def _check_broken(url: str):
    try:
        resp = requests.head(url, timeout=4, allow_redirects=True)
        return url if resp.status_code >= 400 else None
    except Exception:
        return url


def _cleanup_category(category_key: str) -> str:
    """Chạy đồng bộ (blocking) trong executor. Trả về báo cáo kết quả."""
    overused_removed = db.delete_overused_images(category_key, CLEANUP_OVERUSED_THRESHOLD)

    urls = db.get_all_image_urls(category_key)[:CLEANUP_MAX_CHECK]
    broken = []
    if urls:
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            for result in executor.map(_check_broken, urls):
                if result:
                    broken.append(result)
    broken_removed = db.delete_images_by_url(broken)

    remaining = db.count_images(category_key)
    return (
        f"Đã xoá {overused_removed} ảnh bị gửi quá {CLEANUP_OVERUSED_THRESHOLD} lần, "
        f"{broken_removed} ảnh lỗi link (đã kiểm tra {len(urls)} ảnh). Còn lại {remaining} ảnh."
    )


@bot.tree.command(name="cleanup", description="[Admin] Dọn ảnh hỏng link / dùng quá nhiều lần trong 1 chủ đề")
@app_commands.describe(chu_de="Chủ đề cần dọn")
@app_commands.autocomplete(chu_de=category_autocomplete)
async def cleanup_slash(interaction: discord.Interaction, chu_de: str):
    if not is_admin(interaction.user.id):
        await interaction.response.send_message("⚠️ Chỉ admin mới dùng được lệnh này.", ephemeral=True)
        return

    # defer() TRƯỚC get_all_categories_async() — xem ghi chú chi tiết ở
    # mergecategory_slash (lỗi thực tế đã xảy ra khi cache category vừa bị
    # invalidate, khiến lần đọc kế tiếp chậm hơn 3 giây).
    if not await _timed_defer(interaction):
        return

    all_cats = await get_all_categories_async()
    if chu_de not in all_cats:
        await interaction.followup.send(f"❌ Chủ đề không hợp lệ: {chu_de}")
        return
    report = await bot.loop.run_in_executor(None, _cleanup_category, chu_de)
    await interaction.followup.send(f"🧹 **{all_cats[chu_de]['label']}**: {report}")


@bot.command(name="cleanup", help="[Admin] !cleanup <chủ_đề> — dọn ảnh hỏng link / dùng quá nhiều lần")
async def cleanup_prefix(ctx, chu_de: str = None):
    if not is_admin(ctx.author.id):
        await ctx.send("⚠️ Chỉ admin mới dùng được lệnh này.")
        return

    all_cats = await get_all_categories_async()
    if not chu_de or chu_de.lower() not in all_cats:
        await ctx.send("⚠️ Vui lòng chọn 1 chủ đề hợp lệ.")
        return

    chu_de = chu_de.lower()
    await ctx.typing()
    report = await bot.loop.run_in_executor(None, _cleanup_category, chu_de)
    await ctx.send(f"🧹 **{all_cats[chu_de]['label']}**: {report}")


# ============================================================
# Lệnh admin: /showcase, !showcase — đăng 1 "bảng giới thiệu" chủ đề vào
# kênh: ảnh mẫu + tên chủ đề + tag admin quản lý + nút "Bắt đầu". Ai bấm
# "Bắt đầu" sẽ nhận 1 phiên xem ảnh riêng tư (ephemeral, chỉ người bấm thấy).
# ============================================================

async def _post_showcase_board(target_channel, category_key: str, info: dict, admin_id: int):
    """Trả về (message, error_text). error_text != None nếu thất bại."""
    preview_url = await _fetch_next_image_url(category_key, [])
    if not preview_url:
        eta = await _next_crawl_eta_text()
        return None, f"❌ Không lấy được ảnh mẫu cho chủ đề: **{info['label']}** (kho đang trống, lần crawl kế tiếp {eta})."

    embed = discord.Embed(title=f"🖼️ {info['label']}", color=discord.Color.gold())
    embed.set_image(url=preview_url)
    embed.add_field(name="Quản lý bởi", value=f"<@{admin_id}>", inline=True)
    embed.add_field(name="Chủ đề", value=f"`{category_key}`", inline=True)
    embed.set_footer(text="Bấm \"Bắt đầu\" để xem ảnh riêng tư, chỉ bạn thấy được.")

    message = await target_channel.send(embed=embed, view=SHOWCASE_VIEW)
    await bot.loop.run_in_executor(None, db.save_showcase_board, str(message.id), category_key)
    return message, None


@bot.tree.command(name="showcase", description="[Admin] Đăng bảng giới thiệu 1 chủ đề vào kênh (nút Bắt đầu cho mọi người bấm)")
@app_commands.describe(chu_de="Chủ đề cần giới thiệu", kenh="Kênh muốn đăng (bỏ trống = kênh hiện tại)")
@app_commands.autocomplete(chu_de=category_autocomplete)
async def showcase_slash(interaction: discord.Interaction, chu_de: str, kenh: discord.TextChannel = None):
    if not is_admin(interaction.user.id):
        await interaction.response.send_message("⚠️ Chỉ admin mới dùng được lệnh này.", ephemeral=True)
        return

    # defer() TRƯỚC get_all_categories_async() — xem ghi chú chi tiết ở
    # mergecategory_slash (lỗi thực tế đã xảy ra khi cache category vừa bị
    # invalidate, khiến lần đọc kế tiếp chậm hơn 3 giây).
    if not await _timed_defer(interaction, ephemeral=True):
        return

    all_cats = await get_all_categories_async()
    info = all_cats.get(chu_de)
    if not info:
        await interaction.followup.send(f"❌ Chủ đề không hợp lệ: {chu_de}")
        return

    target_channel = kenh or interaction.channel
    if info.get("nsfw") and not _channel_allows_nsfw(target_channel):
        await interaction.followup.send(
            f"🔞 Chủ đề **{info['label']}** là NSFW, chỉ đăng được vào kênh đã đánh dấu Age-Restricted."
        )
        return

    message, error = await _post_showcase_board(target_channel, chu_de, info, interaction.user.id)
    if error:
        await interaction.followup.send(error)
        return
    await interaction.followup.send(f"✅ Đã đăng bảng giới thiệu **{info['label']}** vào {target_channel.mention}.")


@bot.command(name="showcase", help="[Admin] !showcase <chủ_đề> [#kênh] — đăng bảng giới thiệu chủ đề")
async def showcase_prefix(ctx, chu_de: str = None, kenh: discord.TextChannel = None):
    if not is_admin(ctx.author.id):
        await ctx.send("⚠️ Chỉ admin mới dùng được lệnh này.")
        return

    all_cats = await get_all_categories_async()
    if not chu_de or chu_de.lower() not in all_cats:
        await ctx.send("⚠️ Vui lòng chọn 1 chủ đề hợp lệ.")
        return

    category_key = chu_de.lower()
    info = all_cats[category_key]
    target_channel = kenh or ctx.channel
    if info.get("nsfw") and not _channel_allows_nsfw(target_channel):
        await ctx.send(f"🔞 Chủ đề **{info['label']}** là NSFW, chỉ đăng được vào kênh đã đánh dấu Age-Restricted.")
        return
    await ctx.typing()

    message, error = await _post_showcase_board(target_channel, category_key, info, ctx.author.id)
    if error:
        await ctx.send(error)
        return
    await ctx.send(f"✅ Đã đăng bảng giới thiệu **{info['label']}** vào {target_channel.mention}.")


# ============================================================
# Lệnh admin: /config, !config — cấu hình giới hạn kênh/role RIÊNG cho
# từng server (guild). Nếu server chưa cấu hình gì qua đây, bot dùng
# ALLOWED_CHANNEL_IDS/ALLOWED_ROLE_IDS từ biến môi trường làm mặc định.
# ============================================================

def _format_guild_config(cfg: dict) -> str:
    channels_text = ", ".join(f"<#{c}>" for c in cfg["allowed_channel_ids"]) or "(không giới hạn — dùng được mọi kênh)"
    roles_text = ", ".join(f"<@&{r}>" for r in cfg["allowed_role_ids"]) or "(không giới hạn — ai cũng dùng được)"
    return f"**Kênh cho phép:** {channels_text}\n**Role cho phép:** {roles_text}"


CONFIG_ACTIONS = [
    app_commands.Choice(name="Xem cấu hình hiện tại", value="view"),
    app_commands.Choice(name="Thêm kênh cho phép", value="add_channel"),
    app_commands.Choice(name="Xoá 1 kênh khỏi danh sách cho phép", value="remove_channel"),
    app_commands.Choice(name="Xoá hết giới hạn kênh (dùng được mọi kênh)", value="clear_channels"),
    app_commands.Choice(name="Thêm role cho phép", value="add_role"),
    app_commands.Choice(name="Xoá 1 role khỏi danh sách cho phép", value="remove_role"),
    app_commands.Choice(name="Xoá hết giới hạn role (ai cũng dùng được)", value="clear_roles"),
]


@bot.tree.command(name="config", description="[Admin] Cấu hình giới hạn kênh/role dùng lệnh ảnh cho server này")
@app_commands.describe(action="Hành động", kenh="Kênh (dùng cho add_channel/remove_channel)", role="Role (dùng cho add_role/remove_role)")
@app_commands.choices(action=CONFIG_ACTIONS)
async def config_slash(interaction: discord.Interaction, action: app_commands.Choice[str],
                        kenh: discord.TextChannel = None, role: discord.Role = None):
    if not is_admin(interaction.user.id):
        await interaction.response.send_message("⚠️ Chỉ admin mới dùng được lệnh này.", ephemeral=True)
        return
    if interaction.guild_id is None:
        await interaction.response.send_message("⚠️ Lệnh này chỉ dùng được trong server, không dùng được ở DM.", ephemeral=True)
        return

    guild_id = interaction.guild_id
    act = action.value

    if act == "view":
        cfg = await bot.loop.run_in_executor(None, db.get_guild_config, guild_id)
        await interaction.response.send_message(_format_guild_config(cfg), ephemeral=True)
        return

    if act in ("add_channel", "remove_channel"):
        if not kenh:
            await interaction.response.send_message("⚠️ Cần chọn kênh.", ephemeral=True)
            return
        fn = db.add_guild_allowed_channel if act == "add_channel" else db.remove_guild_allowed_channel
        await bot.loop.run_in_executor(None, fn, guild_id, kenh.id)
        _invalidate_guild_config_cache(guild_id)
        verb = "Đã thêm" if act == "add_channel" else "Đã xoá"
        prep = "vào" if act == "add_channel" else "khỏi"
        await interaction.response.send_message(f"✅ {verb} {kenh.mention} {prep} danh sách kênh cho phép.", ephemeral=True)
        return

    if act == "clear_channels":
        await bot.loop.run_in_executor(None, db.clear_guild_allowed_channels, guild_id)
        _invalidate_guild_config_cache(guild_id)
        await interaction.response.send_message("✅ Đã xoá hết giới hạn kênh (dùng được ở mọi kênh).", ephemeral=True)
        return

    if act in ("add_role", "remove_role"):
        if not role:
            await interaction.response.send_message("⚠️ Cần chọn role.", ephemeral=True)
            return
        fn = db.add_guild_allowed_role if act == "add_role" else db.remove_guild_allowed_role
        await bot.loop.run_in_executor(None, fn, guild_id, role.id)
        _invalidate_guild_config_cache(guild_id)
        verb = "Đã thêm" if act == "add_role" else "Đã xoá"
        prep = "vào" if act == "add_role" else "khỏi"
        await interaction.response.send_message(f"✅ {verb} role {role.mention} {prep} danh sách cho phép.", ephemeral=True)
        return

    if act == "clear_roles":
        await bot.loop.run_in_executor(None, db.clear_guild_allowed_roles, guild_id)
        _invalidate_guild_config_cache(guild_id)
        await interaction.response.send_message("✅ Đã xoá hết giới hạn role (ai cũng dùng được).", ephemeral=True)
        return


@bot.command(name="config", help="[Admin] !config view | add_channel #kênh | remove_channel #kênh | clear_channels | add_role @role | remove_role @role | clear_roles")
async def config_prefix(ctx, action: str = None):
    if not is_admin(ctx.author.id):
        await ctx.send("⚠️ Chỉ admin mới dùng được lệnh này.")
        return
    if ctx.guild is None:
        await ctx.send("⚠️ Lệnh này chỉ dùng được trong server, không dùng được ở DM.")
        return

    guild_id = ctx.guild.id
    action = (action or "view").lower()

    if action == "view":
        cfg = await bot.loop.run_in_executor(None, db.get_guild_config, guild_id)
        await ctx.send(_format_guild_config(cfg))
        return

    if action in ("add_channel", "remove_channel"):
        if not ctx.message.channel_mentions:
            await ctx.send(f"⚠️ Cần tag kênh, vd: `!config {action} #anh-vui`")
            return
        channel = ctx.message.channel_mentions[0]
        fn = db.add_guild_allowed_channel if action == "add_channel" else db.remove_guild_allowed_channel
        await bot.loop.run_in_executor(None, fn, guild_id, channel.id)
        _invalidate_guild_config_cache(guild_id)
        verb = "Đã thêm" if action == "add_channel" else "Đã xoá"
        prep = "vào" if action == "add_channel" else "khỏi"
        await ctx.send(f"✅ {verb} {channel.mention} {prep} danh sách kênh cho phép.")
        return

    if action == "clear_channels":
        await bot.loop.run_in_executor(None, db.clear_guild_allowed_channels, guild_id)
        _invalidate_guild_config_cache(guild_id)
        await ctx.send("✅ Đã xoá hết giới hạn kênh.")
        return

    if action in ("add_role", "remove_role"):
        if not ctx.message.role_mentions:
            await ctx.send(f"⚠️ Cần tag role, vd: `!config {action} @Member`")
            return
        role_obj = ctx.message.role_mentions[0]
        fn = db.add_guild_allowed_role if action == "add_role" else db.remove_guild_allowed_role
        await bot.loop.run_in_executor(None, fn, guild_id, role_obj.id)
        _invalidate_guild_config_cache(guild_id)
        verb = "Đã thêm" if action == "add_role" else "Đã xoá"
        prep = "vào" if action == "add_role" else "khỏi"
        await ctx.send(f"✅ {verb} role {role_obj.mention} {prep} danh sách cho phép.")
        return

    if action == "clear_roles":
        await bot.loop.run_in_executor(None, db.clear_guild_allowed_roles, guild_id)
        _invalidate_guild_config_cache(guild_id)
        await ctx.send("✅ Đã xoá hết giới hạn role.")
        return

    await ctx.send("⚠️ Hành động không hợp lệ. Dùng: `view | add_channel | remove_channel | clear_channels | add_role | remove_role | clear_roles`")


# ============================================================
# Lệnh admin: /setup — wizard thiết lập showcase board cho NHIỀU chủ đề
# cùng lúc: chọn chủ đề (nhiều/chọn tất cả) -> gán kênh cho từng chủ đề
# (chọn kênh có sẵn hoặc tạo kênh mới) -> tự động đăng showcase board vào
# đúng kênh tương ứng khi đủ thông tin.
# ============================================================

# Tên category kênh (nhóm kênh trong sidebar Discord) dùng để chứa các kênh
# text được /setup tự tạo mới. Chỉ tạo 1 lần cho mỗi server, các lần chạy
# /setup sau tái sử dụng lại — đổi tên ở đây nếu muốn.
SETUP_CHANNEL_CATEGORY_NAME = "📸 Ảnh chủ đề"


async def _ensure_setup_channel_category(guild: discord.Guild):
    """
    Trả về (category_channel, error_message). category_channel là None nếu
    lỗi (error_message sẽ có nội dung), ngược lại error_message rỗng.
    Ưu tiên tái sử dụng category đã lưu ID trong DB; nếu ID đó không còn hợp
    lệ (bị xoá thủ công...), thử tìm lại theo tên trước khi tạo mới hẳn —
    tránh tạo trùng nhiều category kênh cùng tên qua nhiều lần chạy /setup.
    """
    stored_id = await bot.loop.run_in_executor(None, db.get_guild_setup_category_id, guild.id)
    if stored_id:
        existing = guild.get_channel(stored_id)
        if isinstance(existing, discord.CategoryChannel):
            return existing, ""

    for cat in guild.categories:
        if cat.name == SETUP_CHANNEL_CATEGORY_NAME:
            await bot.loop.run_in_executor(None, db.set_guild_setup_category_id, guild.id, cat.id)
            return cat, ""

    try:
        new_category = await guild.create_category(name=SETUP_CHANNEL_CATEGORY_NAME)
    except discord.Forbidden:
        return None, "❌ Bot không có quyền tạo category kênh (cần quyền **Manage Channels**)."
    except Exception as e:
        logger.warning(f"Lỗi tạo category kênh trong /setup: {e}")
        return None, f"❌ Lỗi khi tạo category kênh: {e}"

    await bot.loop.run_in_executor(None, db.set_guild_setup_category_id, guild.id, new_category.id)
    return new_category, ""


class ChannelAssignWizard:
    """Trạng thái đi qua từng chủ đề đã chọn, hỏi gán kênh, rồi đăng showcase board."""

    def __init__(self, categories: list, admin_id: int):
        self.categories = categories  # list[(key, info)], hàng đợi xử lý tuần tự
        self.admin_id = admin_id
        self.index = 0
        self.assignments = {}  # category_key -> channel_id

    async def start(self, interaction: discord.Interaction):
        await self._prompt_current(interaction)

    async def _prompt_current(self, interaction: discord.Interaction):
        key, info = self.categories[self.index]
        view = ChannelPickerView(self, key, info)
        remaining = len(self.categories) - self.index
        embed = discord.Embed(
            title=f"📌 Chọn kênh cho: {info['label']}",
            description=(
                f"Chủ đề {self.index + 1}/{len(self.categories)} (`{key}`)\n\n"
                "Chọn 1 kênh có sẵn ở dropdown bên dưới, hoặc bấm **➕ Tạo kênh mới**.\n\n"
                f"Muốn nhanh gọn? Bấm **🚀 Tạo tất cả kênh còn lại** để bot tự tạo "
                f"kênh mới (tên = tên chủ đề) cho cả {remaining} chủ đề còn lại cùng lúc."
            ),
            color=discord.Color.blurple(),
        )
        await interaction.response.edit_message(embed=embed, view=view)

    async def advance(self, interaction: discord.Interaction, channel: discord.TextChannel):
        key, _info = self.categories[self.index]
        self.assignments[key] = channel.id
        self.index += 1

        if self.index >= len(self.categories):
            await self._finish(interaction)
            return

        await self._prompt_current(interaction)

    async def create_all_remaining(self, interaction: discord.Interaction):
        """
        Tự tạo kênh mới (tên = label chủ đề) cho TẤT CẢ chủ đề còn lại trong
        hàng đợi (kể cả chủ đề đang hiện ở bước này), gom vào category kênh
        chung, rồi kết thúc wizard luôn — bỏ qua việc hỏi từng chủ đề một.
        Dùng defer() vì có thể tạo nhiều kênh liên tiếp, tốn hơn 3 giây.
        """
        if not await _timed_defer(interaction):
            return

        remaining = self.categories[self.index:]
        setup_category, cat_error = await _ensure_setup_channel_category(interaction.guild)
        if cat_error:
            logger.warning(f"/setup (tạo tất cả kênh) không gom được vào category kênh chung: {cat_error}")

        error_lines = []
        for key, info in remaining:
            try:
                new_channel = await interaction.guild.create_text_channel(name=info["label"][:90], category=setup_category)
            except discord.Forbidden:
                error_lines.append(f"❌ **{info['label']}**: bot không có quyền tạo kênh, bỏ qua.")
                continue
            except Exception as e:
                logger.warning(f"Lỗi tạo kênh cho `{key}` trong /setup (tạo tất cả): {e}")
                error_lines.append(f"❌ **{info['label']}**: lỗi khi tạo kênh, bỏ qua.")
                continue
            self.assignments[key] = new_channel.id

        self.index = len(self.categories)
        await self._finish(interaction, extra_lines=error_lines)

    async def _finish(self, interaction: discord.Interaction, extra_lines: list = None):
        all_cats = await get_all_categories_async()
        lines = list(extra_lines) if extra_lines else []
        for key, channel_id in self.assignments.items():
            info = all_cats.get(key)
            channel = interaction.guild.get_channel(channel_id) if interaction.guild else None
            if not info or not channel:
                lines.append(f"❌ `{key}`: không tìm thấy kênh hoặc chủ đề, bỏ qua.")
                continue
            if info.get("nsfw") and not _channel_allows_nsfw(channel):
                lines.append(f"⚠️ **{info['label']}**: bỏ qua vì là NSFW nhưng {channel.mention} không phải kênh Age-Restricted.")
                continue
            _msg, error = await _post_showcase_board(channel, key, info, self.admin_id)
            if error:
                lines.append(f"❌ **{info['label']}**: {error}")
            else:
                lines.append(f"✅ **{info['label']}** → {channel.mention}")

        embed = discord.Embed(
            title="🎉 Thiết lập hoàn tất",
            description="\n".join(lines) if lines else "(không có chủ đề nào được xử lý)",
            color=discord.Color.green(),
        )
        # create_all_remaining() đã defer() trước đó (response is_done), nên
        # phải sửa qua edit_original_response thay vì response.edit_message.
        if interaction.response.is_done():
            await interaction.edit_original_response(embed=embed, view=None)
        else:
            await interaction.response.edit_message(embed=embed, view=None)


class NewChannelModal(discord.ui.Modal):
    def __init__(self, wizard: ChannelAssignWizard, category_key: str, info: dict):
        super().__init__(title=f"Tạo kênh cho: {info['label'][:40]}")
        self.wizard = wizard
        self.category_key = category_key
        self.channel_name_input = discord.ui.TextInput(
            label="Tên kênh mới",
            placeholder=f"vd: {info['label']}",
            default=info["label"][:90],
            max_length=90,
        )
        self.add_item(self.channel_name_input)

    async def on_submit(self, interaction: discord.Interaction):
        name = self.channel_name_input.value.strip()
        if not name:
            await interaction.response.send_message("⚠️ Tên kênh không hợp lệ.", ephemeral=True)
            return

        # Gom kênh mới vào 1 category kênh chung (tạo sẵn/tái sử dụng theo
        # server) thay vì thả nổi ở ngoài — không chặn tạo kênh nếu lỗi,
        # chỉ báo cho admin biết để tự sắp xếp lại thủ công nếu cần.
        setup_category, cat_error = await _ensure_setup_channel_category(interaction.guild)

        try:
            new_channel = await interaction.guild.create_text_channel(name=name, category=setup_category)
        except discord.Forbidden:
            await interaction.response.send_message("❌ Bot không có quyền tạo kênh trong server này.", ephemeral=True)
            return
        except Exception as e:
            logger.warning(f"Lỗi tạo kênh mới trong /setup: {e}")
            await interaction.response.send_message(f"❌ Lỗi khi tạo kênh: {e}", ephemeral=True)
            return

        if cat_error:
            logger.warning(f"/setup tạo kênh #{new_channel.name} ngoài category kênh chung: {cat_error}")

        await self.wizard.advance(interaction, new_channel)


class ChannelPickerView(discord.ui.View):
    def __init__(self, wizard: ChannelAssignWizard, category_key: str, info: dict):
        super().__init__(timeout=300)
        self.wizard = wizard
        self.category_key = category_key
        self.info = info

        channel_select = discord.ui.ChannelSelect(
            placeholder="Chọn kênh có sẵn...",
            channel_types=[discord.ChannelType.text],
            min_values=1,
            max_values=1,
            row=0,
        )
        channel_select.callback = self._on_channel_selected
        self.channel_select = channel_select
        self.add_item(channel_select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.wizard.admin_id:
            await interaction.response.send_message("⚠️ Chỉ người chạy lệnh `/setup` mới thao tác được ở đây.", ephemeral=True)
            return False
        return True

    async def _on_channel_selected(self, interaction: discord.Interaction):
        picked = self.channel_select.values[0]
        real_channel = interaction.guild.get_channel(picked.id)
        if real_channel is None:
            await interaction.response.send_message("❌ Không lấy được kênh này, thử lại nhé.", ephemeral=True)
            return
        await self.wizard.advance(interaction, real_channel)

    @discord.ui.button(label="➕ Tạo kênh mới", style=discord.ButtonStyle.primary, row=1)
    async def create_channel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(NewChannelModal(self.wizard, self.category_key, self.info))

    @discord.ui.button(label="🚀 Tạo tất cả kênh còn lại", style=discord.ButtonStyle.success, row=1)
    async def create_all_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.wizard.create_all_remaining(interaction)


class CategoryPickerView(discord.ui.View):
    """Bước 1 của /setup: chọn 1 hoặc nhiều chủ đề (hoặc 'Chọn tất cả')."""

    def __init__(self, categories: list, admin_id: int):
        super().__init__(timeout=300)
        self.categories = categories  # list[(key, info)]
        self.admin_id = admin_id

        options = [
            discord.SelectOption(label="✅ Chọn tất cả", value="__all__", description=f"Thiết lập cả {len(categories)} chủ đề")
        ]
        for key, info in categories[:24]:  # chừa 1 chỗ cho "Chọn tất cả", tối đa 25 lựa chọn
            options.append(discord.SelectOption(label=info["label"][:100], value=key, description=f"`{key}`"))

        select = discord.ui.Select(
            placeholder="Chọn 1 hoặc nhiều chủ đề cần thiết lập...",
            min_values=1,
            max_values=len(options),
            options=options,
        )
        select.callback = self._on_select
        self.add_item(select)
        self.select = select

    def build_embed(self) -> discord.Embed:
        lines = [f"**{i + 1}.** {info['label']} (`{key}`)" for i, (key, info) in enumerate(self.categories)]
        return discord.Embed(
            title="🛠️ Thiết lập showcase board",
            description=(
                "Chọn chủ đề cần thiết lập ở dropdown bên dưới (chọn nhiều được, "
                "hoặc \"✅ Chọn tất cả\"):\n\n" + "\n".join(lines)
            ),
            color=discord.Color.blurple(),
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.admin_id:
            await interaction.response.send_message("⚠️ Chỉ người chạy lệnh `/setup` mới thao tác được ở đây.", ephemeral=True)
            return False
        return True

    async def _on_select(self, interaction: discord.Interaction):
        values = self.select.values
        if "__all__" in values:
            selected_keys = {key for key, _ in self.categories}
        else:
            selected_keys = set(values)

        selected = [(key, info) for key, info in self.categories if key in selected_keys]
        if not selected:
            await interaction.response.send_message("⚠️ Chưa chọn chủ đề nào.", ephemeral=True)
            return

        wizard = ChannelAssignWizard(selected, self.admin_id)
        await wizard.start(interaction)


@bot.tree.command(name="setup", description="[Admin] Wizard thiết lập showcase board cho nhiều chủ đề cùng lúc")
async def setup_slash(interaction: discord.Interaction):
    if not is_admin(interaction.user.id):
        await interaction.response.send_message("⚠️ Chỉ admin mới dùng được lệnh này.", ephemeral=True)
        return
    if interaction.guild is None:
        await interaction.response.send_message("⚠️ Lệnh này chỉ dùng được trong server, không dùng được ở DM.", ephemeral=True)
        return

    all_cats = await get_all_categories_async()
    if not all_cats:
        await interaction.response.send_message("❌ Chưa có chủ đề nào để thiết lập.", ephemeral=True)
        return

    view = CategoryPickerView(list(all_cats.items()), interaction.user.id)
    await interaction.response.send_message(embed=view.build_embed(), view=view, ephemeral=True)


TOKEN = os.getenv("DISCORD_TOKEN")

if __name__ == "__main__":
    if not TOKEN:
        logger.error("Chưa thiết lập biến môi trường DISCORD_TOKEN trên Render!")
    else:
        keep_alive()

        # LƯU Ý: KHÔNG tự retry bot.run(TOKEN) trong vòng lặp trên cùng 1
        # process — sau khi bot.run() dừng (kể cả do lỗi), discord.py đóng
        # session HTTP nội bộ của object `bot`, nên gọi lại bot.run() trên
        # cùng object sẽ luôn crash với "RuntimeError: Session is closed",
        # KHÔNG PHẢI retry thật. Cách đúng: chờ rồi thoát hẳn tiến trình,
        # để Render tự khởi động lại process MỚI (bot object mới tinh).
        try:
            bot.run(TOKEN)
        except discord.errors.HTTPException as e:
            if e.status == 429:
                logger.warning(
                    "Bị Discord Rate Limit 429 khi đăng nhập. Chờ 5 phút rồi "
                    "thoát tiến trình để Render tự khởi động lại (không retry "
                    "trong cùng process vì session HTTP đã bị đóng). Chờ lâu "
                    "hơn 60 giây để tránh dính 429 lặp lại liên tục nếu giới "
                    "hạn của Discord chưa được gỡ."
                )
                time.sleep(300)
                sys.exit(1)
            else:
                raise
