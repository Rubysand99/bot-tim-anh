import concurrent.futures
import logging
import os
import random
import time
import discord
import requests
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


def _parse_id_set(env_name: str) -> set:
    raw = os.getenv(env_name, "")
    return {int(x) for x in raw.split(",") if x.strip().isdigit()}


# ============================================================
# Cấu hình quyền hạn / giới hạn dùng lệnh
# ============================================================

# Admin: bỏ qua mọi giới hạn (cooldown, kênh, role) + dùng được lệnh quản trị.
# Có thể thêm nhiều admin qua biến môi trường ADMIN_USER_IDS="id1,id2,..."
# trên Render, không cần sửa code. Mặc định luôn có ID dưới đây.
ADMIN_IDS = _parse_id_set("ADMIN_USER_IDS") or {846332174734983219}

# Nếu set (comma-separated ID trên Render), lệnh ảnh chỉ dùng được ở các kênh này.
# Để trống (mặc định) = không giới hạn kênh.
ALLOWED_CHANNEL_IDS = _parse_id_set("ALLOWED_CHANNEL_IDS")

# Nếu set, chỉ member có 1 trong các role này mới dùng được lệnh ảnh.
# Để trống (mặc định) = không giới hạn role.
ALLOWED_ROLE_IDS = _parse_id_set("ALLOWED_ROLE_IDS")

COOLDOWN_SECONDS = 8
_last_used_at = {}


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def check_access(user_id: int, channel_id: int, roles) -> str:
    """Trả về thông báo lỗi nếu bị chặn, chuỗi rỗng nếu được phép dùng lệnh."""
    if is_admin(user_id):
        return ""
    if ALLOWED_CHANNEL_IDS and channel_id not in ALLOWED_CHANNEL_IDS:
        return "⚠️ Lệnh này chỉ dùng được ở kênh được chỉ định."
    if ALLOWED_ROLE_IDS:
        role_ids = {r.id for r in roles} if roles else set()
        if not (role_ids & ALLOWED_ROLE_IDS):
            return "⚠️ Bạn không có quyền dùng lệnh này."
    return ""


def check_cooldown(user_id: int) -> float:
    """Trả về số giây còn phải chờ (0 nếu được dùng ngay). Admin luôn = 0."""
    if is_admin(user_id):
        return 0
    last = _last_used_at.get(user_id, 0)
    elapsed = time.time() - last
    if elapsed < COOLDOWN_SECONDS:
        return round(COOLDOWN_SECONDS - elapsed, 1)
    return 0


def mark_used(user_id: int) -> None:
    _last_used_at[user_id] = time.time()


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
# Chủ đề (category): gộp CATEGORIES tĩnh (categories.py) với category
# admin thêm qua Discord (lưu trong MongoDB) — luôn đọc mới mỗi lần dùng
# để /addcategory có hiệu lực ngay, không cần restart bot.
# ============================================================

async def get_all_categories_async() -> dict:
    def fetch():
        merged = dict(CATEGORIES)
        try:
            merged.update(db.get_custom_categories())
        except Exception as e:
            logger.warning(f"Không đọc được custom categories từ DB: {e}")
        return merged
    return await bot.loop.run_in_executor(None, fetch)


async def category_autocomplete(interaction: discord.Interaction, current: str):
    all_cats = await get_all_categories_async()
    current_lower = current.lower()
    matches = [
        app_commands.Choice(name=info["label"], value=key)
        for key, info in all_cats.items()
        if current_lower in key.lower() or current_lower in info["label"].lower()
    ]
    return matches[:25]


# ============================================================
# Lệnh /img và !img — lấy ảnh theo chủ đề từ MongoDB (đã crawl sẵn),
# tự động fallback cào Pinterest trực tiếp nếu DB hết ảnh khả dụng.
# ============================================================

async def _fetch_next_image_url(category_key: str, keyword: str, exclude_urls: list):
    """
    Lấy 1 URL ảnh mới cho category:
    1) Ưu tiên đọc ảnh đã crawl sẵn trong MongoDB (nhanh, không tốn quota Pinterest).
    2) Nếu DB hết ảnh khả dụng hoặc lỗi kết nối -> fallback cào trực tiếp Pinterest.
    Trả về None nếu cả 2 cách đều không có ảnh.
    """
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
            results = search_pinterest_images_with_retry(keyword, limit=20, retries=3)
        except Exception as e:
            logger.warning(f"Lỗi fallback cào Pinterest: {e}")
            return None
        for u in results:
            if u not in exclude_urls:
                return u
        return None

    return await bot.loop.run_in_executor(None, fetch_crawl)


class CategoryImagePaginator(discord.ui.View):
    def __init__(self, category_key: str, label: str, keyword: str, first_url: str, author_id: int):
        super().__init__(timeout=180)
        self.category_key = category_key
        self.label = label
        self.keyword = keyword
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
        new_url = await _fetch_next_image_url(self.category_key, self.keyword, self.images)
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
@app_commands.describe(chu_de="Chọn chủ đề ảnh (gõ để tìm)")
@app_commands.autocomplete(chu_de=category_autocomplete)
async def img_slash(interaction: discord.Interaction, chu_de: str):
    roles = getattr(interaction.user, "roles", None)
    access_err = check_access(interaction.user.id, interaction.channel_id, roles)
    if access_err:
        await interaction.response.send_message(access_err, ephemeral=True)
        return
    wait = check_cooldown(interaction.user.id)
    if wait:
        await interaction.response.send_message(f"⏳ Chờ thêm {wait}s rồi thử lại nhé.", ephemeral=True)
        return
    mark_used(interaction.user.id)

    await interaction.response.defer()

    all_cats = await get_all_categories_async()
    info = all_cats.get(chu_de)
    if not info:
        await interaction.followup.send(f"❌ Chủ đề không hợp lệ: **{chu_de}**")
        return

    url = await _fetch_next_image_url(chu_de, info["keyword"], [])
    if not url:
        await interaction.followup.send(f"❌ Không tìm thấy ảnh nào cho chủ đề: **{info['label']}**")
        return

    view = CategoryImagePaginator(chu_de, info["label"], info["keyword"], url, interaction.user.id)
    message = await interaction.followup.send(embed=view.build_embed(), view=view, wait=True)
    view.message = message


@bot.command(name="img", help="Lấy ảnh theo chủ đề. Vd: !img meo")
async def img_prefix(ctx, chu_de: str = None):
    roles = getattr(ctx.author, "roles", None)
    access_err = check_access(ctx.author.id, ctx.channel.id, roles)
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
    await ctx.typing()

    url = await _fetch_next_image_url(category_key, info["keyword"], [])
    if not url:
        await ctx.send(f"❌ Không tìm thấy ảnh nào cho chủ đề: **{info['label']}**")
        return

    view = CategoryImagePaginator(category_key, info["label"], info["keyword"], url, ctx.author.id)
    message = await ctx.send(embed=view.build_embed(), view=view)
    view.message = message


# ============================================================
# Lệnh /random và !random — chọn ngẫu nhiên 1 chủ đề rồi lấy ảnh
# ============================================================

@bot.tree.command(name="random", description="Lấy ảnh từ một chủ đề bất kỳ (ngẫu nhiên)")
async def random_slash(interaction: discord.Interaction):
    roles = getattr(interaction.user, "roles", None)
    access_err = check_access(interaction.user.id, interaction.channel_id, roles)
    if access_err:
        await interaction.response.send_message(access_err, ephemeral=True)
        return
    wait = check_cooldown(interaction.user.id)
    if wait:
        await interaction.response.send_message(f"⏳ Chờ thêm {wait}s rồi thử lại nhé.", ephemeral=True)
        return
    mark_used(interaction.user.id)

    await interaction.response.defer()

    all_cats = await get_all_categories_async()
    category_key = random.choice(list(all_cats.keys()))
    info = all_cats[category_key]
    url = await _fetch_next_image_url(category_key, info["keyword"], [])
    if not url:
        await interaction.followup.send(f"❌ Không tìm thấy ảnh nào cho chủ đề ngẫu nhiên: **{info['label']}**")
        return

    view = CategoryImagePaginator(category_key, info["label"], info["keyword"], url, interaction.user.id)
    message = await interaction.followup.send(embed=view.build_embed(), view=view, wait=True)
    view.message = message


@bot.command(name="random", help="Lấy ảnh từ một chủ đề bất kỳ (ngẫu nhiên)")
async def random_prefix(ctx):
    roles = getattr(ctx.author, "roles", None)
    access_err = check_access(ctx.author.id, ctx.channel.id, roles)
    if access_err:
        await ctx.send(access_err)
        return
    wait = check_cooldown(ctx.author.id)
    if wait:
        await ctx.send(f"⏳ Chờ thêm {wait}s rồi thử lại nhé.")
        return
    mark_used(ctx.author.id)

    await ctx.typing()

    all_cats = await get_all_categories_async()
    category_key = random.choice(list(all_cats.keys()))
    info = all_cats[category_key]
    url = await _fetch_next_image_url(category_key, info["keyword"], [])
    if not url:
        await ctx.send(f"❌ Không tìm thấy ảnh nào cho chủ đề ngẫu nhiên: **{info['label']}**")
        return

    view = CategoryImagePaginator(category_key, info["label"], info["keyword"], url, ctx.author.id)
    message = await ctx.send(embed=view.build_embed(), view=view)
    view.message = message


# ============================================================
# Lệnh /stats và !stats — đếm số ảnh còn trong kho (MongoDB) theo chủ đề
# ============================================================

async def _build_stats_embed() -> discord.Embed:
    all_cats = await get_all_categories_async()

    def fetch_counts():
        counts = {}
        for key in all_cats:
            try:
                counts[key] = (db.count_images(key), db.count_available_images(key))
            except Exception as e:
                logger.warning(f"Lỗi đếm ảnh category '{key}': {e}")
                counts[key] = None
        return counts

    counts = await bot.loop.run_in_executor(None, fetch_counts)

    embed = discord.Embed(
        title="📊 Kho ảnh theo chủ đề",
        description="Tổng ảnh / ảnh khả dụng ngay bây giờ (đã qua cooldown)",
        color=discord.Color.blurple(),
    )
    total = 0
    for key, info in all_cats.items():
        n = counts.get(key)
        if n is None:
            embed.add_field(name=info["label"], value="⚠️ lỗi đọc DB", inline=True)
        else:
            total_n, available_n = n
            embed.add_field(name=info["label"], value=f"{total_n} / {available_n} khả dụng", inline=True)
            total += total_n
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


# ============================================================
# Lệnh admin: /addcategory, /removecategory — quản lý chủ đề qua Discord
# thay vì sửa categories.py + deploy lại. Chỉ ADMIN_IDS dùng được.
# Category thêm qua đây lưu trong MongoDB (custom_categories), có hiệu lực
# ngay lập tức (autocomplete /img đọc lại DB mỗi lần gõ).
# ============================================================

@bot.tree.command(name="addcategory", description="[Admin] Thêm chủ đề ảnh mới")
@app_commands.describe(
    slug="Mã chủ đề, không dấu/không khoảng trắng (vd: hoahong)",
    label="Tên hiển thị trong Discord",
    keyword="Từ khóa tìm kiếm trên Pinterest",
)
async def addcategory_slash(interaction: discord.Interaction, slug: str, label: str, keyword: str):
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

    await interaction.response.defer(ephemeral=True)
    await bot.loop.run_in_executor(None, db.add_custom_category, slug, label, keyword)
    await interaction.followup.send(
        f"✅ Đã thêm chủ đề **{label}** (`{slug}`, từ khóa: `{keyword}`). "
        f"Dùng ngay được với `/img` (gõ để autocomplete) hoặc `!img {slug}`."
    )


@bot.command(name="addcategory", help="[Admin] !addcategory slug | Label hiển thị | từ khóa Pinterest")
async def addcategory_prefix(ctx, *, args: str = None):
    if not is_admin(ctx.author.id):
        await ctx.send("⚠️ Chỉ admin mới dùng được lệnh này.")
        return
    if not args or args.count("|") != 2:
        await ctx.send("⚠️ Cú pháp: `!addcategory slug | Label hiển thị | từ khóa Pinterest`")
        return

    slug, label, keyword = [p.strip() for p in args.split("|")]
    slug = slug.lower()
    if not slug or " " in slug:
        await ctx.send("⚠️ Slug không hợp lệ (không dấu cách).")
        return
    if slug in CATEGORIES:
        await ctx.send(f"⚠️ `{slug}` là chủ đề có sẵn trong code, không thể ghi đè qua lệnh.")
        return

    await ctx.typing()
    await bot.loop.run_in_executor(None, db.add_custom_category, slug, label, keyword)
    await ctx.send(f"✅ Đã thêm chủ đề **{label}** (`{slug}`, từ khóa: `{keyword}`).")


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

    await interaction.response.defer(ephemeral=True)
    removed = await bot.loop.run_in_executor(None, db.remove_custom_category, slug)
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
    if removed:
        await ctx.send(f"✅ Đã xoá chủ đề `{slug}`.")
    else:
        await ctx.send(f"❌ Không tìm thấy chủ đề `{slug}` trong danh sách đã thêm.")


# ============================================================
# Lệnh admin: /cleanup, !cleanup — dọn ảnh lỗi link (404...) hoặc bị gửi
# quá nhiều lần trong 1 category. Chỉ ADMIN_IDS dùng được.
# ============================================================

CLEANUP_OVERUSED_THRESHOLD = 20  # ảnh bị gửi >= N lần thì coi là "cũ", xoá bớt
CLEANUP_MAX_CHECK = 100          # tối đa số ảnh kiểm tra link mỗi lần chạy


def _check_broken(url: str):
    try:
        resp = requests.head(url, timeout=4, allow_redirects=True)
        return url if resp.status_code >= 400 else None
    except Exception:
        return url


def _cleanup_category(category_key: str) -> str:
    """Chạy đồng bộ (blocking) trong executor. Trả về báo cáo kết quả."""
    overused_removed = db.delete_overused_images(category_key, CLEANUP_OVERUSED_THRESHOLD)

    urls = db.get_all_image_urls(category_key)[:CLEANUP_MAX_CHECK]
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

    all_cats = await get_all_categories_async()
    if chu_de not in all_cats:
        await interaction.response.send_message(f"❌ Chủ đề không hợp lệ: {chu_de}", ephemeral=True)
        return

    await interaction.response.defer()
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
