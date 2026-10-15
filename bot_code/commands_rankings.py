def find_songs(query: str, artist: str | None = None, album: str | None = None) -> list[str]:
    """Tìm bài theo ID hoặc theo các từ khóa (khớp với nghệ sĩ + tên bài + album).
    artist / album (nếu có) phải khớp chính xác, không phân biệt hoa thường."""
    q = query.strip().lower()
    if q in SONGS:
        return [q]
    tokens = q.split()
    found = []
    for sid, s in SONGS.items():
        if artist and s["artist"].lower() != artist.strip().lower():
            continue
        if album and s["album"].lower() != album.strip().lower():
            continue
        hay = f"{s['artist']} {s['song']} {s['album']}".lower()
        if all(t in hay for t in tokens):
            found.append(sid)
    return found


async def build_response(found: list[str], user_id: int):
    """Trả về (text, embed, view). Nếu text khác None thì đó là thông báo lỗi/gợi ý."""
    if not found:
        return "Không tìm thấy bài hát này trong songs.json.", None, None
    if len(found) > 1:
        options = "\n".join(f"`{sid}` - {display(sid)}" for sid in found[:10])
        more = f"\n... và {len(found) - 10} bài khác" if len(found) > 10 else ""
        return f"Có {len(found)} bài khớp, hãy gõ cụ thể hơn hoặc dùng ID:\n{options}{more}", None, None

    song_id = found[0]
    entry = await wr.get(song_id)
    if not entry or not entry["data"]:
        entry = None

    cover_url = SONGS[song_id].get("cover", "")
    view = RankView(display(song_id), entry, user_id, cover_url, song_id)
    return None, view._embed(), view


# ---------- Autocomplete: chọn nhóm -> chỉ hiện bài của nhóm đó ----------
def _typed(interaction: discord.Interaction, name: str) -> str | None:
    """Giá trị người dùng đã nhập/chọn ở option khác của cùng lệnh."""
    return getattr(interaction.namespace, name, None)


async def artist_autocomplete(interaction: discord.Interaction, current: str):
    cur = current.lower()
    artists = sorted({s["artist"] for s in SONGS.values() if s["artist"]}, key=str.lower)
    return [
        app_commands.Choice(name=a[:100], value=a[:100]) for a in artists if cur in a.lower()
    ][:25]


async def album_autocomplete(interaction: discord.Interaction, current: str):
    cur = current.lower()
    artist = (_typed(interaction, "artist") or "").lower()
    albums = sorted(
        {
            s["album"]
            for s in SONGS.values()
            if s["album"] and (not artist or s["artist"].lower() == artist)
        },
        key=str.lower,
    )
    return [
        app_commands.Choice(name=a[:100], value=a[:100]) for a in albums if cur in a.lower()
    ][:25]


async def song_autocomplete(interaction: discord.Interaction, current: str):
    artist = _typed(interaction, "artist")
    album = _typed(interaction, "album")
    ids = find_songs(current, artist, album)

    titles = [SONGS[i]["song"].lower() for i in ids]
    choices = []
    for sid in ids:
        s = SONGS[sid]
        label = s["song"] if artist else display(sid)
        # trùng tên bài (khác album) thì thêm album để phân biệt
        if s["album"] and titles.count(s["song"].lower()) > 1:
            label += f" · {s['album']}"
        choices.append(app_commands.Choice(name=label[:100], value=sid))
    return choices[:25]


# ---------- Slash command: /world_record artist song [album] ----------
@bot.tree.command(name="world_record", description="Xem bảng xếp hạng World Record của một bài hát")
@app_commands.describe(
    artist="Nhóm / nghệ sĩ",
    song="Bài hát (chọn nhóm trước để lọc danh sách)",
    album="Album (tùy chọn, để lọc bớt bài)",
)
@app_commands.autocomplete(
    artist=artist_autocomplete, song=song_autocomplete, album=album_autocomplete
)
async def world_record(
    interaction: discord.Interaction, artist: str, song: str, album: str | None = None
):
    await interaction.response.defer()
    text, embed, view = await build_response(find_songs(song, artist, album), interaction.user.id)
    if text:
        await interaction.followup.send(text)
        return
    view.message = await interaction.followup.send(embed=embed, view=view, wait=True)


# ---------- Prefix command: !world_record <từ khóa> (hoặc !wr) ----------
# Ví dụ: !wr 153  |  !wr got7 hard carry  |  !wr imaginary friend
@bot.command(name="world_record", aliases=["wr"])
async def world_record_prefix(ctx: commands.Context, *, query: str = ""):
    if not query.strip():
        await ctx.send(f"Cách dùng: `{PREFIX}wr <ID hoặc tên nhóm/bài>`, ví dụ `{PREFIX}wr got7 hard carry`")
        return
    text, embed, view = await build_response(find_songs(query), ctx.author.id)
    if text:
        await ctx.send(text)
        return
    view.message = await ctx.send(embed=embed, view=view)


# ----------------------------- /top1: WR #1 của tất cả bài của một nghệ sĩ -----------------------------
TOP1_PAGE_SIZE = 8
TOP1_SORTS = {"album": "Theo album", "score": "Điểm cao → thấp", "name": "Tên bài A → Z"}


def resolve_artist(query: str) -> tuple[str | None, str | None]:
    """Trả về (tên nghệ sĩ chuẩn, thông báo lỗi). Khớp chính xác trước, rồi mới khớp một phần."""
    q = _norm(query)
    if not q:
        return None, "Hãy nhập tên nghệ sĩ."
    artists = sorted({s["artist"] for s in SONGS.values() if s["artist"]}, key=str.casefold)
    exact = [a for a in artists if _norm(a) == q]
    if exact:
        return exact[0], None
    partial = [a for a in artists if q in _norm(a)]
    if len(partial) == 1:
        return partial[0], None
    if not partial:
        return None, f"Không có nghệ sĩ nào khớp `{query}` trong songs.json."
    return None, "Có nhiều nghệ sĩ khớp, hãy gõ cụ thể hơn:\n" + "\n".join(f"• {a}" for a in partial[:15])


async def collect_artist_top1(artist: str, season: int = SEASON) -> tuple[list[dict], list[str]]:
    """Với mọi bài của nghệ sĩ: người đứng #1 (và điểm). Trả về (các dòng, ID bài chưa có dữ liệu)."""
    sids = [sid for sid, s in SONGS.items() if _norm(s["artist"]) == _norm(artist)]
    infos = {sid: SONGS[sid] for sid in sids}
    entries = await asyncio.gather(*(
        wr.get(sid, max_age=TRACK_CACHE_TTL, season=season) for sid in sids
    ))
    rows, missing = [], []
    for sid, entry in zip(sids, entries):
        if not entry or not entry["data"]:
            missing.append(sid)
            continue
        data = entry["data"]
        top = max(p["highscore"] for p in data)
        leaders = sorted((p for p in data if p["highscore"] == top), key=lambda p: p.get("rank", 10**9))
        first = leaders[0]  # người đứng hạng 1
        rows.append({
            "sid": sid,
            "info": infos[sid],
            "score": top,
            "nickname": first.get("nickname") or "(không tên)",
            "tied": len(leaders),
            "time": first.get("updatedAt") or 0,
            "updated": entry["updated"],
        })
    return rows, missing


def sort_top1_rows(rows: list[dict], mode: str) -> None:
    album_key = lambda r: _song_album_sort_key((r["sid"], r["info"]))
    if mode == "score":
        rows.sort(key=lambda r: (-r["score"], album_key(r)))
    elif mode == "name":
        rows.sort(key=lambda r: (_norm(r["info"]["song"]), album_key(r)))
    else:
        rows.sort(key=album_key)


def build_top1_embed(
    artist: str, rows: list[dict], missing: list[str], sort_mode: str, page: int,
    season: int = SEASON,
) -> discord.Embed:
    esc = discord.utils.escape_markdown
    pages = max(1, math.ceil(len(rows) / TOP1_PAGE_SIZE))
    chunk = rows[page * TOP1_PAGE_SIZE : (page + 1) * TOP1_PAGE_SIZE]

    # --- tóm tắt ---
    summary = f"🎤 **{esc(artist)}** · **{len(rows)}** bài có dữ liệu"
    if missing:
        summary += f" (+{len(missing)} bài chưa có dữ liệu)"
    counts: dict[str, list] = {}
    for r in rows:
        counts.setdefault(_norm(r["nickname"]), [r["nickname"], 0])[1] += 1
    leaders = sorted((v for v in counts.values() if v[1] >= 2), key=lambda v: -v[1])[:3]
    if leaders:
        summary += "\n👑 Giữ nhiều #1 nhất: " + ", ".join(f"**{esc(n)}** ({c} bài)" for n, c in leaders)

    # --- danh sách ---
    lines: list[str] = []
    last_album = None
    for r in chunk:
        album = r["info"]["album"].strip()
        if sort_mode == "album" and (not lines or album != last_album):
            lines.append(f"**💿 {esc(album) if album else 'Không rõ album'}**")
        last_album = album
        title = r["info"]["song"] or f"Bài {r['sid']}"
        lines.append(f"• **{esc(title)}** · `{r['score']:,}`")
        sub = f"🥇 {esc(r['nickname'])}"
        if r["time"]:
            sub += f" · {ts_tag(r['time'])}"
        if r["tied"] > 1:
            sub += f" · +{r['tied'] - 1} đồng điểm"
        lines.append(f"-# {sub}")

    embed = discord.Embed(
        title=f"{GAME_TITLE} - WR #1 của {artist} (Season {season})",
        description=f"{summary}\n\n" + "\n".join(lines),
        color=discord.Color.gold(),
    )
    embed.set_footer(text=f"Trang {page + 1}/{pages} · Sắp xếp: {TOP1_SORTS.get(sort_mode, sort_mode)}")
    embed.timestamp = max(r["updated"] for r in rows)
    return embed


class Top1View(SeasonMenuMixin, discord.ui.View):
    def __init__(self, owner_id: int, artist: str, rows: list[dict], missing: list[str], sort_mode: str) -> None:
        super().__init__(timeout=300)
        self.owner_id, self.artist, self.rows = owner_id, artist, rows
        self.missing, self.sort_mode = missing, sort_mode
        self.season = SEASON
        self.error: str | None = None
        self.page = 0
        self.pages = max(1, math.ceil(len(rows) / TOP1_PAGE_SIZE))
        self.message: discord.Message | None = None
        self._init_season_menu(select_row=0, nav_row=2)
        self._sync()

    def render(self) -> discord.Embed:
        if self.error:
            return discord.Embed(
                title=f"{GAME_TITLE} - WR #1 của {self.artist} (Season {self.season})",
                description=self.error,
                color=discord.Color.dark_grey(),
            )
        return build_top1_embed(
            self.artist, self.rows, self.missing, self.sort_mode, self.page, self.season
        )

    async def _change_season(self, interaction: discord.Interaction, season: int) -> None:
        await interaction.response.defer()
        self.season = season
        self.rows, self.missing = await collect_artist_top1(self.artist, season)
        self.error = None if self.rows else f"Không có dữ liệu World Record cho **{self.artist}** ở season này."
        if self.rows:
            sort_top1_rows(self.rows, self.sort_mode)
        self.page = 0
        self.pages = max(1, math.ceil(len(self.rows) / TOP1_PAGE_SIZE))
        self._sync()
        await interaction.edit_original_response(embed=self.render(), view=self)

    def _sync(self) -> None:
        self.prev_btn.disabled = self.page <= 0
        self.next_btn.disabled = self.page >= self.pages - 1

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message("Chỉ người dùng lệnh mới bấm được nút này.", ephemeral=True)
        return False

    async def _show(self, interaction: discord.Interaction) -> None:
        self._sync()
        await interaction.response.edit_message(embed=self.render(), view=self)

    @discord.ui.button(label="Trang trước", style=discord.ButtonStyle.primary, row=1)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = max(0, self.page - 1)
        await self._show(interaction)

    @discord.ui.button(label="Trang sau", style=discord.ButtonStyle.success, row=1)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = min(self.pages - 1, self.page + 1)
        await self._show(interaction)

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


async def build_top1_response(artist_query: str, sort_mode: str, user_id: int):
    """Trả về (text, embed, view). Nếu text khác None thì đó là thông báo lỗi/gợi ý."""
    artist, err = resolve_artist(artist_query)
    if err:
        return err, None, None
    rows, missing = await collect_artist_top1(artist)
    sort_mode = sort_mode if sort_mode in TOP1_SORTS else "album"
    sort_top1_rows(rows, sort_mode)
    view = Top1View(user_id, artist, rows, missing, sort_mode)
    if not rows:
        view.error = f"Chưa có dữ liệu World Record cho các bài của **{artist}** ở season này."
    return None, view.render(), view


@bot.tree.command(name="top1", description="Xem WR #1 của tất cả bài hát của một nghệ sĩ")
@app_commands.describe(artist="Nhóm / nghệ sĩ (chọn từ menu)", sort="Cách sắp xếp (mặc định: theo album)")
@app_commands.choices(sort=[app_commands.Choice(name=v, value=k) for k, v in TOP1_SORTS.items()])
@app_commands.autocomplete(artist=artist_autocomplete)
async def top1_slash(interaction: discord.Interaction, artist: str, sort: str = "album"):
    await interaction.response.defer()
    text, embed, view = await build_top1_response(artist, sort, interaction.user.id)
    if text:
        await interaction.followup.send(text, allowed_mentions=discord.AllowedMentions.none())
        return
    view.message = await interaction.followup.send(embed=embed, view=view, wait=True)


# Prefix: !top1 <nghệ sĩ> [--score | --name | --album]   ví dụ: !top1 itzy  |  !top1 got7 --score
@bot.command(name="top1", aliases=["artist_wr"])
async def top1_prefix(ctx: commands.Context, *, query: str = ""):
    sort_mode, words = "album", []
    for token in query.split():
        if token.lower() in ("--score", "--name", "--album"):
            sort_mode = token.lower()[2:]
        else:
            words.append(token)
    artist_query = " ".join(words)
    if not artist_query:
        await ctx.send(f"Cách dùng: `{PREFIX}top1 <nghệ sĩ> [--score | --name]`, ví dụ `{PREFIX}top1 itzy`")
        return
    async with ctx.typing():
        text, embed, view = await build_top1_response(artist_query, sort_mode, ctx.author.id)
    if text:
        await ctx.send(text, allowed_mentions=discord.AllowedMentions.none())
        return
    view.message = await ctx.send(embed=embed, view=view)


# ----------------------------- Quản lý bài hát trong Discord -----------------------------
