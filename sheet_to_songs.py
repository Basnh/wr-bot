"""
Chuyển bảng bài hát từ Google Sheets sang songs.json cho wr_bot.py

Cách dùng:
    python sheet_to_songs.py                # tải thẳng từ Google Sheets
    python sheet_to_songs.py songs.csv      # hoặc đọc từ file CSV đã tải về

Yêu cầu: sheet phải được chia sẻ công khai (ai có link đều xem được).
Nếu không tải trực tiếp được, hãy vào File > Download > CSV rồi chạy với file CSV.

Kết quả: songs.json dạng
    {"0": {"artist": "J.Y. Park", "song": "...", "album": "Back To Stage", "cover": "https://..."}, ...}
"""

import csv
import io
import json
import os
import sys
import urllib.request

SHEET_ID = "1nDmSiHxvK49KogoFQa8JAWgiv8gEppepXRdeaUvoQVg"
GID = "846847389"
EXPORT_URL = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid={GID}"
OUTPUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "songs.json")

# Tên cột trong sheet (không phân biệt hoa thường)
COL_ID, COL_ARTIST, COL_SONG = "id", "artist", "song"
COL_ALBUM = "album name"  # tùy chọn, nếu sheet không có cột này thì để trống
COL_COVER = "album art"   # tùy chọn; cột Image trong sheet hiện chứa #REF!


def read_csv_text() -> str:
    if len(sys.argv) > 1:
        with open(sys.argv[1], "r", encoding="utf-8-sig") as f:
            return f.read()
    req = urllib.request.Request(EXPORT_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8-sig")


def main() -> None:
    rows = list(csv.reader(io.StringIO(read_csv_text())))

    # tìm dòng tiêu đề chứa ID / Artist / Song
    header_idx = next(
        (i for i, row in enumerate(rows)
         if {c.strip().lower() for c in row} >= {COL_ID, COL_ARTIST, COL_SONG}),
        None,
    )
    if header_idx is None:
        sys.exit("Không tìm thấy dòng tiêu đề có các cột ID, Artist, Song.")

    header = [c.strip().lower() for c in rows[header_idx]]
    i_id, i_artist, i_song = (header.index(c) for c in (COL_ID, COL_ARTIST, COL_SONG))
    i_album = header.index(COL_ALBUM) if COL_ALBUM in header else None
    i_cover = header.index(COL_COVER) if COL_COVER in header else None

    songs: dict[str, dict] = {}
    skipped = 0
    for row in rows[header_idx + 1:]:
        if len(row) <= max(i_id, i_artist, i_song):
            continue
        sid, artist, song = row[i_id].strip(), row[i_artist].strip(), row[i_song].strip()
        if not sid.isdigit() or not song:
            skipped += 1
            continue
        if sid in songs:
            print(f"Cảnh báo: ID {sid} bị trùng, giữ dòng đầu tiên")
            continue
        album = row[i_album].strip() if i_album is not None and len(row) > i_album else ""
        cover = row[i_cover].strip() if i_cover is not None and len(row) > i_cover else ""
        songs[sid] = {"artist": artist, "song": song, "album": album, "cover": cover}

    with open(OUTPUT, "w", encoding="utf-8") as f:
        json.dump(songs, f, ensure_ascii=False, indent=2)

    print(f"Đã ghi {len(songs)} bài vào {OUTPUT} (bỏ qua {skipped} dòng không hợp lệ)")
    if "153" in songs:
        s = songs["153"]
        print(f"ID 153 = {s['artist']} - {s['song']}")
    else:
        print("Lưu ý: không có ID 153 trong sheet, hãy kiểm tra lại cách đánh ID.")


if __name__ == "__main__":
    main()
