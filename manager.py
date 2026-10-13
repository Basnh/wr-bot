"""
manager.py - mở bot và điều khiển mọi thứ từ một cửa sổ console

Chạy:
    python manager.py                  # mở bot rồi vào console
    python manager.py --no-start       # chỉ vào console, chưa mở bot
    python manager.py --auto-restart   # bot bị lỗi/tắt bất thường thì tự mở lại

Trong console gõ "help" để xem lệnh. Ví dụ:
    status                       trạng thái bot
    reload                       bot đọc lại songs.json ngay (không cần khởi động lại)
    restart | stop | start       điều khiển bot
    logs 50                      xem lại 50 dòng log gần nhất của bot
    songs add 153 "ITZY" "Imaginary Friend [Verse 2]" --album "Crazy In Love"
    songs sync                   thêm bài mới từ Google Sheets (xem songs -h)
    run my_script.py arg1 arg2   chạy một script bất kỳ trong thư mục này

THÊM LỆNH MỚI SAU NÀY (không cần sửa phần còn lại):
  1) Có script mới (vd: backup.py) -> thêm một dòng vào SCRIPTS bên dưới,
     rồi gõ "backup <tham số>" trong console.
  2) Muốn lệnh viết bằng Python ngay trong file này -> thêm một hàm có decorator @command(...).
  3) Chạy thử nhanh không cần đăng ký: "run ten_file.py <tham số>".
Gợi ý: đường dẫn trong console nên dùng dấu / (vd: run tools/a.py), tên có dấu cách thì đặt trong "ngoặc kép".
"""

import argparse
import collections
import os
import shlex
import subprocess
import sys
import threading
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BOT_SCRIPT = "wr_bot.py"

# ---------------------------------------------------------------------------
# Script đăng ký sẵn: gõ "<tên> <tham số>" = chạy "python <file> <tham số>"
#   desc         : mô tả hiện trong "help"
#   reload_after : None = không; "*" = luôn luôn; hoặc tập các tham số đầu tiên
#                  (vd {"add", "sync"}) -> chạy xong thành công thì tự báo bot reload
# ---------------------------------------------------------------------------
SCRIPTS = {
    "songs": {
        "file": "songs_tool.py",
        "desc": "quản lý songs.json (add / remove / list / search / sync). Gõ 'songs -h' để xem chi tiết",
        "reload_after": {"add", "set", "remove", "rm", "sync"},
    },
    "sheet": {
        "file": "sheet_to_songs.py",
        "desc": "tạo lại songs.json từ Google Sheets (GHI ĐÈ toàn bộ, mất chỉnh sửa tay; nên dùng 'songs sync')",
        "reload_after": "*",
    },
}


def say(msg: str) -> None:
    print(f"[manager] {msg}", flush=True)


def child_env() -> dict:
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    return env


def fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m}m {s}s" if h else (f"{m}m {s}s" if m else f"{s}s")


# ----------------------------- Quản lý tiến trình bot -----------------------------
class BotProcess:
    def __init__(self, script: str, auto_restart: bool = False) -> None:
        self.script = script
        self.auto_restart = auto_restart
        self.proc: subprocess.Popen | None = None
        self.started_at = 0.0
        self.stopping = False
        self.quick_failures = 0
        self.log: collections.deque[str] = collections.deque(maxlen=500)

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self) -> None:
        if self.running:
            say(f"Bot đang chạy rồi (pid {self.proc.pid}).")
            return
        path = os.path.join(BASE_DIR, self.script)
        if not os.path.exists(path):
            say(f"Không thấy file bot: {path}")
            return

        # tách bot khỏi nhóm tiến trình của console để Ctrl+C trong console không giết bot
        extra = {}
        if os.name == "nt":
            extra["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            extra["start_new_session"] = True

        self.stopping = False
        self.proc = subprocess.Popen(
            [sys.executable, "-u", path],
            cwd=BASE_DIR,
            env=child_env(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            **extra,
        )
        self.started_at = time.time()
        threading.Thread(target=self._pump, args=(self.proc,), daemon=True).start()
        say(f"Đã mở bot (pid {self.proc.pid}).")

    def _pump(self, proc: subprocess.Popen) -> None:
        """Đọc log của bot và in ra console; xử lý khi bot dừng."""
        for line in proc.stdout:
            line = line.rstrip("\n")
            self.log.append(line)
            print(f"[bot] {line}", flush=True)
        code = proc.wait()
        uptime = time.time() - self.started_at
        if self.proc is not proc:
            return
        say(f"Bot đã dừng (exit code {code}, chạy được {fmt_duration(uptime)}).")

        if self.stopping or not self.auto_restart or code == 0:
            return
        self.quick_failures = self.quick_failures + 1 if uptime < 30 else 0
        if self.quick_failures >= 5:
            say("Bot lỗi liên tục, ngừng tự mở lại. Hãy xem 'logs' rồi gõ 'start'.")
            return
        say("Tự mở lại bot sau 5 giây...")
        time.sleep(5)
        if not self.stopping and not self.running:
            self.start()

    def send(self, line: str) -> bool:
        if not self.running:
            return False
        try:
            self.proc.stdin.write(line + "\n")
            self.proc.stdin.flush()
            return True
        except (BrokenPipeError, OSError, ValueError):
            return False

    def stop(self, timeout: float = 10.0) -> None:
        if not self.running:
            self.stopping = True  # hủy luôn lần tự mở lại đang chờ (nếu có)
            say("Bot không chạy.")
            return
        self.stopping = True
        say("Đang dừng bot...")
        self.send("quit")
        try:
            self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            say("Bot không phản hồi, buộc dừng.")
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        time.sleep(0.2)  # chờ luồng đọc log in nốt dòng cuối

    def status(self) -> str:
        if self.running:
            return f"Bot ĐANG CHẠY (pid {self.proc.pid}, đã chạy {fmt_duration(time.time() - self.started_at)})"
        if self.proc is not None:
            return f"Bot ĐÃ DỪNG (exit code {self.proc.returncode})"
        return "Bot chưa được mở"


bot_proc: BotProcess  # được tạo trong main()

# ----------------------------- Bộ đăng ký lệnh -----------------------------
COMMANDS: dict[str, tuple] = {}


def command(name: str, help_text: str, aliases: tuple = ()):
    """Đăng ký một lệnh console. Hàm nhận danh sách tham số (list[str])."""

    def deco(fn):
        COMMANDS[name] = (fn, help_text, False)
        for a in aliases:
            COMMANDS[a] = (fn, help_text, True)  # True = bí danh, không hiện trong help
        return fn

    return deco


@command("help", "hiện danh sách lệnh", aliases=("?", "h"))
def cmd_help(args: list[str]) -> None:
    print("Lệnh điều khiển bot:")
    for name, (_, text, is_alias) in COMMANDS.items():
        if not is_alias:
            print(f"  {name:<9} {text}")
    print("Lệnh script:")
    for name, info in SCRIPTS.items():
        print(f"  {name:<9} {info['desc']}")


@command("start", "mở bot")
def cmd_start(args: list[str]) -> None:
    bot_proc.start()


@command("stop", "dừng bot")
def cmd_stop(args: list[str]) -> None:
    bot_proc.stop()


@command("restart", "khởi động lại bot")
def cmd_restart(args: list[str]) -> None:
    if bot_proc.running:
        bot_proc.stop()
    bot_proc.start()


@command("status", "xem trạng thái bot")
def cmd_status(args: list[str]) -> None:
    say(bot_proc.status())


@command("reload", "bot đọc lại songs.json ngay, không cần khởi động lại")
def cmd_reload(args: list[str]) -> None:
    if not bot_proc.running:
        say("Bot chưa chạy, songs.json sẽ được đọc khi bạn gõ 'start'.")
    elif not bot_proc.send("reload"):
        say("Không gửi được lệnh tới bot.")


@command("logs", "xem lại log gần nhất của bot (vd: logs 50)")
def cmd_logs(args: list[str]) -> None:
    n = int(args[0]) if args and args[0].isdigit() else 30
    lines = list(bot_proc.log)[-n:]
    if not lines:
        say("Chưa có log.")
    for line in lines:
        print(f"[bot] {line}")


@command("run", "chạy một script bất kỳ trong thư mục: run <file.py> [tham số]")
def cmd_run(args: list[str]) -> None:
    if not args:
        say("Cách dùng: run <file.py> [tham số]")
        return
    run_script(args[0], args[1:])


@command("exit", "tắt bot và thoát", aliases=("quit", "q"))
def cmd_exit(args: list[str]) -> None:
    raise SystemExit


# ----------------------------- Chạy script -----------------------------
def run_script(file: str, args: list[str], reload_after=None) -> int | None:
    path = file if os.path.isabs(file) else os.path.join(BASE_DIR, file)
    if not os.path.exists(path):
        say(f"Không thấy file: {file}")
        return None
    try:
        code = subprocess.run([sys.executable, path, *args], cwd=BASE_DIR, env=child_env()).returncode
    except KeyboardInterrupt:
        say("Đã hủy.")
        return None

    should_reload = reload_after == "*" or (
        isinstance(reload_after, (set, frozenset, list, tuple)) and args and args[0] in reload_after
    )
    if code == 0 and should_reload and bot_proc.running:
        bot_proc.send("reload")
    elif code != 0:
        say(f"Script kết thúc với mã lỗi {code}.")
    return code


# ----------------------------- Vòng lặp console -----------------------------
def dispatch(line: str) -> None:
    try:
        parts = shlex.split(line)
    except ValueError as e:
        say(f"Sai cú pháp: {e}")
        return
    if not parts:
        return

    name, args = parts[0].lower(), parts[1:]
    if name in COMMANDS:
        COMMANDS[name][0](args)
    elif name in SCRIPTS:
        info = SCRIPTS[name]
        run_script(info["file"], args, info.get("reload_after"))
    else:
        say(f"Lệnh không rõ: {name!r}. Gõ 'help' để xem danh sách.")


def shutdown() -> None:
    if bot_proc.running:
        bot_proc.stop()
    say("Tạm biệt!")


def main() -> None:
    global bot_proc
    ap = argparse.ArgumentParser(description="Mở bot và điều khiển bằng console")
    ap.add_argument("--bot", default=BOT_SCRIPT, help=f"file bot (mặc định {BOT_SCRIPT})")
    ap.add_argument("--no-start", action="store_true", help="chỉ vào console, chưa mở bot")
    ap.add_argument("--auto-restart", action="store_true", help="tự mở lại bot khi bị lỗi")
    opts = ap.parse_args()

    bot_proc = BotProcess(opts.bot, auto_restart=opts.auto_restart)
    if not opts.no_start:
        bot_proc.start()
    say("Gõ 'help' để xem các lệnh.")

    try:
        while True:
            try:
                line = input("> ")
            except EOFError:
                break
            try:
                dispatch(line)
            except SystemExit:
                break
            except Exception as e:  # một lệnh lỗi không được làm sập cả console
                say(f"Lỗi khi chạy lệnh: {e!r}")
    except KeyboardInterrupt:
        print()
    shutdown()


if __name__ == "__main__":
    main()
