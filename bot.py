"""
Điểm vào của bot: heartbeat, self-test, sự kiện, dispatch nút bấm, chạy bot.

(Tách ra từ bot.py cũ — xem README mục Kiến trúc.)
"""

import asyncio
from datetime import datetime, timezone, timedelta
import db
import discord
from keep_alive import keep_alive
import os
import random
import sys
from discord.ext import commands, tasks
import time
from core import (
    LOG_CHANNEL_ID,
    _format_latency_ms,
    bot,
    get_all_categories_async,
    logger,
)
from viewer import (
    EPHEMERAL_PAGINATOR_VIEW,
    PAGINATOR_VIEW,
    SHOWCASE_VIEW,
    _build_image_embed,
    _do_epaginator_save,
    _do_report_image,
    _do_showcase_start,
    _paginator_navigate,
)

# Các module lệnh chỉ cần được IMPORT để tự đăng ký lệnh slash/prefix lên đối
# tượng `bot` dùng chung (core.bot) — không dùng tên nào trong chúng ở đây.
import cmd_admin  # noqa: F401,E402
import cmd_basic  # noqa: F401,E402
import cmd_categories  # noqa: F401,E402
import cmd_images  # noqa: F401,E402
import cmd_showcase  # noqa: F401,E402


_persistent_view_ready = False


_last_ping_message_id = None


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
    "paginator:report": lambda i: _do_report_image(i),
    "epaginator:report": lambda i: _do_report_image(i),
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


TOKEN = os.getenv("DISCORD_TOKEN")


if __name__ == "__main__":
    if not TOKEN:
        logger.error("Chưa thiết lập biến môi trường DISCORD_TOKEN trên Railway!")
    else:
        keep_alive()

        # LƯU Ý: KHÔNG tự retry bot.run(TOKEN) trong vòng lặp trên cùng 1
        # process — sau khi bot.run() dừng (kể cả do lỗi), discord.py đóng
        # session HTTP nội bộ của object `bot`, nên gọi lại bot.run() trên
        # cùng object sẽ luôn crash với "RuntimeError: Session is closed",
        # KHÔNG PHẢI retry thật. Cách đúng: chờ rồi thoát hẳn tiến trình,
        # để Railway tự khởi động lại process MỚI (bot object mới tinh).
        try:
            bot.run(TOKEN)
        except discord.errors.HTTPException as e:
            if e.status == 429:
                logger.warning(
                    "Bị Discord Rate Limit 429 khi đăng nhập. Chờ 5 phút rồi "
                    "thoát tiến trình để Railway tự khởi động lại (không retry "
                    "trong cùng process vì session HTTP đã bị đóng). Chờ lâu "
                    "hơn 60 giây để tránh dính 429 lặp lại liên tục nếu giới "
                    "hạn của Discord chưa được gỡ."
                )
                time.sleep(300)
                sys.exit(1)
            else:
                raise
