import os
import re
import time
import requests
import discord
from discord import app_commands
from discord.ext import commands
from keep_alive import keep_alive
from pinterest_crawler import search_pinterest_images_with_retry

# Khởi tạo Bot với Prefix "!"
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    print(f"Bot đã đăng nhập thành công với tên: {bot.user}")
    try:
        synced = await bot.tree.sync()
        print(f"Đã đồng bộ {len(synced)} slash command(s).")
    except Exception as e:
        print(f"⚠️ Lỗi khi đồng bộ slash command: {e}")
    print("------------------------------------------")


# ============================================================
# Command cũ: !img (DuckDuckGo) — vẫn giữ để đối chiếu/test
# ============================================================

def get_duckduckgo_image(query: str):
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.5',
        'Referer': 'https://duckduckgo.com/'
    }

    session = requests.Session()
    session.headers.update(headers)

    res = session.get("https://duckduckgo.com/", params={'q': query}, timeout=10)
    vqd_match = re.search(r'vqd=([\d-]+)\&', res.text)
    if not vqd_match:
        vqd_match = re.search(r'vqd=["\']([\d-]+)["\']', res.text)

    if not vqd_match:
        raise Exception("Không thể lấy VQD Token từ DuckDuckGo")

    vqd = vqd_match.group(1)

    params = {
        'l': 'wt-wt',
        'o': 'json',
        'q': query,
        'vqd': vqd,
        'f': ',,,',
        'p': '1'
    }

    image_res = session.get("https://duckduckgo.com/i.js", params=params, timeout=10)
    data = image_res.json()

    results = data.get("results", [])
    if results:
        return results[0].get("image")
    return None


@bot.command(name="img", help="Tìm kiếm hình ảnh (DuckDuckGo)")
async def search_image(ctx, *, query: str):
    await ctx.typing()

    def fetch():
        for attempt in range(3):
            try:
                img_url = get_duckduckgo_image(query)
                if img_url:
                    return img_url
            except Exception as err:
                if attempt < 2:
                    time.sleep(2)
                else:
                    raise err
        return None

    try:
        img_url = await bot.loop.run_in_executor(None, fetch)

        if not img_url:
            await ctx.send(f"❌ Không tìm thấy hình ảnh cho: **{query}**")
            return

        embed = discord.Embed(
            title=f"🖼️ Kết quả hình ảnh: {query}",
            color=discord.Color.green()
        )
        embed.set_image(url=img_url)
        embed.set_footer(text="Nguồn: DuckDuckGo via Requests")
        await ctx.send(embed=embed)

    except Exception as e:
        await ctx.send(f"⚠️ Lỗi khi tải ảnh: `{e}`")


# ============================================================
# Command mới: /timanh (Pinterest) — slash command + nút chuyển ảnh
# ============================================================

class ImagePaginator(discord.ui.View):
    def __init__(self, query: str, images: list, author_id: int):
        super().__init__(timeout=180)
        self.query = query
        self.images = images
        self.author_id = author_id
        self.index = 0
        self._update_buttons()

    def _update_buttons(self):
        self.previous_button.disabled = self.index == 0
        self.next_button.disabled = self.index >= len(self.images) - 1

    def build_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title=f"🖼️ Kết quả tìm ảnh: {self.query}",
            description=f"Ảnh {self.index + 1}/{len(self.images)}",
            color=discord.Color.red(),
        )
        embed.set_image(url=self.images[self.index])
        embed.set_footer(text="Nguồn: Pinterest")
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
        self.index += 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


@bot.tree.command(name="timanh", description="Tìm ảnh trên Pinterest theo từ khóa")
@app_commands.describe(tu_khoa="Từ khóa cần tìm ảnh")
async def timanh(interaction: discord.Interaction, tu_khoa: str):
    await interaction.response.defer()

    def fetch():
        return search_pinterest_images_with_retry(tu_khoa, limit=20, retries=3)

    try:
        images = await bot.loop.run_in_executor(None, fetch)
    except Exception as e:
        await interaction.followup.send(f"⚠️ Lỗi khi tìm ảnh trên Pinterest: `{e}`")
        return

    if not images:
        await interaction.followup.send(f"❌ Không tìm thấy ảnh nào cho: **{tu_khoa}**")
        return

    view = ImagePaginator(tu_khoa, images, interaction.user.id)
    message = await interaction.followup.send(embed=view.build_embed(), view=view, wait=True)
    view.message = message


TOKEN = os.getenv("DISCORD_TOKEN")

if __name__ == "__main__":
    if not TOKEN:
        print("❌ LỖI: Chưa thiết lập biến môi trường DISCORD_TOKEN trên Render!")
    else:
        keep_alive()

        while True:
            try:
                bot.run(TOKEN)
                break
            except discord.errors.HTTPException as e:
                if e.status == 429:
                    print("⚠️ Bị Discord Rate Limit 429. Đang chờ 60 giây để thử lại...")
                    time.sleep(60)
                else:
                    raise e
