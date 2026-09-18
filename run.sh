#!/bin/sh
# 安卓分区备份工具 — Linux / macOS 启动脚本
# 自动挑一个带 tkinter 的 Python，找不到就给出对应发行版的安装命令。
set -e
cd "$(dirname "$0")"

PY=""
for c in python3 python; do
    if command -v "$c" >/dev/null 2>&1; then
        if "$c" -c "import tkinter" >/dev/null 2>&1; then PY="$c"; break; fi
    fi
done

if [ -z "$PY" ]; then
    echo "错误：找不到带 tkinter 的 Python 3。"
    echo "  Ubuntu/Debian : sudo apt install python3-tk"
    echo "  Fedora        : sudo dnf install python3-tkinter"
    echo "  Arch          : sudo pacman -S tk"
    echo "  macOS         : brew install python-tk"
    exit 1
fi

if [ ! -x "adb/adb" ] && ! command -v adb >/dev/null 2>&1; then
    echo "提示：未找到 adb。安装方法："
    echo "  Ubuntu/Debian : sudo apt install android-tools-adb"
    echo "  macOS         : brew install android-platform-tools"
    echo "  或者把 platform-tools 里的 adb 放进本目录的 adb/ 下"
    echo
fi

exec "$PY" "backup_gui.py" "$@"
