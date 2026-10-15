class WRClient:
    def __init__(self) -> None:
        self.session: aiohttp.ClientSession | None = None
        self.cache: dict[str, dict] = {}
        self.sem = asyncio.Semaphore(MAX_CONCURRENCY)

    async def start(self) -> None:
        self.session = aiohttp.ClientSession(
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=aiohttp.ClientTimeout(total=15),
        )

    async def close(self) -> None:
        if self.session:
            await self.session.close()

    async def get(
        self,
        song_id: str,
        force: bool = False,
        max_age: float = CACHE_TTL,
        *,
        season: int = SEASON,
    ) -> dict | None:
        """Trả về {"data": [...], "updated": datetime, "etag": str, "fetched": float}.
        Dùng ETag nên nếu file không đổi thì S3 trả 304, không tải lại nội dung."""
        cache_key = song_id if season == SEASON else f"{season}:{song_id}"
        entry = self.cache.get(cache_key)
        now = time.time()
        if entry and not force and now - entry["fetched"] < max_age:
            return entry

        headers = {}
        if entry and entry.get("etag"):
            headers["If-None-Match"] = entry["etag"]

        url = f"{BASE_URL}/{season}/{song_id}/latest.json"
        async with self.sem:
            try:
                async with self.session.get(url, headers=headers) as r:
                    if r.status == 304 and entry:
                        entry["fetched"] = now
                        return entry
                    if r.status != 200:
                        return None  # bài chưa có dữ liệu (403/404)
                    data = await r.json(content_type=None)
                    lm = r.headers.get("Last-Modified")
                    updated = (
                        parsedate_to_datetime(lm) if lm else datetime.now(timezone.utc)
                    )
                    entry = {
                        "data": data,
                        "updated": updated,
                        "etag": r.headers.get("ETag"),
                        "fetched": now,
                        # tăng mỗi khi tải được dữ liệu MỚI (304 không làm đổi) -> biết bài nào thật sự thay đổi
                        "rev": (entry.get("rev", 0) + 1) if entry else 1,
                    }
                    self.cache[cache_key] = entry
                    return entry
            except (aiohttp.ClientError, asyncio.TimeoutError):
                return entry  # lỗi mạng: dùng bản cũ nếu có


wr = WRClient()


SEASON_MENU_SIZE = 24


class SeasonSelect(discord.ui.Select):
    def __init__(self, menu: "SeasonMenuMixin") -> None:
        self.menu = menu
        upper = max(1, SEASON - menu.season_window * SEASON_MENU_SIZE)
        lower = max(1, upper - SEASON_MENU_SIZE + 1)
        options = [
            discord.SelectOption(
                label=f"Season {season}",
                value=str(season),
                default=season == menu.season,
            )
            for season in range(upper, lower - 1, -1)
        ]
        super().__init__(
            placeholder=f"Chọn season ({upper} → {lower})...",
            min_values=1,
            max_values=1,
            options=options,
            row=menu.season_select_row,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.menu._change_season(interaction, int(self.values[0]))


class SeasonWindowButton(discord.ui.Button):
    def __init__(self, menu: "SeasonMenuMixin", older: bool) -> None:
        self.menu = menu
        self.older = older
        pages = max(1, math.ceil(SEASON / SEASON_MENU_SIZE))
        target_window = min(pages - 1, menu.season_window + 1) if older else max(0, menu.season_window - 1)
        upper = max(1, SEASON - target_window * SEASON_MENU_SIZE)
        lower = max(1, upper - SEASON_MENU_SIZE + 1)
        super().__init__(
            label=f"Cũ hơn: {upper} → {lower}" if older else f"Mới hơn: {upper} → {lower}",
            style=discord.ButtonStyle.secondary,
            disabled=(menu.season_window >= pages - 1 if older else menu.season_window == 0),
            row=menu.season_nav_row,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        self.menu.season_window += 1 if self.older else -1
        self.menu._refresh_season_menu()
        await self.menu._show(interaction)


class SeasonMenuMixin:
    def _init_season_menu(self, select_row: int, nav_row: int) -> None:
        self.season = SEASON
        self.season_window = 0
        self.season_select_row = select_row
        self.season_nav_row = nav_row
        self._refresh_season_menu()

    def _refresh_season_menu(self) -> None:
        for item in (getattr(self, "season_select", None),
                     getattr(self, "older_seasons_button", None),
                     getattr(self, "newer_seasons_button", None)):
            if item is not None:
                self.remove_item(item)
        self.season_select = SeasonSelect(self)
        self.older_seasons_button = SeasonWindowButton(self, older=True)
        self.newer_seasons_button = SeasonWindowButton(self, older=False)
        self.add_item(self.season_select)
        self.add_item(self.older_seasons_button)
        self.add_item(self.newer_seasons_button)


# ----------------------------- Embed -----------------------------
MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}
TIME_STYLE = "S"          # kiểu timestamp Discord: t, T, d, D, f, F, R
SHOW_RELATIVE = False      # thêm "3 days ago" cạnh thời gian
COMPACT_LAYOUT = True    # True: thời gian cùng dòng với điểm; False: thời gian ở dòng riêng bên dưới


def ts_tag(ms: int, style: str = TIME_STYLE) -> str:
    """updatedAt (mili giây) -> Discord timestamp, tự hiển thị theo múi giờ của người xem."""
    return f"<t:{int(ms // 1000)}:{style}>"


def time_text(ms: int) -> str:
    text = ts_tag(ms)
    if SHOW_RELATIVE:
        text += f" · {ts_tag(ms, 'R')}"
    return text


def build_embed(
    song_name: str,
    entry: dict,
    page: int,
    cover_url: str = "",
    season: int = SEASON,
) -> discord.Embed:
    data = entry["data"]
    pages = max(1, math.ceil(len(data) / PAGE_SIZE))
    chunk = data[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]
    esc = discord.utils.escape_markdown

    # --- tóm tắt World Record ---
    top = data[0]
    tied = sum(1 for p in data if p["highscore"] == top["highscore"])
    wr_line = f"🏆 **World Record** `{top['highscore']:,}` · {esc(top['nickname'])}"
    if tied > 1:
        wr_line += f" (+{tied - 1} người cùng điểm)"

    # --- danh sách xếp hạng ---
    lines = []
    for p in chunk:
        badge = MEDALS.get(p["rank"], f"`#{p['rank']:<3}`")
        main = f"{badge} **{esc(p['nickname'])}** · `{p['highscore']:,}`"
        if p.get("updatedAt"):
            if COMPACT_LAYOUT:
                main += f" · {time_text(p['updatedAt'])}"
                lines.append(main)
            else:
                lines.append(main)
                lines.append(f"-# 🕒 {time_text(p['updatedAt'])}")
        else:
            lines.append(main)

    embed = discord.Embed(
        title=f"{GAME_TITLE} - World Record (Season {season})",
        description=f"## 🎵 {song_name}\n{wr_line}\n\n" + "\n".join(lines),
        color=discord.Color.gold(),
    )
    if cover_url.startswith(("https://", "http://")):
        embed.set_thumbnail(url=cover_url)
    embed.set_footer(text=f"Page {page + 1}/{pages} · Cập nhật lần cuối")
    embed.timestamp = entry["updated"]
    return embed


class RankView(SeasonMenuMixin, discord.ui.View):
    def __init__(
        self,
        song_name: str,
        entry: dict | None,
        owner_id: int,
        cover_url: str = "",
        song_id: str = "",
    ) -> None:
        super().__init__(timeout=300)
        self.song_name = song_name
        self.entry = entry
        self.owner_id = owner_id
        self.cover_url = cover_url
        self.song_id = song_id
        self.season = SEASON
        self.page = 0
        self.pages = max(1, math.ceil(len(entry["data"]) / PAGE_SIZE)) if entry else 1
        self.message: discord.Message | None = None
        self._init_season_menu(select_row=0, nav_row=2)
        if not entry or not entry.get("data"):
            self.entry = None
        self._sync()

    def _sync(self) -> None:
        self.prev_btn.disabled = self.page <= 0
        self.next_btn.disabled = self.page >= self.pages - 1

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Chỉ người dùng lệnh mới bấm được nút này.", ephemeral=True
            )
            return False
        return True

    def _embed(self) -> discord.Embed:
        if self.entry is None:
            return discord.Embed(
                title=f"{GAME_TITLE} - World Record (Season {self.season})",
                description=f"Không tìm thấy dữ liệu cho **{self.song_name}** ở season này.",
                color=discord.Color.dark_grey(),
            )
        return build_embed(self.song_name, self.entry, self.page, self.cover_url, self.season)

    async def _show(self, interaction: discord.Interaction) -> None:
        self._sync()
        await interaction.response.edit_message(embed=self._embed(), view=self)

    async def _change_season(self, interaction: discord.Interaction, season: int) -> None:
        await interaction.response.defer()
        self.season = season
        self.entry = await wr.get(self.song_id, max_age=CACHE_TTL, season=season)
        if not self.entry or not self.entry.get("data"):
            self.entry = None
            self.pages = 1
        else:
            self.pages = max(1, math.ceil(len(self.entry["data"]) / PAGE_SIZE))
        self.page = 0
        self._sync()
        await interaction.edit_original_response(embed=self._embed(), view=self)

    async def _render(self, interaction: discord.Interaction) -> None:
        await self._show(interaction)

    @discord.ui.button(label="Trang trước", style=discord.ButtonStyle.primary, row=1)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = max(0, self.page - 1)
        await self._render(interaction)

    @discord.ui.button(label="Trang sau", style=discord.ButtonStyle.success, row=1)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = min(self.pages - 1, self.page + 1)
        await self._render(interaction)

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


# ----------------------------- Bot -----------------------------
