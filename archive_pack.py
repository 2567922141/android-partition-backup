# -*- coding: utf-8 -*-
"""
备份产物打包 —— 把整个备份目录压成一个 zip
================================================================================
【定位】
    本模块**只做「目录 → 单个 zip」这一件事**，不参与任何备份/校验逻辑，
    也不决定压缩包放哪里（out_path 一律由调用方给）。

【依赖】
    **零第三方依赖，零外部进程** —— 只用 Python 标准库 zipfile + zlib。

【为什么只做 zip】
    · zip 是 Windows 资源管理器**双击即开**的格式，用户拿到的备份包不需要
      再装任何东西。
    · 曾经评估过 7z（要外挂 7z.exe）与 rar（体积更小），都否决了：
      rar 的 Rar.exe 许可证禁止再分发，无法随本工具一起打包；
      7z 需要用户机器上恰好有 7z.exe，找不到就得降级，收益不抵复杂度。
    · 压缩级别用 `zipfile.ZIP_DEFLATED`（zlib）。
      ⚠️ **刻意不用 ZIP_LZMA** —— 它体积确实更小，但 Windows 资源管理器
         打不开，等于把「双击即开」这个唯一的价值丢掉了。

【进度】
    zip 分支由本模块自己遍历目录，天然知道 total_files / total_bytes，
    每处理完一个文件回调一次 progress_cb(done_files, total_files,
    done_bytes, total_bytes) —— 精确值，不是估算。
    大文件走分块流式，每 1 MiB 查一次取消，所以几百 MB 的镜像也能秒停。

【取消】
    cancel_check() 返回 True 时尽快中止。
    中止/失败后会把**本次自己写出来的半成品**删掉；调用前就存在的文件
    会先被清掉再写（见 make_archive 的说明），避免留下半个坏压缩包。
================================================================================
"""

from __future__ import annotations

import os
import time
import zipfile
from dataclasses import dataclass
from typing import Optional

__all__ = [
    "ArchiveResult",
    "make_archive",
    "add_to_archive",
    "human_size",
    "ONLY_FORMAT",
    "DEFAULT_LEVEL",
]

# 只支持这一种。保留这个常量而不是到处硬编码字符串，
# 是为了让「将来要加格式」这件事有个明确的落点。
ONLY_FORMAT = "zip"

DEFAULT_LEVEL = 6

# 分块读的粒度：大文件每读这么多字节就查一次取消
_COPY_CHUNK = 1024 * 1024

# 小于这个体积的文件直接交给 ZipFile.write()（它一次写完，没有取消的余地）；
# 大文件走分块流式，才能在中途响应取消。
_STREAM_THRESHOLD = 4 * 1024 * 1024


def human_size(n: Optional[int]) -> str:
    """与 backup_core.human_size 完全一致的格式化。

    刻意在这里重写一份而不是 import backup_core —— backup_core 要 import 本模块，
    反过来 import 会构成循环依赖。两边的输出格式必须保持一致（测试里会对拍）。
    """
    if n is None:
        return "-"
    if n < 1024:
        return f"{n} B"
    v = float(n)
    for unit in ("KB", "MB", "GB", "TB"):
        v /= 1024.0
        if v < 1024 or unit == "TB":
            return f"{v:.1f} {unit}"
    return f"{v:.1f} TB"


# ==============================================================================
#  结果
# ==============================================================================

@dataclass
class ArchiveResult:
    ok: bool
    path: str          # 压缩包完整路径（失败时是打算写的路径）
    fmt: str           # 恒为 "zip"（当前只做这一种）
    src_bytes: int     # 原始总字节
    out_bytes: int     # 压缩包字节（失败为 0）
    seconds: float
    message: str       # 给用户看的一句话
    error: str = ""    # 失败原因（原文，用于排错）


def _fail(path: str, src_bytes: int, seconds: float,
          message: str, error: str) -> ArchiveResult:
    return ArchiveResult(ok=False, path=path, fmt=ONLY_FORMAT,
                         src_bytes=src_bytes, out_bytes=0, seconds=seconds,
                         message=message, error=error)


def _success_message(src: int, out: int) -> str:
    if src <= 0:
        return human_size(out)
    saved = (1.0 - out / float(src)) * 100.0
    if saved >= 0:
        return f"{human_size(src)} → {human_size(out)}（省 {saved:.1f}%）"
    return f"{human_size(src)} → {human_size(out)}（反而增大 {-saved:.1f}%）"


# ==============================================================================
#  目录统计
# ==============================================================================

def _entries(src_dir: str, root_name: str) -> tuple[int, int, list[tuple[str, str]]]:
    """遍历源目录，返回 (文件数, 总字节, [(绝对路径, 归档内相对路径)])。

    归档内路径一律用正斜杠（zip 规范），且**以备份目录名开头**，形如：

        Redmi K70/img/persist.img
        Redmi K70/manifest.txt

    这样有两个好处：
      · 包里绝不会出现 `D:\\下载目录（2）\\...` 那种整条绝对路径
      · 解压时会先建出 `Redmi K70\\` 这一层，不会把一堆文件散落到当前目录
    """
    files: list[tuple[str, str]] = []
    total = 0
    for root, dirs, names in os.walk(src_dir):
        dirs.sort()
        for nm in sorted(names):
            full = os.path.join(root, nm)
            if not os.path.isfile(full) or os.path.islink(full):
                continue
            rel = os.path.relpath(full, src_dir).replace(os.sep, "/")
            files.append((full, f"{root_name}/{rel}"))
            try:
                total += os.path.getsize(full)
            except OSError:
                pass
    return len(files), total, files


class _Cancelled(Exception):
    """内部信号：取消。不外泄 —— make_archive 一律翻译成 ok=False。"""


# ==============================================================================
#  打包
# ==============================================================================

def _make_zip(src_dir: str, out_path: str, level: int,
              progress_cb, cancel_check) -> ArchiveResult:
    t0 = time.time()
    root_name = os.path.basename(os.path.normpath(src_dir))
    n_files, src_bytes, files = _entries(src_dir, root_name)
    if n_files == 0:
        return _fail(out_path, 0, time.time() - t0,
                     "源目录里没有任何文件，未生成压缩包", "empty source directory")

    done_files = 0
    done_bytes = 0
    abs_out = os.path.abspath(out_path)

    try:
        with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED,
                             compresslevel=level, allowZip64=True) as zf:
            for full, rel in files:
                if cancel_check and cancel_check():
                    raise _Cancelled()
                # 防御：万一调用方把压缩包放进了源目录里，别把自己也压进去
                if os.path.abspath(full) == abs_out:
                    continue
                try:
                    size = os.path.getsize(full)
                except OSError:
                    size = 0

                if size <= _STREAM_THRESHOLD:
                    # 小文件：交给 zipfile 自己写，mtime / 权限位一并带上
                    zf.write(full, rel)
                else:
                    # 大文件：分块流式，每 1 MiB 查一次取消。
                    # 分区镜像动辄几百 MB，一次性 write() 的话取消要等到天荒地老。
                    _zip_stream_one(zf, full, rel, size, cancel_check)

                done_files += 1
                done_bytes += size
                if progress_cb:
                    progress_cb(done_files, n_files, done_bytes, src_bytes)
    except _Cancelled:
        _discard(out_path)
        return _fail(out_path, src_bytes, time.time() - t0,
                     "已取消打包", "cancelled by user")
    except Exception as e:                        # 兜底：任何异常都不许外泄
        _discard(out_path)
        return _fail(out_path, src_bytes, time.time() - t0,
                     f"打包失败：{type(e).__name__}: {e}",
                     f"{type(e).__name__}: {e}")

    try:
        out_bytes = os.path.getsize(out_path)
    except OSError:
        out_bytes = 0
    if out_bytes <= 0:
        _discard(out_path)
        return _fail(out_path, src_bytes, time.time() - t0,
                     "压缩包没有生成或为空", "output file missing or empty")
    if progress_cb:
        progress_cb(n_files, n_files, src_bytes, src_bytes)
    return ArchiveResult(ok=True, path=out_path, fmt=ONLY_FORMAT,
                         src_bytes=src_bytes, out_bytes=out_bytes,
                         seconds=time.time() - t0,
                         message=_success_message(src_bytes, out_bytes))


def _zip_stream_one(zf: zipfile.ZipFile, full: str, rel: str, size: int,
                    cancel_check) -> None:
    """把一个文件分块压进 zip，保留 mtime 与权限位。

    走 ZipInfo + zf.open(..., "w") 而不是 zf.write()，是为了能在**文件内部**
    也响应取消；代价是元数据要自己填。
    """
    st = os.stat(full)
    # zip 的 DOS 时间戳只能表示 1980~2107，越界会直接抛 ValueError，必须夹一下
    try:
        lt = time.localtime(st.st_mtime)
        dt = (min(max(lt.tm_year, 1980), 2107), lt.tm_mon, lt.tm_mday,
              lt.tm_hour, lt.tm_min, lt.tm_sec)
    except (OSError, ValueError, OverflowError):
        dt = time.localtime()[:6]

    zi = zipfile.ZipInfo(rel, date_time=dt)
    zi.compress_type = zipfile.ZIP_DEFLATED
    zi.external_attr = (st.st_mode & 0xFFFF) << 16
    zi.file_size = size
    with open(full, "rb") as src, zf.open(zi, "w") as dst:
        while True:
            if cancel_check and cancel_check():
                raise _Cancelled()
            chunk = src.read(_COPY_CHUNK)
            if not chunk:
                break
            dst.write(chunk)


def _discard(path: str) -> None:
    """删掉本次写出来的半成品。失败/取消时调用。

    只删我们自己刚写的那一个文件；源目录、已有备份一律不碰。
    """
    try:
        if path and os.path.isfile(path):
            os.remove(path)
    except OSError:
        pass


# ==============================================================================
#  对外入口
# ==============================================================================

def make_archive(src_dir: str, out_path: str,
                 level: int = DEFAULT_LEVEL, progress_cb=None,
                 cancel_check=None) -> ArchiveResult:
    """把 src_dir 整个目录压成 out_path（zip，ZIP_DEFLATED，compresslevel=level）。

    progress_cb(done_files, total_files, done_bytes, total_bytes)
        每处理完一个文件调一次；末尾还会再补一次 100% 的收尾回调。

    cancel_check() -> bool，返回 True 时尽快中止并返回 ok=False。

    ⚠️ 本函数**绝不抛异常** —— 一切失败都翻译成 ok=False 的 ArchiveResult。
    ⚠️ out_path 由调用方决定，本函数不负责选位置。
    ⚠️ 若 out_path 已存在会先删掉再写，保证拿到的压缩包一定是**本次**的产物，
        不会和上一次的内容混在一起。
    """
    t0 = time.time()
    src_dir = os.path.abspath(src_dir or "")
    out_path = os.path.abspath(out_path or "")

    try:
        level = int(level)
    except (TypeError, ValueError):
        level = DEFAULT_LEVEL
    level = min(9, max(1, level))

    if not src_dir or not os.path.isdir(src_dir):
        return _fail(out_path, 0, time.time() - t0,
                     f"源目录不存在：{src_dir}", f"not a directory: {src_dir!r}")
    if not out_path:
        return _fail(out_path, 0, time.time() - t0,
                     "没有指定压缩包的输出路径", "empty out_path")
    if not os.path.isdir(os.path.dirname(out_path)):
        return _fail(out_path, 0, time.time() - t0,
                     f"压缩包的上级目录不存在：{os.path.dirname(out_path)}",
                     "parent dir of out_path missing")

    if os.path.exists(out_path):
        try:
            os.remove(out_path)
        except OSError as e:
            return _fail(out_path, 0, time.time() - t0,
                         f"目标压缩包已存在且无法删除：{e}", f"remove failed: {e}")

    try:
        return _make_zip(src_dir, out_path, level, progress_cb, cancel_check)
    except Exception as e:                        # 最后一道防线
        _discard(out_path)
        return _fail(out_path, 0, time.time() - t0,
                     f"打包失败：{type(e).__name__}: {e}",
                     f"{type(e).__name__}: {e}")


def add_to_archive(zip_path: str, items, level: int = DEFAULT_LEVEL) -> tuple[bool, str]:
    """把几个**压缩包做完之后才写出来**的文件补进已有的 zip。

    【为什么需要这一步】
        备份引擎在 run() 的最后就把备份目录压好了，而 manifest.txt /
        README.md / backup_log.txt 是调用方在 run() 返回**之后**才写的
        （它们的内容依赖 run() 的结果）。不补这一步的话，解压出来的备份会
        缺了校验清单和恢复说明 —— 而 README.md 里正是恢复要点。

    【为什么代价可以忽略】
        走 zipfile 的追加模式：只读一遍中央目录、把新条目写在末尾、
        再重写一次中央目录。**不会重新压缩任何已有条目**，所以哪怕源包是
        971 MB，补三个几 KB 的文件也是毫秒级。

    items: [(本地文件路径, 归档内相对路径)]，路径会统一成正斜杠。
           本地文件不存在的直接跳过（不当作失败）。
    返回 (是否成功, 说明文字)。绝不抛异常。
    """
    if not zip_path or not os.path.isfile(zip_path):
        return False, f"压缩包不存在: {zip_path}"
    try:
        level = min(9, max(1, int(level)))
    except (TypeError, ValueError):
        level = DEFAULT_LEVEL
    added = 0
    try:
        with zipfile.ZipFile(zip_path, "a", zipfile.ZIP_DEFLATED,
                             compresslevel=level, allowZip64=True) as zf:
            have = set(zf.namelist())
            for src, arc in items:
                if not src or not os.path.isfile(src):
                    continue
                arc = str(arc).replace(os.sep, "/").lstrip("/")
                if not arc or arc in have:
                    # 同名条目已经在了就直接用旧的：ZipFile 允许写重名条目，
                    # 但那样解压行为会变得依赖具体实现，不值得。
                    continue
                zf.write(src, arc)
                have.add(arc)
                added += 1
        return True, f"补入 {added} 个文件"
    except Exception as e:                        # noqa: BLE001 —— 绝不外泄
        return False, f"{type(e).__name__}: {e}"
