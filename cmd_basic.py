"""
Lệnh /ping, /version, /stats.

(Tách ra từ bot.py cũ — xem README mục Kiến trúc.)
"""

from categories import CATEGORIES, _LEGACY_STATIC_CATEGORIES, get_keywords, keywords_display
from version import __version__, __description__
import db
import discord
from core import (
    _format_latency_ms,
    _relative_time_vi,
    bot,
    get_all_categories_async,
    logger,
)
from viewer import (
    _timed_defer,
)


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
# Lệnh /stats và !stats — thống kê chi tiết kho ảnh theo chủ đề
# ============================================================

async def _build_stats_embed() -> discord.Embed:
    all_cats = await get_all_categories_async()

    empty_stats = {"total": 0, "available": 0, "avg_sent_count": 0, "max_sent_count": 0,
                   "oldest_created_at": None, "newest_created_at": None}

    def fetch():
        # 1 aggregation duy nhất cho TẤT CẢ chủ đề (trước đây 3 truy vấn / chủ đề).
        try:
            grouped = db.get_all_category_stats()
        except Exception as e:
            logger.warning(f"Lỗi lấy stats kho ảnh: {e}")
            grouped = None
        # Chủ đề chưa có ảnh nào không xuất hiện trong kết quả -> coi là 0 ảnh.
        stats = {key: (None if grouped is None else grouped.get(key, dict(empty_stats))) for key in all_cats}
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
