"""
Lệnh admin /showcase và wizard /setup.

(Tách ra từ bot.py cũ — xem README mục Kiến trúc.)
"""

from discord import app_commands
import db
import discord
from core import (
    _channel_allows_nsfw,
    bot,
    category_autocomplete,
    get_all_categories_async,
    is_admin,
    logger,
)
from viewer import (
    SHOWCASE_VIEW,
    _fetch_next_image_url,
    _next_crawl_eta_text,
    _timed_defer,
)


# ============================================================
# Lệnh admin: /showcase, !showcase — đăng 1 "bảng giới thiệu" chủ đề vào
# kênh: ảnh mẫu + tên chủ đề + tag admin quản lý + nút "Bắt đầu". Ai bấm
# "Bắt đầu" sẽ nhận 1 phiên xem ảnh riêng tư (ephemeral, chỉ người bấm thấy).
# ============================================================

async def _post_showcase_board(target_channel, category_key: str, info: dict, admin_id: int):
    """Trả về (message, error_text). error_text != None nếu thất bại."""
    preview_url = await _fetch_next_image_url(category_key, [])
    if not preview_url:
        eta = await _next_crawl_eta_text()
        return None, f"❌ Không lấy được ảnh mẫu cho chủ đề: **{info['label']}** (kho đang trống, lần crawl kế tiếp {eta})."

    embed = discord.Embed(title=f"🖼️ {info['label']}", color=discord.Color.gold())
    embed.set_image(url=preview_url)
    embed.add_field(name="Quản lý bởi", value=f"<@{admin_id}>", inline=True)
    embed.add_field(name="Chủ đề", value=f"`{category_key}`", inline=True)
    embed.set_footer(text="Bấm \"Bắt đầu\" để xem ảnh riêng tư, chỉ bạn thấy được.")

    message = await target_channel.send(embed=embed, view=SHOWCASE_VIEW)
    await bot.loop.run_in_executor(None, db.save_showcase_board, str(message.id), category_key)
    return message, None


@bot.tree.command(name="showcase", description="[Admin] Đăng bảng giới thiệu 1 chủ đề vào kênh (nút Bắt đầu cho mọi người bấm)")
@app_commands.guild_only()
@app_commands.describe(chu_de="Chủ đề cần giới thiệu", kenh="Kênh muốn đăng (bỏ trống = kênh hiện tại)")
@app_commands.autocomplete(chu_de=category_autocomplete)
async def showcase_slash(interaction: discord.Interaction, chu_de: str, kenh: discord.TextChannel = None):
    if not is_admin(interaction.user.id):
        await interaction.response.send_message("⚠️ Chỉ admin mới dùng được lệnh này.", ephemeral=True)
        return

    # defer() TRƯỚC get_all_categories_async() — xem ghi chú chi tiết ở
    # mergecategory_slash (lỗi thực tế đã xảy ra khi cache category vừa bị
    # invalidate, khiến lần đọc kế tiếp chậm hơn 3 giây).
    if not await _timed_defer(interaction, ephemeral=True):
        return

    all_cats = await get_all_categories_async()
    info = all_cats.get(chu_de)
    if not info:
        await interaction.followup.send(f"❌ Chủ đề không hợp lệ: {chu_de}")
        return

    target_channel = kenh or interaction.channel
    if info.get("nsfw") and not _channel_allows_nsfw(target_channel):
        await interaction.followup.send(
            f"🔞 Chủ đề **{info['label']}** là NSFW, chỉ đăng được vào kênh đã đánh dấu Age-Restricted."
        )
        return

    message, error = await _post_showcase_board(target_channel, chu_de, info, interaction.user.id)
    if error:
        await interaction.followup.send(error)
        return
    await interaction.followup.send(f"✅ Đã đăng bảng giới thiệu **{info['label']}** vào {target_channel.mention}.")


@bot.command(name="showcase", help="[Admin] !showcase <chủ_đề> [#kênh] — đăng bảng giới thiệu chủ đề")
async def showcase_prefix(ctx, chu_de: str = None, kenh: discord.TextChannel = None):
    if not is_admin(ctx.author.id):
        await ctx.send("⚠️ Chỉ admin mới dùng được lệnh này.")
        return

    all_cats = await get_all_categories_async()
    if not chu_de or chu_de.lower() not in all_cats:
        await ctx.send("⚠️ Vui lòng chọn 1 chủ đề hợp lệ.")
        return

    category_key = chu_de.lower()
    info = all_cats[category_key]
    target_channel = kenh or ctx.channel
    if info.get("nsfw") and not _channel_allows_nsfw(target_channel):
        await ctx.send(f"🔞 Chủ đề **{info['label']}** là NSFW, chỉ đăng được vào kênh đã đánh dấu Age-Restricted.")
        return
    await ctx.typing()

    message, error = await _post_showcase_board(target_channel, category_key, info, ctx.author.id)
    if error:
        await ctx.send(error)
        return
    await ctx.send(f"✅ Đã đăng bảng giới thiệu **{info['label']}** vào {target_channel.mention}.")


# ============================================================
# Lệnh admin: /setup — wizard thiết lập showcase board cho NHIỀU chủ đề
# cùng lúc: chọn chủ đề (nhiều/chọn tất cả) -> gán kênh cho từng chủ đề
# (chọn kênh có sẵn hoặc tạo kênh mới) -> tự động đăng showcase board vào
# đúng kênh tương ứng khi đủ thông tin.
# ============================================================

# Tên category kênh (nhóm kênh trong sidebar Discord) dùng để chứa các kênh
# text được /setup tự tạo mới. Chỉ tạo 1 lần cho mỗi server, các lần chạy
# /setup sau tái sử dụng lại — đổi tên ở đây nếu muốn.
SETUP_CHANNEL_CATEGORY_NAME = "📸 Ảnh chủ đề"


async def _ensure_setup_channel_category(guild: discord.Guild):
    """
    Trả về (category_channel, error_message). category_channel là None nếu
    lỗi (error_message sẽ có nội dung), ngược lại error_message rỗng.
    Ưu tiên tái sử dụng category đã lưu ID trong DB; nếu ID đó không còn hợp
    lệ (bị xoá thủ công...), thử tìm lại theo tên trước khi tạo mới hẳn —
    tránh tạo trùng nhiều category kênh cùng tên qua nhiều lần chạy /setup.
    """
    stored_id = await bot.loop.run_in_executor(None, db.get_guild_setup_category_id, guild.id)
    if stored_id:
        existing = guild.get_channel(stored_id)
        if isinstance(existing, discord.CategoryChannel):
            return existing, ""

    for cat in guild.categories:
        if cat.name == SETUP_CHANNEL_CATEGORY_NAME:
            await bot.loop.run_in_executor(None, db.set_guild_setup_category_id, guild.id, cat.id)
            return cat, ""

    try:
        new_category = await guild.create_category(name=SETUP_CHANNEL_CATEGORY_NAME)
    except discord.Forbidden:
        return None, "❌ Bot không có quyền tạo category kênh (cần quyền **Manage Channels**)."
    except Exception as e:
        logger.warning(f"Lỗi tạo category kênh trong /setup: {e}")
        return None, f"❌ Lỗi khi tạo category kênh: {e}"

    await bot.loop.run_in_executor(None, db.set_guild_setup_category_id, guild.id, new_category.id)
    return new_category, ""


class ChannelAssignWizard:
    """Trạng thái đi qua từng chủ đề đã chọn, hỏi gán kênh, rồi đăng showcase board."""

    def __init__(self, categories: list, admin_id: int):
        self.categories = categories  # list[(key, info)], hàng đợi xử lý tuần tự
        self.admin_id = admin_id
        self.index = 0
        self.assignments = {}  # category_key -> channel_id

    async def start(self, interaction: discord.Interaction):
        await self._prompt_current(interaction)

    async def _prompt_current(self, interaction: discord.Interaction):
        key, info = self.categories[self.index]
        view = ChannelPickerView(self, key, info)
        remaining = len(self.categories) - self.index
        embed = discord.Embed(
            title=f"📌 Chọn kênh cho: {info['label']}",
            description=(
                f"Chủ đề {self.index + 1}/{len(self.categories)} (`{key}`)\n\n"
                "Chọn 1 kênh có sẵn ở dropdown bên dưới, hoặc bấm **➕ Tạo kênh mới**.\n\n"
                f"Muốn nhanh gọn? Bấm **🚀 Tạo tất cả kênh còn lại** để bot tự tạo "
                f"kênh mới (tên = tên chủ đề) cho cả {remaining} chủ đề còn lại cùng lúc."
            ),
            color=discord.Color.blurple(),
        )
        await interaction.response.edit_message(embed=embed, view=view)

    async def advance(self, interaction: discord.Interaction, channel: discord.TextChannel):
        key, _info = self.categories[self.index]
        self.assignments[key] = channel.id
        self.index += 1

        if self.index >= len(self.categories):
            await self._finish(interaction)
            return

        await self._prompt_current(interaction)

    async def create_all_remaining(self, interaction: discord.Interaction):
        """
        Tự tạo kênh mới (tên = label chủ đề) cho TẤT CẢ chủ đề còn lại trong
        hàng đợi (kể cả chủ đề đang hiện ở bước này), gom vào category kênh
        chung, rồi kết thúc wizard luôn — bỏ qua việc hỏi từng chủ đề một.
        Dùng defer() vì có thể tạo nhiều kênh liên tiếp, tốn hơn 3 giây.
        """
        if not await _timed_defer(interaction):
            return

        remaining = self.categories[self.index:]
        setup_category, cat_error = await _ensure_setup_channel_category(interaction.guild)
        if cat_error:
            logger.warning(f"/setup (tạo tất cả kênh) không gom được vào category kênh chung: {cat_error}")

        error_lines = []
        for key, info in remaining:
            try:
                new_channel = await interaction.guild.create_text_channel(name=info["label"][:90], category=setup_category)
            except discord.Forbidden:
                error_lines.append(f"❌ **{info['label']}**: bot không có quyền tạo kênh, bỏ qua.")
                continue
            except Exception as e:
                logger.warning(f"Lỗi tạo kênh cho `{key}` trong /setup (tạo tất cả): {e}")
                error_lines.append(f"❌ **{info['label']}**: lỗi khi tạo kênh, bỏ qua.")
                continue
            self.assignments[key] = new_channel.id

        self.index = len(self.categories)
        await self._finish(interaction, extra_lines=error_lines)

    async def _finish(self, interaction: discord.Interaction, extra_lines: list = None):
        all_cats = await get_all_categories_async()
        lines = list(extra_lines) if extra_lines else []
        for key, channel_id in self.assignments.items():
            info = all_cats.get(key)
            channel = interaction.guild.get_channel(channel_id) if interaction.guild else None
            if not info or not channel:
                lines.append(f"❌ `{key}`: không tìm thấy kênh hoặc chủ đề, bỏ qua.")
                continue
            if info.get("nsfw") and not _channel_allows_nsfw(channel):
                lines.append(f"⚠️ **{info['label']}**: bỏ qua vì là NSFW nhưng {channel.mention} không phải kênh Age-Restricted.")
                continue
            _msg, error = await _post_showcase_board(channel, key, info, self.admin_id)
            if error:
                lines.append(f"❌ **{info['label']}**: {error}")
            else:
                lines.append(f"✅ **{info['label']}** → {channel.mention}")

        embed = discord.Embed(
            title="🎉 Thiết lập hoàn tất",
            description="\n".join(lines) if lines else "(không có chủ đề nào được xử lý)",
            color=discord.Color.green(),
        )
        # create_all_remaining() đã defer() trước đó (response is_done), nên
        # phải sửa qua edit_original_response thay vì response.edit_message.
        if interaction.response.is_done():
            await interaction.edit_original_response(embed=embed, view=None)
        else:
            await interaction.response.edit_message(embed=embed, view=None)


class NewChannelModal(discord.ui.Modal):
    def __init__(self, wizard: ChannelAssignWizard, category_key: str, info: dict):
        super().__init__(title=f"Tạo kênh cho: {info['label'][:40]}")
        self.wizard = wizard
        self.category_key = category_key
        self.channel_name_input = discord.ui.TextInput(
            label="Tên kênh mới",
            placeholder=f"vd: {info['label']}",
            default=info["label"][:90],
            max_length=90,
        )
        self.add_item(self.channel_name_input)

    async def on_submit(self, interaction: discord.Interaction):
        name = self.channel_name_input.value.strip()
        if not name:
            await interaction.response.send_message("⚠️ Tên kênh không hợp lệ.", ephemeral=True)
            return

        # Gom kênh mới vào 1 category kênh chung (tạo sẵn/tái sử dụng theo
        # server) thay vì thả nổi ở ngoài — không chặn tạo kênh nếu lỗi,
        # chỉ báo cho admin biết để tự sắp xếp lại thủ công nếu cần.
        setup_category, cat_error = await _ensure_setup_channel_category(interaction.guild)

        try:
            new_channel = await interaction.guild.create_text_channel(name=name, category=setup_category)
        except discord.Forbidden:
            await interaction.response.send_message("❌ Bot không có quyền tạo kênh trong server này.", ephemeral=True)
            return
        except Exception as e:
            logger.warning(f"Lỗi tạo kênh mới trong /setup: {e}")
            await interaction.response.send_message(f"❌ Lỗi khi tạo kênh: {e}", ephemeral=True)
            return

        if cat_error:
            logger.warning(f"/setup tạo kênh #{new_channel.name} ngoài category kênh chung: {cat_error}")

        await self.wizard.advance(interaction, new_channel)


class ChannelPickerView(discord.ui.View):
    def __init__(self, wizard: ChannelAssignWizard, category_key: str, info: dict):
        super().__init__(timeout=300)
        self.wizard = wizard
        self.category_key = category_key
        self.info = info

        channel_select = discord.ui.ChannelSelect(
            placeholder="Chọn kênh có sẵn...",
            channel_types=[discord.ChannelType.text],
            min_values=1,
            max_values=1,
            row=0,
        )
        channel_select.callback = self._on_channel_selected
        self.channel_select = channel_select
        self.add_item(channel_select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.wizard.admin_id:
            await interaction.response.send_message("⚠️ Chỉ người chạy lệnh `/setup` mới thao tác được ở đây.", ephemeral=True)
            return False
        return True

    async def _on_channel_selected(self, interaction: discord.Interaction):
        picked = self.channel_select.values[0]
        real_channel = interaction.guild.get_channel(picked.id)
        if real_channel is None:
            await interaction.response.send_message("❌ Không lấy được kênh này, thử lại nhé.", ephemeral=True)
            return
        await self.wizard.advance(interaction, real_channel)

    @discord.ui.button(label="➕ Tạo kênh mới", style=discord.ButtonStyle.primary, row=1)
    async def create_channel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(NewChannelModal(self.wizard, self.category_key, self.info))

    @discord.ui.button(label="🚀 Tạo tất cả kênh còn lại", style=discord.ButtonStyle.success, row=1)
    async def create_all_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.wizard.create_all_remaining(interaction)


class CategoryPickerView(discord.ui.View):
    """Bước 1 của /setup: chọn 1 hoặc nhiều chủ đề (hoặc 'Chọn tất cả')."""

    def __init__(self, categories: list, admin_id: int):
        super().__init__(timeout=300)
        self.categories = categories  # list[(key, info)]
        self.admin_id = admin_id

        options = [
            discord.SelectOption(label="✅ Chọn tất cả", value="__all__", description=f"Thiết lập cả {len(categories)} chủ đề")
        ]
        for key, info in categories[:24]:  # chừa 1 chỗ cho "Chọn tất cả", tối đa 25 lựa chọn
            options.append(discord.SelectOption(label=info["label"][:100], value=key, description=f"`{key}`"))

        select = discord.ui.Select(
            placeholder="Chọn 1 hoặc nhiều chủ đề cần thiết lập...",
            min_values=1,
            max_values=len(options),
            options=options,
        )
        select.callback = self._on_select
        self.add_item(select)
        self.select = select

    def build_embed(self) -> discord.Embed:
        lines = [f"**{i + 1}.** {info['label']} (`{key}`)" for i, (key, info) in enumerate(self.categories)]
        return discord.Embed(
            title="🛠️ Thiết lập showcase board",
            description=(
                "Chọn chủ đề cần thiết lập ở dropdown bên dưới (chọn nhiều được, "
                "hoặc \"✅ Chọn tất cả\"):\n\n" + "\n".join(lines)
            ),
            color=discord.Color.blurple(),
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.admin_id:
            await interaction.response.send_message("⚠️ Chỉ người chạy lệnh `/setup` mới thao tác được ở đây.", ephemeral=True)
            return False
        return True

    async def _on_select(self, interaction: discord.Interaction):
        values = self.select.values
        if "__all__" in values:
            selected_keys = {key for key, _ in self.categories}
        else:
            selected_keys = set(values)

        selected = [(key, info) for key, info in self.categories if key in selected_keys]
        if not selected:
            await interaction.response.send_message("⚠️ Chưa chọn chủ đề nào.", ephemeral=True)
            return

        wizard = ChannelAssignWizard(selected, self.admin_id)
        await wizard.start(interaction)


@bot.tree.command(name="setup", description="[Admin] Wizard thiết lập showcase board cho nhiều chủ đề cùng lúc")
@app_commands.guild_only()
async def setup_slash(interaction: discord.Interaction):
    if not is_admin(interaction.user.id):
        await interaction.response.send_message("⚠️ Chỉ admin mới dùng được lệnh này.", ephemeral=True)
        return
    if interaction.guild is None:
        await interaction.response.send_message("⚠️ Lệnh này chỉ dùng được trong server, không dùng được ở DM.", ephemeral=True)
        return

    all_cats = await get_all_categories_async()
    if not all_cats:
        await interaction.response.send_message("❌ Chưa có chủ đề nào để thiết lập.", ephemeral=True)
        return

    view = CategoryPickerView(list(all_cats.items()), interaction.user.id)
    await interaction.response.send_message(embed=view.build_embed(), view=view, ephemeral=True)
