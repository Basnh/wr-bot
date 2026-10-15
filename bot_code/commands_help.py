class WRBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True  # bắt buộc để đọc lệnh prefix
        super().__init__(command_prefix=PREFIX, intents=intents)

    async def setup_hook(self) -> None:
        await wr.start()
        guild = discord.Object(id=SLASH_COMMAND_GUILD_ID)
        self.tree.copy_global_to(guild=guild)
        self.tree.clear_commands(guild=None)
        await self.tree.sync()  # Gỡ các slash command global đã đăng ký trước đây.
        synced_commands = await self.tree.sync(guild=guild)
        print(
            f"[slash] Đã đồng bộ {len(synced_commands)} lệnh cho server "
            f"{SLASH_COMMAND_GUILD_ID}.",
            flush=True,
        )
        watch.start()
        if AUTO_RELOAD:
            songs_watcher.start()
        threading.Thread(
            target=_console_listener, args=(asyncio.get_running_loop(),), daemon=True
        ).start()

    async def close(self) -> None:
        await wr.close()
        await super().close()


bot = WRBot()
bot.remove_command("help")


HELP_PAGES = {
    "main": {
        "title": "📖 Hướng dẫn sử dụng bot",
        "description": (
            "Chọn một mục trong menu bên dưới để xem hướng dẫn.\n\n"
            f"**Prefix hiện tại:** `{PREFIX}`\n"
            "Có thể dùng lệnh slash bắt đầu bằng `/` hoặc lệnh prefix bắt đầu bằng `!`."
        ),
        "fields": [
            (
                "🏆 Xem World Record",
                "`/world_record` — chọn nghệ sĩ, bài hát và album.\n"
                f"`{PREFIX}world_record` hoặc `{PREFIX}wr <ID hoặc từ khóa>` — "
                f"ví dụ `{PREFIX}wr got7 hard carry`. Embed có menu đổi season; dữ liệu season cũ được tải trực tiếp và không lưu vào JSON.",
            ),
            (
                "🎤 WR #1 theo nghệ sĩ",
                "`/top1` — chọn nghệ sĩ từ menu, bot liệt kê người đứng #1 ở **tất cả bài** của nghệ sĩ đó "
                "(sắp xếp theo album, điểm hoặc tên bài), có menu đổi season.\n"
                f"`{PREFIX}top1 <nghệ sĩ>` — ví dụ `{PREFIX}top1 itzy`; dùng `--score` hoặc `--name` để đổi cách xếp.",
            ),
            (
                "🎵 Danh sách bài hát",
                "`/songs list`, `/songs artists`, `/songs search` — tra cứu; `/songs add`, "
                "`/songs remove`, `/songs sync` — quản lý (chỉ chủ bot).\n"
                f"`{PREFIX}songs list|artists|search` — tra cứu; `{PREFIX}songs add|remove|sync` "
                "— thao tác quản lý (chỉ chủ bot).",
            ),
            (
                "👤 Theo dõi người chơi",
                "`/track` — mở menu, chọn **Tất cả** hoặc một người trong `players.txt` để xem họ đang giữ World Record ở bài nào.\n"
                f"`{PREFIX}track` — mở menu; `{PREFIX}track <tên>` — tra cứu nhanh; "
                f"`{PREFIX}track list` — xem danh sách. Kết quả có menu đổi season.",
            ),
            (
                "🔒 Lệnh chủ bot",
                f"`{PREFIX}reload` — tải lại dữ liệu; `{PREFIX}refresh_wr` — cập nhật WR toàn bộ ID "
                "và lưu snapshot; "
                f"`{PREFIX}deploy` — cập nhật và khởi động lại bot trên VPS.\n"
                f"`{PREFIX}help` (bí danh `{PREFIX}h`) hoặc `/help` — mở menu trợ giúp.",
            ),
        ],
    },
    "songs": {
        "title": "🎵 Lệnh quản lý bài hát",
        "description": "Các lệnh slash dùng theo dạng `/songs <lệnh>`.",
        "fields": [
            (
                "Tra cứu (mọi người)",
                "`/songs list [artist]` — danh sách embed, có thể lọc nghệ sĩ và chuyển trang.\n"
                "`/songs artists` — nghệ sĩ kèm số lượng bài.\n"
                "`/songs search <keywords>` — tìm theo tên bài, nghệ sĩ hoặc album.\n"
                f"`{PREFIX}songs list [nghệ sĩ]`, `{PREFIX}songs artists`, "
                f"`{PREFIX}songs search <từ khóa>` — bản prefix.",
            ),
            (
                "Chỉnh sửa (chủ bot)",
                "`/songs add` — thêm hoặc sửa bài theo ID.\n"
                "`/songs remove` — xóa bài theo ID.\n"
                "`/songs sync` — đồng bộ từ Google Sheets hoặc CSV đính kèm.\n"
                "Đồng bộ mặc định thêm bài mới và cập nhật riêng cover; bật `overwrite` để "
                "cập nhật toàn bộ thông tin bài đã có hoặc `dry_run` để xem trước.\n"
                "`!refresh_wr` — cập nhật WR toàn bộ ID, lưu snapshot JSON.\n"
                "`!deploy` — (chỉ chủ bot) lấy commit mới từ GitHub và restart bot trên VPS.",
            ),
            (
                "Ví dụ prefix",
                f'`{PREFIX}songs add 153 "ITZY" "Imaginary Friend" '
                '`--album "Crazy In Love"`\n'
                f"`{PREFIX}songs remove 153`\n"
                f"`{PREFIX}songs sync --dry-run`",
            ),
        ],
    },
    "track": {
        "title": "👤 Theo dõi người chơi",
        "description": (
            "Bot đọc danh sách tên trong file `players.txt` (mỗi dòng một người) rồi cho biết từng người "
            "đang giữ World Record ở những bài nào. **Đạt WR** = điểm bằng điểm cao nhất của bài "
            "(người đồng hạng cũng tính)."
        ),
        "fields": [
            (
                "Xem (mọi người)",
                "`/track` — menu liệt kê **Tất cả** và từng người trong `players.txt`, chọn một mục để xem.\n"
                f"`{PREFIX}track` — mở menu chọn người; `{PREFIX}track <tên>` — tìm nhanh theo tên.\n"
                f"`{PREFIX}track list` — danh sách đang theo dõi.",
            ),
            (
                "Quản lý danh sách (chủ bot hoặc quyền Manage Server)",
                f"`{PREFIX}track add <nickname hoặc ID>`\n"
                f"`{PREFIX}track remove <nickname hoặc ID>`\n"
                "Các lệnh slash `/track add`, `/track remove` hiện không có; dùng lệnh prefix hoặc sửa `players.txt`.\n"
                "Hoặc mở `players.txt` sửa tay: mỗi dòng một tên, ghi `tên | objectID` để khớp chính xác "
                "kể cả khi người đó đổi tên. Bot tự đọc lại khi bạn lưu file.",
            ),
        ],
    },
    "admin": {
        "title": "🔒 Lệnh dành cho chủ bot",
        "description": "Các lệnh này chỉ chủ bot mới có thể thực hiện.",
        "fields": [
            (
                "Thao tác bài hát",
                "Dùng `/songs add`, `/songs remove`, `/songs sync` hoặc các lệnh prefix "
                f"`{PREFIX}songs add|remove|sync`.",
            ),
            (
                "Tải lại dữ liệu",
                f"`{PREFIX}reload` — đọc lại `songs.json` và `players.txt` mà không cần khởi động lại bot.\n"
                "Bot cũng tự đọc lại file khi phát hiện có thay đổi.\n"
                f"`{PREFIX}refresh_wr` — cập nhật leaderboard toàn bộ bài; "
                f"`{PREFIX}deploy` — cập nhật từ Git và restart systemd trên VPS.\n"
                "Các lệnh này chưa có slash command tương ứng.",
            ),
        ],
    },
}


def _build_help_embed(page: str) -> discord.Embed:
    details = HELP_PAGES[page]
    embed = discord.Embed(
        title=details["title"],
        description=details["description"],
        color=discord.Color.blurple(),
    )
    for name, value in details["fields"]:
        embed.add_field(name=name, value=value, inline=False)
    embed.set_footer(text="Dùng menu bên dưới để chuyển giữa các mục trợ giúp.")
    return embed


class HelpMenuSelect(discord.ui.Select):
    def __init__(self) -> None:
        options = [
            discord.SelectOption(label="Tổng quan", value="main", emoji="📖"),
            discord.SelectOption(label="Lệnh bài hát", value="songs", emoji="🎵"),
            discord.SelectOption(label="Theo dõi người chơi", value="track", emoji="👤"),
            discord.SelectOption(label="Chủ bot", value="admin", emoji="🔒"),
        ]
        super().__init__(
            placeholder="Chọn mục trợ giúp...",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(
            embed=_build_help_embed(self.values[0]), view=self.view
        )


class HelpMenuView(discord.ui.View):
    def __init__(self, user_id: int) -> None:
        super().__init__(timeout=180)
        self.user_id = user_id
        self.add_item(HelpMenuSelect())

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.user_id:
            return True
        await interaction.response.send_message(
            "Menu trợ giúp này thuộc về người đã mở nó.",
            ephemeral=True,
        )
        return False

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


async def _send_help_menu(target) -> None:
    user_id = target.user.id if isinstance(target, discord.Interaction) else target.author.id
    view = HelpMenuView(user_id)
    embed = _build_help_embed("main")
    if isinstance(target, discord.Interaction):
        await target.response.send_message(embed=embed, view=view, ephemeral=True)
        view.message = await target.original_response()
    else:
        view.message = await target.send(embed=embed, view=view)


@bot.tree.command(name="help", description="Mở menu hướng dẫn sử dụng bot")
async def help_slash(interaction: discord.Interaction):
    await _send_help_menu(interaction)


@bot.command(name="help", aliases=["h"])
async def help_prefix(ctx: commands.Context):
    await _send_help_menu(ctx)


