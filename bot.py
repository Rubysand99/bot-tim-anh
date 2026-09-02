import logging
import os
import random
import time
import discord
from discord import app_commands
from discord.ext import commands
from keep_alive import keep_alive
from pinterest_crawler import search_pinterest_images_with_retry
from categories import CATEGORIES
import db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("bot-tim-anh")

# Giảm bớt log rác từ discord.py (mặc định khá ồn ở mức INFO)
logging.getLogger("discord").setLevel(logging.WARNING)

# Khởi tạo Bot với Prefix "!"
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    logger.info(f"Bot đã đăng nhập thành công với tên: {bot.user}")
    try:
        synced = await bot.tree.sync()
        logger.info(f"Đã đồng bộ {len(synced)} slash command(s).")
    except Exception as e:
        logger.warning(f"Lỗi khi đồng bộ slash command: {e}")
    logger.info("------------------------------------------")


# ============================================================
# Lệnh ping — kiểm tra độ trễ của bot
# ============================================================

@bot.tree.command(name="ping", description="Kiểm tra độ trễ của bot")
async def ping_slash(interaction: discord.Interaction):
    latency_ms = round(bot.latency * 1000)
    await interaction.response.send_message(f"🏓 Pong! Độ trễ: **{latency_ms}ms**")


@bot.command(name="ping", help="Kiểm tra độ trễ của bot")
async def ping_prefix(ctx):
    latency_ms = round(bot.latency * 1000)
    await ctx.send(f"🏓 Pong! Độ trễ: **{latency_ms}ms**")


# ============================================================
# Lệnh /img và !img — lấy ảnh theo chủ đề từ MongoDB (đã crawl sẵn),
# tự động fallback cào Pinterest trực tiếp nếu DB hết ảnh khả dụng.
# ============================================================

def _category_choices():
    return [
        app_commands.Choice(name=info["label"], value=key)
        for key, info in CATEGORIES.items()
    ]


async def _fetch_next_image_url(category_key: str, exclude_urls: list):
    """
    Lấy 1 URL ảnh mới cho category:
    1) Ưu tiên đọc ảnh đã crawl sẵn trong MongoDB (nhanh, không tốn quota Pinterest).
    2) Nếu DB hết ảnh khả dụng hoặc lỗi kết nối -> fallback cào trực tiếp Pinterest.
    Trả về None nếu cả 2 cách đều không có ảnh.
    """
    info = CATEGORIES.get(category_key)
    if not info:
        return None

    def fetch_db():
        try:
            doc = db.get_next_image(category_key, exclude_urls)
            return doc["image_url"] if doc else None
        except Exception as e:
            logger.warning(f"Lỗi đọc MongoDB, sẽ fallback sang cào trực tiếp: {e}")
            return None

    url = await bot.loop.run_in_executor(None, fetch_db)
    if url:
        return url

    def fetch_crawl():
        try:
            results = search_pinterest_images_with_retry(info["keyword"], limit=20, retries=3)
        except Exception as e:
            logger.warning(f"Lỗi fallback cào Pinterest: {e}")
            return None
        for u in results:
            if u not in exclude_urls:
                return u
        return None

    return await bot.loop.run_in_executor(None, fetch_crawl)


class CategoryImagePaginator(discord.ui.View):
    def __init__(self, category_key: str, first_url: str, author_id: int):
        super().__init__(timeout=180)
        self.category_key = category_key
        self.label = CATEGORIES[category_key]["label"]
        self.author_id = author_id
        self.images = [first_url]
        self.index = 0
        self.message = None
        self._update_buttons()

    def _update_buttons(self):
        self.previous_button.disabled = self.index == 0

    def build_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title=f"🖼️ {self.label}",
            description=f"Ảnh {self.index + 1}/{len(self.images)}",
            color=discord.Color.red(),
        )
        embed.set_image(url=self.images[self.index])
        embed.set_footer(text="Nguồn: kho ảnh đã crawl · fallback Pinterest")
        return embed

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "⚠️ Bạn không thể điều khiển kết quả tìm kiếm của người khác.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="◀ Trước", style=discord.ButtonStyle.secondary)
    async def previous_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index -= 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="Sau ▶", style=discord.ButtonStyle.secondary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Còn ảnh đã tải sẵn trong bộ nhớ đệm -> chuyển luôn, không cần fetch mới
        if self.index < len(self.images) - 1:
            self.index += 1
            self._update_buttons()
            await interaction.response.edit_message(embed=self.build_embed(), view=self)
            return

        # Hết ảnh đệm -> lấy ảnh mới (DB hoặc fallback Pinterest), có thể mất vài giây
        await interaction.response.defer()
        new_url = await _fetch_next_image_url(self.category_key, self.images)
        if not new_url:
            await interaction.followup.send(
                "❌ Hết ảnh khả dụng cho chủ đề này rồi, thử lại sau nhé.", ephemeral=True
            )
            return

        self.images.append(new_url)
        self.index += 1
        self._update_buttons()
        await interaction.edit_original_response(embed=self.build_embed(), view=self)

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


@bot.tree.command(name="img", description="Lấy ảnh theo chủ đề (đã crawl sẵn từ Pinterest)")
@app_commands.describe(chu_de="Chọn chủ đề ảnh")
@app_commands.choices(chu_de=_category_choices())
async def img_slash(interaction: discord.Interaction, chu_de: app_commands.Choice[str]):
    await interaction.response.defer()

    url = await _fetch_next_image_url(chu_de.value, [])
    if not url:
        await interaction.followup.send(f"❌ Không tìm thấy ảnh nào cho chủ đề: **{chu_de.name}**")
        return

    view = CategoryImagePaginator(chu_de.value, url, interaction.user.id)
    message = await interaction.followup.send(embed=view.build_embed(), view=view, wait=True)
    view.message = message


@bot.command(name="img", help="Lấy ảnh theo chủ đề. Vd: !img meo")
async def img_prefix(ctx, chu_de: str = None):
    if not chu_de or chu_de.lower() not in CATEGORIES:
        options_text = "\n".join(f"`{k}` — {v['label']}" for k, v in CATEGORIES.items())
        await ctx.send(f"⚠️ Vui lòng chọn 1 chủ đề hợp lệ:\n{options_text}")
        return

    category_key = chu_de.lower()
    await ctx.typing()

    url = await _fetch_next_image_url(category_key, [])
    if not url:
        await ctx.send(f"❌ Không tìm thấy ảnh nào cho chủ đề: **{CATEGORIES[category_key]['label']}**")
        return

    view = CategoryImagePaginator(category_key, url, ctx.author.id)
    message = await ctx.send(embed=view.build_embed(), view=view)
    view.message = message


# ============================================================
# Lệnh /random và !random — chọn ngẫu nhiên 1 chủ đề rồi lấy ảnh
# ============================================================

@bot.tree.command(name="random", description="Lấy ảnh từ một chủ đề bất kỳ (ngẫu nhiên)")
async def random_slash(interaction: discord.Interaction):
    await interaction.response.defer()

    category_key = random.choice(list(CATEGORIES.keys()))
    url = await _fetch_next_image_url(category_key, [])
    if not url:
        await interaction.followup.send(
            f"❌ Không tìm thấy ảnh nào cho chủ đề ngẫu nhiên: **{CATEGORIES[category_key]['label']}**"
        )
        return

    view = CategoryImagePaginator(category_key, url, interaction.user.id)
    message = await interaction.followup.send(embed=view.build_embed(), view=view, wait=True)
    view.message = message


@bot.command(name="random", help="Lấy ảnh từ một chủ đề bất kỳ (ngẫu nhiên)")
async def random_prefix(ctx):
    await ctx.typing()

    category_key = random.choice(list(CATEGORIES.keys()))
    url = await _fetch_next_image_url(category_key, [])
    if not url:
        await ctx.send(
            f"❌ Không tìm thấy ảnh nào cho chủ đề ngẫu nhiên: **{CATEGORIES[category_key]['label']}**"
        )
        return

    view = CategoryImagePaginator(category_key, url, ctx.author.id)
    message = await ctx.send(embed=view.build_embed(), view=view)
    view.message = message


# ============================================================
# Lệnh /stats và !stats — đếm số ảnh còn trong kho (MongoDB) theo chủ đề
# ============================================================

async def _build_stats_embed() -> discord.Embed:
    def fetch_counts():
        counts = {}
        for key in CATEGORIES:
            try:
                counts[key] = db.count_images(key)
            except Exception as e:
                logger.warning(f"Lỗi đếm ảnh category '{key}': {e}")
                counts[key] = None
        return counts

    counts = await bot.loop.run_in_executor(None, fetch_counts)

    embed = discord.Embed(
        title="📊 Kho ảnh theo chủ đề",
        color=discord.Color.blurple(),
    )
    total = 0
    for key, info in CATEGORIES.items():
        n = counts.get(key)
        if n is None:
            embed.add_field(name=info["label"], value="⚠️ lỗi đọc DB", inline=True)
        else:
            embed.add_field(name=info["label"], value=f"{n} ảnh", inline=True)
            total += n
    embed.set_footer(text=f"Tổng cộng: {total} ảnh trong kho")
    return embed


@bot.tree.command(name="stats", description="Xem số lượng ảnh còn trong kho theo từng chủ đề")
async def stats_slash(interaction: discord.Interaction):
    await interaction.response.defer()
    embed = await _build_stats_embed()
    await interaction.followup.send(embed=embed)


@bot.command(name="stats", help="Xem số lượng ảnh còn trong kho theo từng chủ đề")
async def stats_prefix(ctx):
    await ctx.typing()
    embed = await _build_stats_embed()
    await ctx.send(embed=embed)


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
