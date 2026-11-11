"""
Lệnh admin quản lý chủ đề (addcategory, editcategory, removecategory, migratecategories, mergecategory).

(Tách ra từ bot.py cũ — xem README mục Kiến trúc.)
"""

from categories import CATEGORIES, _LEGACY_STATIC_CATEGORIES, get_keywords, keywords_display
from discord import app_commands
import crawl_job
from datetime import datetime, timezone, timedelta
import db
import discord
from core import (
    _invalidate_categories_cache,
    bot,
    get_all_categories_async,
    is_admin,
    logger,
)
from viewer import (
    _timed_defer,
)


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


async def _do_migrate_categories() -> str:
    """
    Chuyển hết category từng cấu hình cứng trong categories.py (khối
    _LEGACY_STATIC_CATEGORIES) sang MongoDB dưới dạng category thêm qua
    lệnh (custom) — để KHÔNG còn category nào sống trong code nữa, mọi thứ
    quản lý qua /addcategory /editcategory /removecategory /mergecategory.
    An toàn khi chạy nhiều lần (idempotent): category nào đã có sẵn trong
    MongoDB rồi (trùng slug) thì bỏ qua, không ghi đè.
    """
    migrated = []
    skipped = []
    for slug, info in _LEGACY_STATIC_CATEGORIES.items():
        exists = await bot.loop.run_in_executor(None, db.custom_category_exists, slug)
        if exists:
            skipped.append(slug)
            continue
        await bot.loop.run_in_executor(
            None, db.add_custom_category, slug, info["label"], get_keywords(info), info.get("nsfw", False)
        )
        migrated.append(slug)
    _invalidate_categories_cache()

    lines = []
    if migrated:
        lines.append(f"✅ Đã chuyển {len(migrated)} chủ đề vào MongoDB: `{', '.join(migrated)}`")
    if skipped:
        lines.append(f"⏭️ Bỏ qua {len(skipped)} chủ đề (đã có sẵn trong MongoDB từ trước): `{', '.join(skipped)}`")
    if not migrated and not skipped:
        lines.append("Không có chủ đề nào trong _LEGACY_STATIC_CATEGORIES để chuyển.")
    if migrated or skipped:
        lines.append(
            "\nTừ giờ dùng `/editcategory` `/removecategory` `/mergecategory` được với TẤT CẢ chủ đề này rồi "
            "(trước đây các chủ đề có sẵn trong code không sửa/xoá/gộp được qua lệnh)."
        )
    return "\n".join(lines)


@bot.tree.command(name="migratecategories", description="[Admin] Chuyển category có sẵn trong code sang MongoDB (chỉ cần chạy 1 lần)")
async def migratecategories_slash(interaction: discord.Interaction):
    if not is_admin(interaction.user.id):
        await interaction.response.send_message("⚠️ Chỉ admin mới dùng được lệnh này.", ephemeral=True)
        return
    if not await _timed_defer(interaction, ephemeral=True):
        return
    await interaction.followup.send(await _do_migrate_categories())


@bot.command(name="migratecategories", help="[Admin] !migratecategories — chuyển category có sẵn trong code sang MongoDB (chỉ cần chạy 1 lần)")
async def migratecategories_prefix(ctx):
    if not is_admin(ctx.author.id):
        await ctx.send("⚠️ Chỉ admin mới dùng được lệnh này.")
        return
    await ctx.typing()
    await ctx.send(await _do_migrate_categories())


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
