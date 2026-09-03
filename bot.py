import asyncio
import concurrent.futures
import logging
import os
import random
import time
from datetime import datetime, timezone, timedelta

import discord
import requests
from discord import app_commands
from discord.ext import commands, tasks
from keep_alive import keep_alive
from pinterest_crawler import search_pinterest_images_with_retry
from categories import CATEGORIES
import db
import crawl_job

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


def check_access(user_id: int, channel_id: int, roles) -> str:
    """Trả về thông báo lỗi nếu bị chặn, chuỗi rỗng nếu được phép dùng lệnh."""
    if is_admin(user_id):
        return ""
    if ALLOWED_CHANNEL_IDS and channel_id not in ALLOWED_CHANNEL_IDS:
        return "⚠️ Lệnh này chỉ dùng được ở kênh được chỉ định."
    if ALLOWED_ROLE_IDS:
        role_ids = {r.id for r in roles} if roles else set()
        if not (role_ids & ALLOWED_ROLE_IDS):
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


@bot.event
async def on_ready():
    global _persistent_view_ready
    logger.info(f"Bot đã đăng nhập thành công với tên: {bot.user}")
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
# Lấy ảnh: ưu tiên MongoDB (random trong category, không theo thứ tự),
# fallback cào Pinterest trực tiếp nếu DB hết ảnh khả dụng. Có đo thời gian
# và cảnh báo (-> kênh log nếu bật) khi 1 bước chậm bất thường, để dễ chẩn
# đoán chỗ nào đang làm bot phản hồi chậm.
# ============================================================

SLOW_DB_THRESHOLD_SECONDS = 3
SLOW_CRAWL_THRESHOLD_SECONDS = 15


async def _fetch_next_image_url(category_key: str, keyword: str, exclude_urls: list):
    def fetch_db():
        try:
            doc = db.get_next_image(category_key, exclude_urls)
            return doc["image_url"] if doc else None
        except Exception as e:
            logger.warning(f"Lỗi đọc MongoDB, sẽ fallback sang cào trực tiếp: {e}")
            return None

    t0 = time.monotonic()
    url = await bot.loop.run_in_executor(None, fetch_db)
    db_elapsed = time.monotonic() - t0
    if db_elapsed > SLOW_DB_THRESHOLD_SECONDS:
        logger.warning(f"Đọc MongoDB cho '{category_key}' chậm bất thường: {db_elapsed:.1f}s")
    else:
        logger.info(f"[perf] Đọc MongoDB cho '{category_key}': {db_elapsed:.2f}s")

    if url:
        return url

    def fetch_crawl():
        try:
            results = search_pinterest_images_with_retry(keyword, limit=20, retries=3)
        except Exception as e:
            logger.warning(f"Lỗi fallback cào Pinterest: {e}")
            return None
        for u in results:
            if u not in exclude_urls:
                return u
        return None

    t1 = time.monotonic()
    url = await bot.loop.run_in_executor(None, fetch_crawl)
    crawl_elapsed = time.monotonic() - t1
    if crawl_elapsed > SLOW_CRAWL_THRESHOLD_SECONDS:
        logger.warning(f"Fallback cào Pinterest cho '{category_key}' chậm bất thường: {crawl_elapsed:.1f}s")
    else:
        logger.info(f"[perf] Fallback cào Pinterest cho '{category_key}': {crawl_elapsed:.2f}s")

    return url



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


def _build_image_embed(label: str, url: str) -> discord.Embed:
    embed = discord.Embed(title=f"🖼️ {label}", color=discord.Color.red())
    embed.set_image(url=url)
    embed.set_footer(text="Nguồn: kho ảnh đã crawl · fallback Pinterest")
    return embed


async def _send_image_result(send_func, category_key: str, label: str, keyword: str, url: str, author_id: int):
    """
    send_func: async callable(embed, view) -> discord.Message
    Gửi ảnh + lưu phiên xem (paginator session) vào MongoDB theo message.id,
    để nút Trước/Sau hoạt động vĩnh viễn (không phụ thuộc RAM của bot).
    """
    embed = _build_image_embed(label, url)
    message = await send_func(embed=embed, view=PAGINATOR_VIEW)
    await bot.loop.run_in_executor(
        None, db.save_paginator_session, str(message.id), category_key, label, keyword, [url], 0, author_id
    )


async def _paginator_navigate(interaction: discord.Interaction, direction: int, view: discord.ui.View):
    """
    Logic điều hướng dùng chung cho cả PersistentImagePaginator (nút Trước/Sau
    công khai của /img, /random) và EphemeralImagePaginator (phiên xem riêng
    tư sau khi bấm "Bắt đầu" ở showcase board).
    """
    message_id = str(interaction.message.id)
    session = await bot.loop.run_in_executor(None, db.get_paginator_session, message_id)

    if not session:
        await interaction.response.send_message(
            "⚠️ Không tìm thấy dữ liệu phiên xem ảnh này nữa. Dùng lại `/img` để bắt đầu phiên mới.",
            ephemeral=True,
        )
        return
    if interaction.user.id != session["author_id"]:
        await interaction.response.send_message(
            "⚠️ Bạn không thể điều khiển kết quả tìm kiếm của người khác.", ephemeral=True
        )
        return

    images = session["images"]
    index = session["index"]

    if direction < 0:
        index = max(0, index - 1)
        await bot.loop.run_in_executor(None, db.update_paginator_session, message_id, images, index)
        await interaction.response.edit_message(embed=_build_image_embed(session["label"], images[index]), view=view)
        return

    # direction > 0 ("Sau"): còn ảnh đệm sẵn -> chuyển luôn
    if index < len(images) - 1:
        index += 1
        await bot.loop.run_in_executor(None, db.update_paginator_session, message_id, images, index)
        await interaction.response.edit_message(embed=_build_image_embed(session["label"], images[index]), view=view)
        return

    # Danh sách hữu hạn (vd: favorites, keyword rỗng) -> không có gì để fetch thêm
    if not session.get("keyword"):
        await interaction.response.send_message("📭 Đã hết ảnh trong danh sách này.", ephemeral=True)
        return

    # Hết ảnh đệm -> lấy ảnh mới (DB hoặc fallback Pinterest), có thể mất vài giây
    await interaction.response.defer()
    new_url = await _fetch_next_image_url(session["category_key"], session["keyword"], images)
    if not new_url:
        await interaction.followup.send(
            "❌ Hết ảnh khả dụng cho chủ đề này rồi, thử lại sau nhé.", ephemeral=True
        )
        return

    images.append(new_url)
    index += 1
    await bot.loop.run_in_executor(None, db.update_paginator_session, message_id, images, index)
    await interaction.edit_original_response(embed=_build_image_embed(session["label"], images[index]), view=view)


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


class EphemeralImagePaginator(discord.ui.View):
    """
    Giống PersistentImagePaginator nhưng có thêm nút "💾 Lưu ảnh" — dùng cho
    phiên xem riêng tư (ephemeral) mở ra sau khi bấm "Bắt đầu" ở showcase
    board, hoặc khi xem lại /favorites. custom_id khác PAGINATOR_VIEW để
    Discord phân biệt được 2 view khi cùng đăng ký persistent.
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
        message_id = str(interaction.message.id)
        session = await bot.loop.run_in_executor(None, db.get_paginator_session, message_id)
        if not session:
            await interaction.response.send_message("⚠️ Không tìm thấy dữ liệu ảnh này nữa.", ephemeral=True)
            return
        if interaction.user.id != session["author_id"]:
            await interaction.response.send_message("⚠️ Bạn không thể lưu ảnh của người khác.", ephemeral=True)
            return

        url = session["images"][session["index"]]
        label = session["label"]
        await interaction.response.defer(ephemeral=True)

        saved = await bot.loop.run_in_executor(
            None, db.add_favorite, interaction.user.id, session["category_key"], url
        )
        note = (
            "Đã lưu vào danh sách yêu thích của bạn (xem lại bằng `/favorites`)."
            if saved else
            "Ảnh này bạn đã lưu từ trước rồi (đã có trong `/favorites`)."
        )

        # Cố gắng lấy thêm thông tin ảnh (định dạng, dung lượng) qua HEAD request.
        # Không bắt buộc phải thành công — nếu lỗi/timeout thì bỏ qua, chỉ dùng ghi chú thường.
        info_text = await bot.loop.run_in_executor(None, _try_get_image_info, url)

        dm_embed = discord.Embed(title=f"💾 Ảnh đã lưu — {label}", description=f"✅ {note}", color=discord.Color.green())
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
            await interaction.followup.send("✅ Đã lưu và gửi ảnh vào tin nhắn riêng (DM) của bạn.", ephemeral=True)
            return

        logger.warning(f"Không gửi được DM lưu ảnh cho user {interaction.user.id}: {dm_error_text}")

        fail_embed = discord.Embed(
            title="⚠️ Đã lưu ảnh, nhưng không gửi được tin nhắn riêng (DM)",
            description=f"{note}\n\n**Lý do không gửi được DM:** {dm_error_text}",
            color=discord.Color.orange(),
        )
        fail_embed.set_image(url=url)
        if info_text:
            fail_embed.add_field(name="Thông tin ảnh", value=info_text, inline=False)
        await interaction.followup.send(embed=fail_embed, ephemeral=True)


EPHEMERAL_PAGINATOR_VIEW = EphemeralImagePaginator()


async def _send_ephemeral_image_result(send_func, category_key: str, label: str, keyword: str, url: str, author_id: int):
    """Giống _send_image_result nhưng dùng EPHEMERAL_PAGINATOR_VIEW (3 nút, có Lưu ảnh)."""
    embed = _build_image_embed(label, url)
    message = await send_func(embed=embed, view=EPHEMERAL_PAGINATOR_VIEW)
    await bot.loop.run_in_executor(
        None, db.save_paginator_session, str(message.id), category_key, label, keyword, [url], 0, author_id
    )


async def _send_ephemeral_image_list(send_func, label: str, images: list, author_id: int):
    """
    Giống _send_ephemeral_image_result nhưng nạp sẵn TOÀN BỘ danh sách ảnh
    (không phải 1 ảnh rồi fetch thêm dần) — dùng cho /favorites, vì đây là
    danh sách hữu hạn có sẵn, không cần gọi DB lại mỗi lần bấm Sau/Trước.
    keyword để trống ("") để _paginator_navigate biết đây là danh sách hữu
    hạn, không cố fetch thêm khi hết.
    """
    embed = _build_image_embed(label, images[0])
    message = await send_func(embed=embed, view=EPHEMERAL_PAGINATOR_VIEW)
    await bot.loop.run_in_executor(
        None, db.save_paginator_session, str(message.id), "", label, "", images, 0, author_id
    )


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
        message_id = str(interaction.message.id)
        board = await bot.loop.run_in_executor(None, db.get_showcase_board, message_id)
        if not board:
            await interaction.response.send_message(
                "❌ Bảng giới thiệu này thiếu dữ liệu (có thể tạo từ bản bot cũ), không dùng được nữa.",
                ephemeral=True,
            )
            return

        category_key = board["category_key"]
        all_cats = await get_all_categories_async()
        info = all_cats.get(category_key)
        if not info:
            await interaction.response.send_message("❌ Chủ đề này không còn tồn tại nữa.", ephemeral=True)
            return

        roles = getattr(interaction.user, "roles", None)
        access_err = check_access(interaction.user.id, interaction.channel_id, roles)
        if access_err:
            await interaction.response.send_message(access_err, ephemeral=True)
            return
        wait = check_cooldown(interaction.user.id)
        if wait:
            await interaction.response.send_message(f"⏳ Chờ thêm {wait}s rồi thử lại nhé.", ephemeral=True)
            return
        mark_used(interaction.user.id)

        await interaction.response.defer(ephemeral=True)
        url = await _fetch_next_image_url(category_key, info["keyword"], [])
        if not url:
            await interaction.followup.send(f"❌ Không tìm thấy ảnh nào cho chủ đề: **{info['label']}**", ephemeral=True)
            return

        async def send_func(embed, view):
            return await interaction.followup.send(embed=embed, view=view, wait=True)

        await _send_ephemeral_image_result(send_func, category_key, info["label"], info["keyword"], url, interaction.user.id)


SHOWCASE_VIEW = ShowcaseStartView()


# ============================================================
# Lệnh /img và !img — lấy ảnh theo chủ đề từ MongoDB (random trong
# category, không theo thứ tự), fallback cào Pinterest trực tiếp nếu
# DB hết ảnh khả dụng.
# ============================================================

@bot.tree.command(name="img", description="Lấy ảnh theo chủ đề (đã crawl sẵn từ Pinterest)")
@app_commands.describe(chu_de="Chọn chủ đề ảnh (gõ để tìm)")
@app_commands.autocomplete(chu_de=category_autocomplete)
async def img_slash(interaction: discord.Interaction, chu_de: str):
    roles = getattr(interaction.user, "roles", None)
    access_err = check_access(interaction.user.id, interaction.channel_id, roles)
    if access_err:
        await interaction.response.send_message(access_err, ephemeral=True)
        return
    wait = check_cooldown(interaction.user.id)
    if wait:
        await interaction.response.send_message(f"⏳ Chờ thêm {wait}s rồi thử lại nhé.", ephemeral=True)
        return
    mark_used(interaction.user.id)

    await interaction.response.defer()

    all_cats = await get_all_categories_async()
    info = all_cats.get(chu_de)
    if not info:
        await interaction.followup.send(f"❌ Chủ đề không hợp lệ: **{chu_de}**")
        return

    url = await _fetch_next_image_url(chu_de, info["keyword"], [])
    if not url:
        await interaction.followup.send(f"❌ Không tìm thấy ảnh nào cho chủ đề: **{info['label']}**")
        return

    async def send_func(embed, view):
        return await interaction.followup.send(embed=embed, view=view, wait=True)

    await _send_image_result(send_func, chu_de, info["label"], info["keyword"], url, interaction.user.id)


@bot.command(name="img", help="Lấy ảnh theo chủ đề. Vd: !img meo")
async def img_prefix(ctx, chu_de: str = None):
    roles = getattr(ctx.author, "roles", None)
    access_err = check_access(ctx.author.id, ctx.channel.id, roles)
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
    await ctx.typing()

    url = await _fetch_next_image_url(category_key, info["keyword"], [])
    if not url:
        await ctx.send(f"❌ Không tìm thấy ảnh nào cho chủ đề: **{info['label']}**")
        return

    async def send_func(embed, view):
        return await ctx.send(embed=embed, view=view)

    await _send_image_result(send_func, category_key, info["label"], info["keyword"], url, ctx.author.id)


# ============================================================
# Lệnh /random và !random — lấy 1 ảnh NGẪU NHIÊN TRÊN TOÀN BỘ KHO
# (không phải random category rồi mới chọn ảnh trong đó).
# ============================================================

async def _get_random_image_result():
    """Trả về (category_key, label, keyword, url) hoặc (None, None, None, None) nếu hết ảnh."""
    doc = await bot.loop.run_in_executor(None, db.get_random_image, None)
    all_cats = await get_all_categories_async()

    if doc:
        category_key = doc["category"]
        info = all_cats.get(category_key, {"label": category_key, "keyword": category_key})
        return category_key, info["label"], info["keyword"], doc["image_url"]

    # DB trống hoàn toàn ảnh khả dụng -> fallback: random 1 chủ đề rồi cào trực tiếp
    if not all_cats:
        return None, None, None, None
    category_key = random.choice(list(all_cats.keys()))
    info = all_cats[category_key]
    url = await _fetch_next_image_url(category_key, info["keyword"], [])
    if not url:
        return None, None, None, None
    return category_key, info["label"], info["keyword"], url


@bot.tree.command(name="random", description="Lấy 1 ảnh ngẫu nhiên bất kỳ trong toàn bộ kho")
async def random_slash(interaction: discord.Interaction):
    roles = getattr(interaction.user, "roles", None)
    access_err = check_access(interaction.user.id, interaction.channel_id, roles)
    if access_err:
        await interaction.response.send_message(access_err, ephemeral=True)
        return
    wait = check_cooldown(interaction.user.id)
    if wait:
        await interaction.response.send_message(f"⏳ Chờ thêm {wait}s rồi thử lại nhé.", ephemeral=True)
        return
    mark_used(interaction.user.id)

    await interaction.response.defer()

    category_key, label, keyword, url = await _get_random_image_result()
    if not url:
        await interaction.followup.send("❌ Kho ảnh hiện đang trống, thử lại sau nhé.")
        return

    async def send_func(embed, view):
        return await interaction.followup.send(embed=embed, view=view, wait=True)

    await _send_image_result(send_func, category_key, label, keyword, url, interaction.user.id)


@bot.command(name="random", help="Lấy 1 ảnh ngẫu nhiên bất kỳ trong toàn bộ kho")
async def random_prefix(ctx):
    roles = getattr(ctx.author, "roles", None)
    access_err = check_access(ctx.author.id, ctx.channel.id, roles)
    if access_err:
        await ctx.send(access_err)
        return
    wait = check_cooldown(ctx.author.id)
    if wait:
        await ctx.send(f"⏳ Chờ thêm {wait}s rồi thử lại nhé.")
        return
    mark_used(ctx.author.id)

    await ctx.typing()

    category_key, label, keyword, url = await _get_random_image_result()
    if not url:
        await ctx.send("❌ Kho ảnh hiện đang trống, thử lại sau nhé.")
        return

    async def send_func(embed, view):
        return await ctx.send(embed=embed, view=view)

    await _send_image_result(send_func, category_key, label, keyword, url, ctx.author.id)


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
            embed.add_field(name=info["label"], value="⚠️ lỗi đọc DB", inline=False)
            continue
        total_all += s["total"]
        available_all += s["available"]
        value = (
            f"Tổng: **{s['total']}** · Khả dụng: **{s['available']}**\n"
            f"TB gửi: {s['avg_sent_count']} lần/ảnh (cao nhất {s['max_sent_count']})\n"
            f"Ảnh mới nhất: {_relative_time_vi(s['newest_created_at'])}"
        )
        embed.add_field(name=info["label"], value=value, inline=True)

    custom_count = len(all_cats) - len(CATEGORIES)
    embed.description = (
        f"Tổng cộng: **{total_all}** ảnh · Khả dụng ngay: **{available_all}**\n"
        f"Chủ đề: {len(CATEGORIES)} có sẵn trong code + {custom_count} thêm qua Discord\n"
        f"Lần crawl định kỳ gần nhất: {_relative_time_vi(last_crawl)}"
    )
    return embed


@bot.tree.command(name="stats", description="Xem thống kê chi tiết kho ảnh theo từng chủ đề")
async def stats_slash(interaction: discord.Interaction):
    await interaction.response.defer()
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

async def _maybe_crawl_new_category_now(slug: str, keyword: str) -> str:
    """
    Nếu lần crawl định kỳ gần nhất đã >= 1 tiếng trước (hoặc chưa từng crawl),
    crawl ngay category mới thêm để không phải chờ tới chu kỳ crawl tiếp theo.
    Trả về 1 câu mô tả kết quả để nối vào tin nhắn phản hồi.
    """
    last_crawl = await bot.loop.run_in_executor(None, db.get_last_crawl_time)
    now = datetime.now(timezone.utc)
    should_crawl_now = (last_crawl is None) or ((now - last_crawl) >= timedelta(hours=1))

    if not should_crawl_now:
        return " Chủ đề sẽ được crawl ở lần chạy định kỳ tiếp theo (crawl gần đây vừa mới chạy xong)."

    def do_crawl():
        return crawl_job.crawl_category(slug, keyword)

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
    keyword="Từ khóa tìm kiếm trên Pinterest",
)
async def addcategory_slash(interaction: discord.Interaction, slug: str, label: str, keyword: str):
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

    await interaction.response.defer()
    await bot.loop.run_in_executor(None, db.add_custom_category, slug, label, keyword)
    _invalidate_categories_cache()
    extra = await _maybe_crawl_new_category_now(slug, keyword)
    await interaction.followup.send(
        f"✅ Đã thêm chủ đề **{label}** (`{slug}`, từ khóa: `{keyword}`)." + extra +
        f"\nDùng ngay được với `/img` (gõ để autocomplete) hoặc `!img {slug}`."
    )


@bot.command(name="addcategory", help="[Admin] !addcategory slug | Label hiển thị | từ khóa Pinterest")
async def addcategory_prefix(ctx, *, args: str = None):
    if not is_admin(ctx.author.id):
        await ctx.send("⚠️ Chỉ admin mới dùng được lệnh này.")
        return
    if not args or args.count("|") != 2:
        await ctx.send("⚠️ Cú pháp: `!addcategory slug | Label hiển thị | từ khóa Pinterest`")
        return

    slug, label, keyword = [p.strip() for p in args.split("|")]
    slug = slug.lower()
    if not slug or " " in slug:
        await ctx.send("⚠️ Slug không hợp lệ (không dấu cách).")
        return
    if slug in CATEGORIES:
        await ctx.send(f"⚠️ `{slug}` là chủ đề có sẵn trong code, không thể ghi đè qua lệnh.")
        return

    await ctx.typing()
    await bot.loop.run_in_executor(None, db.add_custom_category, slug, label, keyword)
    _invalidate_categories_cache()
    extra = await _maybe_crawl_new_category_now(slug, keyword)
    await ctx.send(f"✅ Đã thêm chủ đề **{label}** (`{slug}`, từ khóa: `{keyword}`)." + extra)


@bot.tree.command(name="editcategory", description="[Admin] Sửa label/keyword của 1 chủ đề đã thêm qua lệnh")
@app_commands.describe(
    slug="Mã chủ đề cần sửa",
    label="Tên hiển thị mới (bỏ trống nếu giữ nguyên)",
    keyword="Từ khóa Pinterest mới (bỏ trống nếu giữ nguyên)",
)
async def editcategory_slash(interaction: discord.Interaction, slug: str, label: str = None, keyword: str = None):
    if not is_admin(interaction.user.id):
        await interaction.response.send_message("⚠️ Chỉ admin mới dùng được lệnh này.", ephemeral=True)
        return

    slug = slug.strip().lower()
    if slug in CATEGORIES:
        await interaction.response.send_message(
            f"⚠️ `{slug}` là chủ đề có sẵn trong code, không sửa được qua lệnh.", ephemeral=True
        )
        return
    if not label and not keyword:
        await interaction.response.send_message(
            "⚠️ Cần cung cấp ít nhất 1 trong 2: label hoặc keyword để sửa.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)
    ok = await bot.loop.run_in_executor(None, db.edit_custom_category, slug, label, keyword)
    _invalidate_categories_cache()
    if ok:
        await interaction.followup.send(f"✅ Đã cập nhật chủ đề `{slug}`.")
    else:
        await interaction.followup.send(f"❌ Không tìm thấy chủ đề `{slug}` (chưa từng thêm qua `/addcategory`).")


@bot.command(name="editcategory", help="[Admin] !editcategory slug | Label mới | keyword mới (để trống phần nào nếu giữ nguyên)")
async def editcategory_prefix(ctx, *, args: str = None):
    if not is_admin(ctx.author.id):
        await ctx.send("⚠️ Chỉ admin mới dùng được lệnh này.")
        return
    if not args or args.count("|") != 2:
        await ctx.send("⚠️ Cú pháp: `!editcategory slug | Label mới | keyword mới` (để trống phần nào nếu muốn giữ nguyên)")
        return

    slug, label, keyword = [p.strip() for p in args.split("|")]
    slug = slug.lower()
    if slug in CATEGORIES:
        await ctx.send(f"⚠️ `{slug}` là chủ đề có sẵn trong code, không sửa được qua lệnh.")
        return
    if not label and not keyword:
        await ctx.send("⚠️ Cần cung cấp ít nhất 1 trong 2: label hoặc keyword để sửa.")
        return

    await ctx.typing()
    ok = await bot.loop.run_in_executor(None, db.edit_custom_category, slug, label or None, keyword or None)
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

    await interaction.response.defer(ephemeral=True)
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

    all_cats = await get_all_categories_async()
    if chu_de not in all_cats:
        await interaction.response.send_message(f"❌ Chủ đề không hợp lệ: {chu_de}", ephemeral=True)
        return

    await interaction.response.defer()
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
    preview_url = await _fetch_next_image_url(category_key, info["keyword"], [])
    if not preview_url:
        return None, f"❌ Không lấy được ảnh mẫu cho chủ đề: **{info['label']}**"

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

    all_cats = await get_all_categories_async()
    info = all_cats.get(chu_de)
    if not info:
        await interaction.response.send_message(f"❌ Chủ đề không hợp lệ: {chu_de}", ephemeral=True)
        return

    target_channel = kenh or interaction.channel
    await interaction.response.defer(ephemeral=True)

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
    await ctx.typing()

    message, error = await _post_showcase_board(target_channel, category_key, info, ctx.author.id)
    if error:
        await ctx.send(error)
        return
    await ctx.send(f"✅ Đã đăng bảng giới thiệu **{info['label']}** vào {target_channel.mention}.")


# ============================================================
# Lệnh /favorites, !favorites — xem lại ảnh đã lưu qua nút "💾 Lưu ảnh"
# ============================================================

@bot.tree.command(name="favorites", description="Xem lại các ảnh bạn đã lưu")
async def favorites_slash(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    urls = await bot.loop.run_in_executor(None, db.get_favorites, interaction.user.id)
    if not urls:
        await interaction.followup.send(
            "📭 Bạn chưa lưu ảnh nào. Bấm nút 💾 Lưu ảnh khi xem ảnh (qua showcase board) để thêm vào đây.",
            ephemeral=True,
        )
        return

    async def send_func(embed, view):
        return await interaction.followup.send(embed=embed, view=view, wait=True)

    await _send_ephemeral_image_list(send_func, f"⭐ Ảnh đã lưu của bạn ({len(urls)} ảnh)", urls, interaction.user.id)


@bot.command(name="favorites", help="Xem lại các ảnh bạn đã lưu")
async def favorites_prefix(ctx):
    urls = await bot.loop.run_in_executor(None, db.get_favorites, ctx.author.id)
    if not urls:
        await ctx.send("📭 Bạn chưa lưu ảnh nào. Bấm nút 💾 Lưu ảnh khi xem ảnh (qua showcase board) để thêm vào đây.")
        return

    async def send_func(embed, view):
        return await ctx.send(embed=embed, view=view)

    await _send_ephemeral_image_list(send_func, f"⭐ Ảnh đã lưu của bạn ({len(urls)} ảnh)", urls, ctx.author.id)


TOKEN = os.getenv("DISCORD_TOKEN")

if __name__ == "__main__":
    if not TOKEN:
        logger.error("Chưa thiết lập biến môi trường DISCORD_TOKEN trên Render!")
    else:
        keep_alive()

        while True:
            try:
                bot.run(TOKEN)
                break
            except discord.errors.HTTPException as e:
                if e.status == 429:
                    logger.warning("Bị Discord Rate Limit 429. Đang chờ 60 giây để thử lại...")
                    time.sleep(60)
                else:
                    raise e
