"""
Lấy ảnh, phân trang (nút Trước/Sau/Báo lỗi/Lưu ảnh), prefetch, showcase view.

(Tách ra từ bot.py cũ — xem README mục Kiến trúc.)
"""

from datetime import datetime, timezone, timedelta
import db
import discord
from categories import CATEGORIES, _LEGACY_STATIC_CATEGORIES, get_keywords, keywords_display
import random
import requests
import time
from core import (
    _channel_allows_nsfw,
    bot,
    check_access,
    check_cooldown,
    get_all_categories_async,
    logger,
    mark_used,
)


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


async def _fetch_next_image_url(category_key: str, exclude_urls: list, reserve: bool = False):
    """
    reserve=True: chỉ GIỮ CHỖ ảnh (dùng cho prefetch, xem db.RESERVE_MINUTES) —
    ảnh chưa được coi là đã gửi cho tới khi người dùng thật sự bấm "Sau" tới nó.

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
            doc = db.get_next_image(category_key, exclude_urls, reserve=reserve)
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
            new_url = await _fetch_next_image_url(category_key, exclude_urls, reserve=True)
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
        # Ảnh đệm này mới chỉ được GIỮ CHỖ lúc tải trước — giờ người dùng thật sự
        # xem nó nên mới tính là "đã gửi" (idempotent, bấm qua lại không tăng thêm).
        await bot.loop.run_in_executor(None, db.confirm_image_sent, images[index])
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
        await bot.loop.run_in_executor(None, db.confirm_image_sent, item["url"])
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

    @discord.ui.button(label="🚫 Báo lỗi", style=discord.ButtonStyle.danger, custom_id="paginator:report")
    async def report_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _do_report_image(interaction)


PAGINATOR_VIEW = PersistentImagePaginator()


# Số người KHÁC NHAU cùng báo 1 ảnh lỗi thì ảnh bị xoá khỏi kho. Riêng ảnh mà
# bot tự kiểm tra thấy chắc chắn đã chết (HTTP 404/410) thì xoá ngay từ lần báo
# đầu — ngưỡng này chỉ để chống 1 người cố tình báo bừa ảnh còn dùng tốt.
REPORT_DELETE_THRESHOLD = 3


async def _do_report_image(interaction: discord.Interaction):
    """Logic nút "🚫 Báo lỗi": gỡ ảnh đang xem khỏi phiên, ghi nhận báo lỗi (hoặc
    xoá khỏi kho nếu chắc chắn hỏng / đủ người báo), rồi chuyển sang ảnh khác."""
    if not await _timed_defer(interaction):
        return

    message_id = str(interaction.message.id)
    session = await bot.loop.run_in_executor(None, db.get_paginator_session, message_id)
    if not session:
        await interaction.followup.send("⚠️ Không tìm thấy dữ liệu phiên xem ảnh này nữa.", ephemeral=True)
        return
    if interaction.user.id != session["author_id"]:
        await interaction.followup.send("⚠️ Bạn không thể báo lỗi ảnh trong phiên của người khác.", ephemeral=True)
        return

    is_random = session.get("mode") == "random"
    entries = session["items"] if is_random else session["images"]
    index = session["index"]
    if index >= len(entries):
        await interaction.followup.send("⚠️ Không xác định được ảnh đang xem.", ephemeral=True)
        return
    url = entries[index]["url"] if is_random else entries[index]

    # 1) Xoá khỏi kho nếu CHẮC CHẮN đã chết (404/410), nếu không thì chỉ ghi nhận
    #    báo lỗi và xoá khi đủ REPORT_DELETE_THRESHOLD người khác nhau cùng báo.
    deleted = False
    reports = 0
    if await bot.loop.run_in_executor(None, _check_broken, url):
        await bot.loop.run_in_executor(None, db.delete_images_by_url, [url])
        deleted = True
    else:
        reports = await bot.loop.run_in_executor(None, db.report_image, url, interaction.user.id)
        if reports >= REPORT_DELETE_THRESHOLD:
            await bot.loop.run_in_executor(None, db.delete_images_by_url, [url])
            deleted = True

    # 2) Gỡ khỏi phiên của người này (dù có xoá khỏi kho hay không, họ không
    #    muốn xem lại ảnh này nữa).
    await bot.loop.run_in_executor(None, db.remove_url_from_session, message_id, url)
    remaining = [e for e in entries if (e["url"] if is_random else e) != url]

    # 3) Chuyển sang ảnh khác: ưu tiên ảnh còn trong phiên, hết thì bốc ảnh mới.
    view = EPHEMERAL_PAGINATOR_VIEW if str(interaction.data.get("custom_id", "")).startswith("epaginator") else PAGINATOR_VIEW
    shown = False
    if remaining:
        new_index = min(index, len(remaining) - 1)
        nxt = remaining[new_index]
        label, nxt_url = (nxt["label"], nxt["url"]) if is_random else (session["label"], nxt)
        await interaction.edit_original_response(embed=_build_image_embed(label, nxt_url), view=view)
        await bot.loop.run_in_executor(None, db.update_paginator_index, message_id, new_index)
        await bot.loop.run_in_executor(None, db.confirm_image_sent, nxt_url)
        shown = True
    elif is_random:
        all_cats = await get_all_categories_async()
        result = await _pick_random_image(all_cats, session.get("allowed_keys") or [], [url])
        if result:
            category_key, label, new_url = result
            new_item = {"url": new_url, "category_key": category_key, "label": label}
            await interaction.edit_original_response(embed=_build_image_embed(label, new_url), view=view)
            await bot.loop.run_in_executor(None, db.append_random_item_and_set_index, message_id, new_item, 0)
            shown = True
    else:
        new_url = await _fetch_next_image_url(session["category_key"], [url])
        if new_url:
            await interaction.edit_original_response(embed=_build_image_embed(session["label"], new_url), view=view)
            await bot.loop.run_in_executor(None, db.append_image_and_set_index, message_id, new_url, 0)
            shown = True

    note = "🚫 Đã xoá ảnh lỗi khỏi kho." if deleted else f"🚫 Đã ghi nhận ảnh lỗi ({reports}/{REPORT_DELETE_THRESHOLD} lượt báo trước khi xoá)."
    if not shown:
        note += " Hiện hết ảnh khả dụng để thay thế — ảnh cũ vẫn hiển thị trên tin nhắn này."
    await interaction.followup.send(note, ephemeral=True)


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

    @discord.ui.button(label="🚫 Báo lỗi", style=discord.ButtonStyle.danger, custom_id="epaginator:report")
    async def report_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _do_report_image(interaction)


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
# Lệnh /random và !random — lấy 1 ảnh NGẪU NHIÊN TRÊN TOÀN BỘ KHO
# (không phải random category rồi mới chọn ảnh trong đó).
# ============================================================

async def _pick_random_image(all_cats: dict, allowed_keys: list, exclude_urls: list, reserve: bool = False):
    """
    reserve=True: chỉ giữ chỗ ảnh (prefetch) — xem db.RESERVE_MINUTES.
    Lõi chọn 1 ảnh random dùng CHUNG cho: lệnh /random lần đầu, mỗi lần bấm
    "Sau" trong phiên /random (hết ảnh đệm), và tác vụ tải trước (prefetch)
    của phiên /random. Trả về (category_key, label, url) hoặc None nếu hết
    ảnh khả dụng trong phạm vi allowed_keys.
    """
    if not allowed_keys:
        return None
    doc = await bot.loop.run_in_executor(None, db.get_random_image, exclude_urls, allowed_keys, reserve)
    if doc:
        category_key = doc["category"]
        info = all_cats.get(category_key, {"label": category_key, "keywords": [category_key]})
        return category_key, info["label"], doc["image_url"]

    # DB trống hoàn toàn ảnh khả dụng (trong phạm vi allowed_keys) -> thử
    # random 1 chủ đề khác (vẫn trong allowed_keys) qua _fetch_next_image_url
    # (chỉ đọc DB, không cào Pinterest trực tiếp nữa — xem ghi chú đầu file).
    category_key = random.choice(allowed_keys)
    info = all_cats[category_key]
    url = await _fetch_next_image_url(category_key, exclude_urls, reserve=reserve)
    if not url:
        return None
    return category_key, info["label"], url


def _schedule_random_prefetch(message_id: str, all_cats: dict, allowed_keys: list, exclude_urls: list) -> None:
    """Bản dành cho phiên /random của _schedule_prefetch — mỗi ảnh tải trước
    có thể thuộc 1 category khác với ảnh hiện tại, nên lưu kèm cả
    category_key/label riêng cho từng ảnh (xem save_random_paginator_session)."""
    async def _do_prefetch():
        try:
            result = await _pick_random_image(all_cats, allowed_keys, exclude_urls, reserve=True)
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


def _check_broken(url: str):
    """Trả về url nếu ảnh CHẮC CHẮN đã hỏng (HTTP 404/410), ngược lại None.

    Chỉ 404/410 mới là bằng chứng link đã chết. Lỗi mạng, timeout, 429 (bị giới
    hạn tốc độ), 403/405 (CDN không cho HEAD)... đều KHÔNG chứng minh ảnh hỏng —
    trước đây mọi trường hợp đó đều bị coi là hỏng và xoá nhầm ảnh còn dùng tốt."""
    try:
        resp = requests.head(url, timeout=4, allow_redirects=True)
    except Exception:
        return None
    return url if resp.status_code in (404, 410) else None
