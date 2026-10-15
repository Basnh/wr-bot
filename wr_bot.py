"""Entry point cho bot; triển khai được chia trong thư mục bot_code."""

from pathlib import Path


_MODULE_DIR = Path(__file__).with_name("bot_code")
_MODULES = (
    "core.py",
    "presentation.py",
    "commands_help.py",
    "commands_rankings.py",
    "commands_songs.py",
    "commands_track.py",
    "runtime.py",
)

# Nạp theo cùng namespace để giữ nguyên cách chia sẻ trạng thái của phiên bản cũ.
for _module_name in _MODULES:
    _module_path = _MODULE_DIR / _module_name
    exec(compile(_module_path.read_text(encoding="utf-8"), str(_module_path), "exec"), globals())
