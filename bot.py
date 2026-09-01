import os
import re
import json
import time
import requests
import discord
from discord.ext import commands
from keep_alive import keep_alive

# Khởi tạo Bot với Prefix "!"
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print(f"Bot đã đăng nhập thành công với tên: {bot.user}")
    print("------------------------------------------")

# Hàm giả lập trình duyệt tìm ảnh DuckDuckGo qua requests
def get_duckduckgo_image(query: str):
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.5',
        'Referer': 'https://duckduckgo.com/'
    }

    session = requests.Session()
    session.headers.update(headers)

    # Bước 1: Lấy Token VQD từ DuckDuckGo
    res = session.get("https://duckduckgo.com/", params={'q': query}, timeout=10)
    vqd_match = re.search(r'vqd=([\d-]+)\&', res.text)
    if not vqd_match:
        # Thử regex dự phòng cho định dạng JS
        vqd_match = re.search(r'vqd=["\']([\d-]+)["\']', res.text)
    
    if not vqd_match:
        raise Exception("Không thể lấy VQD Token từ DuckDuckGo")

    vqd = vqd_match.group(1)

    # Bước 2: Gọi API tìm ảnh
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

# Command !img
@bot.command(name="img", help="Tìm kiếm hình ảnh")
async def search_image(ctx, *, query: str):
    await ctx.typing()

    def fetch():
        # Thử lại 3 lần nếu gặp sự cố kết nối
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
                    
