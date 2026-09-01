import os
import time
import discord
from discord.ext import commands
from duckduckgo_search import DDGS
from keep_alive import keep_alive

# Khởi tạo Bot với Prefix "!"
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print(f"Bot đã đăng nhập thành công với tên: {bot.user}")
    print("------------------------------------------")

# Command 1: Tìm kiếm web (!search <từ khóa>)
@bot.command(name="search", help="Tìm kiếm thông tin trên Web")
async def search_web(ctx, *, query: str):
    await ctx.typing()

    def fetch_results():
        for attempt in range(3):
            try:
                with DDGS(timeout=20) as ddgs:
                    return list(ddgs.text(query, max_results=3))
            except Exception as err:
                if attempt < 2:
                    time.sleep(2)
                else:
                    raise err
        return []

    try:
        results = await bot.loop.run_in_executor(None, fetch_results)

        if not results:
            await ctx.send(f"❌ Không tìm thấy kết quả nào cho: **{query}**")
            return

        embed = discord.Embed(
            title=f"🔍 Kết quả tìm kiếm: {query}",
            color=discord.Color.blue()
        )
        for idx, item in enumerate(results, 1):
            title = item.get("title", "Không có tiêu đề")
            href = item.get("href", "#")
            body = item.get("body", "Không có mô tả")
            embed.add_field(
                name=f"{idx}. {title}",
                value=f"{body[:150]}...\n🔗 [Xem chi tiết]({href})",
                inline=False
            )
        await ctx.send(embed=embed)

    except Exception as e:
        await ctx.send(f"⚠️ Có lỗi xảy ra khi tìm kiếm: `{e}`")

# Command 2: Tìm kiếm hình ảnh (!img <từ khóa>)
@bot.command(name="img", help="Tìm kiếm hình ảnh")
async def search_image(ctx, *, query: str):
    await ctx.typing()

    def fetch_image():
        for attempt in range(3):
            try:
                with DDGS(timeout=20) as ddgs:
                    results = list(ddgs.images(query, max_results=1))
                    if results:
                        return results[0]
            except Exception as err:
                if attempt < 2:
                    time.sleep(2)
                else:
                    raise err
        return None

    try:
        img_data = await bot.loop.run_in_executor(None, fetch_image)

        if not img_data:
            await ctx.send(f"❌ Không tìm thấy hình ảnh cho: **{query}**")
            return

        embed = discord.Embed(
            title=f"🖼️ Kết quả hình ảnh: {query}",
            color=discord.Color.green()
        )
        embed.set_image(url=img_data["image"])
        embed.set_footer(text=f"Nguồn: {img_data.get('title', 'DuckDuckGo')}")
        await ctx.send(embed=embed)

    except Exception as e:
        await ctx.send(f"⚠️ Không thể tải ảnh do kết nối mạng chập chờn: `{e}`")

TOKEN = os.getenv("DISCORD_TOKEN")

if __name__ == "__main__":
    if not TOKEN:
        print("❌ LỖI: Chưa thiết lập biến môi trường DISCORD_TOKEN trên Render!")
    else:
        # Khởi chạy server Flask ngầm giữ web service sống
        keep_alive()

        # Vòng lặp chống sập khi dính Rate Limit 429 từ Discord
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
                    
