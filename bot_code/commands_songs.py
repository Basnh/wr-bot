songs_group = app_commands.Group(name="songs", description="Tra cứu và quản lý danh sách bài hát")


def _song_id(value: str) -> str:
    try:
        song_id = int(value)
    except (TypeError, ValueError):
        raise ValueError("ID bài hát phải là số nguyên.") from None
    if song_id < 0:
        raise ValueError("ID bài hát phải là số không âm.")
    return str(song_id)


def _song_sort_key(item: tuple[str, dict]) -> tuple[int, int | str]:
    sid = item[0]
    return (0, int(sid)) if sid.isdigit() else (1, sid.lower())


def _artist_lines() -> list[str]:
    counts: dict[str, int] = {}
    for song in songs_tool.load().values():
        artist = song["artist"] or "(không rõ)"
        counts[artist] = counts.get(artist, 0) + 1
    rows = [f"{discord.utils.escape_mentions(name)}: {count} bài"
            for name, count in sorted(counts.items(), key=lambda item: item[0].lower())]
    return rows or ["Chưa có bài hát nào."]


def _search_song_lines(query: str) -> list[str]:
    tokens = query.lower().split()
    rows = []
    for sid, song in sorted(songs_tool.load().items(), key=_song_sort_key):
        haystack = f"{song['artist']} {song['song']} {song['album']}".lower()
        if all(token in haystack for token in tokens):
            details = " | ".join(
                value for value in (song["artist"], song["song"], song["album"]) if value
            )
            rows.append(f"`{sid}` | {discord.utils.escape_mentions(details)}")
    return rows or ["Không tìm thấy bài hát nào khớp."]


async def _send_song_lines(target, lines: list[str]) -> None:
    chunks: list[str] = []
    current = ""
    for line in lines:
        while len(line) > 1800:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:1800])
            line = line[1800:]
        if current and len(current) + len(line) + 1 > 1800:
            chunks.append(current)
            current = ""
        current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)

    for chunk in chunks:
        if isinstance(target, discord.Interaction):
            if target.response.is_done():
                await target.followup.send(chunk, allowed_mentions=discord.AllowedMentions.none())
            else:
                await target.response.send_message(
                    chunk, allowed_mentions=discord.AllowedMentions.none()
                )
        else:
            await target.send(chunk, allowed_mentions=discord.AllowedMentions.none())


SONG_LIST_PAGE_SIZE = 12


def _song_album_sort_key(item: tuple[str, dict]) -> tuple:
    album = item[1]["album"].strip()
    artist = item[1]["artist"].strip()
    return (
        not bool(album),
        album.casefold(),
        artist.casefold(),
        _song_sort_key(item),
    )


def _song_list_embed(
    entries: list[tuple[str, dict]], artist: str | None, page: int
) -> discord.Embed:
    pages = max(1, math.ceil(len(entries) / SONG_LIST_PAGE_SIZE))
    start = page * SONG_LIST_PAGE_SIZE
    current = entries[start:start + SONG_LIST_PAGE_SIZE]
    esc = discord.utils.escape_markdown
    safe = lambda value: esc(discord.utils.escape_mentions(value))
    album_count = len({
        (song["artist"].casefold(), song["album"].casefold())
        for _, song in entries
        if song["album"].strip()
    })
    artist_label = safe(artist)[:80] if artist else "Tất cả nghệ sĩ"
    lines = [
        f"**{artist_label}**  ·  **{len(entries)}** bài hát  ·  **{album_count}** album",
        "────────────────────────",
    ]
    previous_group: tuple[str, str] | None = None
    for sid, song in current:
        group = (
            song["artist"].strip().casefold(),
            song["album"].strip().casefold(),
        )
        if group != previous_group:
            album_title = safe(song["album"].strip() or "Chưa xếp album")[:85]
            if not artist and song["artist"]:
                album_title = f"{safe(song['artist'])[:65]}  ·  {album_title}"
            lines.append(f"\n💿  **{album_title}**")
            previous_group = group

        title = safe(song["song"] or "(chưa rõ tên)")[:100]
        lines.append(f"> `{sid}`  ·  {title}")

    if not entries:
        lines.append("\n*Không tìm thấy bài hát nào.*")

    embed = discord.Embed(
        title="🎵 Danh sách bài hát",
        description="\n".join(lines),
        color=discord.Color.blurple(),
    )
    embed.set_author(name="SUPERSTAR JYP  •  THƯ VIỆN ÂM NHẠC")
    embed.set_footer(
        text=f"{len(entries)} bài hát  ·  {album_count} album  •  Trang {page + 1}/{pages}"
    )
    return embed


class SongListView(discord.ui.View):
    def __init__(self, owner_id: int, entries: list[tuple[str, dict]], artist: str | None):
        super().__init__(timeout=300)
        self.owner_id = owner_id
        self.entries = entries
        self.artist = artist
        self.page = 0
        self.pages = max(1, math.ceil(len(entries) / SONG_LIST_PAGE_SIZE))
        self.message: discord.Message | None = None
        self._sync()

    def _sync(self) -> None:
        self.prev_btn.disabled = self.page <= 0
        self.next_btn.disabled = self.page >= self.pages - 1

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message(
            "Chỉ người đã mở danh sách mới có thể chuyển trang.",
            ephemeral=True,
        )
        return False

    async def _show(self, interaction: discord.Interaction) -> None:
        self._sync()
        await interaction.response.edit_message(
            embed=_song_list_embed(self.entries, self.artist, self.page),
            view=self,
        )

    @discord.ui.button(label="Trang trước", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page -= 1
        await self._show(interaction)

    @discord.ui.button(label="Trang sau", style=discord.ButtonStyle.primary)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page += 1
        await self._show(interaction)

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


async def _send_song_list(target, artist: str | None = None) -> None:
    songs = songs_tool.load()
    entries = sorted(
        (
            (sid, song)
            for sid, song in songs.items()
            if not artist or artist.casefold() in song["artist"].casefold()
        ),
        key=_song_album_sort_key,
    )
    owner_id = target.user.id if isinstance(target, discord.Interaction) else target.author.id
    view = SongListView(owner_id, entries, artist)
    embed = _song_list_embed(entries, artist, 0)
    if isinstance(target, discord.Interaction):
        await target.response.send_message(
            embed=embed, view=view, allowed_mentions=discord.AllowedMentions.none()
        )
        view.message = await target.original_response()
    else:
        view.message = await target.send(
            embed=embed, view=view, allowed_mentions=discord.AllowedMentions.none()
        )


async def _add_song_record(song_id: str, artist: str, song: str, album: str | None) -> str:
    sid = _song_id(song_id)
    artist, song = artist.strip(), song.strip()
    if not artist or not song:
        raise ValueError("Nhóm/nghệ sĩ và tên bài hát không được để trống.")
    songs = songs_tool.load()
    old = songs.get(sid)
    new = {
        "artist": artist,
        "song": song,
        "album": (album if album is not None else old["album"] if old else "").strip(),
        "cover": old["cover"] if old else "",
    }
    if old == new:
        return f"ID {sid} đã đúng như vậy, không có gì thay đổi."
    songs[sid] = new
    songs_tool.save(songs)
    ok, message = reload_songs()
    if not ok:
        return f"Đã ghi ID {sid}, nhưng không tải lại được dữ liệu: {message}"
    action = "cập nhật" if old else "thêm"
    return f"Đã {action} ID {sid}: {songs_tool.label(new)} [{new['album']}]"


async def _remove_song_record(song_id: str) -> str:
    sid = _song_id(song_id)
    songs = songs_tool.load()
    if sid not in songs:
        raise ValueError(f"Không có ID {sid} trong danh sách.")
    removed = songs.pop(sid)
    songs_tool.save(songs)
    ok, message = reload_songs()
    if not ok:
        return f"Đã xóa ID {sid}, nhưng không tải lại được dữ liệu: {message}"
    return f"Đã xóa ID {sid}: {songs_tool.label(removed)}"


def _merge_songs_from_sheet(
    rows: list[list[str]], overwrite: bool = False, dry_run: bool = False
) -> list[str]:
    incoming = songs_tool.parse_sheet(rows)
    songs = songs_tool.load()
    added = [sid for sid in incoming if sid not in songs]
    updated = [
        sid
        for sid, song in incoming.items()
        if sid in songs
        and (
            (overwrite and songs[sid] != song)
            or (not overwrite and song["cover"] and songs[sid]["cover"] != song["cover"])
        )
    ]

    output = []
    for ids, title in ((added, "Bài mới"), (updated, "Bài sẽ được cập nhật")):
        if ids:
            output.append(f"{title}: {len(ids)} bài")
            output.extend(
                f"`{sid}` | {discord.utils.escape_mentions(songs_tool.label(incoming[sid]))}"
                for sid in sorted(ids, key=int)[:15]
            )
            if len(ids) > 15:
                output.append(f"... và {len(ids) - 15} bài khác")
    if not added and not updated:
        return ["Không có gì mới để đồng bộ."]
    if dry_run:
        output.append("Dry-run: chưa ghi file.")
        return output

    for sid in added + updated:
        if sid in added or overwrite:
            songs[sid] = incoming[sid]
        else:
            songs[sid]["cover"] = incoming[sid]["cover"]
    songs_tool.save(songs)
    ok, message = reload_songs()
    output.append(f"Đã ghi songs.json: thêm {len(added)}, cập nhật {len(updated)}.")
    if not ok:
        output.append(f"⚠️ {message}")
    return output


async def _sync_songs(csv_text: str | None, overwrite: bool, dry_run: bool) -> list[str]:
    if csv_text is None:
        rows = await asyncio.to_thread(songs_tool.read_sheet_rows, None)
    else:
        rows = songs_tool.parse_sheet_text(csv_text)
    return await asyncio.to_thread(_merge_songs_from_sheet, rows, overwrite, dry_run)


async def _slash_owner_only(interaction: discord.Interaction) -> bool:
    if await bot.is_owner(interaction.user):
        return True
    await interaction.response.send_message(
        "Chỉ chủ bot mới dùng được thao tác sửa, xóa hoặc đồng bộ.",
        ephemeral=True,
    )
    return False


@songs_group.command(name="add", description="Thêm bài hát hoặc cập nhật bài đã có")
@app_commands.describe(
    song_id="ID bài hát trong game",
    artist="Chọn hoặc nhập nhóm / nghệ sĩ",
    song="Tên bài hát",
    album="Chọn hoặc nhập album; bỏ trống khi sửa để giữ album hiện tại",
)
@app_commands.autocomplete(artist=artist_autocomplete, album=album_autocomplete)
async def songs_slash_add(
    interaction: discord.Interaction,
    song_id: str,
    artist: str,
    song: str,
    album: str | None = None,
):
    if not await _slash_owner_only(interaction):
        return
    try:
        result = await _add_song_record(song_id, artist, song, album)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        await interaction.response.send_message(
            f"⚠️ {error}", allowed_mentions=discord.AllowedMentions.none()
        )
        return
    await interaction.response.send_message(
        result, allowed_mentions=discord.AllowedMentions.none()
    )


@songs_group.command(name="remove", description="Xóa bài hát theo ID")
async def songs_slash_remove(interaction: discord.Interaction, song_id: str):
    if not await _slash_owner_only(interaction):
        return
    try:
        result = await _remove_song_record(song_id)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        await interaction.response.send_message(f"⚠️ {error}", ephemeral=True)
        return
    await interaction.response.send_message(
        result, ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
    )


@songs_group.command(name="list", description="Liệt kê bài hát, có thể lọc theo nghệ sĩ")
async def songs_slash_list(interaction: discord.Interaction, artist: str | None = None):
    await _send_song_list(interaction, artist)


@songs_group.command(name="artists", description="Liệt kê nghệ sĩ kèm số bài")
async def songs_slash_artists(interaction: discord.Interaction):
    await _send_song_lines(interaction, _artist_lines())


@songs_group.command(name="search", description="Tìm bài theo từ khóa")
async def songs_slash_search(interaction: discord.Interaction, keywords: str):
    if not keywords.strip():
        await interaction.response.send_message("Nhập từ khóa để tìm bài hát.", ephemeral=True)
        return
    await _send_song_lines(interaction, _search_song_lines(keywords))


@songs_group.command(name="sync", description="Đồng bộ bài hát từ Google Sheets hoặc CSV")
@app_commands.describe(
    overwrite="Cập nhật cả bài đã tồn tại theo dữ liệu nguồn",
    dry_run="Chỉ xem trước, không ghi songs.json",
    csv_file="CSV tùy chọn; bỏ trống để đọc Google Sheets",
)
async def songs_slash_sync(
    interaction: discord.Interaction,
    overwrite: bool = False,
    dry_run: bool = False,
    csv_file: discord.Attachment | None = None,
):
    if not await _slash_owner_only(interaction):
        return
    if csv_file and not csv_file.filename.lower().endswith(".csv"):
        await interaction.response.send_message("Tệp đính kèm phải có đuôi .csv.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        csv_text = None
        if csv_file:
            csv_text = (await csv_file.read()).decode("utf-8-sig")
        lines = await _sync_songs(csv_text, overwrite, dry_run)
    except (OSError, ValueError, json.JSONDecodeError, discord.HTTPException) as error:
        await interaction.followup.send(f"⚠️ Không thể đồng bộ: {error}", ephemeral=True)
        return
    await _send_song_lines(interaction, lines)


bot.tree.add_command(songs_group)


@bot.group(name="songs", invoke_without_command=True)
async def songs_prefix(ctx: commands.Context):
    await ctx.send(
        f"Các lệnh: `{PREFIX}songs add`, `remove`, `list`, `artists`, `search`, `sync`.\n"
        f"Ví dụ: `{PREFIX}songs search hard carry` hoặc `{PREFIX}songs list ITZY`."
    )


@songs_prefix.command(name="add")
@commands.is_owner()
async def songs_prefix_add(ctx: commands.Context, *, arguments: str = ""):
    try:
        tokens = shlex.split(arguments)
        album = None
        if "--album" in tokens:
            index = tokens.index("--album")
            if len(tokens[index:]) != 2:
                raise ValueError("Dùng --album \"tên album\".")
            album = tokens[index + 1]
            tokens = tokens[:index]
        if len(tokens) != 3:
            raise ValueError(
                f'Dùng: `{PREFIX}songs add <ID> "<nghệ sĩ>" "<bài hát>" '
                '[--album "tên album"]'
            )
        result = await _add_song_record(tokens[0], tokens[1], tokens[2], album)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        await ctx.send(f"⚠️ {error}", allowed_mentions=discord.AllowedMentions.none())
        return
    await ctx.send(result, allowed_mentions=discord.AllowedMentions.none())


@songs_prefix.command(name="remove", aliases=["rm"])
@commands.is_owner()
async def songs_prefix_remove(ctx: commands.Context, *, song_id: str = ""):
    try:
        result = await _remove_song_record(song_id.strip())
    except (OSError, ValueError, json.JSONDecodeError) as error:
        await ctx.send(f"⚠️ {error}", allowed_mentions=discord.AllowedMentions.none())
        return
    await ctx.send(result, allowed_mentions=discord.AllowedMentions.none())


@songs_prefix.command(name="list")
async def songs_prefix_list(ctx: commands.Context, *, artist: str = ""):
    await _send_song_list(ctx, artist.strip() or None)


@songs_prefix.command(name="artists")
async def songs_prefix_artists(ctx: commands.Context):
    await _send_song_lines(ctx, _artist_lines())


@songs_prefix.command(name="search")
async def songs_prefix_search(ctx: commands.Context, *, keywords: str = ""):
    if not keywords.strip():
        await ctx.send(f"Cách dùng: `{PREFIX}songs search <từ khóa>`")
        return
    await _send_song_lines(ctx, _search_song_lines(keywords))


@songs_prefix.command(name="sync")
@commands.is_owner()
async def songs_prefix_sync(ctx: commands.Context, *, arguments: str = ""):
    try:
        tokens = shlex.split(arguments)
        overwrite = "--overwrite" in tokens
        dry_run = "--dry-run" in tokens
        csv_path = None
        if "--csv" in tokens:
            index = tokens.index("--csv")
            if index + 1 >= len(tokens):
                raise ValueError("Dùng --csv <file.csv>.")
            csv_path = tokens[index + 1]
            del tokens[index:index + 2]
        if any(token not in ("--overwrite", "--dry-run") for token in tokens):
            raise ValueError("Tùy chọn hợp lệ: --overwrite, --dry-run, --csv <file.csv>.")
        if csv_path and not os.path.isabs(csv_path):
            csv_path = os.path.join(BASE_DIR, csv_path)
        await ctx.typing()
        rows = await asyncio.to_thread(songs_tool.read_sheet_rows, csv_path)
        lines = await asyncio.to_thread(_merge_songs_from_sheet, rows, overwrite, dry_run)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        await ctx.send(
            f"⚠️ Không thể đồng bộ: {error}",
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return
    await _send_song_lines(ctx, lines)


# ----------------------------- /track: người chơi đang giữ WR ở những bài nào -----------------------------
# Quy ước: một người "đạt WR" ở một bài khi điểm của họ bằng điểm cao nhất của bài đó
# (nhiều người cùng điểm tối đa đều tính là WR, kể cả người đồng hạng).
NO_PING = discord.AllowedMentions.none()
track_manager = commands.check_any(commands.is_owner(), commands.has_permissions(manage_guild=True))


