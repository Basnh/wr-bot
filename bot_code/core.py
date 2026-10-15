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
  /top1 <nghệ sĩ> (hoặc !top1 <nghệ sĩ>) -> người đứng #1 ở tất cả bài của nghệ sĩ đó
  !track add|remove|list               -> quản lý danh sách theo dõi (add/remove cần quyền Manage Server)

Chạy nền:
  - Theo dõi top 1/top 100, báo vào kênh ALERT_CHANNEL_ID khi điểm người chơi tăng
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
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import aiohttp
import discord
import songs_tool
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
TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
if not TOKEN:
    raise SystemExit(
        "Thiếu DISCORD_TOKEN. Hãy sao chép .env.example thành .env trong thư mục "
        "wr_bot rồi điền token bot Discord vào DISCORD_TOKEN."
    )
ALERT_CHANNEL_ID = int(os.getenv("ALERT_CHANNEL_ID", "0"))
WR_ALERT_CHANNEL_ID = int(os.getenv("WR_ALERT_CHANNEL_ID", "0"))
DEPLOY_GIT_BRANCH = os.getenv("DEPLOY_GIT_BRANCH", "main").strip()
DEPLOY_SYSTEMD_SERVICE = os.getenv("DEPLOY_SYSTEMD_SERVICE", "").strip()

SEASON = 31
BASE_URL = "https://superstar-jyp.s3.ap-northeast-2.amazonaws.com/world_record_ranking/production"
GAME_TITLE = "SUPERSTAR JYP"
PREFIX = "!"            # dùng được cả !world_record và /world_record
SLASH_COMMAND_GUILD_ID = int(os.getenv("SLASH_COMMAND_GUILD_ID", "967430273883045898"))

PAGE_SIZE = 10          # số người mỗi trang
CACHE_TTL = 60          # giây, dùng cho lệnh /world_record
POLL_MINUTES = 15        # chu kỳ theo dõi nền
MAX_CONCURRENCY = 5     # số request song song tối đa

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
SONGS_FILE = os.path.join(DATA_DIR, "songs.json")       # {"153": {"artist": ..., "song": ..., "album": ...}}
STATE_FILE = os.path.join(DATA_DIR, "wr_state.json")    # lưu điểm top 1 lần trước để so sánh
RANKINGS_FILE = os.path.join(DATA_DIR, "wr_rankings.json")  # snapshot bảng xếp hạng từng bài

PLAYERS_FILE = os.path.join(DATA_DIR, "players.txt")    # mỗi dòng một người chơi cần theo dõi
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


def _dump_json_file(path: str, data) -> None:
    """Ghi JSON gọn + atomic. Dùng json.dumps (bộ mã hóa C) thay vì json.dump/indent
    (bộ mã hóa Python thuần, chậm gấp nhiều lần với file lớn)."""
    tmp_path = path + ".tmp"
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp_path, path)


def save_state(state: dict) -> None:
    _dump_json_file(STATE_FILE, state)


def load_rankings() -> dict:
    try:
        with open(RANKINGS_FILE, "r", encoding="utf-8") as f:
            rankings = json.load(f)
    except FileNotFoundError:
        return {"season": SEASON, "updated_at": None, "songs": {}}
    if not isinstance(rankings, dict) or not isinstance(rankings.get("songs"), dict):
        raise ValueError(f"{RANKINGS_FILE} không có định dạng snapshot bảng xếp hạng hợp lệ.")
    if rankings.get("season") != SEASON:
        return {"season": SEASON, "updated_at": None, "songs": {}}
    return rankings


def save_rankings(rankings: dict) -> None:
    _dump_json_file(RANKINGS_FILE, rankings)


def _rank_player_key(player: dict) -> str | None:
    player_id = player.get("objectID")
    if player_id is not None and str(player_id):
        return f"id:{player_id}"
    nickname = (player.get("nickname") or "").strip().casefold()
    return f"name:{nickname}" if nickname else None


def compare_rankings(previous: list[dict], current: list[dict]) -> dict:
    """So sánh hạng và điểm giữa hai snapshot, ghép người chơi bằng objectID."""
    old_by_player = {
        key: player
        for player in previous
        if (key := _rank_player_key(player)) is not None
    }
    new_by_player = {
        key: player
        for player in current
        if (key := _rank_player_key(player)) is not None
    }

    changes = []
    rank_changes = score_changes = 0
    for key, player in new_by_player.items():
        old = old_by_player.get(key)
        if old is None:
            changes.append({
                "type": "entered_top100",
                "objectID": player.get("objectID"),
                "nickname": player.get("nickname"),
                "old_rank": None,
                "new_rank": player.get("rank"),
                "old_score": None,
                "new_score": player.get("highscore"),
            })
            continue

        rank_changed = old.get("rank") != player.get("rank")
        score_changed = old.get("highscore") != player.get("highscore")
        rank_changes += int(rank_changed)
        score_changes += int(score_changed)
        if rank_changed or score_changed:
            changes.append({
                "type": "rank_and_score" if rank_changed and score_changed else (
                    "rank" if rank_changed else "score"
                ),
                "objectID": player.get("objectID"),
                "nickname": player.get("nickname"),
                "old_rank": old.get("rank"),
                "new_rank": player.get("rank"),
                "old_score": old.get("highscore"),
                "new_score": player.get("highscore"),
            })

    for key, player in old_by_player.items():
        if key not in new_by_player:
            changes.append({
                "type": "left_top100",
                "objectID": player.get("objectID"),
                "nickname": player.get("nickname"),
                "old_rank": player.get("rank"),
                "new_rank": None,
                "old_score": player.get("highscore"),
                "new_score": None,
            })

    return {
        "rank_changes": rank_changes,
        "score_changes": score_changes,
        "entered_top100": sum(change["type"] == "entered_top100" for change in changes),
        "left_top100": sum(change["type"] == "left_top100" for change in changes),
        "players": changes,
    }


def _score_increase(change: dict) -> int | float | None:
    old_score, new_score = change.get("old_score"), change.get("new_score")
    if not isinstance(old_score, (int, float)) or not isinstance(new_score, (int, float)):
        return None
    increase = new_score - old_score
    return increase if increase > 0 else None


def _is_tracked_player(change: dict) -> bool:
    """True nếu người chơi trong thay đổi này có trong players.txt
    (khớp objectID nếu dòng có ID, ngược lại khớp nickname không phân biệt hoa thường)."""
    pid = change.get("objectID")
    nick = _norm(change.get("nickname"))
    for entry in PLAYERS:
        if entry.get("id") is not None:
            if pid is not None and str(pid) == str(entry["id"]):
                return True
        elif nick and _norm(entry["nickname"]) == nick:
            return True
    return False


def _top100_change_embeds(song_id: str, report: dict) -> list[discord.Embed]:
    tracked = [change for change in report["players"] if _is_tracked_player(change)]
    improvements = [
        change for change in tracked
        if change.get("type") != "entered_top100" and _score_increase(change) is not None
    ]
    entrants = [
        change for change in tracked
        if change.get("type") == "entered_top100"
    ]
    if not improvements and not entrants:
        return []

    song = SONGS.get(song_id, {})
    title = discord.utils.escape_markdown(
        discord.utils.escape_mentions(display(song_id) if song_id in SONGS else f"Bài {song_id}")
    )[:250]
    summary_parts = []
    if entrants:
        summary_parts.append(f"🆕 **{len(entrants)}** người chơi mới vào top 100")
    if improvements:
        summary_parts.append(f"🎯 Điểm tăng của **{len(improvements)}** người chơi trong top 100")
    summary = "\n".join(summary_parts)

    def score_text(value) -> str:
        if isinstance(value, (int, float)):
            return f"{value:,.0f}"
        return str(value) if value is not None else "—"

    lines = []
    for change in entrants:
        nickname = discord.utils.escape_markdown(
            discord.utils.escape_mentions(change.get("nickname") or "Unknown")
        )[:50]
        rank = f"**#{change['new_rank']}**" if change.get("new_rank") else "—"
        lines.append(
            f"• 🆕 **{nickname}** · vào top 100 ở hạng {rank} · "
            f"**`{score_text(change['new_score'])}`**"
        )
    for change in improvements:
        nickname = discord.utils.escape_markdown(
            discord.utils.escape_mentions(change.get("nickname") or "Unknown")
        )[:50]
        increase = _score_increase(change) or 0
        rank = f" · hạng hiện tại **#{change['new_rank']}**" if change.get("new_rank") else ""
        lines.append(
            f"• **{nickname}**{rank} · "
            f"`{score_text(change['old_score'])}` → **`{score_text(change['new_score'])}`** "
            f"(+{score_text(increase)})"
        )

    lines_per_embed = 20
    chunks = [
        lines[index:index + lines_per_embed]
        for index in range(0, len(lines), lines_per_embed)
    ] or [[]]
    cover_url = song.get("cover", "")
    embeds = []
    for page, chunk in enumerate(chunks, 1):
        description = f"{summary}\n\n" + ("\n".join(chunk) if chunk else "Chưa có thay đổi.")
        embed = discord.Embed(
            title=f"📊 Thay đổi World Record · Top 100",
            description=description,
            color=discord.Color.blurple(),
        )
        embed.set_author(name=title)
        if cover_url.startswith(("https://", "http://")):
            embed.set_thumbnail(url=cover_url)
        footer = f"Season {SEASON}"
        if len(chunks) > 1:
            footer += f" · Trang {page}/{len(chunks)}"
        embed.set_footer(text=footer)
        embeds.append(embed)
    return embeds


async def _send_alert_embeds(
    embeds: list[discord.Embed],
    source: str,
    channel_id: int = ALERT_CHANNEL_ID,
) -> None:
    if not embeds:
        return
    if not channel_id:
        print(f"[{source}] Chưa cấu hình channel nhận cảnh báo; bỏ qua {len(embeds)} embed.", flush=True)
        return

    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except discord.HTTPException as error:
            print(f"[{source}] Không tìm thấy kênh cảnh báo {channel_id}: {error}", flush=True)
            return
    if not isinstance(channel, discord.abc.Messageable):
        print(f"[{source}] Kênh {channel_id} không hỗ trợ gửi tin nhắn.", flush=True)
        return

    for embed in embeds:
        try:
            await channel.send(embed=embed)
        except discord.HTTPException as error:
            print(f"[{source}] Không gửi được cảnh báo vào kênh {channel_id}: {error}", flush=True)


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
        for cache_key in list(wr.cache):
            if cache_key == sid or cache_key.endswith(f":{sid}"):
                wr.cache.pop(cache_key, None)
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
