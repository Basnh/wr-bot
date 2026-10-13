# SUPERSTAR JYP World Record Discord Bot

Discord bot theo dõi bảng xếp hạng World Record (WR) của SUPERSTAR JYP. Bot có thể tra cứu leaderboard, thông báo khi điểm WR tăng, hiển thị album cover và theo dõi WR của một danh sách người chơi.

## Tính năng

- Tra cứu leaderboard bằng slash command hoặc prefix command, có phân trang.
- Hiển thị album cover dạng thumbnail; màu embed được lấy từ cover khi tải ảnh thành công.
- Theo dõi điểm top 1 của các bài hát và gửi thông báo khi có WR mới.
- Tự động so sánh top 100 theo chu kỳ và chỉ gửi thông báo khi điểm của người chơi tăng.
- Tra cứu những bài mà người chơi trong danh sách theo dõi đang giữ WR.
- Quản lý và đồng bộ danh sách bài hát từ Google Sheets hoặc CSV.
- Tùy chọn tự cập nhật mã nguồn trên VPS qua `!deploy` và systemd.

## Yêu cầu

- Python 3.10 trở lên.
- Bot application trên [Discord Developer Portal](https://discord.com/developers/applications).
- Kết nối Internet để gọi API leaderboard, tải cover và đọc Google Sheets.

## Cài đặt và chạy

Trong thư mục chứa `wr_bot.py`:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Mở `.env`, điền Discord bot token và cấu hình tùy chọn:

```dotenv
DISCORD_TOKEN=your_bot_token
ALERT_CHANNEL_ID=0

DEPLOY_GIT_BRANCH=main
DEPLOY_SYSTEMD_SERVICE=wr-bot.service
```

`ALERT_CHANNEL_ID` là ID kênh nhận thông báo WR mới; để `0` nếu chưa cấu hình. Các biến `DEPLOY_*` chỉ cần cho chức năng tự deploy lên VPS.

Trong Discord Developer Portal:

1. Bật **Message Content Intent** ở phần Bot để sử dụng lệnh prefix như `!wr`.
2. Mời bot vào server với quyền xem/gửi tin nhắn và nhúng liên kết (**Embed Links**).
3. Bảo đảm bot có thể truy cập kênh nhận thông báo đã cấu hình.

Slash commands hiện được đăng ký cho server có ID trong `SLASH_COMMAND_GUILD_ID` ở `wr_bot.py`. Nếu dùng server khác, đổi giá trị này trước khi chạy bot.

Khởi chạy bot:

```powershell
python manager.py
```

`manager.py` mở bot cùng cửa sổ console quản lý. Có thể chạy trực tiếp bằng `python wr_bot.py`; khi đó bot vẫn nhận lệnh Discord, nhưng không có tiện ích quản lý tiến trình của manager.

## Lệnh Discord

### Tra cứu leaderboard

| Lệnh | Mô tả |
| --- | --- |
| `/world_record` | Chọn nghệ sĩ và bài hát để xem leaderboard. Album là tùy chọn. |
| `!wr <ID hoặc từ khóa>` | Tra cứu nhanh theo ID, nghệ sĩ hoặc tên bài. |
| `!world_record <ID hoặc từ khóa>` | Tương đương `!wr`. |
| `!help` | Xem hướng dẫn lệnh trong bot. |

Ví dụ:

```text
!wr 153
!wr GOT7 Hard Carry
```

### Theo dõi người chơi

| Lệnh | Mô tả |
| --- | --- |
| `/track player:<tên hoặc Tất cả>` | Chọn một người chơi hoặc tất cả người trong danh sách theo dõi. |
| `!track` | Mở menu người chơi. Sau khi xem kết quả, dùng menu nghệ sĩ để lọc; mặc định bài được gom và xếp theo nghệ sĩ. |
| `!track <tên>` | Tra cứu nhanh một người chơi. |
| `!track list` | Xem danh sách theo dõi. |
| `!track add <tên>` | Thêm người chơi vào danh sách theo dõi. |
| `!track remove <tên>` | Xóa người chơi khỏi danh sách. |

Lệnh thêm/xóa người chơi yêu cầu quyền **Manage Server**. Danh sách theo dõi cũng được lưu trong `players.txt`, mỗi dòng một tên.

### Danh sách bài hát

Lệnh slash `/songs` hỗ trợ `list`, `artists`, `search`, `add`, `remove` và `sync`. Kết quả `list` có embed phân trang, gom và sắp xếp bài theo album; nếu không lọc nghệ sĩ, tiêu đề nhóm cũng hiện tên nghệ sĩ để phân biệt các album trùng tên. Nhóm lệnh prefix tương ứng là:

```text
!songs list
!songs artists
!songs search hard carry
!songs add 153 "ITZY" "Imaginary Friend" --album "Crazy In Love"
!songs remove 153
!songs sync
```

Các lệnh thay đổi dữ liệu chỉ dành cho chủ bot. `!songs sync` đọc sheet đã cấu hình trong `songs_tool.py`; mặc định thêm bài mới và cập nhật album cover mà không ghi đè thông tin bài đã có. Có thể dùng `!songs sync --overwrite` để đồng bộ đè thông tin từ sheet hoặc `!songs sync --dry-run` để xem trước. Công cụ cũng nhận CSV bằng `!songs sync --csv <đường-dẫn-tệp.csv>`.

### Lệnh chủ bot

| Lệnh | Mô tả |
| --- | --- |
| `!reload` | Đọc lại `songs.json` và `players.txt` ngay lập tức. |
| `!refresh_wr` | Làm mới leaderboard cho tất cả ID và ghi snapshot để so sánh với lần sau. Alias: `!update_wr`, `!wr_update`. |
| `!deploy` | Trên VPS đã cấu hình, lấy code mới từ Git và khởi động lại systemd service. |

Watcher kiểm tra top 1 và top 100 khoảng 3 phút một lần. Khi điểm của người đã có trong top 100 tăng, bot gửi embed vào `ALERT_CHANNEL_ID`, kèm điểm cũ/mới và hạng hiện tại. Bot không gửi thông báo chỉ vì đổi thứ hạng, tụt hạng, rời top 100 hoặc lần đầu xuất hiện trong top 100. Dù vậy, các thay đổi này vẫn được cập nhật trong snapshot để lần so sánh sau dùng dữ liệu mới nhất. Lần đầu với ID chưa có snapshot chỉ tạo baseline, không gửi thông báo hàng loạt. ID không tải được dữ liệu mới sẽ giữ snapshot cũ.

`!refresh_wr` vẫn có thể dùng để làm mới ngay toàn bộ danh sách bài. Lệnh này tạo baseline nếu chưa có snapshot; nếu phát hiện điểm tăng trong top 100 thì gửi embed vào kênh cảnh báo đã cấu hình và báo tổng kết tại kênh gọi lệnh. Trạng thái snapshot được lưu ở `wr_rankings.json`, không đồng bộ qua Git.

Bot kiểm tra điểm WR nền định kỳ khoảng 3 phút. Ở lần chạy đầu, bot lưu trạng thái hiện tại làm baseline và không gửi cảnh báo cho các bài đó. Để nhận cảnh báo, đặt `ALERT_CHANNEL_ID` thành ID kênh Discord phù hợp.

## Dữ liệu

- `songs.json`: danh sách bài hát theo ID, gồm nghệ sĩ, tên bài, album và URL cover. Cover được hiển thị cạnh nội dung embed dưới dạng thumbnail.
- `players.txt`: tên người chơi cần theo dõi, mỗi tên một dòng.
- `wr_state.json`: điểm top 1 gần nhất dùng cho watcher thông báo WR mới.
- `wr_rankings.json`: snapshot leaderboard được watcher và `!refresh_wr` dùng để so sánh rank/điểm. Đây là dữ liệu runtime và được loại khỏi Git.

Ví dụ một bản ghi trong `songs.json`:

```json
{
  "153": {
    "artist": "ITZY",
    "song": "Tên bài hát",
    "album": "Tên album",
    "cover": "https://example.com/album-cover.jpg"
  }
}
```

Không commit `.env` hoặc Discord token. `.gitignore` đã loại các file token, cache và trạng thái runtime phổ biến.

## Chạy manager từ console

Khi dùng `python manager.py`, nhập `help` trong cửa sổ console để xem các thao tác quản lý. Một số ví dụ:

```text
status
logs 50
reload
restart
songs list
songs sync --dry-run
```

## Deploy lên VPS

Các bước thiết lập GitHub private repository, systemd và quyền restart an toàn được mô tả trong [DEPLOY.md](./DEPLOY.md). Tóm tắt quy trình cập nhật: commit và push code từ máy phát triển, sau đó chạy `!deploy` trên bot đang hoạt động ở VPS.

## Cấu trúc chính

| Tệp | Vai trò |
| --- | --- |
| `wr_bot.py` | Bot Discord, leaderboard, watcher WR, tra cứu và lệnh quản trị. |
| `manager.py` | Chạy bot và điều khiển tiến trình/log từ console. |
| `songs_tool.py` | Đọc Google Sheets/CSV và quản lý `songs.json`. |
| `sheet_to_songs.py` | Tiện ích chuyển dữ liệu sheet sang định dạng danh sách bài hát. |
| `requirements.txt` | Các thư viện Python cần cài đặt. |
| `.env.example` | Mẫu biến môi trường. |
