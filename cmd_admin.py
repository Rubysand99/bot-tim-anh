"""
Lệnh admin /cleanup và /config.

(Tách ra từ bot.py cũ — xem README mục Kiến trúc.)
"""

from discord import app_commands
import concurrent.futures
import db
import discord
import random
from core import (
    ALLOWED_CHANNEL_IDS,
    ALLOWED_ROLE_IDS,
    _invalidate_guild_config_cache,
    bot,
    category_autocomplete,
    get_all_categories_async,
    is_admin,
)
from viewer import (
    _check_broken,
    _timed_defer,
)


# ============================================================
# Lệnh admin: /cleanup, !cleanup — dọn ảnh lỗi link (404...) hoặc bị gửi
# quá nhiều lần trong 1 category. Chỉ ADMIN_IDS dùng được.
# ============================================================

CLEANUP_OVERUSED_THRESHOLD = 20  # ảnh bị gửi >= N lần thì coi là "cũ", xoá bớt


CLEANUP_MAX_CHECK = 100          # tối đa số ảnh kiểm tra link mỗi lần chạy


def _cleanup_category(category_key: str) -> str:
    """Chạy đồng bộ (blocking) trong executor. Trả về báo cáo kết quả."""
    overused_removed = db.delete_overused_images(category_key, CLEANUP_OVERUSED_THRESHOLD)

    # Lấy MẪU NGẪU NHIÊN thay vì luôn 100 ảnh đầu tiên — nếu không, mỗi lần chạy
    # đều kiểm tra đúng cùng 1 nhóm ảnh và không bao giờ chạm tới phần còn lại.
    all_urls = db.get_all_image_urls(category_key)
    urls = random.sample(all_urls, min(len(all_urls), CLEANUP_MAX_CHECK))
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
# Lệnh admin: /config, !config — cấu hình giới hạn kênh/role RIÊNG cho
# từng server (guild). Nếu server chưa cấu hình gì qua đây, bot dùng
# ALLOWED_CHANNEL_IDS/ALLOWED_ROLE_IDS từ biến môi trường làm mặc định.
# ============================================================

def _format_guild_config(cfg: dict) -> str:
    if not cfg.get("configured"):
        # Chưa cấu hình riêng -> đang áp dụng mặc định toàn cục (biến môi trường)
        channels = ", ".join(f"<#{c}>" for c in sorted(ALLOWED_CHANNEL_IDS)) or "(không giới hạn — dùng được mọi kênh)"
        roles = ", ".join(f"<@&{r}>" for r in sorted(ALLOWED_ROLE_IDS)) or "(không giới hạn — ai cũng dùng được)"
        return (
            f"**Kênh cho phép:** {channels}\n**Role cho phép:** {roles}\n"
            f"_(Server chưa cấu hình riêng — đang dùng mặc định toàn cục của bot.)_"
        )
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
@app_commands.guild_only()
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
