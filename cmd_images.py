"""
Lệnh /img và /random.

(Tách ra từ bot.py cũ — xem README mục Kiến trúc.)
"""

from discord import app_commands
import db
import discord
from categories import CATEGORIES, _LEGACY_STATIC_CATEGORIES, get_keywords, keywords_display
from core import (
    _channel_allows_nsfw,
    bot,
    category_autocomplete,
    check_access,
    check_cooldown,
    get_all_categories_async,
    mark_used,
)
from viewer import (
    PAGINATOR_VIEW,
    _build_image_embed,
    _fetch_next_image_url,
    _maybe_schedule_random_prefetch,
    _next_crawl_eta_text,
    _pick_random_image,
    _send_image_result,
    _timed_defer,
)


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
