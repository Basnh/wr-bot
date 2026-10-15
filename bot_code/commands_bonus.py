"""Quản lý lịch birthday/anniversary bonus hằng năm."""

import json
import os
import shlex
from datetime import date, datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks


BONUS_EVENT_KINDS = {
    "birthday": "Sinh nhật thành viên",
    "song": "Kỷ niệm phát hành bài hát",
    "album": "Kỷ niệm phát hành album",
}
BONUS_WINDOW_DAYS = 3
BONUS_TYPES = (0, 2, 3, 4, 5, 6, 7, 9)
BONUS_TIMEZONE = timezone(timedelta(hours=7))


def _bonus_today() -> date:
    return datetime.now(BONUS_TIMEZONE).date()


def _load_bonus_events() -> dict:
    try:
        with open(BONUS_EVENTS_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)
    except FileNotFoundError:
        return {"version": 1, "events": [], "last_announced_date": None}
    if not isinstance(data, dict) or not isinstance(data.get("events"), list):
        raise ValueError(f"{BONUS_EVENTS_FILE} cần có dạng {{'events': [...]}}.")
    data.setdefault("last_announced_date", None)
    return data


def _save_bonus_events(data: dict) -> None:
    os.makedirs(os.path.dirname(BONUS_EVENTS_FILE), exist_ok=True)
    temp_path = BONUS_EVENTS_FILE + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)
    os.replace(temp_path, BONUS_EVENTS_FILE)


def _parse_bonus_date(value: str) -> tuple[int, int]:
    try:
        parsed = datetime.strptime(f"2000-{value.strip()}", "%Y-%m-%d")
    except ValueError as error:
        raise ValueError("Ngày phải theo định dạng MM-DD, ví dụ 08-25.") from error
    return parsed.month, parsed.day


def _add_bonus_event(name: str, kind: str, event_date: str, bonus: int) -> str:
    name = name.strip()
    if not name:
        raise ValueError("Tên thành viên, bài hát hoặc album không được để trống.")
    if kind not in BONUS_EVENT_KINDS:
        raise ValueError("Loại sự kiện phải là birthday, song hoặc album.")
    if bonus not in BONUS_TYPES:
        raise ValueError("Bonus chỉ có thể là 2 hoặc 3 phần trăm.")
    month, day = _parse_bonus_date(event_date)
    normalized_date = f"{month:02d}-{day:02d}"

    data = _load_bonus_events()
    for event in data["events"]:
        if (
            event.get("name", "").casefold() == name.casefold()
            and event.get("kind") == kind
            and event.get("date") == normalized_date
        ):
            event["bonus_percent"] = bonus
            data["last_announced_date"] = None
            _save_bonus_events(data)
            return f"Đã cập nhật **{name}**: bonus {bonus}% mỗi năm vào {normalized_date}."

    next_id = max((int(event.get("id", 0)) for event in data["events"]), default=0) + 1
    data["events"].append({
        "id": str(next_id),
        "name": name,
        "kind": kind,
        "date": normalized_date,
        "bonus_percent": bonus,
    })
    data["last_announced_date"] = None
    _save_bonus_events(data)
    return f"Đã thêm **{name}** (ID `{next_id}`): bonus {bonus}% mỗi năm vào {normalized_date}."


def _remove_bonus_event(event_id: str) -> str:
    data = _load_bonus_events()
    for index, event in enumerate(data["events"]):
        if str(event.get("id")) == str(event_id).strip():
            removed = data["events"].pop(index)
            _save_bonus_events(data)
            return f"Đã xóa sự kiện `{event_id}` — **{removed.get('name', 'Không tên')}**."
    raise ValueError(f"Không tìm thấy sự kiện ID `{event_id}`.")


def _bonus_occurrences(today: date | None = None) -> list[tuple[dict, date]]:
    today = today or _bonus_today()
    active = []
    for event in _load_bonus_events()["events"]:
        month, day = _parse_bonus_date(event["date"])
        start_value = event.get("from")
        end_value = event.get("to")
        if start_value and end_value:
            start_month, start_day = _parse_bonus_date(start_value)
            end_month, end_day = _parse_bonus_date(end_value)
        else:
            start_date = date(2000, month, day) - timedelta(days=BONUS_WINDOW_DAYS)
            end_date = date(2000, month, day) + timedelta(days=BONUS_WINDOW_DAYS)
            start_month, start_day = start_date.month, start_date.day
            end_month, end_day = end_date.month, end_date.day
        wraps_year = (end_month, end_day) < (start_month, start_day)
        for start_year in (today.year - 1, today.year):
            try:
                start = date(start_year, start_month, start_day)
                end_year = start_year + int(wraps_year)
                end = date(end_year, end_month, end_day)
                occurrence_year = start_year + int(wraps_year and (month, day) < (start_month, start_day))
                occurrence = date(occurrence_year, month, day)
            except ValueError:  # Ví dụ 29/02 trong năm không nhuận.
                continue
            if start <= today <= end:
                active.append((event, occurrence))
                break
    return active


BONUS_MONTH_NAMES = (
    "Tháng 1", "Tháng 2", "Tháng 3", "Tháng 4", "Tháng 5", "Tháng 6",
    "Tháng 7", "Tháng 8", "Tháng 9", "Tháng 10", "Tháng 11", "Tháng 12",
)
BONUS_LIST_PAGE_CHARS = 3400


def _bonus_list_line(event: dict, today: date) -> str:
    month, day = _parse_bonus_date(event["date"])
    try:
        occurrence_year = today.year
        occurrence = date(occurrence_year, month, day)
    except ValueError:  # Sinh nhật 29/02: hiển thị theo lần kế tiếp có thể xảy ra.
        occurrence_year = next(year for year in range(today.year + 1, today.year + 9) if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0))
        occurrence = date(occurrence_year, month, day)

    if event.get("from") and event.get("to"):
        start_month, start_day = _parse_bonus_date(event["from"])
        end_month, end_day = _parse_bonus_date(event["to"])
    else:
        start_date = date(2000, month, day) - timedelta(days=BONUS_WINDOW_DAYS)
        end_date = date(2000, month, day) + timedelta(days=BONUS_WINDOW_DAYS)
        start_month, start_day = start_date.month, start_date.day
        end_month, end_day = end_date.month, end_date.day
    wraps_year = (end_month, end_day) < (start_month, start_day)
    start_year = occurrence.year - int(wraps_year and (month, day) < (start_month, start_day))
    start = date(start_year, start_month, start_day)
    end = date(start_year + int(wraps_year), end_month, end_day)

    if start <= today <= end:
        status = "✅"
    elif today < start:
        status = "🔜"
    else:
        status = "⚪"

    if event.get("birthday"):
        item_name = f"🎂 {event['birthday']} · All Songs"
    elif event.get("song"):
        item_name = event["song"]
        if event.get("album") and event["album"] not in (event["song"], "All Songs"):
            item_name = f"{event['song']} · {event['album']}"
    else:
        item_name = event.get("name", "Sự kiện")
    item_name = discord.utils.escape_markdown(discord.utils.escape_mentions(item_name))
    month_names = (
        "January", "February", "March", "April", "May", "June",
        "July", "August", "September", "October", "November", "December",
    )
    date_range = (
        f"{month_names[start.month - 1]} {start.day} – "
        f"{month_names[end.month - 1]} {end.day}"
    )
    if start <= today <= end:
        days_left = (end - today).days
        ending = "Ends today" if days_left == 0 else f"Ends in {days_left} day{'s' if days_left != 1 else ''}"
    elif today < start:
        days_left = (start - today).days
        ending = f"Starts in {days_left} day{'s' if days_left != 1 else ''}"
    else:
        ending = "Expired"
    detail = f"{event.get('bonus_percent', 0)}% | {date_range} | {ending}"
    line = f"{status} **{item_name}**\n{detail}"
    return f"~~{line}~~" if status == "⚪" else line


def _bonus_artist_pages(events: list[dict], today: date) -> list[dict]:
    by_artist = {}
    for event in events:
        by_artist.setdefault(event.get("group") or "Khác", []).append(event)

    pages = []
    page_lines = []
    page_ids = []
    page_chars = 0

    def flush_page():
        nonlocal page_lines, page_ids, page_chars
        if page_lines:
            pages.append({"text": "\n".join(page_lines), "ids": page_ids})
            page_lines, page_ids, page_chars = [], [], 0

    for artist in sorted(by_artist, key=str.casefold):
        artist_name = discord.utils.escape_markdown(discord.utils.escape_mentions(artist))
        artist_events = sorted(
            by_artist[artist],
            key=lambda event: (
                _parse_bonus_date(event["date"]),
                int(event.get("id", 0)) if str(event.get("id", "0")).isdigit() else 0,
            ),
        )
        artist_lines = [f"**{artist_name}**"]
        artist_lines.extend(_bonus_list_line(event, today) for event in artist_events)
        artist_text = "\n".join(artist_lines)

        if len(artist_text) <= BONUS_LIST_PAGE_CHARS:
            required = len(artist_text) + (1 if page_lines else 0)
            if page_lines and page_chars + required > BONUS_LIST_PAGE_CHARS:
                flush_page()
            page_lines.extend(artist_lines)
            page_ids.extend(str(event.get("id")) for event in artist_events)
            page_chars += len(artist_text) + (1 if page_chars else 0)
            continue

        flush_page()
        section_lines = [f"**{artist_name}**"]
        section_ids = []
        section_chars = len(section_lines[0])
        for event in artist_events:
            event_line = _bonus_list_line(event, today)
            required = len(event_line) + 1
            if section_ids and section_chars + required > BONUS_LIST_PAGE_CHARS:
                pages.append({"text": "\n".join(section_lines), "ids": section_ids})
                section_lines = [f"**{artist_name}**"]
                section_ids = []
                section_chars = len(section_lines[0])
            section_lines.append(event_line)
            section_ids.append(str(event.get("id")))
            section_chars += required
        if section_ids:
            page_lines = section_lines
            page_ids = section_ids
            page_chars = section_chars

    flush_page()
    return pages


def _bonus_embed(page: dict, *, footer: str | None = None) -> discord.Embed:
    embed = discord.Embed(description=page["text"], color=discord.Color.blurple())
    if bot.user:
        embed.set_author(name="SUPERSTAR JYP - Top Bonuses", icon_url=bot.user.display_avatar.url)
    else:
        embed.set_author(name="SUPERSTAR JYP - Top Bonuses")
    if footer:
        embed.set_footer(text=footer)
    return embed


async def _send_bonus_embeds(target, embeds: list[discord.Embed], *, ephemeral: bool = False) -> None:
    if not embeds:
        embeds = [_bonus_embed({"text": "Hôm nay không có sự kiện bonus nào đang hiệu lực."})]
    for embed in embeds:
        if isinstance(target, discord.Interaction):
            if target.response.is_done():
                await target.followup.send(embed=embed, ephemeral=ephemeral)
            else:
                await target.response.send_message(embed=embed, ephemeral=ephemeral)
        else:
            await target.send(embed=embed)


class BonusMonthSelect(discord.ui.Select):
    def __init__(self, menu: "BonusListView"):
        self.menu = menu
        super().__init__(
            placeholder=BONUS_MONTH_NAMES[menu.month - 1],
            min_values=1,
            max_values=1,
            options=menu.month_options(),
            row=0,
        )

    async def callback(self, interaction: discord.Interaction):
        self.menu.month = int(self.values[0])
        self.menu.page = self.menu.current_date_page() if self.menu.month == self.menu.today.month else 0
        self.placeholder = BONUS_MONTH_NAMES[self.menu.month - 1]
        self.options = self.menu.month_options()
        self.menu.sync_controls()
        await interaction.response.edit_message(embed=self.menu.render_embed(), view=self.menu)


class BonusListView(discord.ui.View):
    """Menu tháng và phân trang; mặc định mở ở tháng/ngày gần thời điểm gọi lệnh."""

    def __init__(self, events: list[dict], owner_id: int):
        super().__init__(timeout=180)
        self.today = _bonus_today()
        self.events = sorted(
            events,
            key=lambda event: (
                _parse_bonus_date(event["date"]),
                int(event.get("id", 0)) if str(event.get("id", "0")).isdigit() else 0,
            ),
        )
        self.owner_id = owner_id
        self.month = self.today.month
        self.page = self.current_date_page()
        self.add_item(BonusMonthSelect(self))
        self.sync_controls()

    def month_events(self, month: int | None = None) -> list[dict]:
        month = month or self.month
        return [event for event in self.events if _parse_bonus_date(event["date"])[0] == month]

    def month_options(self) -> list[discord.SelectOption]:
        options = []
        for month, label in enumerate(BONUS_MONTH_NAMES, start=1):
            count = len(self.month_events(month))
            options.append(discord.SelectOption(
                label=label,
                value=str(month),
                description=f"{count} mục bonus",
                default=month == self.month,
            ))
        return options

    def current_date_page(self) -> int:
        month_events = self.month_events(self.today.month)
        if not month_events:
            return 0
        today_day = self.today.day
        target = next(
            (index for index, event in enumerate(month_events)
             if _parse_bonus_date(event["date"])[1] >= today_day),
            len(month_events) - 1,
        )
        target_id = str(month_events[target].get("id"))
        for page_number, page in enumerate(self.month_pages(self.today.month)):
            if target_id in page["ids"]:
                return page_number
        return 0

    def month_pages(self, month: int | None = None) -> list[dict]:
        return _bonus_artist_pages(self.month_events(month), self.today)

    def page_count(self) -> int:
        return max(1, len(self.month_pages()))

    def sync_controls(self) -> None:
        last_page = self.page_count() - 1
        self.page = min(max(self.page, 0), last_page)
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                if item.custom_id == "bonus_list_previous":
                    item.disabled = self.page == 0
                elif item.custom_id == "bonus_list_next":
                    item.disabled = self.page >= last_page

    def render_embed(self) -> discord.Embed:
        month_events = self.month_events()
        pages = self.month_pages()
        description = pages[self.page]["text"] if pages else ""
        if not description:
            description = "Tháng này chưa có sự kiện bonus trong danh sách."
        return _bonus_embed(
            {"text": description},
            footer=(
                f"{BONUS_MONTH_NAMES[self.month - 1]} · Trang {self.page + 1}/{self.page_count()} · "
                f"{len(month_events)} mục · Mở lúc {self.today:%d/%m/%Y} (giờ Việt Nam)"
            ),
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Menu này thuộc về người đã gọi lệnh.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="◀ Trước", style=discord.ButtonStyle.secondary, custom_id="bonus_list_previous", row=1)
    async def previous_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page -= 1
        self.sync_controls()
        await interaction.response.edit_message(embed=self.render_embed(), view=self)

    @discord.ui.button(label="Sau ▶", style=discord.ButtonStyle.secondary, custom_id="bonus_list_next", row=1)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page += 1
        self.sync_controls()
        await interaction.response.edit_message(embed=self.render_embed(), view=self)


bonus_group = app_commands.Group(name="bonus", description="Quản lý sinh nhật và kỷ niệm bonus")


@bonus_group.command(name="add", description="Thêm hoặc cập nhật sự kiện bonus hằng năm")
@app_commands.choices(
    kind=[app_commands.Choice(name=label, value=key) for key, label in BONUS_EVENT_KINDS.items()],
    bonus=[app_commands.Choice(name=f"{value}%", value=value) for value in BONUS_TYPES],
)
@app_commands.describe(
    name="Tên thành viên, bài hát hoặc album",
    kind="Loại sự kiện",
    event_date="Ngày hằng năm theo MM-DD, ví dụ 08-25",
    bonus="Mức bonus theo baseline (0%, 2%–7% hoặc 9%)",
)
async def bonus_slash_add(
    interaction: discord.Interaction,
    name: str,
    kind: app_commands.Choice[str],
    event_date: str,
    bonus: app_commands.Choice[int],
):
    if not await _slash_owner_only(interaction):
        return
    try:
        result = _add_bonus_event(name, kind.value, event_date, bonus.value)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        await interaction.response.send_message(f"⚠️ {error}", ephemeral=True)
        return
    await interaction.response.send_message(result, ephemeral=True)


@bonus_group.command(name="remove", description="Xóa sự kiện bonus theo ID")
async def bonus_slash_remove(interaction: discord.Interaction, event_id: str):
    if not await _slash_owner_only(interaction):
        return
    try:
        result = _remove_bonus_event(event_id)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        await interaction.response.send_message(f"⚠️ {error}", ephemeral=True)
        return
    await interaction.response.send_message(result, ephemeral=True)


@bonus_group.command(name="list", description="Xem toàn bộ sự kiện bonus đã lưu")
async def bonus_slash_list(interaction: discord.Interaction):
    try:
        events = _load_bonus_events()["events"]
        view = BonusListView(events, interaction.user.id)
        await interaction.response.send_message(embed=view.render_embed(), view=view, ephemeral=True)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        await interaction.response.send_message(f"⚠️ Không đọc được lịch bonus: {error}", ephemeral=True)


@bonus_group.command(name="now", description="Xem bonus đang hiệu lực trong cửa sổ 7 ngày")
async def bonus_slash_now(interaction: discord.Interaction):
    try:
        occurrences = _bonus_occurrences()
        today = _bonus_today()
        events = [event for event, _ in occurrences]
        pages = _bonus_artist_pages(events, today)
        total = len(events)
        embeds = [
            _bonus_embed(
                page,
                footer=f"Đang hiệu lực · Trang {index}/{len(pages)} · {total} mục · {today:%d/%m/%Y}",
            )
            for index, page in enumerate(pages, start=1)
        ]
        await _send_bonus_embeds(interaction, embeds, ephemeral=True)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        await interaction.response.send_message(f"⚠️ Không đọc được lịch bonus: {error}", ephemeral=True)


bot.tree.add_command(bonus_group)


@tasks.loop(minutes=5)
async def bonus_watcher():
    """Gửi thông báo một lần mỗi ngày khi có bonus đang trong cửa sổ hiệu lực."""
    today = _bonus_today()
    data = _load_bonus_events()
    today_key = today.isoformat()
    if data.get("last_announced_date") == today_key:
        return

    occurrences = _bonus_occurrences(today)
    if not occurrences:
        data["last_announced_date"] = today_key
        _save_bonus_events(data)
        return
    if not ALERT_CHANNEL_ID:
        return

    channel = bot.get_channel(ALERT_CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(ALERT_CHANNEL_ID)
        except discord.HTTPException as error:
            print(f"[bonus] Không tìm thấy kênh cảnh báo {ALERT_CHANNEL_ID}: {error}", flush=True)
            return
    if not isinstance(channel, discord.abc.Messageable):
        print(f"[bonus] Kênh {ALERT_CHANNEL_ID} không hỗ trợ gửi tin nhắn.", flush=True)
        return

    try:
        grouped = {}
        for event, occurrence in occurrences:
            key = (
                event.get("group", event.get("name", "")),
                event.get("bonus_percent", 0),
                event.get("birthday", ""),
                event.get("album", ""),
                event["date"],
                event.get("from", ""),
                event.get("to", ""),
                occurrence,
            )
            if key not in grouped:
                grouped[key] = {"event": event, "occurrence": occurrence, "events": []}
            grouped[key]["events"].append(event)

        for group in grouped.values():
            event = group["event"]
            occurrence = group["occurrence"]
            events = group["events"]
            offset = (today - occurrence).days
            if offset < 0:
                day_label = f"Còn {abs(offset)} ngày đến ngày chính"
            elif offset == 0:
                day_label = "Ngày chính hôm nay"
            else:
                day_label = f"Ngày chính cách đây {offset} ngày"
            event_date = occurrence.strftime("%d/%m")
            label = BONUS_EVENT_KINDS.get(event.get("kind"), "Kỷ niệm phát hành")
            subject = event.get("birthday") or event.get("album") or event.get("name", "Sự kiện")
            group = event.get("group")
            embed = discord.Embed(
                title="🎁 Bonus đang hiệu lực",
                description=(f"**{group} · {subject}** · {label}" if group else f"**{subject}** · {label}"),
                color=discord.Color.gold(),
            )
            embed.add_field(name="Bonus", value=f"{event.get('bonus_percent', 0)}%", inline=True)
            embed.add_field(name="Ngày chính", value=event_date, inline=True)
            embed.add_field(name="Hôm nay", value=day_label, inline=True)
            start = event.get("from")
            end = event.get("to")
            if start and end:
                start_month, start_day = _parse_bonus_date(start)
                end_month, end_day = _parse_bonus_date(end)
                start_year = today.year - int((end_month, end_day) < (start_month, start_day) and today.month == 1)
                end_year = start_year + int((end_month, end_day) < (start_month, start_day))
                window_start = date(start_year, start_month, start_day)
                window_end = date(end_year, end_month, end_day)
            else:
                window_start = occurrence - timedelta(days=BONUS_WINDOW_DAYS)
                window_end = occurrence + timedelta(days=BONUS_WINDOW_DAYS)
            songs = list(dict.fromkeys(item.get("song", "") for item in events if item.get("song")))
            if songs and songs != [event.get("album")]:
                song_text = ", ".join(songs)
                if len(song_text) > 1000:
                    song_text = song_text[:990] + "…"
                embed.add_field(name="Bài hát", value=song_text, inline=False)
            embed.set_footer(text=f"Cửa sổ bonus: {window_start:%d/%m}–{window_end:%d/%m} · Vietnam time")
            await channel.send(embed=embed)
    except discord.HTTPException as error:
        print(f"[bonus] Không gửi được thông báo vào kênh {ALERT_CHANNEL_ID}: {error}", flush=True)
        return

    data["last_announced_date"] = today_key
    _save_bonus_events(data)


@bonus_watcher.before_loop
async def before_bonus_watcher():
    await bot.wait_until_ready()


@bot.group(name="bonus", invoke_without_command=True)
async def bonus_prefix(ctx: commands.Context):
    await ctx.send(
        f"Lệnh: `{PREFIX}bonus add <birthday|song|album> <MM-DD> <0|2|3|4|5|6|7|9> <tên>` · "
        f"`{PREFIX}bonus remove <ID>` · `{PREFIX}bonus list` · `{PREFIX}bonus now`."
    )


@bonus_prefix.command(name="add")
@commands.is_owner()
async def bonus_prefix_add(ctx: commands.Context, *, arguments: str = ""):
    try:
        tokens = shlex.split(arguments)
        if len(tokens) < 4:
            raise ValueError(
                f'Dùng: `{PREFIX}bonus add <birthday|song|album> <MM-DD> <0|2|3|4|5|6|7|9> "tên"`'
            )
        kind, event_date, bonus_text = tokens[:3]
        kind = kind.casefold()
        if kind not in BONUS_EVENT_KINDS:
            raise ValueError("Loại sự kiện phải là birthday, song hoặc album.")
        result = _add_bonus_event(" ".join(tokens[3:]), kind, event_date, int(bonus_text))
    except (OSError, ValueError, json.JSONDecodeError) as error:
        await ctx.send(f"⚠️ {error}", allowed_mentions=discord.AllowedMentions.none())
        return
    await ctx.send(result, allowed_mentions=discord.AllowedMentions.none())


@bonus_prefix.command(name="remove", aliases=["rm"])
@commands.is_owner()
async def bonus_prefix_remove(ctx: commands.Context, *, event_id: str = ""):
    try:
        result = _remove_bonus_event(event_id)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        await ctx.send(f"⚠️ {error}", allowed_mentions=discord.AllowedMentions.none())
        return
    await ctx.send(result, allowed_mentions=discord.AllowedMentions.none())


@bonus_prefix.command(name="list")
async def bonus_prefix_list(ctx: commands.Context):
    try:
        view = BonusListView(_load_bonus_events()["events"], ctx.author.id)
        await ctx.send(embed=view.render_embed(), view=view)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        await ctx.send(f"⚠️ Không đọc được lịch bonus: {error}")


@bonus_prefix.command(name="now", aliases=["active"])
async def bonus_prefix_now(ctx: commands.Context):
    try:
        occurrences = _bonus_occurrences()
        today = _bonus_today()
        events = [event for event, _ in occurrences]
        pages = _bonus_artist_pages(events, today)
        total = len(events)
        embeds = [
            _bonus_embed(
                page,
                footer=f"Đang hiệu lực · Trang {index}/{len(pages)} · {total} mục · {today:%d/%m/%Y}",
            )
            for index, page in enumerate(pages, start=1)
        ]
        await _send_bonus_embeds(ctx, embeds)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        await ctx.send(f"⚠️ Không đọc được lịch bonus: {error}")
