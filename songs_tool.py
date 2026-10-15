"""
songs_tool.py - quản lý songs.json cho wr_bot.py

Ví dụ:
  python songs_tool.py add 153 "ITZY" "Imaginary Friend [Verse 2]" --album "Crazy In Love"
  python songs_tool.py add 153 "ITZY" "Imaginary Friend [Verse 2]"     # sửa bài đã có (giữ nguyên album cũ)
  python songs_tool.py remove 153
  python songs_tool.py list                  # tất cả bài
  python songs_tool.py list --artist GOT7    # chỉ một nhóm
  python songs_tool.py list --artists        # danh sách nhóm kèm số bài
  python songs_tool.py search hard carry     # tìm theo từ khóa
  python songs_tool.py sync --dry-run        # xem trước, chưa ghi gì
  python songs_tool.py sync                  # thêm bài mới và cập nhật cover (không đè thông tin bài đã có)
  python songs_tool.py sync --overwrite      # cập nhật cả bài đã có theo sheet
  python songs_tool.py sync --csv songs.csv  # đọc từ file CSV thay vì tải từ Google Sheets

Mỗi lần ghi, file cũ được sao lưu thành songs.json.bak.
Sau khi cập nhật, khởi động lại bot để nó đọc lại songs.json.
"""

import argparse
import csv
import io
import json
import os
import shutil
import sys
import urllib.request

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
SONGS_FILE = os.path.join(DATA_DIR, "songs.json")

SHEET_ID = "1nDmSiHxvK49KogoFQa8JAWgiv8gEppepXRdeaUvoQVg"
GID = "846847389"
EXPORT_URL = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid={GID}"


# ----------------------------- Đọc / ghi songs.json -----------------------------
def normalize(value) -> dict:
    """Chấp nhận định dạng cũ và chuẩn hóa thông tin bài hát cùng cover."""
    if isinstance(value, str):
        return {"artist": "", "song": value.strip(), "album": "", "cover": ""}
    return {
        "artist": (value.get("artist") or "").strip(),
        "song": (value.get("song") or "").strip(),
        "album": (value.get("album") or "").strip(),
        "cover": (value.get("cover") or "").strip(),
    }


def load() -> dict[str, dict]:
    if not os.path.exists(SONGS_FILE):
        return {}
    with open(SONGS_FILE, "r", encoding="utf-8") as f:
        return {str(k): normalize(v) for k, v in json.load(f).items()}


def save(songs: dict[str, dict]) -> None:
    if os.path.exists(SONGS_FILE):
        shutil.copy2(SONGS_FILE, SONGS_FILE + ".bak")
    ordered = dict(sorted(songs.items(), key=lambda kv: int(kv[0]) if kv[0].isdigit() else 10**9))
    tmp = SONGS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(ordered, f, ensure_ascii=False, indent=2)
    os.replace(tmp, SONGS_FILE)  # ghi nguyên tử, tránh hỏng file nếu bị ngắt giữa chừng


def label(s: dict) -> str:
    return f"{s['artist']} - {s['song']}" if s["artist"] else s["song"]


def check_id(sid: str) -> str:
    if not sid.isdigit():
        sys.exit(f"ID phải là số nguyên, nhận được: {sid!r}")
    return str(int(sid))


# ----------------------------- Các lệnh -----------------------------
def cmd_add(a) -> None:
    songs = load()
    sid = check_id(a.id)
    old = songs.get(sid)
    album = a.album if a.album is not None else (old["album"] if old else "")
    new = {
        "artist": a.artist.strip(),
        "song": a.song.strip(),
        "album": album.strip(),
        "cover": old["cover"] if old else "",
    }

    if old == new:
        print(f"ID {sid} đã đúng như vậy, không có gì thay đổi.")
        return
    songs[sid] = new
    save(songs)
    if old:
        print(f"Đã cập nhật ID {sid}:\n  trước: {label(old)} [{old['album']}]\n  sau  : {label(new)} [{new['album']}]")
    else:
        print(f"Đã thêm ID {sid}: {label(new)} [{new['album']}]")


def cmd_remove(a) -> None:
    songs = load()
    sid = check_id(a.id)
    if sid not in songs:
        sys.exit(f"Không có ID {sid} trong {SONGS_FILE}.")
    removed = songs.pop(sid)
    save(songs)
    print(f"Đã xóa ID {sid}: {label(removed)}")


def cmd_list(a) -> None:
    songs = load()
    if a.artists:
        counts: dict[str, int] = {}
        for s in songs.values():
            counts[s["artist"] or "(không rõ)"] = counts.get(s["artist"] or "(không rõ)", 0) + 1
        for name in sorted(counts, key=str.lower):
            print(f"{name}: {counts[name]} bài")
        print(f"Tổng: {len(counts)} nhóm, {len(songs)} bài")
        return

    shown = 0
    for sid, s in sorted(songs.items(), key=lambda kv: int(kv[0])):
        if a.artist and a.artist.lower() not in s["artist"].lower():
            continue
        print(f"{sid:>5} | {s['artist']} | {s['song']} | {s['album']}")
        shown += 1
    print(f"-- {shown} bài")


def cmd_search(a) -> None:
    songs = load()
    tokens = " ".join(a.keywords).lower().split()
    shown = 0
    for sid, s in sorted(songs.items(), key=lambda kv: int(kv[0])):
        hay = f"{s['artist']} {s['song']} {s['album']}".lower()
        if all(t in hay for t in tokens):
            print(f"{sid:>5} | {s['artist']} | {s['song']} | {s['album']}")
            shown += 1
    print(f"-- {shown} bài khớp")


# ----------------------------- Đồng bộ từ Google Sheets -----------------------------
def read_sheet_rows(csv_path: str | None) -> list[list[str]]:
    if csv_path:
        with open(csv_path, "r", encoding="utf-8-sig") as f:
            text = f.read()
    else:
        req = urllib.request.Request(EXPORT_URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            text = r.read().decode("utf-8-sig")
    return parse_sheet_text(text)


def parse_sheet_text(text: str) -> list[list[str]]:
    """Đọc các dòng CSV từ nội dung text, dùng được cả với file đính kèm Discord."""
    return list(csv.reader(io.StringIO(text)))


def parse_sheet(rows: list[list[str]]) -> dict[str, dict]:
    header_idx = next(
        (i for i, row in enumerate(rows)
         if {c.strip().lower() for c in row} >= {"id", "artist", "song"}),
        None,
    )
    if header_idx is None:
        raise ValueError("Không tìm thấy dòng tiêu đề có các cột ID, Artist, Song.")

    header = [c.strip().lower() for c in rows[header_idx]]
    i_id, i_artist, i_song = header.index("id"), header.index("artist"), header.index("song")
    i_album = header.index("album name") if "album name" in header else None
    i_cover = next(
        (
            header.index(name)
            for name in ("album art", "cover", "cover url", "cover image")
            if name in header
        ),
        None,
    )

    result: dict[str, dict] = {}
    for row in rows[header_idx + 1:]:
        if len(row) <= max(i_id, i_artist, i_song):
            continue
        sid, artist, song = row[i_id].strip(), row[i_artist].strip(), row[i_song].strip()
        if not sid.isdigit() or not song:
            continue
        album = row[i_album].strip() if i_album is not None and len(row) > i_album else ""
        cover = row[i_cover].strip() if i_cover is not None and len(row) > i_cover else ""
        result.setdefault(
            str(int(sid)),
            {"artist": artist, "song": song, "album": album, "cover": cover},
        )
    return result


def cmd_sync(a) -> None:
    songs = load()
    try:
        sheet = parse_sheet(read_sheet_rows(a.csv))
    except (OSError, ValueError) as e:
        sys.exit(f"Không tải được sheet ({e}). Hãy tải CSV về rồi chạy với --csv <file>.")

    added, updated = [], []
    for sid, new in sheet.items():
        old = songs.get(sid)
        if old is None:
            added.append(sid)
        elif (
            (a.overwrite and old != new)
            or (not a.overwrite and new["cover"] and old["cover"] != new["cover"])
        ):
            updated.append(sid)

    def preview(ids: list[str], title: str) -> None:
        if not ids:
            return
        print(f"{title}: {len(ids)} bài")
        for sid in sorted(ids, key=int)[:15]:
            print(f"   {sid:>5} | {label(sheet[sid])}")
        if len(ids) > 15:
            print(f"   ... và {len(ids) - 15} bài khác")

    preview(added, "Bài mới")
    preview(updated, "Bài sẽ được cập nhật")
    if not added and not updated:
        print("Không có gì mới để đồng bộ.")
        return
    if a.dry_run:
        print("(dry-run: chưa ghi file)")
        return

    for sid in added + updated:
        if sid in added or a.overwrite:
            songs[sid] = sheet[sid]
        else:
            songs[sid]["cover"] = sheet[sid]["cover"]
    save(songs)
    print(f"Đã ghi {SONGS_FILE}: thêm {len(added)}, cập nhật {len(updated)}, tổng {len(songs)} bài.")


# ----------------------------- CLI -----------------------------
def main() -> None:
    p = argparse.ArgumentParser(description="Quản lý songs.json cho wr_bot")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("add", aliases=["set"], help="thêm bài mới hoặc sửa bài đã có")
    s.add_argument("id")
    s.add_argument("artist")
    s.add_argument("song")
    s.add_argument("--album", default=None, help="nếu bỏ qua khi sửa bài cũ thì giữ album cũ")
    s.set_defaults(func=cmd_add)

    s = sub.add_parser("remove", aliases=["rm"], help="xóa một bài theo ID")
    s.add_argument("id")
    s.set_defaults(func=cmd_remove)

    s = sub.add_parser("list", help="liệt kê bài hát")
    s.add_argument("--artist", help="chỉ hiện bài của nhóm này")
    s.add_argument("--artists", action="store_true", help="liệt kê nhóm kèm số bài")
    s.set_defaults(func=cmd_list)

    s = sub.add_parser("search", help="tìm bài theo từ khóa")
    s.add_argument("keywords", nargs="+")
    s.set_defaults(func=cmd_search)

    s = sub.add_parser("sync", help="đồng bộ bài mới và cover; dùng --overwrite để cập nhật toàn bộ")
    s.add_argument("--csv", help="đọc từ file CSV thay vì tải từ Google Sheets")
    s.add_argument("--overwrite", action="store_true", help="cập nhật cả bài đã có theo sheet")
    s.add_argument("--dry-run", action="store_true", help="chỉ xem trước, không ghi file")
    s.set_defaults(func=cmd_sync)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
