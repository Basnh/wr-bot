def _matcher(entry: dict):
    if entry.get("id") is not None:
        pid = entry["id"]
        return lambda p: p.get("objectID") == pid
    name = _norm(entry["nickname"])
    return lambda p: _norm(p.get("nickname")) == name


async def load_all(max_age: float = TRACK_CACHE_TTL, season: int = SEASON) -> dict[str, dict]:
    """Dữ liệu xếp hạng của mọi bài trong songs.json (dùng cache, bài nào hết hạn mới tải lại)."""
    ids = list(SONGS)
    entries = await asyncio.gather(*(
        wr.get(sid, max_age=max_age, season=season) for sid in ids
    ))
    return {sid: e for sid, e in zip(ids, entries) if e and e["data"]}


def find_players(entries: dict[str, dict], query: str) -> list[dict]:
    """Tìm người chơi theo objectID (nếu query là số) hoặc theo nickname (chính xác trước, rồi mới khớp một phần)."""
    q = query.strip()
    for exact in (True, False):
        players: dict[int, dict] = {}
        for e in entries.values():
            for p in e["data"]:
                pid, nick = p.get("objectID"), p.get("nickname", "")
                if q.isdigit():
                    ok = pid == int(q)
                elif exact:
                    ok = _norm(nick) == _norm(q)
                else:
                    ok = _norm(q) in _norm(nick)
                if not ok:
                    continue
                info = players.setdefault(pid, {"id": pid, "nickname": nick, "songs": 0, "_t": -1})
                info["songs"] += 1
                if p.get("updatedAt", 0) >= info["_t"]:  # lấy nickname mới nhất
                    info["_t"], info["nickname"] = p.get("updatedAt", 0), nick
        if players or q.isdigit():
            return sorted(players.values(), key=lambda x: -x["songs"])
    return []


def compute_player_wrs(entries: dict[str, dict], wanted: list[dict]) -> list[dict]:
    """Với mỗi người trong danh sách theo dõi: các bài họ đang giữ WR."""
    tops = {}
    for sid, e in entries.items():
        top = max(p["highscore"] for p in e["data"])
        tops[sid] = (top, sum(1 for p in e["data"] if p["highscore"] == top))

    results = []
    for w in wanted:
        match = _matcher(w)
        songs, name, seen_in = [], w["nickname"], 0
        for sid, e in entries.items():
            hits = [p for p in e["data"] if match(p)]
            if not hits:
                continue
            hit = max(hits, key=lambda p: p["highscore"])
            seen_in += 1
            name = hit["nickname"]  # nickname hiện tại
            top, tied = tops[sid]
            if hit["highscore"] == top:
                songs.append({
                    "sid": sid, "score": top, "tied": tied,
                    "rank": hit.get("rank"), "time": hit.get("updatedAt") or 0,
                    "artist": SONGS.get(sid, {}).get("artist") or "Không rõ nghệ sĩ",
                })
        songs.sort(key=lambda s: (
            s["artist"].casefold(),
            SONGS.get(s["sid"], {}).get("song", "").casefold(),
            int(s["sid"]) if s["sid"].isdigit() else 10**9,
        ))
        results.append({"entry": w, "name": name, "songs": songs, "seen_in": seen_in})
    results.sort(key=lambda r: (-len(r["songs"]), r["name"].casefold()))
    return results


def _song_line(s: dict) -> str:
    esc = discord.utils.escape_markdown
    song = SONGS.get(s["sid"])
    if song:
        title = song.get("song") or display(s["sid"])
    else:
        title = f"Bài {s['sid']}"
    if s.get("rank") is not None:  # hạng hiện tại của người này ở bài đó
        place = f"hạng **#{s['rank']}**"
        # if s["tied"] > 1:
            # place += f" (đồng điểm {s['tied']} người)" # Phần này comment để bỏ phần đồng hạng
    else:
        place = "một mình" if s["tied"] == 1 else f"đồng hạng với {s['tied'] - 1} người"
    when = f" · {ts_tag(s['time'])}" if s["time"] else ""
    return f"• **{esc(title)}** · `{s['score']:,}`{when} · {place}"


def build_track_pages(results: list[dict], artist_filter: str | None = None) -> list[str]:
    """Chia danh sách thành các trang theo số ký tự/số dòng; người nào bị cắt giữa chừng sẽ có tiêu đề '(tiếp)'."""
    esc = discord.utils.escape_markdown
    pages: list[str] = []
    cur: list[str] = []
    size = 0

    def flush() -> None:
        nonlocal cur, size
        if cur:
            pages.append("\n".join(cur))
        cur, size = [], 0

    def push(text: str, owner: str | None = None) -> None:
        nonlocal size
        if cur and (size + len(text) + 1 > TRACK_PAGE_CHARS or len(cur) >= TRACK_PAGE_LINES):
            flush()
            if owner:
                cont = f"{owner} *(tiếp)*"
                cur.append(cont)
                size += len(cont) + 1
        cur.append(text)
        size += len(text) + 1

    def start_block(header: str, first_line: str) -> None:
        nonlocal size
        if cur and (size + len(header) + len(first_line) + 2 > TRACK_PAGE_CHARS or len(cur) + 2 > TRACK_PAGE_LINES):
            flush()
        cur.append(header)
        size += len(header) + 1

    visible = []
    for result in results:
        songs = result["songs"]
        if artist_filter is not None:
            songs = [s for s in songs if s["artist"].casefold() == artist_filter.casefold()]
        visible.append({**result, "songs": songs})

    for r in (r for r in visible if r["songs"]):
        header = f"### 👤 {esc(r['name'])} — **{len(r['songs'])} WR**"
        grouped: dict[str, list[dict]] = {}
        for song in r["songs"]:
            grouped.setdefault(song["artist"], []).append(song)
        first_artist = next(iter(grouped))
        start_block(header, f"## 🎤 {esc(first_artist)}")
        for artist, songs in grouped.items():
            push(f"## 🎤 {esc(artist)}", owner=header)
            for song in songs:
                push(_song_line(song), owner=header)

    idle = [r for r in visible if not r["songs"]] if artist_filter is None else []
    if idle:
        names = [
            esc(r["name"]) + (f" ({r['seen_in']} bài trong top 100)" if r["seen_in"] else " (chưa vào top 100)")
            for r in idle
        ]
        chunks, chunk = [], ""
        for n in names:
            if chunk and len(chunk) + len(n) + 2 > 1500:
                chunks.append(chunk)
                chunk = ""
            chunk = f"{chunk}, {n}" if chunk else n
        chunks.append(chunk)
        header = "### 💤 Chưa có WR"
        start_block(header, chunks[0])
        for c in chunks:
            push(c)
    flush()
    if pages:
        return pages
    if artist_filter is not None:
        return [f"Không có WR thuộc nghệ sĩ **{esc(artist_filter)}**."]
    return ["Chưa có dữ liệu."]


def build_track_embed(
    results: list[dict],
    pages: list[str],
    updated: datetime,
    page: int,
    artist_filter: str | None = None,
    season: int = SEASON,
) -> discord.Embed:
    total_wr = sum(len(r["songs"]) for r in results)
    distinct = len({s["sid"] for r in results for s in r["songs"]})
    summary = f"👥 **{len(results)}** người · 🏆 **{total_wr} WR** · 🎵 **{distinct}** bài khác nhau"
    if artist_filter is not None:
        summary += f"`\n🎤 Nghệ sĩ: **{discord.utils.escape_markdown(artist_filter)}**`"
    embed = discord.Embed(
        title=f"{GAME_TITLE} - Player Tracker (Season {season})",
        description=f"{summary}\n\n{pages[page]}",
        color=discord.Color.gold(),
    )
    embed.set_footer(text=f"Trang {page + 1}/{len(pages)} · Dữ liệu cập nhật")
    embed.timestamp = updated
    return embed


TRACK_ALL_ARTISTS = "__all_artists__"
TRACK_ARTISTS_PER_PAGE = 24


class TrackArtistSelect(discord.ui.Select):
    def __init__(self, view: "TrackResultsView") -> None:
        self.track_view = view
        options = [
            discord.SelectOption(
                label="Tất cả nghệ sĩ",
                value=TRACK_ALL_ARTISTS,
                emoji="🎤",
                default=view.artist_filter is None,
            )
        ]
        start = view.artist_page * TRACK_ARTISTS_PER_PAGE
        for index, artist in enumerate(
            view.artists[start:start + TRACK_ARTISTS_PER_PAGE], start=start
        ):
            options.append(discord.SelectOption(
                label=artist[:100],
                value=f"artist:{index}",
                default=(
                    view.artist_filter is not None
                    and artist.casefold() == view.artist_filter.casefold()
                ),
            ))
        super().__init__(
            placeholder="Lọc kết quả theo nghệ sĩ...",
            min_values=1,
            max_values=1,
            options=options,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        selected = self.values[0]
        if selected == TRACK_ALL_ARTISTS:
            self.track_view.artist_filter = None
        else:
            artist_index = int(selected.split(":", 1)[1])
            self.track_view.artist_filter = self.track_view.artists[artist_index]
        self.track_view.page = 0
        self.track_view._update_artist_select()
        await self.track_view._show(interaction)


class TrackResultsView(SeasonMenuMixin, discord.ui.View):
    """Phân trang kết quả WR và lọc chúng theo nghệ sĩ."""

    def __init__(
        self, owner_id: int, results: list[dict], updated: datetime,
        season: int = SEASON, wanted: list[dict] | None = None,
    ) -> None:
        super().__init__(timeout=300)
        self.owner_id = owner_id
        self.results = results
        self.wanted = wanted if wanted is not None else [r["entry"] for r in results]
        self.updated = updated
        self.season = season
        self.error: str | None = None
        self.artists = sorted(
            {song["artist"] for result in results for song in result["songs"]},
            key=str.casefold,
        )
        self.artist_filter: str | None = None
        self.artist_page = 0
        self.page = 0
        self.message: discord.Message | None = None
        self.artist_select = TrackArtistSelect(self)
        self.add_item(self.artist_select)
        self._init_season_menu(select_row=3, nav_row=4)
        self._sync()

    def _sync(self) -> None:
        pages = self._pages()
        self.prev_btn.disabled = self.page <= 0
        self.next_btn.disabled = self.page >= len(pages) - 1
        artist_pages = max(1, math.ceil(len(self.artists) / TRACK_ARTISTS_PER_PAGE))
        self.prev_artist_btn.disabled = self.artist_page <= 0
        self.next_artist_btn.disabled = self.artist_page >= artist_pages - 1
        self.prev_artist_btn.label = f"Nghệ sĩ trước ({self.artist_page + 1}/{artist_pages})"
        self.next_artist_btn.label = f"Nghệ sĩ sau ({self.artist_page + 1}/{artist_pages})"

    def _filtered_results(self) -> list[dict]:
        if self.artist_filter is None:
            return self.results
        return [
            {**result, "songs": [
                song for song in result["songs"]
                if song["artist"].casefold() == self.artist_filter.casefold()
            ]}
            for result in self.results
            if any(
                song["artist"].casefold() == self.artist_filter.casefold()
                for song in result["songs"]
            )
        ]

    def _pages(self) -> list[str]:
        return build_track_pages(self._filtered_results(), self.artist_filter)

    def _embed(self) -> discord.Embed:
        if self.error:
            return discord.Embed(
                title=f"{GAME_TITLE} - Player Tracker (Season {self.season})",
                description=self.error,
                color=discord.Color.dark_grey(),
            )
        pages = self._pages()
        self.page = min(self.page, len(pages) - 1)
        return build_track_embed(
            self._filtered_results(), pages, self.updated, self.page, self.artist_filter, self.season
        )

    async def _change_season(self, interaction: discord.Interaction, season: int) -> None:
        await interaction.response.defer()
        self.season = season
        entries = await load_all(season=season)
        self.error = None
        if not entries:
            self.results = []
            self.error = f"Không có dữ liệu World Record cho season {season}."
            self.artists = []
            self.artist_filter = None
        else:
            self.results = compute_player_wrs(entries, self.wanted)
            self.updated = max(e["updated"] for e in entries.values())
            self.artists = sorted(
                {song["artist"] for result in self.results for song in result["songs"]},
                key=str.casefold,
            )
            if self.artist_filter and self.artist_filter.casefold() not in {
                artist.casefold() for artist in self.artists
            }:
                self.artist_filter = None
        self.artist_page = 0
        self.page = 0
        self._update_artist_select()
        self._sync()
        await interaction.edit_original_response(embed=self._embed(), view=self)

    def _update_artist_select(self) -> None:
        self.remove_item(self.artist_select)
        self.artist_select = TrackArtistSelect(self)
        self.add_item(self.artist_select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Chỉ người dùng lệnh mới bấm được nút này.", ephemeral=True)
            return False
        return True

    async def _show(self, interaction: discord.Interaction) -> None:
        self._sync()
        await interaction.response.edit_message(embed=self._embed(), view=self)

    @discord.ui.button(label="Trang trước", style=discord.ButtonStyle.primary, row=1)
    async def prev_btn(self, interaction: discord.Interaction, _button: discord.ui.Button):
        self.page -= 1
        await self._show(interaction)

    @discord.ui.button(label="Trang sau", style=discord.ButtonStyle.success, row=1)
    async def next_btn(self, interaction: discord.Interaction, _button: discord.ui.Button):
        self.page += 1
        await self._show(interaction)

    @discord.ui.button(label="Nghệ sĩ trước", style=discord.ButtonStyle.secondary, row=2)
    async def prev_artist_btn(self, interaction: discord.Interaction, _button: discord.ui.Button):
        self.artist_page -= 1
        self._update_artist_select()
        await self._show(interaction)

    @discord.ui.button(label="Nghệ sĩ sau", style=discord.ButtonStyle.secondary, row=2)
    async def next_artist_btn(self, interaction: discord.Interaction, _button: discord.ui.Button):
        self.artist_page += 1
        self._update_artist_select()
        await self._show(interaction)

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


async def build_track_response(
    query: str | None, user_id: int, selected_player: dict | None = None
):
    """Trả về (text, embed, view). Nếu text khác None thì đó là thông báo lỗi/gợi ý."""
    if not PLAYERS:
        return (
            "Danh sách theo dõi đang trống. Mở file `players.txt` và ghi mỗi dòng một tên, "
            f"hoặc dùng `{PREFIX}track add <tên hoặc ID>`.",
            None,
            None,
        )
    entries = await load_all()
    if not entries:
        targets = [selected_player] if selected_player is not None else list(PLAYERS)
        q = _norm(query)
        if q and selected_player is None:
            def matches_target(entry: dict) -> bool:
                nickname = _norm(entry["nickname"])
                if q.isdigit():
                    return entry.get("id") == int(q) or q in nickname
                return q in nickname

            targets = [entry for entry in targets if matches_target(entry)]
            if not targets:
                return f"Không có ai khớp `{query}` trong danh sách theo dõi.", None, None
        placeholders = [
            {"entry": entry, "name": entry["nickname"], "songs": [], "seen_in": 0}
            for entry in targets
        ]
        view = TrackResultsView(
            user_id, placeholders, datetime.now(timezone.utc), wanted=targets
        )
        view.error = "Chưa tải được dữ liệu World Record cho season hiện tại. Hãy chọn season khác bên dưới."
        return None, view._embed(), view

    results = compute_player_wrs(entries, PLAYERS)
    q = _norm(query)
    if selected_player is not None:
        selected_key = player_key(selected_player)
        results = [r for r in results if player_key(r["entry"]) == selected_key]
        if not results:
            return "Người chơi này không còn trong danh sách theo dõi.", None, None
    elif q:
        def matches(r: dict) -> bool:
            e = r["entry"]
            if q.isdigit():  # số -> khớp objectID (hoặc một nickname chỉ gồm số), không khớp một phần
                return e.get("id") == int(q) or q in (_norm(r["name"]), _norm(e["nickname"]))
            return q in _norm(r["name"]) or q in _norm(e["nickname"])

        results = [r for r in results if matches(r)]
        if not results:
            return f"Không có ai khớp `{query}` trong danh sách theo dõi.", None, None

    updated = max(e["updated"] for e in entries.values())
    view = TrackResultsView(user_id, results, updated)
    return None, view._embed(), view


# ---------- Quản lý danh sách (ghi vào players.txt, giữ nguyên ghi chú) ----------
async def track_add(query: str) -> str:
    q = query.strip()
    found = find_players(await load_all(), q)
    if len(found) > 1:
        lines = "\n".join(f"`{p['id']}` - {p['nickname']} ({p['songs']} bài)" for p in found[:10])
        return f"Có {len(found)} người chơi khớp, hãy chạy lại với ID:\n{lines}"

    note = ""
    if found:
        entry = {"id": found[0]["id"], "nickname": found[0]["nickname"]}
    elif q.isdigit():
        entry = {"id": int(q), "nickname": f"ID {int(q)}"}
        note = " (chưa thấy ID này trong top 100 của bài nào)"
    else:
        entry = {"nickname": q}
        note = " (chưa thấy trong top 100 của bài nào, sẽ khớp theo tên)"

    for e in PLAYERS:
        if player_key(e) == player_key(entry):
            return f"**{entry['nickname']}** đã có trong danh sách rồi."
        if entry.get("id") is not None and e.get("id") is None and _norm(e["nickname"]) == _norm(entry["nickname"]):
            # dòng cũ chỉ có tên -> nâng lên "tên | ID" tại chỗ
            rewrite_players_file(
                lambda line, parsed: player_line(entry)
                if parsed and parsed.get("id") is None and _norm(parsed["nickname"]) == _norm(entry["nickname"])
                else line
            )
            reload_players()
            return f"Đã gắn ID `{entry['id']}` cho **{entry['nickname']}** (đã có trong danh sách)."

    append_player(entry)
    reload_players()
    return f"✅ Đã thêm **{entry['nickname']}** vào danh sách theo dõi{note}"


def track_remove(query: str) -> str:
    q = _norm(query)
    hit = [e for e in PLAYERS if (q.isdigit() and e.get("id") == int(q)) or _norm(e["nickname"]) == q]
    if not hit:
        hit = [e for e in PLAYERS if q in _norm(e["nickname"])]
    if not hit:
        return "Không thấy người này trong danh sách."
    if len(hit) > 1:
        return "Có nhiều người khớp: " + ", ".join(e["nickname"] for e in hit) + ". Hãy gõ chính xác hơn hoặc dùng ID."
    target = hit[0]
    rewrite_players_file(
        lambda line, parsed: None if parsed and player_key(parsed) == player_key(target) else line
    )
    reload_players()
    return f"Đã xóa **{target['nickname']}** khỏi danh sách theo dõi."


# ---------- Slash command: /track (menu chọn người trong players.txt) ----------
TRACK_ALL = "__all__"  # giá trị của mục "Tất cả" trong menu


async def player_autocomplete(interaction: discord.Interaction, current: str):
    """Menu của option `player`: mục "Tất cả" rồi từng người trong players.txt."""
    cur = _norm(current)
    choices = []
    if not cur or cur in "tất cả" or cur in "all":
        choices.append(app_commands.Choice(name=f"Tất cả ({len(PLAYERS)} người)", value=TRACK_ALL))
    for e in PLAYERS:
        if cur and cur not in _norm(e["nickname"]) and cur not in str(e.get("id", "")):
            continue
        value = str(e["id"]) if e.get("id") is not None else e["nickname"]
        choices.append(app_commands.Choice(name=e["nickname"][:100], value=value[:100]))
    return choices[:25]


@bot.tree.command(name="track", description="Xem người chơi trong danh sách theo dõi đang giữ WR ở những bài nào")
@app_commands.describe(player="Chọn Tất cả hoặc một người trong danh sách theo dõi")
@app_commands.autocomplete(player=player_autocomplete)
async def track_slash(interaction: discord.Interaction, player: str):
    await interaction.response.defer()
    query = None if player == TRACK_ALL else player
    text, embed, view = await build_track_response(query, interaction.user.id)
    if text:
        await interaction.followup.send(text, allowed_mentions=NO_PING)
        return
    view.message = await interaction.followup.send(embed=embed, view=view, wait=True)


TRACK_MENU_PAGE_SIZE = 24


class TrackPlayerSelect(discord.ui.Select):
    def __init__(self, menu: "TrackPlayerMenu") -> None:
        self.menu = menu
        options = [
            discord.SelectOption(
                label=f"Tất cả ({len(menu.players)} người)"[:100],
                value=TRACK_ALL,
                emoji="👥",
            )
        ]
        start = menu.page * TRACK_MENU_PAGE_SIZE
        for index, player in enumerate(
            menu.players[start:start + TRACK_MENU_PAGE_SIZE], start=start
        ):
            label = player["nickname"]
            description = f"ID {player['id']}" if player.get("id") is not None else None
            options.append(
                discord.SelectOption(
                    label=label[:100],
                    value=f"player:{index}",
                    description=description[:100] if description else None,
                    emoji="👤",
                )
            )
        super().__init__(
            placeholder="Chọn tất cả hoặc một người chơi...",
            min_values=1,
            max_values=1,
            options=options,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        selected = self.values[0]
        query = None
        selected_player = None
        if selected != TRACK_ALL:
            selected_player = self.menu.players[int(selected.split(":", 1)[1])]

        text, embed, result_view = await build_track_response(
            query, interaction.user.id, selected_player
        )
        if text:
            await interaction.edit_original_response(content=text, embed=None, view=None)
            return
        await interaction.edit_original_response(content=None, embed=embed, view=result_view)
        result_view.message = interaction.message


class TrackPlayerMenu(discord.ui.View):
    def __init__(self, owner_id: int) -> None:
        super().__init__(timeout=180)
        self.owner_id = owner_id
        self.players = list(PLAYERS)
        self.page = 0
        self.pages = max(1, math.ceil(len(self.players) / TRACK_MENU_PAGE_SIZE))
        self.message: discord.Message | None = None
        self.player_select = TrackPlayerSelect(self)
        self.add_item(self.player_select)
        self._sync()

    def _sync(self) -> None:
        self.prev_btn.disabled = self.page <= 0
        self.next_btn.disabled = self.page >= self.pages - 1

    def _update_select(self) -> None:
        self.remove_item(self.player_select)
        self.player_select = TrackPlayerSelect(self)
        self.add_item(self.player_select)

    def _embed(self) -> discord.Embed:
        embed = discord.Embed(
            title="👤 Chọn người chơi cần theo dõi",
            description=(
                f"Danh sách có **{len(self.players)}** người. Chọn **Tất cả** hoặc một người "
                "trong menu bên dưới."
            ),
            color=discord.Color.blurple(),
        )
        if self.pages > 1:
            embed.set_footer(text=f"Trang {self.page + 1}/{self.pages} · Dùng nút để chuyển trang")
        return embed

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message(
            "Menu này thuộc về người đã gọi lệnh.",
            ephemeral=True,
        )
        return False

    @discord.ui.button(label="Trang trước", style=discord.ButtonStyle.secondary, row=1)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page -= 1
        self._sync()
        self._update_select()
        await interaction.response.edit_message(embed=self._embed(), view=self)

    @discord.ui.button(label="Trang sau", style=discord.ButtonStyle.secondary, row=1)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page += 1
        self._sync()
        self._update_select()
        await interaction.response.edit_message(embed=self._embed(), view=self)

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


# ---------- Prefix: !track ... ----------
@bot.group(name="track", invoke_without_command=True)
async def track_group(ctx: commands.Context, *, name: str = ""):
    """!track [tên] -> mở menu chọn người hoặc tra cứu trực tiếp theo tên."""
    if not name.strip():
        if not PLAYERS:
            await ctx.send(
                f"Danh sách theo dõi đang trống. Dùng `{PREFIX}track add <tên hoặc ID>` để thêm.",
                allowed_mentions=NO_PING,
            )
            return
        view = TrackPlayerMenu(ctx.author.id)
        view.message = await ctx.send(embed=view._embed(), view=view)
        return
    async with ctx.typing():
        text, embed, view = await build_track_response(name, ctx.author.id)
    if text:
        await ctx.send(text, allowed_mentions=NO_PING)
        return
    view.message = await ctx.send(embed=embed, view=view)


@track_group.command(name="list")
async def track_list(ctx: commands.Context):
    """!track list -> danh sách đang theo dõi."""
    if not PLAYERS:
        await ctx.send("Danh sách theo dõi đang trống.")
        return
    lines = []
    for i, e in enumerate(PLAYERS, 1):
        extra = f" (`{e['id']}`)" if e.get("id") is not None and not e["nickname"].startswith("ID ") else ""
        lines.append(f"{i}. {e['nickname']}{extra}")
    text = "\n".join(lines)
    await ctx.send(text[:1900] + ("…" if len(text) > 1900 else ""), allowed_mentions=NO_PING)


@track_group.command(name="add")
@track_manager
async def track_add_cmd(ctx: commands.Context, *, player: str):
    """!track add <nickname hoặc ID>"""
    async with ctx.typing():
        msg = await track_add(player)
    await ctx.send(msg, allowed_mentions=NO_PING)


@track_group.command(name="remove", aliases=["rm"])
@track_manager
async def track_remove_cmd(ctx: commands.Context, *, player: str):
    """!track remove <nickname hoặc ID>"""
    await ctx.send(track_remove(player), allowed_mentions=NO_PING)


# ----------------------------- Reload songs.json -----------------------------
@bot.command(name="reload")
@commands.is_owner()
async def reload_cmd(ctx: commands.Context):
    ok, msg = reload_all()
    await ctx.send(("✅ " if ok else "⚠️ ") + msg)


