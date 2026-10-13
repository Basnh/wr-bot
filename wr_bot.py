"""
SUPERSTAR JYP - World Record Discord bot

Lệnh Discord:
  /world_record <nhóm> <bài> [album]   -> bảng xếp hạng dạng embed, 10 người/trang, nút Previous/Next
  !world_record <từ khóa|ID> (!wr)     -> như trên nhưng gõ theo kiểu prefix
  /songs list|artists|search           -> tra cứu danh sách bài hát
  /songs add|remove|sync               -> quản lý danh sách (chỉ chủ bot)
  !songs <add|remove|list|artists|search|sync> -> dùng các lệnh trên qua prefix
  !reload                              -> (chỉ chủ bot) đọc lại songs.json và players.txt không cần khởi động lại
  /track (menu: Tất cả | từng người) hoặc !track (menu chọn người) -> người trong players.txt đang giữ WR ở bài nào
  !track <tên>                         -> tra cứu nhanh một người
  !track add|remove|list               -> quản lý danh sách theo dõi (add/remove cần quyền Manage Server)

Chạy nền:
  - Theo dõi mọi bài trong songs.json, báo vào kênh ALERT_CHANNEL_ID khi điểm top 1 tăng
  - Tự đọc lại songs.json khi file thay đổi (AUTO_RELOAD)
  - Nhận lệnh gõ trong terminal (hoặc từ manager.py): reload, quit

Cài đặt:
  pip install -r requirements.txt

Chạy (khuyên dùng manager.py để vừa mở bot vừa chạy được các lệnh quản lý):
  python manager.py
  hoặc chạy riêng: python wr_bot.py   (token đặt trong file .env)
"""

import asyncio
import json
import math
import os
import re
import shlex
import sys
import threading
import time
import traceback
from io import BytesIO
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

import aiohttp
import discord
import songs_tool
from PIL import Image
from discord import app_commands
from discord.ext import commands, tasks

def load_env(filename: str = ".env") -> None:
    """Đọc file .env (cùng thư mục với wr_bot.py) vào biến môi trường, không cần cài thêm thư viện."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env()

# ----------------------------- Cấu hình -----------------------------
TOKEN = os.environ["DISCORD_TOKEN"]
ALERT_CHANNEL_ID = int(os.getenv("ALERT_CHANNEL_ID", "0"))
DEPLOY_GIT_BRANCH = os.getenv("DEPLOY_GIT_BRANCH", "main").strip()
DEPLOY_SYSTEMD_SERVICE = os.getenv("DEPLOY_SYSTEMD_SERVICE", "").strip()

SEASON = 31
BASE_URL = "https://superstar-jyp.s3.ap-northeast-2.amazonaws.com/world_record_ranking/production"
GAME_TITLE = "SUPERSTAR JYP"
PREFIX = "!"            # dùng được cả !world_record và /world_record
SLASH_COMMAND_GUILD_ID = 967430273883045898

PAGE_SIZE = 10          # số người mỗi trang
CACHE_TTL = 60          # giây, dùng cho lệnh /world_record
POLL_MINUTES = 5        # chu kỳ theo dõi nền
MAX_CONCURRENCY = 5     # số request song song tối đa

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SONGS_FILE = os.path.join(BASE_DIR, "songs.json")       # {"153": {"artist": ..., "song": ..., "album": ...}}
STATE_FILE = os.path.join(BASE_DIR, "wr_state.json")    # lưu điểm top 1 lần trước để so sánh

PLAYERS_FILE = os.path.join(BASE_DIR, "players.txt")    # mỗi dòng một người chơi cần theo dõi
TRACK_CACHE_TTL = 600       # giây, dữ liệu cũ nhất được chấp nhận khi dùng /track
TRACK_PAGE_CHARS = 3300     # số ký tự tối đa mỗi trang
TRACK_PAGE_LINES = 28       # số dòng tối đa mỗi trang

AUTO_RELOAD = True          # tự đọc lại songs.json khi file thay đổi
AUTO_RELOAD_SECONDS = 10    # chu kỳ kiểm tra file



# ----------------------------- Dữ liệu bài hát -----------------------------
def load_songs() -> dict[str, dict]:
    """songs.json: {"153": {"artist": "ITZY", "song": "...", "album": "..."}}
    Vẫn đọc được định dạng cũ {"153": "ITZY - Imaginary Friend"}."""
    with open(SONGS_FILE, "r", encoding="utf-8") as f:
        raw = json.load(f)
    songs: dict[str, dict] = {}
    for sid, v in raw.items():
        if isinstance(v, str):
            v = {"artist": "", "song": v, "album": ""}
        songs[str(sid)] = {
            "artist": (v.get("artist") or "").strip(),
            "song": (v.get("song") or "").strip(),
            "album": (v.get("album") or "").strip(),
            "cover": (v.get("cover") or "").strip(),
        }
    return songs


def load_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


SONGS = load_songs()


def _songs_mtime_now() -> float:
    try:
        return os.path.getmtime(SONGS_FILE)
    except OSError:
        return 0.0


_songs_mtime = _songs_mtime_now()


def reload_songs() -> tuple[bool, str]:
    """Đọc lại songs.json. Nếu file lỗi thì giữ nguyên dữ liệu cũ.
    Cập nhật SONGS tại chỗ (không gán lại) để mọi nơi đang dùng SONGS đều thấy dữ liệu mới."""
    global _songs_mtime
    _songs_mtime = _songs_mtime_now()  # đặt trước để file lỗi không bị báo lặp lại mỗi chu kỳ
    try:
        new = load_songs()
    except Exception as e:  # JSON hỏng, thiếu file, sai cấu trúc...
        return False, f"Không đọc được songs.json: {e} (giữ nguyên dữ liệu cũ)"

    old = dict(SONGS)
    SONGS.clear()
    SONGS.update(new)
    added = len(new.keys() - old.keys())
    removed = len(old.keys() - new.keys())
    changed = sum(1 for sid in new if sid in old and new[sid] != old[sid])
    for sid in old.keys() - new.keys():
        wr.cache.pop(sid, None)
    return True, f"Đã tải lại songs.json: {len(new)} bài (+{added} mới, -{removed} xóa, ~{changed} sửa)"


# ----------------------------- Danh sách người chơi theo dõi (players.txt) -----------------------------
def _norm(s: str | None) -> str:
    return (s or "").strip().casefold()


PLAYERS_HEADER = (
    "# Danh sách người chơi cần theo dõi World Record - mỗi dòng một người.\n"
    "# Dòng bắt đầu bằng # là ghi chú, dòng trống sẽ bị bỏ qua. Cách ghi:\n"
    "#   nickname              -> khớp theo tên (không phân biệt hoa thường)\n"
    "#   nickname | objectID   -> khớp theo objectID, vẫn đúng khi người đó đổi tên\n"
    "#   objectID              -> chỉ ghi số objectID\n"
    "# Bot tự đọc lại file này khi bạn lưu, không cần khởi động lại.\n"
)


def parse_player_line(line: str) -> dict | None:
    """Một dòng của players.txt -> {"nickname": ..., "id": ...} hoặc None nếu là ghi chú/dòng trống."""
    text = line.strip()
    if not text or text.startswith("#"):
        return None
    if text.isdigit():
        return {"id": int(text), "nickname": f"ID {int(text)}"}
    name, sep, tail = text.rpartition("|")
    if sep and name.strip() and tail.strip().isdigit():
        return {"id": int(tail.strip()), "nickname": name.strip()}
    return {"nickname": text}


def player_line(entry: dict) -> str:
    if entry.get("id") is None:
        return entry["nickname"]
    if entry["nickname"].startswith("ID "):
        return str(entry["id"])
    return f"{entry['nickname']} | {entry['id']}"


def player_key(entry: dict):
    return ("id", entry["id"]) if entry.get("id") is not None else ("name", _norm(entry["nickname"]))


def load_players() -> list[dict]:
    if not os.path.exists(PLAYERS_FILE):
        return []
    with open(PLAYERS_FILE, "r", encoding="utf-8-sig") as f:
        parsed = [parse_player_line(line) for line in f]
    seen, players = set(), []
    for entry in parsed:
        if entry and player_key(entry) not in seen:
            seen.add(player_key(entry))
            players.append(entry)
    return players


def _players_mtime_now() -> float:
    try:
        return os.path.getmtime(PLAYERS_FILE)
    except OSError:
        return 0.0


def ensure_players_file() -> None:
    """Tạo sẵn players.txt kèm hướng dẫn nếu chưa có."""
    if os.path.exists(PLAYERS_FILE):
        return
    try:
        with open(PLAYERS_FILE, "w", encoding="utf-8", newline="\n") as f:
            f.write(PLAYERS_HEADER + "\n")
    except OSError as e:
        print(f"[players] Không tạo được players.txt: {e}", flush=True)


PLAYERS: list[dict] = []
ensure_players_file()
try:
    PLAYERS.extend(load_players())
except Exception as e:
    print(f"[players] Không đọc được players.txt: {e}", flush=True)
_players_mtime = _players_mtime_now()


def _write_players_text(text: str) -> None:
    tmp = PLAYERS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, PLAYERS_FILE)


def append_player(entry: dict) -> None:
    """Thêm một dòng vào cuối players.txt, giữ nguyên ghi chú và các dòng khác."""
    text = PLAYERS_HEADER if not os.path.exists(PLAYERS_FILE) else open(PLAYERS_FILE, encoding="utf-8-sig").read()
    if text and not text.endswith("\n"):
        text += "\n"
    _write_players_text(text + player_line(entry) + "\n")


def rewrite_players_file(fn) -> None:
    """Duyệt từng dòng: fn(dòng, entry_đã_phân_tích) trả về dòng mới, hoặc None để xóa dòng đó."""
    if not os.path.exists(PLAYERS_FILE):
        return
    with open(PLAYERS_FILE, "r", encoding="utf-8-sig") as f:
        lines = f.read().splitlines()
    out = [new for line in lines if (new := fn(line, parse_player_line(line))) is not None]
    _write_players_text("\n".join(out) + ("\n" if out else ""))


def reload_players() -> tuple[bool, str]:
    global _players_mtime
    _players_mtime = _players_mtime_now()  # đặt trước để file lỗi không bị báo lặp lại
    try:
        new = load_players()
    except Exception as e:
        return False, f"Không đọc được players.txt: {e} (giữ nguyên danh sách cũ)"
    PLAYERS.clear()
    PLAYERS.extend(new)
    return True, f"Đã tải lại players.txt: {len(new)} người chơi"


def reload_all() -> tuple[bool, str]:
    ok1, m1 = reload_songs()
    ok2, m2 = reload_players()
    return ok1 and ok2, m1 + "\n" + m2


def display(sid: str) -> str:
    """Tên hiển thị trong embed: 'Artist - Song'."""
    s = SONGS[sid]
    return f"{s['artist']} - {s['song']}" if s["artist"] else s["song"]


# ----------------------------- Lấy dữ liệu WR -----------------------------
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

    async def get(self, song_id: str, force: bool = False, max_age: float = CACHE_TTL) -> dict | None:
        """Trả về {"data": [...], "updated": datetime, "etag": str, "fetched": float}.
        Dùng ETag nên nếu file không đổi thì S3 trả 304, không tải lại nội dung."""
        entry = self.cache.get(song_id)
        now = time.time()
        if entry and not force and now - entry["fetched"] < max_age:
            return entry

        headers = {}
        if entry and entry.get("etag"):
            headers["If-None-Match"] = entry["etag"]

        url = f"{BASE_URL}/{SEASON}/{song_id}/latest.json"
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
                    }
                    self.cache[song_id] = entry
                    return entry
            except (aiohttp.ClientError, asyncio.TimeoutError):
                return entry  # lỗi mạng: dùng bản cũ nếu có


wr = WRClient()


ALBUM_COLOR_CACHE: dict[str, int] = {}


async def album_cover_color(url: str) -> int | None:
    """Tải màu chủ đạo của cover để dùng làm màu embed."""
    if url in ALBUM_COLOR_CACHE:
        return ALBUM_COLOR_CACHE[url]

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    if not wr.session:
        print("[watch] Không thể đọc màu cover: HTTP session chưa khởi tạo.", flush=True)
        return None

    try:
        async with wr.sem:
            async with wr.session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as response:
                if response.status != 200:
                    print(f"[watch] Không tải được cover ({response.status}): {url}", flush=True)
                    return None
                if not response.headers.get("Content-Type", "").lower().startswith("image/"):
                    print(f"[watch] URL cover không trả về ảnh: {url}", flush=True)
                    return None
                if response.content_length is not None and response.content_length > 8 * 1024 * 1024:
                    print(f"[watch] Bỏ qua cover lớn hơn 8 MB: {url}", flush=True)
                    return None
                image_data = await response.content.read(8 * 1024 * 1024 + 1)
                if len(image_data) > 8 * 1024 * 1024:
                    print(f"[watch] Bỏ qua cover lớn hơn 8 MB: {url}", flush=True)
                    return None

        with Image.open(BytesIO(image_data)) as image:
            if image.width * image.height > 16 * 1024 * 1024:
                print(f"[watch] Bỏ qua cover có kích thước ảnh quá lớn: {url}", flush=True)
                return None
            image = image.convert("RGB")
            image.thumbnail((64, 64))
            colors = image.quantize(colors=16).convert("RGB").getcolors(64 * 64)
            if not colors:
                print(f"[watch] Không xác định được màu cover: {url}", flush=True)
                return None
            _, (red, green, blue) = max(colors, key=lambda item: item[0])
            color = (red << 16) | (green << 8) | blue
            ALBUM_COLOR_CACHE[url] = color
            return color
    except (
        aiohttp.ClientError,
        asyncio.TimeoutError,
        OSError,
        Image.DecompressionBombError,
    ) as error:
        print(f"[watch] Lỗi khi đọc màu cover {url}: {error}", flush=True)
        return None


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
    cover_color: int | None = None,
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
        title=f"{GAME_TITLE} - World Record (Season {SEASON})",
        description=f"## 🎵 {song_name}\n{wr_line}\n\n" + "\n".join(lines),
        color=cover_color if cover_color is not None else discord.Color.gold(),
    )
    if cover_url.startswith(("https://", "http://")):
        embed.set_thumbnail(url=cover_url)
    embed.set_footer(text=f"Page {page + 1}/{pages} · Cập nhật lần cuối")
    embed.timestamp = entry["updated"]
    return embed


class RankView(discord.ui.View):
    def __init__(
        self,
        song_name: str,
        entry: dict,
        owner_id: int,
        cover_url: str = "",
        cover_color: int | None = None,
    ) -> None:
        super().__init__(timeout=300)
        self.song_name = song_name
        self.entry = entry
        self.owner_id = owner_id
        self.cover_url = cover_url
        self.cover_color = cover_color
        self.page = 0
        self.pages = max(1, math.ceil(len(entry["data"]) / PAGE_SIZE))
        self.message: discord.Message | None = None
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

    async def _render(self, interaction: discord.Interaction) -> None:
        self._sync()
        await interaction.response.edit_message(
            embed=build_embed(
                self.song_name,
                self.entry,
                self.page,
                self.cover_url,
                self.cover_color,
            ),
            view=self,
        )

    @discord.ui.button(label="Trang trước", style=discord.ButtonStyle.primary)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page -= 1
        await self._render(interaction)

    @discord.ui.button(label="Trang sau", style=discord.ButtonStyle.success)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page += 1
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
        await self.tree.sync(guild=guild)
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
                f"`{PREFIX}wr <ID hoặc từ khóa>` — ví dụ `{PREFIX}wr got7 hard carry`.",
            ),
            (
                "🎵 Danh sách bài hát",
                "`/songs list`, `/songs artists`, `/songs search` — tra cứu bài hát; "
                "danh sách có embed và nút chuyển trang.\n"
                f"`{PREFIX}songs` — xem cách dùng các lệnh bài hát.",
            ),
            (
                "👤 Theo dõi người chơi",
                "`/track` — mở menu, chọn **Tất cả** hoặc một người trong `players.txt` để xem họ đang giữ World Record ở bài nào.\n"
                f"`{PREFIX}track` — mở menu chọn người; `{PREFIX}track <tên>` — tra cứu nhanh.",
            ),
            (
                "🔒 Quyền quản lý",
                "Thêm, xóa, đồng bộ bài hát và tải lại dữ liệu chỉ dành cho chủ bot.",
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
                "Bot cũng tự đọc lại file khi phát hiện có thay đổi.",
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
        return "Chưa có dữ liệu World Record cho bài này.", None, None

    cover_url = SONGS[song_id].get("cover", "")
    cover_color = await album_cover_color(cover_url) if cover_url else None
    view = RankView(display(song_id), entry, user_id, cover_url, cover_color)
    return None, build_embed(display(song_id), entry, 0, cover_url, cover_color), view


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


# ----------------------------- Quản lý bài hát trong Discord -----------------------------
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


def _song_list_embed(
    entries: list[tuple[str, dict]], artist: str | None, page: int
) -> discord.Embed:
    pages = max(1, math.ceil(len(entries) / SONG_LIST_PAGE_SIZE))
    start = page * SONG_LIST_PAGE_SIZE
    current = entries[start:start + SONG_LIST_PAGE_SIZE]
    esc = discord.utils.escape_markdown
    lines = []
    for sid, song in current:
        title = esc(song["song"] or "(chưa rõ tên)")[:100]
        details = [esc(song["artist"])[:80]] if song["artist"] else []
        if song["album"]:
            details.append(esc(song["album"])[:80])
        suffix = f"\n　└ {' · '.join(details)}" if details else ""
        lines.append(f"**`{sid}`**　{title}{suffix}")

    description = "\n\n".join(lines) if lines else "Không tìm thấy bài hát nào."
    if artist:
        description = f"**Lọc nghệ sĩ:** {esc(artist)[:100]}\n\n{description}"

    embed = discord.Embed(
        title="🎵 Danh sách bài hát",
        description=description,
        color=discord.Color.blurple(),
    )
    embed.set_footer(text=f"{len(entries)} bài · Trang {page + 1}/{pages}")
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
        key=_song_sort_key,
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


def _matcher(entry: dict):
    if entry.get("id") is not None:
        pid = entry["id"]
        return lambda p: p.get("objectID") == pid
    name = _norm(entry["nickname"])
    return lambda p: _norm(p.get("nickname")) == name


async def load_all(max_age: float = TRACK_CACHE_TTL) -> dict[str, dict]:
    """Dữ liệu xếp hạng của mọi bài trong songs.json (dùng cache, bài nào hết hạn mới tải lại)."""
    ids = list(SONGS)
    entries = await asyncio.gather(*(wr.get(sid, max_age=max_age) for sid in ids))
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
                })
        songs.sort(key=lambda s: int(s["sid"]) if s["sid"].isdigit() else 10**9)
        results.append({"entry": w, "name": name, "songs": songs, "seen_in": seen_in})
    results.sort(key=lambda r: (-len(r["songs"]), r["name"].casefold()))
    return results


def _song_line(s: dict) -> str:
    esc = discord.utils.escape_markdown
    title = display(s["sid"]) if s["sid"] in SONGS else f"Bài {s['sid']}"
    if s.get("rank") is not None:  # hạng hiện tại của người này ở bài đó
        place = f"hạng **#{s['rank']}**"
        if s["tied"] > 1:
            place += f" (đồng điểm {s['tied']} người)"
    else:
        place = "một mình" if s["tied"] == 1 else f"đồng hạng với {s['tied'] - 1} người"
    when = f" · {ts_tag(s['time'])}" if s["time"] else ""
    return f"• **{esc(title)}** · `{s['score']:,}`{when} · {place}"


def build_track_pages(results: list[dict]) -> list[str]:
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

    for r in (r for r in results if r["songs"]):
        header = f"### 👤 {esc(r['name'])} — **{len(r['songs'])} WR**"
        lines = [_song_line(s) for s in r["songs"]]
        start_block(header, lines[0])
        for line in lines:
            push(line, owner=header)

    idle = [r for r in results if not r["songs"]]
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
    return pages or ["Chưa có dữ liệu."]


def build_track_embed(results: list[dict], pages: list[str], updated: datetime, page: int) -> discord.Embed:
    total_wr = sum(len(r["songs"]) for r in results)
    distinct = len({s["sid"] for r in results for s in r["songs"]})
    summary = f"👥 **{len(results)}** người · 🏆 **{total_wr} WR** · 🎵 **{distinct}** bài khác nhau"
    embed = discord.Embed(
        title=f"{GAME_TITLE} - Người chơi theo dõi (Season {SEASON})",
        description=f"{summary}\n\n{pages[page]}",
        color=discord.Color.gold(),
    )
    embed.set_footer(text=f"Page {page + 1}/{len(pages)} · Dữ liệu cập nhật")
    embed.timestamp = updated
    return embed


class PagedView(discord.ui.View):
    """Nút Previous/Next dùng chung: render(page) -> Embed."""

    def __init__(self, owner_id: int, pages: int, render) -> None:
        super().__init__(timeout=300)
        self.owner_id, self.pages, self.render, self.page = owner_id, pages, render, 0
        self.message: discord.Message | None = None
        self._sync()

    def _sync(self) -> None:
        self.prev_btn.disabled = self.page <= 0
        self.next_btn.disabled = self.page >= self.pages - 1

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Chỉ người dùng lệnh mới bấm được nút này.", ephemeral=True)
            return False
        return True

    async def _show(self, interaction: discord.Interaction) -> None:
        self._sync()
        await interaction.response.edit_message(embed=self.render(self.page), view=self)

    @discord.ui.button(label="Trang trước", style=discord.ButtonStyle.primary)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page -= 1
        await self._show(interaction)

    @discord.ui.button(label="Trang sau", style=discord.ButtonStyle.success)
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
        return "Chưa tải được dữ liệu World Record.", None, None

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

    pages = build_track_pages(results)
    updated = max(e["updated"] for e in entries.values())
    view = PagedView(user_id, len(pages), lambda p: build_track_embed(results, pages, updated, p))
    return None, build_track_embed(results, pages, updated, 0), view


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


async def _run_deploy_process(
    args: list[str],
    cwd: str | None = None,
    timeout: float = 120,
) -> tuple[int, str]:
    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except FileNotFoundError as error:
        return 127, f"Không tìm thấy chương trình: {error.filename}"
    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        process.kill()
        await process.communicate()
        raise
    return (
        process.returncode if process.returncode is not None else 0,
        output.decode("utf-8", errors="replace").strip(),
    )


@bot.command(name="deploy")
@commands.is_owner()
async def deploy_cmd(ctx: commands.Context):
    missing = [
        name
        for name, value in (
            ("DEPLOY_SYSTEMD_SERVICE", DEPLOY_SYSTEMD_SERVICE),
            ("DEPLOY_GIT_BRANCH", DEPLOY_GIT_BRANCH),
        )
        if not value
    ]
    if missing:
        await ctx.send(
            "Chưa cấu hình deploy trên VPS: "
            + ", ".join(f"`{name}`" for name in missing)
            + ". Hãy điền các giá trị trong `.env` theo `.env.example`."
        )
        return
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", DEPLOY_GIT_BRANCH):
        await ctx.send("DEPLOY_GIT_BRANCH không hợp lệ.")
        return
    if not re.fullmatch(r"[A-Za-z0-9_.@-]+\.service", DEPLOY_SYSTEMD_SERVICE):
        await ctx.send("DEPLOY_SYSTEMD_SERVICE phải là tên systemd service, ví dụ `wr-bot.service`.")
        return

    git_root_code, git_root = await _run_deploy_process(
        ["git", "rev-parse", "--show-toplevel"], cwd=BASE_DIR
    )
    if git_root_code != 0 or not git_root:
        await ctx.send("Không tìm thấy Git repository của bot trên VPS.")
        return
    git_root = git_root.splitlines()[-1]

    relative_env = os.path.relpath(os.path.join(BASE_DIR, ".env"), git_root)
    tracked_env_code, _ = await _run_deploy_process(
        ["git", "-C", git_root, "ls-files", "--error-unmatch", "--", relative_env]
    )
    if tracked_env_code == 0:
        await ctx.send(
            f"Deploy đã dừng: `{relative_env}` đang được Git theo dõi. "
            "Hãy gỡ file bí mật khỏi repository trước."
        )
        return

    branch_code, branch = await _run_deploy_process(
        ["git", "-C", git_root, "branch", "--show-current"]
    )
    if branch_code != 0 or branch != DEPLOY_GIT_BRANCH:
        await ctx.send(
            f"Branch đang checkout trên VPS không khớp với `{DEPLOY_GIT_BRANCH}`."
        )
        return

    status_code, status = await _run_deploy_process(
        ["git", "-C", git_root, "status", "--porcelain"]
    )
    if status_code != 0:
        await ctx.send(f"Không kiểm tra được trạng thái Git: {status}")
        return
    if status:
        await ctx.send(
            "Deploy đã dừng vì có thay đổi cục bộ chưa commit trên VPS. "
            "Hãy kiểm tra các thay đổi đó trước; bot không tự ghi đè chúng."
        )
        return

    remote_code, _ = await _run_deploy_process(
        ["git", "-C", git_root, "remote", "get-url", "origin"]
    )
    if remote_code != 0:
        await ctx.send("Git repository trên VPS chưa có remote `origin`.")
        return

    try:
        fetch_code, fetch_output = await _run_deploy_process(
            ["git", "-C", git_root, "fetch", "origin", DEPLOY_GIT_BRANCH],
            timeout=120,
        )
    except FileNotFoundError as error:
        await ctx.send(f"Không tìm thấy Git trên VPS: {error.filename}")
        return
    except asyncio.TimeoutError:
        await ctx.send("Git fetch quá thời gian chờ; chưa restart bot.")
        return
    except OSError as error:
        await ctx.send(f"Không thể chạy Git fetch: {error}")
        return
    if fetch_code != 0:
        await ctx.send(f"Không tải được code từ GitHub:\n```text\n{fetch_output[-1200:]}\n```")
        return

    pull_code, pull_output = await _run_deploy_process(
        ["git", "-C", git_root, "merge", "--ff-only", "FETCH_HEAD"],
        timeout=120,
    )
    if pull_code != 0:
        await ctx.send(
            f"Không thể fast-forward code trên VPS; bot chưa restart:\n"
            f"```text\n{pull_output[-1200:] or 'git merge --ff-only failed'}\n```"
        )
        return

    dependency_code, dependency_output = await _run_deploy_process(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "-r",
            os.path.join(git_root, "requirements.txt"),
        ],
        cwd=git_root,
        timeout=300,
    )
    if dependency_code != 0:
        await ctx.send(
            "Code đã được cập nhật nhưng cài dependencies thất bại; bot chưa restart:\n"
            f"```text\n{dependency_output[-1200:] or 'pip install failed'}\n```"
        )
        return

    revision_code, revision = await _run_deploy_process(
        ["git", "-C", git_root, "rev-parse", "--short", "HEAD"]
    )
    if revision_code != 0:
        revision = "không xác định"
    await ctx.send(
        f"Đã cập nhật code tới `{revision}`. Đang restart `{DEPLOY_SYSTEMD_SERVICE}`..."
    )

    try:
        restart_code, restart_output = await _run_deploy_process(
            ["sudo", "-n", "systemctl", "restart", DEPLOY_SYSTEMD_SERVICE],
            timeout=30,
        )
    except FileNotFoundError as error:
        await ctx.send(f"Không tìm thấy lệnh cần thiết trên VPS: {error.filename}")
        return
    except asyncio.TimeoutError:
        await ctx.send("Lệnh restart quá thời gian chờ; hãy kiểm tra trạng thái service trên VPS.")
        return
    except OSError as error:
        await ctx.send(f"Không thể restart service: {error}")
        return
    if restart_code != 0:
        await ctx.send(
            f"Code đã được cập nhật nhưng restart thất bại. "
            f"Kiểm tra quyền sudo/systemd:\n```text\n{restart_output[-1200:] or 'systemctl restart failed'}\n```"
        )


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.CommandNotFound):
        return  # bỏ qua các tin nhắn bắt đầu bằng "!" nhưng không phải lệnh của bot
    if isinstance(error, commands.CheckFailure):
        await ctx.send("Bạn không có quyền dùng lệnh này.")
        return
    if isinstance(error, (commands.MissingRequiredArgument, commands.BadArgument)):
        await ctx.send(f"Cách dùng: `{PREFIX}{ctx.command.qualified_name} {ctx.command.signature}`")
        return
    traceback.print_exception(type(error), error, error.__traceback__)


@tasks.loop(seconds=AUTO_RELOAD_SECONDS)
async def songs_watcher():
    if _songs_mtime_now() != _songs_mtime:
        print(f"[auto-reload] {reload_songs()[1]}", flush=True)
    if _players_mtime_now() != _players_mtime:
        print(f"[auto-reload] {reload_players()[1]}", flush=True)


def _console_listener(loop: asyncio.AbstractEventLoop) -> None:
    """Đọc lệnh từ stdin: do manager.py gửi, hoặc gõ trực tiếp khi chạy bot trong terminal.
    Lệnh có sẵn: reload, quit. Mọi thay đổi dữ liệu đều chạy trên luồng của bot để tránh xung đột."""
    if sys.stdin is None:
        return
    try:
        while True:
            line = sys.stdin.readline()
            if not line:  # stdin đã đóng
                return
            cmd = line.strip().lower()
            if cmd == "reload":
                loop.call_soon_threadsafe(lambda: print(f"[reload] {reload_all()[1]}", flush=True))
            elif cmd in ("quit", "exit", "stop"):
                asyncio.run_coroutine_threadsafe(bot.close(), loop)
                return
            elif cmd:
                print(f"[console] lệnh không rõ: {cmd!r} (có: reload, quit)", flush=True)
    except Exception:
        return

# Source - https://stackoverflow.com/a/76881515
# Posted by treeben77
# Retrieved 2026-10-13, License - CC BY-SA 4.0

@bot.event
async def on_ready():
    print('Bot is now working!')
    await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name="Dino đang thức và đang code"))




# ----------------------------- Theo dõi WR mới -----------------------------
@tasks.loop(minutes=POLL_MINUTES)
async def watch():
    state = load_state()
    first_run = not state
    alerts: list[discord.Embed] = []

    async def check(song_id: str):
        entry = await wr.get(song_id, force=True)
        if not entry or not entry["data"]:
            return
        data = entry["data"]
        top_score = data[0]["highscore"]
        holders = [p["nickname"] for p in data if p["highscore"] == top_score]
        top_rank = data[0].get("rank")

        prev = state.get(song_id)
        state[song_id] = {"score": top_score, "holders": holders, "rank": top_rank}

        # bỏ qua lần chạy đầu (chỉ ghi nền) hoặc bài mới chưa có trạng thái
        if first_run or prev is None:
            return
        if top_score > prev["score"]:
            cover_url = SONGS[song_id].get("cover", "")
            color = await album_cover_color(cover_url) if cover_url else None
            embed = discord.Embed(
                title="🏆 World Record mới!",
                description=f"**{display(song_id)}**",
                color=color if color is not None else discord.Color.gold(),
            )
            if cover_url.startswith(("https://", "http://")):
                embed.set_thumbnail(url=cover_url)
            embed.add_field(name="Điểm mới", value=f"{top_score:,}", inline=True)
            embed.add_field(name="Điểm cũ", value=f"{prev['score']:,}", inline=True)
            previous_rank = prev.get("rank")
            rank_text = (
                f"#{previous_rank if previous_rank is not None else '—'}"
                f" → #{top_rank if top_rank is not None else '—'}"
            )
            embed.add_field(name="Hạng trước → sau", value=rank_text, inline=True)
            if data[0].get("updatedAt"):
                embed.add_field(name="Đạt lúc", value=time_text(data[0]["updatedAt"]), inline=True)
            embed.add_field(
                name="Người đạt",
                value=", ".join(discord.utils.escape_markdown(n) for n in holders[:10]),
                inline=False,
            )
            embed.set_footer(text=f"Season {SEASON}")
            embed.timestamp = entry["updated"]
            alerts.append(embed)

    await asyncio.gather(*(check(sid) for sid in SONGS))
    save_state(state)

    if alerts and ALERT_CHANNEL_ID:
        channel = bot.get_channel(ALERT_CHANNEL_ID)
        if channel:
            for i in range(0, len(alerts), 10):  # tối đa 10 embed/tin nhắn
                await channel.send(embeds=alerts[i : i + 10])


@watch.before_loop
async def before_watch():
    await bot.wait_until_ready()


bot.run(TOKEN)
