# -*- coding: utf-8 -*-
"""
安卓分区备份工具 —— 核心引擎（全设备通用）
================================================================================
本模块不含任何 GUI 依赖，可独立运行/测试，便于代码审查。

【适用范围】
    任何已 root（Magisk / KernelSU / APatch）且开启 USB 调试的 Android 设备。
    不绑定机型、不绑定 SoC —— 平台与分区价值由 partition_profiles 按语义推断。

【安全声明 —— 本模块只做只读导出】
    · 对设备分区的唯一访问方式是 `dd if=<分区>`（读取）
    · 全文件不存在 `dd of=/dev/block/*`、`fastboot` 等任何写设备分区的路径
    · 唯一会写入设备的是 /data/local/tmp 与 /sdcard 下的临时文件，用完即删
    · 分区名一律经过白名单正则校验，杜绝命令注入

【两套数据传输路径】
    路径 A（默认，快）：adb exec-out → stdout 直接重定向到本地文件
                        实测 14.8 MB/s，不占用手机存储
    路径 B（回退，稳）：设备端 dd 到 /sdcard → adb pull → 设备端删除

    为什么必须用 exec-out 而不是 shell：
        adb shell 会分配 PTY，把二进制流里的 \\n 转成 \\r\\n 从而损坏数据。
        实测 32MB 分区经 adb shell 会多出 76739 字节，sha256 完全不匹配。
================================================================================
"""

from __future__ import annotations

import atexit
import hashlib
import os
import re
import socket
import subprocess
import sys
import tarfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Iterable, Optional

from partition_profiles import (
    BYNAME_CANDIDATES,
    MAPPER_DIR,
    ROOT_BACKENDS,
    WHOLE_DISK_RE,
    Classification,
    Platform,
    classify,
    detect_platform,
    PLATFORM_GENERIC,
)

# ==== APB_ARCHIVE BEGIN ====
# 备份完成后可选地把整个备份目录打包成一个 zip。
# 单独成模块的原因：压缩细节（分块、取消、进度）与本模块的备份/校验逻辑
# 毫无关系，混在一起会让这个已经「逐字节验证过」的引擎更难审。
from archive_pack import add_to_archive, make_archive
# ==== APB_ARCHIVE END ====
CORE_VERSION = "1.0.0"

_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


# ------------------------------------------------------------------ 子进程登记
#
# 【为什么需要】adb 子进程是**独立进程**，Python 退出不会顺带把它们带走。
# 关窗时若正有一次 adb 调用在飞（设备轮询、读分区表、或一次备份），那个
# adb.exe 就会变成孤儿留在任务管理器里 —— 用户看到的就是「程序关了还有
# 进程占着」。
#
# 实测（Windows / Python 3.14）：`Popen(["adb", "wait-for-device"])` 之后
# 直接让解释器退出，该 adb 进程**依然存活**（PID 15532）。
#
# 所以每次 spawn 都登记，退出前调 kill_live_children() 统一清掉。
_children_lock = threading.Lock()
_children: set = set()


def _register_child(proc):
    with _children_lock:
        _children.add(proc)
    return proc


def _unregister_child(proc):
    with _children_lock:
        _children.discard(proc)


def live_child_count() -> int:
    """还活着的子进程数（测试用）。"""
    with _children_lock:
        return sum(1 for p in _children if p.poll() is None)


def kill_live_children(wait: float = 1.5) -> int:
    """杀掉所有还活着的子进程，返回杀掉的数量 —— 关窗时调用。

    先 kill 再等一小会儿让句柄真正释放，否则 Windows 上文件可能仍被占用。
    """
    with _children_lock:
        procs = list(_children)
        _children.clear()
    killed = []
    for p in procs:
        try:
            if p.poll() is None:
                p.kill()
                killed.append(p)
        except Exception:
            pass
    if killed and wait:
        deadline = time.monotonic() + wait
        for p in killed:
            try:
                left = deadline - time.monotonic()
                if left > 0:
                    p.wait(timeout=left)
            except Exception:
                pass
    return len(killed)


def _run(cmd, timeout=None, check=False, **kw):
    """subprocess.run 的替代品 —— 登记子进程，退出时能被统一清掉。

    返回值与 subprocess.run 完全一致（CompletedProcess），调用处无需改动。
    """
    p = subprocess.Popen(cmd, **kw)
    _register_child(p)
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        p.kill()
        try:
            p.communicate(timeout=5)
        except Exception:
            pass
        raise
    finally:
        _unregister_child(p)
    if check and p.returncode != 0:
        raise subprocess.CalledProcessError(p.returncode, cmd, out, err)
    return subprocess.CompletedProcess(cmd, p.returncode, out, err)


# adb 服务端默认端口。它是**多个工具共用**的常驻进程，不是本程序私有的，
# 所以本程序只在自己退出时按用户意愿把它停掉，绝不在运行期间擅自杀它。
ADB_SERVER_PORT = 5037


def adb_server_running(port: int = ADB_SERVER_PORT) -> bool:
    """adb 服务端在不在跑 —— 看 5037 端口有没有人监听。

    比跑 `adb start-server` 去探测快得多（毫秒级 vs 几百毫秒），
    也不会产生副作用。
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.35)
    try:
        s.connect(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        try:
            s.close()
        except OSError:
            pass


def adb_start_server(adb_path: str, timeout: int = 30) -> tuple[bool, str]:
    """启动 adb 服务端。返回 (是否成功, 说明文字)。"""
    if not adb_path:
        return False, "未找到 adb.exe"
    try:
        p = _run([adb_path, "start-server"], stdout=subprocess.PIPE,
                 stderr=subprocess.STDOUT, creationflags=_CREATE_NO_WINDOW,
                 timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"启动超时（{timeout} 秒）"
    except OSError as e:
        return False, f"无法执行 adb: {e}"
    text = (p.stdout or b"").decode("utf-8", "replace").strip()
    ok = adb_server_running()
    return ok, (text or ("已启动" if ok else "启动后仍未监听 5037"))


def adb_stop_server(adb_path: str, timeout: int = 20) -> tuple[bool, str]:
    """停止 adb 服务端。返回 (是否已停止, 说明文字)。

    ⚠️ 这会影响到**其它**正在用 adb 的程序（Android Studio、scrcpy 等）。
    所以只在用户明确要求时调用。
    """
    if not adb_path:
        return True, "未找到 adb.exe，无需停止"
    try:
        p = _run([adb_path, "kill-server"], stdout=subprocess.PIPE,
                 stderr=subprocess.STDOUT, creationflags=_CREATE_NO_WINDOW,
                 timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"停止超时（{timeout} 秒）"
    except OSError as e:
        return False, f"无法执行 adb: {e}"
    text = (p.stdout or b"").decode("utf-8", "replace").strip()
    gone = not adb_server_running()
    return gone, (text or ("已停止" if gone else "仍在监听 5037"))


# 兜底：无论从哪条路径退出（GUI 关窗、CLI 跑完、异常退出），
# 都不要把 adb 子进程留在任务管理器里。
atexit.register(kill_live_children, 0.5)
# 分区名：首字符必须是字母/数字/下划线 —— 这样 "." 与 ".." 会被直接拒绝。
# （真实分区名如 persist / modemst1 / init_boot_a / vbmeta_system_a 都满足）
_PART_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]{0,63}$")

# 设备端路径：先做前缀白名单，再由 validate_shell_path 逐段拒绝 ".."
_SHELL_PATH_RE = re.compile(
    r"^/(dev/block|sdcard|data/local/tmp|data/adb)(/[A-Za-z0-9_.\-]+)*$")

# 传输参数（由 Spike 实测得出：4 MiB 块 + 直接重定向最快）
DD_BLOCK_SIZE = 4 * 1024 * 1024
STREAM_CHUNK = 1024 * 1024
GPT_SLICE = 1024 * 1024              # GPT 头/尾各备份 1 MiB

DEVICE_STAGE_DIR = "/sdcard/.apb_tmp"          # 临时暂存目录
DEVICE_PROBE_SCRIPT = "/data/local/tmp/.apb_probe.sh"

PATH_MODE_STREAM = "stream"
PATH_MODE_STAGE = "stage"

# 体积提醒阈值
HUGE_THRESHOLD = 1 * 1024 * 1024 * 1024        # 1 GiB
ENORMOUS_THRESHOLD = 20 * 1024 * 1024 * 1024   # 20 GiB

# 传输停滞保护：这么多秒内输出文件没有任何增长就判定为卡死并中止。
# 没有这道保护的话，adb 一旦挂住，备份线程会无限等待。
STALL_TIMEOUT = 120.0


# ==============================================================================
#  异常
# ==============================================================================

class BackupError(Exception):
    """本工具所有可预期错误的基类。"""


class AdbError(BackupError):
    pass


class RootUnavailable(AdbError):
    pass


class Cancelled(BackupError):
    """用户主动取消。"""


class FallbackNeeded(BackupError):
    """路径 A 失败，需要切换到路径 B。"""


# ==============================================================================
#  工具函数
# ==============================================================================

def human_size(n: Optional[int]) -> str:
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


def human_duration(sec: Optional[float]) -> str:
    """把秒数格式化成 12s / 3m05s / 1h12m 这样的短形式。"""
    if sec is None:
        return "--"
    try:
        if sec != sec or sec < 0 or sec == float("inf"):   # NaN / 负数 / 无穷
            return "--"
    except TypeError:
        return "--"
    s = int(sec)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"


class SpeedMeter:
    """
    速度平滑器。

    采样瞬时速度再取平均 —— 直接用 (已传字节/已耗时) 会在启动时给出
    虚高的速度（分母还很小），到后面又骤降，进度条上的数字看起来像抽风。
    """

    def __init__(self, alpha: float = 0.25):
        self.alpha = alpha
        self.rate = 0.0
        self._last_t = 0.0
        self._last_b = 0

    def sample(self, done: int, now: float) -> float:
        if self._last_t > 0:
            dt = now - self._last_t
            db = done - self._last_b
            if dt > 0.15 and db >= 0:
                inst = db / dt
                self.rate = inst if self.rate <= 0 else (
                    self.alpha * inst + (1 - self.alpha) * self.rate)
        self._last_t = now
        self._last_b = done
        return self.rate

    def eta(self, done: int, total: int) -> Optional[float]:
        if self.rate <= 0 or total <= done:
            return None
        return (total - done) / self.rate


class ConsoleProgress:
    """
    终端单行进度条。

    用 \\r 覆盖刷新，而不是每帧 print 一行 —— 后者会把终端刷成瀑布。
    非 TTY（输出被重定向到文件 / 被上层捕获）时自动退化为
    "每跨越 10% 记一行"，否则会把成千上万个 \\r 塞进日志。
    """

    BAR_W = 26

    def __init__(self, stream=None, force_tty: bool = False, enabled: bool = True):
        self.stream = stream if stream is not None else sys.stdout
        self.enabled = enabled
        try:
            self.tty = force_tty or bool(self.stream.isatty())
        except Exception:
            self.tty = False
        self._bucket = -1
        self._open = False

    def update(self, label: str, done: int, total: int,
               speed: float = 0.0, eta: Optional[float] = None) -> None:
        if not self.enabled or total <= 0:
            return
        pct = min(100.0, done * 100.0 / total)

        if self.tty:
            filled = int(self.BAR_W * pct / 100.0)
            bar = "█" * filled + "░" * (self.BAR_W - filled)
            spd = f"{speed / 1048576:6.1f} MB/s" if speed > 0 else " " * 12
            tail = f"  剩 {human_duration(eta)}" if eta else ""
            # 用空格补齐再回\r，避免上一帧更长时留下残影
            line = (f"  {label[:20]:<20} [{bar}] {pct:5.1f}%  "
                    f"{human_size(done):>10}/{human_size(total):<10} {spd}{tail}")
            self.stream.write("\r" + line[:130].ljust(130))
            self.stream.flush()
            self._open = True
        else:
            bucket = int(pct // 10)
            if bucket != self._bucket:
                self._bucket = bucket
                self.stream.write(
                    f"      {label} {pct:3.0f}%  "
                    f"({human_size(done)} / {human_size(total)})\n")
                self.stream.flush()

    def finish(self) -> None:
        """结束当前进度行（换行），必须调用，否则后续输出会覆盖进度条。"""
        if self._open:
            self.stream.write("\n")
            self.stream.flush()
            self._open = False
        self._bucket = -1


def validate_partition_name(name: str) -> str:
    """
    分区名白名单 —— 防命令注入的第一道也是最关键的一道防线。

    正则要求首字符是字母/数字/下划线，因此 "." 与 ".." 会被天然拒绝
    （早期版本写成 [A-Za-z0-9_.\\-]{1,64}，".." 能溜过去 ——
     虽然只是拼进 dd 的 if= 参数、不构成注入，但仍是校验缺口）。
    """
    if not isinstance(name, str) or not _PART_NAME_RE.match(name):
        raise BackupError(f"非法分区名: {name!r}")
    return name


def validate_shell_path(path: str) -> str:
    """
    设备端路径白名单 —— 只允许 /dev/block、/sdcard、/data/local/tmp、/data/adb。

    ⚠️ 光靠正则的字符类挡不住路径回溯：`..` 里的点也在允许字符集内，
        `/dev/block/../../etc/passwd` 能通过前缀白名单。
        所以必须再逐段检查一次 `..`。
    """
    if not isinstance(path, str) or not _SHELL_PATH_RE.match(path):
        raise BackupError(f"非法设备路径: {path!r}")
    if ".." in path.split("/"):
        raise BackupError(f"设备路径不得包含 .. : {path!r}")
    return path


def sha256_file(path: str, progress_cb: Optional[Callable[[int], None]] = None,
                cancel: Optional[threading.Event] = None) -> str:
    h = hashlib.sha256()
    done = 0
    with open(path, "rb") as f:
        while True:
            if cancel is not None and cancel.is_set():
                raise Cancelled("用户取消")
            chunk = f.read(STREAM_CHUNK)
            if not chunk:
                break
            h.update(chunk)
            done += len(chunk)
            if progress_cb:
                progress_cb(done)
    return h.hexdigest()


def _verify_targz(path: str) -> int:
    """校验 tar.gz 是否完整，返回条目数。

    为什么非校验不可：gzip 是流式格式，**截断的包在体积上可能看不出来**，
    sha256 也照样算得出来 —— 唯一可靠的判据是真正解一遍。
    tarfile 读到流末尾会抛 EOFError/ReadError，正好用来抓截断。

    顺带把条目数报给用户，是个"内容合理"的旁证。
    """
    n = 0
    try:
        with tarfile.open(path, "r:gz") as tf:
            for _ in tf:
                n += 1
                if n > 500000:          # 防呆：正常 /data/adb 到不了这个量级
                    break
    except Exception as e:               # tarfile 的异常类型较杂，统一包成 BackupError
        raise BackupError(f"压缩包不完整或已损坏（{type(e).__name__}: {e}）") from e
    if n == 0:
        raise BackupError("压缩包是空的（没有任何条目）")
    return n


def format_guid(b: bytes) -> str:
    """GPT GUID 是混合端序：前三个字段小端，后两个字段大端。"""
    if len(b) < 16:
        return ""
    d1 = int.from_bytes(b[0:4], "little")
    d2 = int.from_bytes(b[4:6], "little")
    d3 = int.from_bytes(b[6:8], "little")
    d4 = b[8:10].hex().upper()
    d5 = b[10:16].hex().upper()
    return f"{d1:08X}-{d2:04X}-{d3:04X}-{d4}-{d5}"


# ==============================================================================
#  数据模型
# ==============================================================================

@dataclass
class DeviceCaps:
    """设备能力探测结果 —— 决定后续命令怎么写、哪些功能可用。"""
    byname_dir: str = "/dev/block/by-name"
    dd: str = "dd"
    blockdev: str = "blockdev"
    sgdisk: str = ""
    sha256sum: str = "sha256sum"
    tar: str = "tar"
    awk: str = "awk"
    busybox: str = ""
    toybox: str = ""
    slot: str = ""
    has_mapper: bool = False
    root_backend: str = ""
    root_backend_name: str = ""
    raw: dict[str, str] = field(default_factory=dict)

    @property
    def has_sgdisk(self) -> bool:
        return bool(self.sgdisk)


# ------------------------------------------------------------------ 芯片型号
#
# ⚠️ 只在 ro.soc.model（Android 12+）**缺失**时才查这张表，而且查到了也一定把
# 原始平台代号一并显示 —— 不做「猜一个好看的型号」这种事。
# 表里只放把握得住的常见平台，其余一律显示原始值。
_BOARD_FRIENDLY = {
    "kalama": "骁龙 8 Gen 2",
    "pineapple": "骁龙 8 Gen 3",
    "sun": "骁龙 8 Elite",
    "taro": "骁龙 8 Gen 1",
    "cape": "骁龙 8+ Gen 1",
    "waipio": "骁龙 8 Gen 1",
    "lahaina": "骁龙 888",
    "shima": "骁龙 888",
    "kona": "骁龙 865",
    "msmnile": "骁龙 855",
    "holi": "骁龙 695",
    "mt6983": "天玑 9000",
    "mt6985": "天玑 9200",
    "mt6989": "天玑 9300",
    "mt6897": "天玑 8300",
    "zuma": "Tensor G3",
    "gs201": "Tensor G2",
}

_SOC_MAKER_CN = {
    "qualcomm": "高通", "qti": "高通", "qualcomm technologies, inc": "高通",
    "mediatek": "联发科", "mtk": "联发科",
    "samsung": "三星", "exynos": "三星",
    "unisoc": "紫光展锐", "spreadtrum": "紫光展锐",
    "hisilicon": "海思", "huawei": "海思",
    "google": "Google",
}


def describe_soc(info: "DeviceInfo") -> str:
    """把芯片信息拼成一句人话。

    优先级：ro.soc.model（Android 12+ 的权威值）> 平台代号对照表 > ro.hardware。
    查表命中时会带上原始代号，方便你核对。
    """
    model = (info.soc_model or "").strip()
    board = (info.board or "").strip()
    hardware = (info.hardware or "").strip()
    maker_raw = (info.soc_maker or "").strip()
    maker = _SOC_MAKER_CN.get(maker_raw.lower(), maker_raw)
    friendly = _BOARD_FRIENDLY.get(board.lower(), "")

    if model:
        base = f"{friendly}（{model}）" if friendly and friendly not in model else model
    elif friendly:
        base = friendly
    else:
        base = board or hardware

    parts = [p for p in (base, maker) if p]
    # 用了友好名就不必再重复平台代号；否则把它补上，别丢信息
    if board and not friendly and board.lower() not in base.lower():
        parts.append(board)
    return " · ".join(parts)


def parse_sysinfo(text: str, kernel_fallback: str = "") -> tuple[str, str, str]:
    """解析 `uname -r; head -1 /proc/meminfo; wm size` 的输出。

    返回 (内核, 内存, 屏幕)。任一项取不到就返回空串。

    ⚠️ 内核以 `uname -r` 为准。有些 ROM 的 ro.kernel.version 只有 "5.15"
    这种两位短值，若让它抢先，界面上就只剩个 "5.15" 了。
    """
    kernel = ""
    ram = ""
    screen = ""
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("MemTotal:"):
            m = re.search(r"(\d+)", line)
            if m:
                try:
                    ram = human_size(int(m.group(1)) * 1024)
                except (ValueError, OverflowError):
                    pass
        elif line.startswith(("Physical size:", "Override size:")):
            screen = line.split(":", 1)[1].strip()
        elif re.match(r"^\d+\.\d+[\w.\-]*", line):
            kernel = line
    return (kernel or kernel_fallback), ram, screen


@dataclass
class DeviceInfo:
    serial: str = ""
    state: str = ""
    brand: str = ""
    model: str = ""
    codename: str = ""          # ro.product.device，用于输出目录命名
    android: str = ""
    sdk: str = ""
    version: str = ""
    slot: str = ""
    root_ok: bool = False
    root_context: str = ""
    platform: Platform = PLATFORM_GENERIC
    platform_score: int = 0
    caps: DeviceCaps = field(default_factory=DeviceCaps)

    # ---- 详情字段（顶部设备信息栏用）----
    # 取不到一律留空串，界面显示 "—"。绝不为了好看去猜一个值。
    build_id: str = ""          # ro.build.display.id（系统构建号，如 OS1.0.1.0.ABCDEF）
    security_patch: str = ""    # ro.build.version.security_patch
    soc_model: str = ""         # ro.soc.model（Android 12+ 才有）
    soc_maker: str = ""         # ro.soc.manufacturer
    board: str = ""             # ro.board.platform（如 kalama）
    hardware: str = ""          # ro.hardware（如 qcom）
    abi: str = ""               # ro.product.cpu.abi
    kernel: str = ""            # uname -r
    ram: str = ""               # /proc/meminfo MemTotal，已格式化
    screen: str = ""            # wm size
    os_name: str = ""           # ro.mi.os.version.name / ro.miui.ui.version.name
    fingerprint: str = ""       # ro.build.fingerprint



    @property
    def ready(self) -> bool:
        return self.state == "device"

    @property
    def display(self) -> str:
        bits = [b for b in (self.brand, self.model) if b]
        return " ".join(bits) if bits else "未知设备"

    @property
    def tag(self) -> str:
        """输出目录用的短标识。"""
        base = self.codename or self.model or "android"
        return re.sub(r"[^A-Za-z0-9_.\-]", "_", base)


@dataclass
class PartitionInfo:
    name: str
    size: int = 0
    devnode: str = ""
    tier: int = 0
    label: str = ""
    reason: str = ""

    @property
    def is_critical(self) -> bool:
        return self.tier == 1

    @property
    def is_huge(self) -> bool:
        return self.size >= HUGE_THRESHOLD

    @property
    def is_enormous(self) -> bool:
        return self.size >= ENORMOUS_THRESHOLD

    @property
    def note(self) -> str:
        bits = []
        if self.label:
            bits.append(self.label)
        if self.is_enormous:
            bits.append("⚠极大")
        elif self.is_huge:
            bits.append("⚠巨大")
        return "  ".join(bits)


@dataclass
class StorageDisk:
    node: str            # /dev/block/sda
    name: str            # sda
    size: int = 0
    sector_size: int = 512
    is_boot_part: bool = False


@dataclass
class StorageTopology:
    kind: str = "unknown"                  # ufs / emmc / nvme / unknown
    disks: list[StorageDisk] = field(default_factory=list)

    @property
    def display(self) -> str:
        return {"ufs": "UFS", "emmc": "eMMC", "nvme": "NVMe",
                "unknown": "未知"}.get(self.kind, self.kind)


@dataclass
class ItemResult:
    kind: str            # PART / GPT / TAR / TXT
    name: str
    sub: str = ""
    expect_size: int = 0
    real_size: int = 0
    sha256: str = ""
    path: str = ""
    ok: bool = False
    message: str = ""
    seconds: float = 0.0
    path_mode: str = PATH_MODE_STREAM
    # 是否真的和设备端哈希对过了。None = 这一项不适用（GPT/TAR 等）；
    # False = 想做但设备端没结果（要如实告诉用户，不能假装验过）
    device_verified: Optional[bool] = None

    @property
    def label(self) -> str:
        return f"{self.name}:{self.sub}" if self.sub else self.name


# ==============================================================================
#  GPT 解析（纯 Python，不依赖 sgdisk）
# ==============================================================================

@dataclass
class GptEntry:
    index: int
    type_guid: str
    part_guid: str
    start_lba: int
    end_lba: int
    name: str

    @property
    def sectors(self) -> int:
        return max(0, self.end_lba - self.start_lba + 1)


@dataclass
class GptHeader:
    sector_size: int = 0
    header_offset: int = 0
    disk_guid: str = ""
    my_lba: int = 0
    alt_lba: int = 0
    first_usable: int = 0
    last_usable: int = 0
    entries_lba: int = 0
    num_entries: int = 0
    entry_size: int = 0
    header_size: int = 0
    valid: bool = False
    mbr_sig: str = ""
    detail: str = ""


def parse_gpt_head(data: bytes) -> GptHeader:
    """
    从「原始前 1 MiB」字节里解析 GPT 头。

    逻辑扇区可能是 512（多数 eMMC）也可能是 4096（高通 UFS），
    所以先试 512，再试 4096，最后全文搜索签名 —— 这样两种设备都能吃。
    """
    h = GptHeader()
    if len(data) < 1024:
        h.detail = "数据长度不足"
        return h

    h.mbr_sig = f"{data[510]:02X}{data[511]:02X}"

    off = -1
    for cand in (512, 4096, 2048):
        if len(data) >= cand + 8 and data[cand:cand + 8] == b"EFI PART":
            off, h.sector_size = cand, cand
            break
    if off < 0:
        idx = data[:131072].find(b"EFI PART")
        if idx < 0:
            h.detail = "未找到 GPT 签名 EFI PART"
            return h
        off = idx
        h.sector_size = 0                       # 非标准，仅用于解析

    h.header_offset = off
    h.disk_guid = format_guid(data[off + 56:off + 72])
    h.my_lba = int.from_bytes(data[off + 24:off + 32], "little")
    h.alt_lba = int.from_bytes(data[off + 32:off + 40], "little")
    h.first_usable = int.from_bytes(data[off + 40:off + 48], "little")
    h.last_usable = int.from_bytes(data[off + 48:off + 56], "little")
    h.entries_lba = int.from_bytes(data[off + 72:off + 80], "little")
    h.num_entries = int.from_bytes(data[off + 80:off + 84], "little")
    h.entry_size = int.from_bytes(data[off + 84:off + 88], "little")
    h.header_size = int.from_bytes(data[off + 12:off + 16], "little")

    problems = []
    if h.mbr_sig != "55AA":
        problems.append(f"MBR 签名异常({h.mbr_sig})")
    if h.my_lba != 1:
        problems.append(f"MyLBA 应为 1，实为 {h.my_lba}")
    if h.header_size != 92:
        problems.append(f"HeaderSize 应为 92，实为 {h.header_size}")
    if h.entry_size != 128:
        problems.append(f"EntrySize 应为 128，实为 {h.entry_size}")
    if h.num_entries <= 0 or h.num_entries > 1024:
        problems.append(f"分区表条目数异常({h.num_entries})")

    h.valid = not problems
    h.detail = "OK" if h.valid else "; ".join(problems)
    return h


def parse_gpt_entries(data: bytes, h: GptHeader) -> list[GptEntry]:
    """从头部字节里解出分区条目数组（无需再读设备）。"""
    if not h.valid or not h.sector_size:
        return []
    base = h.entries_lba * h.sector_size
    total = h.num_entries * h.entry_size
    if base + total > len(data):
        return []

    out: list[GptEntry] = []
    for i in range(h.num_entries):
        e = data[base + i * h.entry_size: base + (i + 1) * h.entry_size]
        if len(e) < 128 or e[0:16] == b"\x00" * 16:
            continue
        try:
            nm = e[56:128].decode("utf-16-le", "replace").rstrip("\x00").strip()
        except Exception:
            nm = ""
        out.append(GptEntry(
            index=i + 1,
            type_guid=format_guid(e[0:16]),
            part_guid=format_guid(e[16:32]),
            start_lba=int.from_bytes(e[32:40], "little"),
            end_lba=int.from_bytes(e[40:48], "little"),
            name=nm,
        ))
    return out


def render_layout(lun: str, h: GptHeader, entries: list[GptEntry],
                  disk_bytes: int = 0) -> str:
    """
    生成人类可读的分区布局表 —— 即使三份二进制全丢，
    凭这张表也能用 sgdisk 逐条手工重建分区表。
    """
    L = []
    ss = h.sector_size or 512
    L.append(f"# {lun} 分区布局")
    L.append(f"# 磁盘容量      : {disk_bytes} 字节 ({human_size(disk_bytes)})")
    L.append(f"# 逻辑扇区      : {ss} 字节" + ("  ← 注意不是 512" if ss != 512 else ""))
    L.append(f"# 磁盘 GUID     : {h.disk_guid}")
    L.append(f"# 分区表条目    : {h.num_entries} x {h.entry_size} 字节")
    L.append(f"# 首个可用扇区  : {h.first_usable}")
    L.append(f"# 末个可用扇区  : {h.last_usable}")
    L.append(f"# 结构校验      : {h.detail}")
    L.append("")
    L.append(f"{'编号':>4}  {'起始扇区':>12}  {'结束扇区':>12}  "
             f"{'大小':>12}  {'类型 GUID':<36}  名称")
    L.append("-" * 110)
    for e in entries:
        size = human_size(e.sectors * ss)
        L.append(f"{e.index:>4}  {e.start_lba:>12}  {e.end_lba:>12}  "
                 f"{size:>12}  {e.type_guid:<36}  {e.name}")
    L.append("")
    L.append("# 手工重建提示（假设用 sgdisk）：")
    for e in entries[:3]:
        safe = re.sub(r"[^A-Za-z0-9_\-]", "_", e.name) or f"part{e.index}"
        L.append(f"#   sgdisk --new={e.index}:{e.start_lba}:{e.end_lba} "
                 f"--change-name={e.index}:{safe} /dev/block/{lun}")
    if len(entries) > 3:
        L.append(f"#   ... 其余 {len(entries) - 3} 条同理")
    return "\n".join(L) + "\n"


# ==============================================================================
#  ADB 封装
# ==============================================================================

class Adb:
    """adb.exe 的最小封装。所有方法同步阻塞，由调用方决定放在哪个线程。"""

    def __init__(self, adb_path: str, serial: str = ""):
        self.adb_path = adb_path
        self.serial = serial
        # 能力探测结果；attach_caps() 会覆盖它。先给个安全默认值，
        # 这样即使调用方忘了 attach_caps 也不会 AttributeError。
        self._caps = DeviceCaps()

    def _base(self) -> list[str]:
        cmd = [self.adb_path]
        if self.serial:
            cmd += ["-s", self.serial]
        return cmd

    # ---------------------------------------------------------------- 执行
    def run(self, args: list[str], timeout: float = 60) -> tuple[bytes, bytes, int]:
        try:
            p = _run(self._base() + args, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE,
                               creationflags=_CREATE_NO_WINDOW, timeout=timeout)
        except FileNotFoundError as e:
            raise AdbError(f"找不到 adb 可执行文件: {self.adb_path}") from e
        except subprocess.TimeoutExpired as e:
            raise AdbError(f"adb 命令超时（{timeout}s）") from e
        return p.stdout, p.stderr, p.returncode

    def text(self, args: list[str], timeout: float = 60) -> str:
        out, err, rc = self.run(args, timeout)
        if rc != 0:
            msg = (err.decode("utf-8", "replace").strip()
                   or out.decode("utf-8", "replace").strip())
            raise AdbError(f"adb 失败 (rc={rc}): {msg}")
        return out.decode("utf-8", "replace")

    def shell(self, cmd: str, timeout: float = 60) -> str:
        """普通 shell（有 PTY —— 只用于文本命令，绝不可传二进制）。"""
        return self.text(["shell", cmd], timeout)

    def su(self, cmd: str, timeout: float = 120) -> str:
        """以 root 执行。命令中不允许出现单引号（会破坏 su -c 的引号包裹）。"""
        if "'" in cmd:
            raise BackupError("su 命令中不允许出现单引号")
        return self.text(["shell", f"su -c '{cmd}'"], timeout)

    def su_ok(self, cmd: str, timeout: float = 60) -> bool:
        try:
            self.su(cmd, timeout)
            return True
        except AdbError:
            return False

    def getprop(self, key: str) -> str:
        try:
            return self.shell(f"getprop {key}", timeout=15).strip()
        except AdbError:
            return ""

    def getprops(self, keys: list[str]) -> dict[str, str]:
        """
        一次调用批量读取多个属性。

        逐个 getprop 每个属性就是一次 adb 往返（约 200ms），
        读 6 个属性要 1.2 秒以上，用户会明显感觉"设备半天不出来"。
        合并成一条 shell 后只剩一次往返。
        """
        if not keys:
            return {}
        script = "; ".join(f'echo "{k}=$(getprop {k})"' for k in keys)
        out: dict[str, str] = {}
        try:
            raw = self.shell(script, timeout=30)
        except AdbError:
            return {k: "" for k in keys}
        for line in raw.splitlines():
            line = line.strip()
            if "=" in line:
                k, _, v = line.partition("=")
                out[k.strip()] = v.strip()
        for k in keys:
            out.setdefault(k, "")
        return out

    # ---------------------------------------------------------------- 静态
    @staticmethod
    def list_devices(adb_path: str) -> list[tuple[str, str]]:
        try:
            p = _run([adb_path, "devices"], stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE,
                               creationflags=_CREATE_NO_WINDOW, timeout=20)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return []
        out = []
        for line in p.stdout.decode("utf-8", "replace").splitlines():
            line = line.strip()
            if not line or line.lower().startswith("list of devices"):
                continue
            parts = line.split()
            if len(parts) >= 2:
                out.append((parts[0], parts[1]))
        return out

    @staticmethod
    def adb_version(adb_path: str) -> str:
        try:
            p = _run([adb_path, "version"], stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE,
                               creationflags=_CREATE_NO_WINDOW, timeout=15)
            first = p.stdout.decode("utf-8", "replace").splitlines()
            return first[0].strip() if first else ""
        except Exception:
            return ""

    # ---------------------------------------------------------------- 能力探测
    def probe_capabilities(self) -> DeviceCaps:
        """
        一条命令探测全部能力。

        为什么不用 for 循环逐个 `command -v`：
            每次都来回一趟 adb 太慢；这里把所有探测拼成一条 shell 脚本，
            一次往返拿回 key=value 结果。
        """
        script = (
            'for d in /dev/block/by-name /dev/block/bootdevice/by-name '
            '/dev/block/platform/*/by-name /dev/block/platform/*/*/by-name '
            '/dev/block/platform/*/*/*/by-name; do '
            'if [ -d "$d" ]; then echo "BYNAME=$d"; break; fi; done; '
            'echo "DD=$(command -v dd || echo /system/bin/dd)"; '
            'echo "BLOCKDEV=$(command -v blockdev || echo /system/bin/blockdev)"; '
            'echo "SGDISK=$(command -v sgdisk || true)"; '
            'echo "SHA256=$(command -v sha256sum || true)"; '
            'echo "TAR=$(command -v tar || true)"; '
            'echo "AWK=$(command -v awk || true)"; '
            'echo "BUSYBOX=$(command -v busybox || true)"; '
            'echo "TOYBOX=$(command -v toybox || true)"; '
            'echo "SLOT=$(getprop ro.boot.slot_suffix)"; '
            '[ -d /dev/block/mapper ] && echo "MAPPER=yes" || echo "MAPPER=no"; '
            '[ -d /data/adb/ksu ] && echo "ROOT=kernelsu"; '
            '[ -d /data/adb/magisk ] && echo "ROOT=magisk"; '
            '[ -d /data/adb/ap ] && echo "ROOT=apatch"; '
            'true'
        )
        caps = DeviceCaps()
        try:
            out = self.su(script, timeout=60)
        except AdbError:
            return caps

        kv: dict[str, str] = {}
        for line in out.splitlines():
            line = line.strip()
            if "=" in line:
                k, _, v = line.partition("=")
                kv[k.strip()] = v.strip()
        caps.raw = kv

        if kv.get("BYNAME"):
            caps.byname_dir = kv["BYNAME"]
        caps.dd = kv.get("DD") or "dd"
        caps.blockdev = kv.get("BLOCKDEV") or "blockdev"
        caps.sgdisk = kv.get("SGDISK", "")
        caps.sha256sum = kv.get("SHA256", "")
        caps.tar = kv.get("TAR", "")
        caps.awk = kv.get("AWK", "")
        caps.busybox = kv.get("BUSYBOX", "")
        caps.toybox = kv.get("TOYBOX", "")
        caps.slot = kv.get("SLOT", "")
        caps.has_mapper = kv.get("MAPPER") == "yes"
        caps.root_backend = kv.get("ROOT", "")
        for path, key, disp in ROOT_BACKENDS:
            if key == caps.root_backend:
                caps.root_backend_name = disp
                break
        return caps

    # ---------------------------------------------------------------- 设备信息
    def check_root(self) -> tuple[bool, str]:
        try:
            out = self.shell("su -c id", timeout=20).strip()
        except AdbError as e:
            return False, str(e)
        return ("uid=0" in out), out

    def probe(self) -> DeviceInfo:
        info = DeviceInfo(serial=self.serial)
        ok, detail = self.check_root()
        info.root_ok = ok
        if ok:
            m = re.search(r"context=(\S+)", detail)
            info.root_context = m.group(1) if m else ""

        # 一次往返读完所有属性（见 getprops 的说明）
        props = self.getprops([
            "ro.product.brand", "ro.product.manufacturer", "ro.product.model",
            "ro.product.device", "ro.product.vendor.device",
            "ro.build.version.release", "ro.build.version.sdk",
            "ro.build.version.incremental", "ro.boot.slot_suffix",
            # ---- 详情栏 ----
            "ro.build.display.id", "ro.build.version.security_patch",
            "ro.soc.model", "ro.soc.manufacturer", "ro.board.platform",
            "ro.hardware", "ro.product.cpu.abi", "ro.build.fingerprint",
            "ro.mi.os.version.name", "ro.miui.ui.version.name",
            "ro.kernel.version",
        ])
        info.brand = props.get("ro.product.brand") or props.get("ro.product.manufacturer", "")
        info.model = props.get("ro.product.model", "")
        info.codename = (props.get("ro.product.device")
                         or props.get("ro.product.vendor.device", ""))
        info.android = props.get("ro.build.version.release", "")
        info.sdk = props.get("ro.build.version.sdk", "")
        info.version = props.get("ro.build.version.incremental", "")
        info.slot = props.get("ro.boot.slot_suffix", "")

        info.build_id = props.get("ro.build.display.id", "")
        info.security_patch = props.get("ro.build.version.security_patch", "")
        info.soc_model = props.get("ro.soc.model", "")
        info.soc_maker = props.get("ro.soc.manufacturer", "")
        info.board = props.get("ro.board.platform", "")
        info.hardware = props.get("ro.hardware", "")
        info.abi = props.get("ro.product.cpu.abi", "")
        info.fingerprint = props.get("ro.build.fingerprint", "")
        info.os_name = (props.get("ro.mi.os.version.name")
                        or props.get("ro.miui.ui.version.name", ""))
        info.kernel = props.get("ro.kernel.version", "")

        # 内核 / 内存 / 屏幕：一条 shell 一次拿完，省往返。
        # 走 shell 而不是 su —— 这几项普通权限就能读，没 root 的设备也能看到。
        try:
            extra = self.shell("uname -r; head -1 /proc/meminfo; "
                               "wm size 2>/dev/null; true", timeout=25)
            info.kernel, info.ram, info.screen = parse_sysinfo(extra, info.kernel)
        except Exception:
            pass

        if ok:
            info.caps = self.probe_capabilities()
            if not info.slot:
                info.slot = info.caps.slot
        return info

    # ---------------------------------------------------------------- 分区枚举
    def list_partitions(self) -> list[PartitionInfo]:
        """
        一次性取回全部分区名与大小。

        逐个调用 blockdev 的话，上百个分区就是上百次 adb 往返（约 20 秒）；
        一条 shell 循环只需 1 次往返（约 1 秒）。
        """
        byname = self._caps.byname_dir or "/dev/block/by-name"
        bd = self._caps.blockdev or "blockdev"
        script = (
            f'for p in {byname}/*; do '
            'n=$(basename $p); '
            f's=$({bd} --getsize64 $p 2>/dev/null); '
            'echo "$n|$s"; '
            'done; true'
        )
        out = self.su(script, timeout=120)
        parts: list[PartitionInfo] = []
        for line in out.splitlines():
            line = line.strip()
            if "|" not in line:
                continue
            name, _, size = line.partition("|")
            name = name.strip()
            if not _PART_NAME_RE.match(name):
                continue
            # ⚠️ 实机踩坑：部分设备的 by-name 目录里也放了【整盘】符号链接
            #    （如 sda 235.9GB / sde 2.2GB）。那是物理磁盘不是分区，
            #    若不排除会被当成可备份分区列出，甚至被全选误勾。
            if WHOLE_DISK_RE.match(name):
                continue
            try:
                n = int(size.strip())
            except ValueError:
                n = 0
            parts.append(PartitionInfo(name=name, size=n,
                                       devnode=f"{byname}/{name}"))
        parts.sort(key=lambda p: p.name)
        return parts

    def classify_all(self, parts: list[PartitionInfo],
                     platform: Platform) -> list[PartitionInfo]:
        """就地为每个分区打上级别标签。"""
        for p in parts:
            c: Classification = classify(p.name, platform)
            p.tier = c.tier
            p.label = c.label
            p.reason = c.reason
        return parts

    # ---------------------------------------------------------------- 存储拓扑
    def detect_topology(self) -> StorageTopology:
        """
        识别整盘设备与存储类型。

        UFS : /dev/block/sda ... sdf（多个 LUN）
        eMMC: /dev/block/mmcblk0（单盘）+ mmcblk0boot0/boot1
        NVMe: /dev/block/nvme0n1
        """
        topo = StorageTopology()
        bd = self._caps.blockdev or "blockdev"
        script = (
            'for d in /dev/block/sd? /dev/block/mmcblk? /dev/block/mmcblk?boot? '
            '/dev/block/mmcblk?rpmb /dev/block/nvme?n? /dev/block/vd?; do '
            '[ -b "$d" ] || continue; '
            'n=$(basename $d); '
            f'sz=$({bd} --getsize64 "$d" 2>/dev/null); '
            f'ss=$({bd} --getss "$d" 2>/dev/null); '
            'echo "$n|$sz|$ss"; '
            'done; true'
        )
        try:
            out = self.su(script, timeout=60)
        except AdbError:
            return topo

        for line in out.splitlines():
            bits = line.strip().split("|")
            if len(bits) != 3:
                continue
            name = bits[0].strip()
            if not WHOLE_DISK_RE.match(name):
                continue
            try:
                size = int(bits[1])
            except ValueError:
                size = 0
            try:
                ss = int(bits[2])
            except ValueError:
                ss = 512
            topo.disks.append(StorageDisk(
                node=f"/dev/block/{name}", name=name, size=size, sector_size=ss,
                is_boot_part=bool(re.search(r"boot\d+|rpmb", name)),
            ))

        names = [d.name for d in topo.disks]
        if any(n.startswith("mmcblk") for n in names):
            topo.kind = "emmc"
        elif any(n.startswith("nvme") for n in names):
            topo.kind = "nvme"
        elif any(re.fullmatch(r"sd[a-z]+", n) for n in names):
            topo.kind = "ufs"
        topo.disks.sort(key=lambda d: d.name)
        return topo

    def attach_caps(self, caps: DeviceCaps):
        """把能力探测结果塞给 Adb，供后续命令使用完整路径。"""
        self._caps = caps

    # ---------------------------------------------------------------- 流式传输
    def stream_partition_to_file(
        self,
        part_name: str,
        dest_path: str,
        expected_size: int,
        dd_path: str = "dd",
        progress_cb: Optional[Callable[[int, float], None]] = None,
        cancel: Optional[threading.Event] = None,
    ) -> tuple[int, float]:
        """
        路径 A：exec-out 把 dd 的输出直接重定向到本地文件。

        为什么 stdout 直接重定向而不是经 Python 中转：
            实测 4 MiB 块 + 直接重定向 = 14.8 MB/s，比 PIPE 中转快约 25%。

        为什么要比对字节数而不是只看返回码：
            su 失败时 stdout 为空但 rc 仍可能是 0，必须用字节数兜底。
        """
        validate_partition_name(part_name)
        remote = f"{dd_path} if=/dev/block/by-name/{part_name} bs={DD_BLOCK_SIZE} 2>/dev/null"
        err_path = dest_path + ".stderr"
        t0 = time.time()

        with open(dest_path, "wb") as fout, open(err_path, "wb") as ferr:
            proc = subprocess.Popen(
                self._base() + ["exec-out", f"su -c '{remote}'"],
                stdout=fout, stderr=ferr, creationflags=_CREATE_NO_WINDOW)
            _register_child(proc)     # 关窗时能被统一清掉，不留孤儿 adb.exe
            last = 0
            last_size = -1
            stall_since = time.time()
            while proc.poll() is None:
                if cancel is not None and cancel.is_set():
                    proc.kill()
                    proc.wait(timeout=10)
                    raise Cancelled("用户取消")
                cur = os.path.getsize(dest_path)
                if cur != last_size:
                    last_size = cur
                    stall_since = time.time()
                elif time.time() - stall_since > STALL_TIMEOUT:
                    # 见 STALL_TIMEOUT 注释：防止 adb 挂住导致永远等待
                    proc.kill()
                    try:
                        proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        pass
                    raise FallbackNeeded(
                        f"传输停滞超过 {STALL_TIMEOUT:.0f} 秒（已收到 {human_size(cur)}），已中止")
                if progress_cb and cur != last:
                    progress_cb(cur, time.time() - t0)
                    last = cur
                time.sleep(0.20)
            proc.wait()
            _unregister_child(proc)
            if progress_cb:
                progress_cb(os.path.getsize(dest_path), time.time() - t0)

        elapsed = time.time() - t0
        size = os.path.getsize(dest_path)

        err_text = ""
        try:
            with open(err_path, "rb") as f:
                err_text = f.read(4096).decode("utf-8", "replace").strip()
        except OSError:
            pass
        finally:
            try:
                os.remove(err_path)
            except OSError:
                pass

        if proc.returncode != 0:
            raise FallbackNeeded(f"exec-out 返回码 {proc.returncode}: {err_text[:200]}")
        if expected_size and size != expected_size:
            raise FallbackNeeded(
                f"字节数不符：应为 {expected_size}，实收 {size}（差 {size - expected_size}）"
                + (f" {err_text[:120]}" if err_text else ""))
        if size == 0:
            raise FallbackNeeded(f"收到 0 字节 {err_text[:200]}")
        return size, elapsed

    def stage_partition_and_pull(
        self,
        part_name: str,
        dest_path: str,
        dd_path: str = "dd",
        progress_cb: Optional[Callable[[int, float], None]] = None,
        cancel: Optional[threading.Event] = None,
    ) -> tuple[int, float]:
        """路径 B（回退）：设备端 dd 到 /sdcard → adb pull → 删除设备端文件。"""
        validate_partition_name(part_name)
        remote_file = f"{DEVICE_STAGE_DIR}/{part_name}.img"
        t0 = time.time()

        self.su(f"mkdir -p {DEVICE_STAGE_DIR}", timeout=30)
        self.su(f"rm -f {remote_file}", timeout=30)
        self.su(f"{dd_path} if=/dev/block/by-name/{part_name} of={remote_file} "
                f"bs={DD_BLOCK_SIZE} 2>/dev/null", timeout=3600)

        try:
            _run(self._base() + ["pull", remote_file, dest_path],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           creationflags=_CREATE_NO_WINDOW, timeout=7200)
        finally:
            try:
                self.su(f"rm -f {remote_file}", timeout=30)
            except AdbError:
                pass

        size = os.path.getsize(dest_path) if os.path.exists(dest_path) else 0
        return size, time.time() - t0

    # ---------------------------------------------------------------- 小工具
    def get_sector_size(self, dev: str) -> int:
        try:
            return int(self.su(f"{self._caps.blockdev} --getss {dev} 2>/dev/null",
                               timeout=20).strip() or 512)
        except (AdbError, ValueError):
            return 512

    def get_device_size(self, dev: str) -> int:
        try:
            return int(self.su(f"{self._caps.blockdev} --getsize64 {dev} 2>/dev/null",
                               timeout=20).strip() or 0)
        except (AdbError, ValueError):
            return 0

    def read_device_file(self, dev: str, local_path: str,
                         offset: int, length: int) -> tuple[int, str]:
        """
        用 dd 从设备读一段字节【回传】到本地文件（GPT 头/尾用）。

        ⚠️ 关键区别：本方法走 `exec-out`，把设备上的字节流通过 adb 管道写进
           本地的 local_path。所以 local_path 是【本地】路径，这是对的。

           绝不能换成 `self.su(f"dd if=... of={local_path}")` —— 那条命令在
           【设备】上执行，of= 必须是设备上的路径。设备上没有 D:\\ 这种盘符，
           必然失败。v1.1.0 里六个 LUN 的尾部 GPT 全部失败就是这个原因。

        返回 (实际读到的字节数, dd 的 stderr 文本)。
        stderr 一并返回，是因为早先这里把 stderr 直接删掉，
        导致失败时只能看到"未生成"三个字，根本查不出原因。
        """
        validate_shell_path(dev)
        # ⚠️⚠️ 命令末尾的 2>/dev/null 绝对不能去掉！⚠️⚠️
        #
        #   `su -c` 会把子进程的 stderr 合并进 stdout，而 stdout 正是我们要的
        #   二进制数据流。去掉这个重定向之后，dd 的摘要文字
        #   （"2048+0 records in / 2048+0 records out / 1048576 bytes copied"，
        #    约 83 字符）会被直接塞进镜像里。
        #
        #   实测：1 MiB 的 GPT 头部会变成 1048659 字节，数据整体错位，
        #   备份文件静默报废 —— 而且 sha256 照样能算出来，看不出任何异常。
        #
        #   需要报错信息时走 _dd_error()：它单独跑一次、不关心 stdout 干不干净。
        # 用 bs=512 + skip 保证任意偏移都能精确读取（块设备上 dd 会 lseek，不会真读）
        cmd = (f"{self._caps.dd} if={dev} bs=512 skip={offset // 512} "
               f"count={length // 512} 2>/dev/null")
        err_path = local_path + ".stderr"
        with open(local_path, "wb") as fout, open(err_path, "wb") as ferr:
            p = subprocess.Popen(self._base() + ["exec-out", f"su -c '{cmd}'"],
                                 stdout=fout, stderr=ferr,
                                 creationflags=_CREATE_NO_WINDOW)
            _register_child(p)        # 关窗时能被统一清掉，不留孤儿 adb.exe
            try:
                p.wait(timeout=300)          # GPT 头尾各只有 1 MiB，300 秒绰绰有余
            except subprocess.TimeoutExpired:
                p.kill()
                try:
                    p.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    pass
            finally:
                _unregister_child(p)
        err_text = ""
        try:
            with open(err_path, "rb") as f:
                err_text = f.read().decode("utf-8", "replace").strip()
        except OSError:
            pass
        try:
            os.remove(err_path)
        except OSError:
            pass
        size = os.path.getsize(local_path) if os.path.exists(local_path) else 0
        if size != length:
            # 读少了才有必要单独捞报错 —— 正常路径上不要多跑一次 adb
            extra = self._dd_error(dev, offset, length)
            if extra:
                err_text = extra
        return size, err_text

    def _dd_error(self, dev: str, offset: int, length: int) -> str:
        """
        读取失败后，单独跑一次把设备端的报错捞出来。

        为什么要单独一次：见 read_device_file 的说明 —— 正常读取必须
        带 2>/dev/null 才能保证字节流干净，所以报错没法在同一个进程里拿到。
        这次用 `of=/dev/null` 丢弃数据、只关心文字输出，脏一点无所谓。
        """
        try:
            out = self.su(f"{self._caps.dd} if={dev} bs=512 "
                          f"skip={offset // 512} count={length // 512} "
                          f"of=/dev/null", timeout=120)
        except (AdbError, BackupError) as e:
            return str(e)[:300]
        return " ".join((out or "").split())[:300]

    def pull_file(self, remote: str, local_path: str,
                  timeout: int = 180) -> bool:
        """
        把【设备端】文件取回本地，返回是否成功。

        配套 sgdisk --backup 这类"必须在设备上生成文件"的工具使用 ——
        它们没法像 exec-out 那样直接把字节流管道回本地。
        """
        try:
            p = _run(self._base() + ["pull", remote, local_path],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               creationflags=_CREATE_NO_WINDOW, timeout=timeout)
        except (subprocess.TimeoutExpired, OSError):
            return False
        return (p.returncode == 0
                and os.path.exists(local_path)
                and os.path.getsize(local_path) > 0)

    def cleanup_device(self):
        """删除设备端遗留的暂存目录与临时脚本。"""
        for cmd in (f"rm -rf {DEVICE_STAGE_DIR}", f"rm -f {DEVICE_PROBE_SCRIPT}"):
            try:
                self.su(cmd, timeout=60)
            except AdbError:
                pass


# ==============================================================================
#  备份引擎
# ==============================================================================

@dataclass
class EngineOptions:
    do_gpt: bool = True
    do_env: bool = False
    # 默认开 —— 这是唯一能证明「设备上的字节 == 硬盘上的字节」的一层
    verify_device_side: bool = True
    # 超过这个大小就不做设备端校验了（0 = 不限）。
    # 关键分区全在这个阈值之下：persist 32MB、modemst1/2 8MB、fsg 8MB、
    # devinfo 16MB、frb 512KB、secdata 32KB —— 校验耗时可以忽略。
    # 跳过的只有 super(9GB)/userdata(226GB) 这类可再生的大块头。
    verify_max_size: int = HUGE_THRESHOLD
    allow_fallback: bool = True
    exclude_busybox: bool = True
    skip_boot_disks: bool = True      # eMMC 的 boot0/boot1 默认不碰
    # ==== APB_ARCHIVE BEGIN ====
    # 备份全部跑完（含校验）之后，可选地把整个备份目录压成一个压缩包。
    # 默认关 —— 这是一步纯附带的便利功能，默认开启会让每次备份都多花时间。
    archive_enabled: bool = False
    archive_format: str = "zip"       # 当前只实现 zip；见 ArchiveSummary 的说明
    archive_level: int = 6            # zip 压缩等级 1~9
    # ==== APB_ARCHIVE END ====
# ==== APB_ARCHIVE BEGIN ====


@dataclass
class ArchiveSummary:
    """打包环节的结果汇总 —— GUI / CLI / 日志统一从这里读。

    刻意与 archive_pack.ArchiveResult 分开：
      · ArchiveResult 描述「某一次打包调用的结果」
      · ArchiveSummary 描述「这次备份的打包环节到底发生了什么」，
        还要能表达「压根没开启」这种根本没调用打包的状态。

    ⚠️ 它**不参与** run() 的返回值 —— run() 仍然只返回 list[ItemResult]，
       这样既有的调用方、测试、失败计数逻辑一行都不用改。
       打包失败绝不能把整次备份算成失败：备份数据本身是好的。
    """
    enabled: bool = False
    ok: bool = False
    path: str = ""                     # 压缩包路径（同级、同名、加扩展名）
    fmt: str = "zip"
    src_bytes: int = 0                 # 原始总字节
    out_bytes: int = 0                 # 压缩后字节（失败为 0）
    seconds: float = 0.0
    message: str = ""                  # 成功时的一句话
    error: str = ""                    # 失败原因（原文）

    @property
    def saved_pct(self) -> Optional[float]:
        """省了百分之多少。没成功或原始大小为 0 时返回 None。"""
        if not self.ok or self.src_bytes <= 0:
            return None
        return (1.0 - self.out_bytes / float(self.src_bytes)) * 100.0

    @property
    def display(self) -> str:
        """一句话汇总，GUI / CLI 共用。"""
        if not self.enabled:
            return "未启用"
        if self.ok:
            return f"{self.message}   →   {self.path}"
        return f"失败：{self.message or self.error}"

    @classmethod
    def from_result(cls, res) -> "ArchiveSummary":
        """由 archive_pack.ArchiveResult 构造。"""
        return cls(enabled=True, ok=bool(res.ok), path=res.path, fmt=res.fmt,
                   src_bytes=res.src_bytes, out_bytes=res.out_bytes,
                   seconds=res.seconds, message=res.message, error=res.error)
# ==== APB_ARCHIVE END ====


class BackupEngine:
    """
    备份流程编排。

    进度与日志通过回调上报，本类不关心它们最终进 Tk 控件还是控制台，
    因此在无 GUI 环境下也能完整跑通（便于自动化测试）。
    """

    def __init__(
        self,
        adb: Adb,
        outdir: str,
        options: EngineOptions,
        info: DeviceInfo,
        log_cb: Callable[[str], None] = lambda s: None,
        progress_cb: Callable[[dict], None] = lambda d: None,
        cancel: Optional[threading.Event] = None,
    ):
        self.adb = adb
        self.outdir = outdir
        self.opt = options
        self.info = info
        self.log = log_cb
        self.progress = progress_cb
        self.cancel = cancel or threading.Event()

        self.img_dir = os.path.join(outdir, "img")
        self.gpt_dir = os.path.join(outdir, "gpt")
        self.results: list[ItemResult] = []
        self._total = 0
        self._done = 0
        self._dd = info.caps.dd or "dd"
        # ==== APB_ARCHIVE BEGIN ====
        # 打包环节的结果汇总。默认「未启用」，archive_enabled 为真时才被改写。
        self.archive = ArchiveSummary()
        # ==== APB_ARCHIVE END ====

    # ------------------------------------------------------------------ 内部
    def _check_cancel(self):
        if self.cancel.is_set():
            raise Cancelled("用户取消")

    def _emit(self, **kw):
        self.progress(kw)

    def _ensure_dirs(self):
        os.makedirs(self.img_dir, exist_ok=True)
        if self.opt.do_gpt:
            os.makedirs(self.gpt_dir, exist_ok=True)

    # ------------------------------------------------------------------ 单分区
    def backup_partition(self, part: PartitionInfo) -> ItemResult:
        name = validate_partition_name(part.name)
        dest = os.path.join(self.img_dir, f"{name}.img")
        res = ItemResult(kind="PART", name=name, expect_size=part.size, path=dest)
        self.log(f"开始 {name}  ({human_size(part.size)})  [{part.label}]")
        self._emit(phase="start", item=name, expect=part.size)

        def on_progress(cur: int, elapsed: float):
            self._emit(phase="item", item=name, done=cur, expect=part.size,
                       elapsed=elapsed, speed=(cur / elapsed) if elapsed > 0 else 0)

        t0 = time.time()
        try:
            try:
                size, _ = self.adb.stream_partition_to_file(
                    name, dest, part.size, self._dd, on_progress, self.cancel)
                res.path_mode = PATH_MODE_STREAM
            except FallbackNeeded as e:
                if not self.opt.allow_fallback:
                    raise
                self.log(f"  [!] 流式失败（{e}），回退到设备端暂存模式 ...")
                res.path_mode = PATH_MODE_STAGE
                size, _ = self.adb.stage_partition_and_pull(
                    name, dest, self._dd, on_progress, self.cancel)

            self._check_cancel()
            res.real_size = size
            self._emit(phase="hash", item=name, done=0, expect=size)
            res.sha256 = sha256_file(dest, cancel=self.cancel)

            # 设备端二次校验 —— 这是**唯一能证明「设备上的字节 == 你硬盘上的字节」**
            # 的一层。头尾抽样、双读比对、第二条传输路径能抓的东西它全抓得到，
            # 所以有它就不需要那些了。
            #
            # 代价是设备要把分区再读一遍算 sha256。UFS 顺序读 1~2 GB/s，
            # 而 adb 传输通常是 30~150 MB/s —— 也就是说对同一个分区，
            # 校验耗时只有传输耗时的百分之几，基本等于白送。
            # 但 super(9GB)/userdata(226GB) 这种大到离谱的还是会拖时间，
            # 而且它们本来就是可再生的，所以超过阈值就自动跳过。
            if self.opt.verify_device_side:
                cap = self.opt.verify_max_size
                if cap and size > cap:
                    self.log(f"  [i] {name} 有 {human_size(size)}，"
                             f"超过 {human_size(cap)} 的校验上限，跳过设备端校验")
                else:
                    self.log(f"  设备端校验 {name} ...")
                    dev = self.adb.su(
                        f"sha256sum /dev/block/by-name/{name} 2>/dev/null",
                        timeout=1800)
                    dev_sha = dev.split()[0] if dev.strip() else ""
                    if dev_sha and dev_sha != res.sha256:
                        res.ok = False
                        res.message = (f"设备端哈希不一致 设备={dev_sha[:16]} "
                                       f"本地={res.sha256[:16]}")
                        self.log(f"  [X] {name}：{res.message}")
                        return res
                    if not dev_sha:
                        # 设备端没算出哈希（没有 sha256sum、权限不足…）
                        # 不当作失败，但必须说出来 —— 否则用户以为验过了
                        self.log(f"  [!] {name} 设备端没算出哈希，"
                                 f"这一项未做二次校验")
                        res.device_verified = False
                    else:
                        res.device_verified = True

            res.ok = True
            res.message = "OK"
        except Cancelled:
            raise
        except BackupError as e:
            res.ok, res.message = False, str(e)
        except OSError as e:
            res.ok, res.message = False, f"本地写入失败: {e}"
        finally:
            res.seconds = time.time() - t0

        if res.ok:
            spd = (res.real_size / res.seconds / 1048576) if res.seconds > 0 else 0
            self.log(f"  [OK] {name}  {human_size(res.real_size)}  {res.seconds:.1f}s  "
                     f"{spd:.1f}MB/s  {res.sha256[:12]}…")
        else:
            self.log(f"  [X] {name} 失败: {res.message}")

        self._done += 1
        self._emit(phase="item_done", item=name, done=self._done, total=self._total)
        return res

    # ------------------------------------------------------------------ GPT
    def backup_gpt(self) -> list[ItemResult]:
        outs: list[ItemResult] = []
        topo = self.adb.detect_topology()
        self.log(f"存储类型: {topo.display}  整盘 {len(topo.disks)} 个")

        targets = [d for d in topo.disks
                   if not (self.opt.skip_boot_disks and d.is_boot_part)]
        if not targets:
            self.log("  [!] 未探测到可备份的整盘设备")
            return outs

        for disk in targets:
            self._check_cancel()
            self._emit(phase="gpt", item=disk.name)
            try:
                outs.extend(self._backup_one_disk(disk))
            except Cancelled:
                raise
            except BackupError as e:
                outs.append(ItemResult(kind="GPT", name=disk.name, sub="error",
                                       ok=False, message=str(e)))
                self.log(f"  [X] {disk.name} 失败: {e}")
        return outs

    def _backup_one_disk(self, disk: StorageDisk) -> list[ItemResult]:
        outs: list[ItemResult] = []
        dev = disk.node
        ss = disk.sector_size or 512
        size = disk.size or self.adb.get_device_size(dev)
        count = GPT_SLICE // ss

        # --- 1) 原始前 1 MiB（保护性 MBR + 主 GPT + 分区表）---
        head = os.path.join(self.gpt_dir, f"{disk.name}_head_1M.bin")
        got, err = self.adb.read_device_file(dev, head, 0, GPT_SLICE)
        head_ok = (got == GPT_SLICE)          # 第 4 段的布局解析要用
        if head_ok:
            outs.append(self._file_result("GPT", disk.name, "head_1M", head))
        else:
            outs.append(ItemResult(
                kind="GPT", name=disk.name, sub="head_1M", ok=False,
                message=f"只读到 {human_size(got)}，应为 {human_size(GPT_SLICE)}"
                        + (f"；dd: {err}" if err else "")))

        # --- 2) 原始后 1 MiB（备份 GPT）---
        #     ⚠️ 必须走 read_device_file —— 它用 exec-out 把字节流【回传】到本地。
        #
        #     绝不能写成 self.adb.su(f"dd ... of={tail}")：那条命令在【设备】上
        #     执行，而 tail 是本地 Windows 路径（D:\...\gpt\sda_tail_1M.bin），
        #     设备上根本不存在，必然失败。
        #     v1.1.0 里六个 LUN 的尾部全部失败正是这个原因 —— 而且当时命令末尾
        #     挂了 2>/dev/null，把 dd 的报错全部吞掉，界面上只显示
        #     「尾部 GPT 未生成」，完全查不出所以然。
        #
        #     偏移在 Python 里算 —— 原生 64 位，天然避开 shell 的 32 位整数溢出坑。
        tail = os.path.join(self.gpt_dir, f"{disk.name}_tail_1M.bin")
        if size > 2 * GPT_SLICE:
            got, err = self.adb.read_device_file(dev, tail,
                                                 size - GPT_SLICE, GPT_SLICE)
            if got == GPT_SLICE:
                outs.append(self._file_result("GPT", disk.name, "tail_1M", tail))
            else:
                outs.append(ItemResult(
                    kind="GPT", name=disk.name, sub="tail_1M", ok=False,
                    message=f"尾部不完整：拿到 {human_size(got)}，"
                            f"应为 {human_size(GPT_SLICE)}"
                            + (f"；dd: {err}" if err else "")))
        else:
            outs.append(ItemResult(kind="GPT", name=disk.name, sub="tail_1M",
                                   ok=False, message="磁盘过小，无尾部 GPT"))

        # --- 3) sgdisk 结构化备份（可选，仅当设备上有 sgdisk）---
        #     ⚠️ 与上面尾部同一个坑：sgdisk 跑在【设备】上，--backup 的目标必须是
        #        【设备端路径】。写完再 adb pull 取回本地。
        #        直接写本地 Windows 路径同样会失败，而且 >/dev/null 2>&1 会把
        #        报错吞掉，只留下"未生成"三个字。
        sg = os.path.join(self.gpt_dir, f"{disk.name}_gpt_sgdisk.bin")
        if self.info.caps.has_sgdisk:
            dev_sg = f"{DEVICE_STAGE_DIR}/{disk.name}_gpt_sgdisk.bin"
            try:
                self.adb.su(f"mkdir -p {DEVICE_STAGE_DIR}", timeout=30)
                self.adb.su(f"rm -f {dev_sg}", timeout=30)
                self.adb.su(f"sgdisk --backup={dev_sg} {dev}", timeout=120)
                pulled = self.adb.pull_file(dev_sg, sg)
            except AdbError:
                pulled = False
            finally:
                try:
                    self.adb.su(f"rm -f {dev_sg}", timeout=30)
                except AdbError:
                    pass
            if pulled:
                outs.append(self._file_result("GPT", disk.name, "gpt_sgdisk", sg))
            else:
                self.log(f"  [!] {disk.name} sgdisk 备份未生成（不影响原始头尾备份）")
        else:
            self.log(f"  [i] 设备无 sgdisk，跳过结构化备份（原始头/尾已足够恢复）")

        # --- 4) 布局文本：纯 Python 解析，不依赖设备工具 ---
        hdr = None
        if head_ok:
            try:
                with open(head, "rb") as f:
                    hb = f.read(GPT_SLICE)
                hdr = parse_gpt_head(hb)
                entries = parse_gpt_entries(hb, hdr) if hdr.valid else []
                lay = os.path.join(self.gpt_dir, f"{disk.name}_layout.txt")
                with open(lay, "w", encoding="utf-8") as f:
                    f.write(render_layout(disk.name, hdr, entries, size))
                # 登记进 manifest —— 这张表是手工重建 GPT 时的唯一依据，
                # 跟镜像一样需要哈希保护，烂了要能查出来
                outs.append(self._file_result("GPT", disk.name, "layout", lay))
                self.log(f"      {disk.name}: 逻辑扇区={hdr.sector_size} "
                         f"MBR={hdr.mbr_sig} GPT={'OK' if hdr.valid else hdr.detail} "
                         f"分区={len(entries)} 个")
                self._verify_tail(disk.name, tail, hdr, outs)
            except OSError as e:
                self.log(f"  [!] {disk.name} 布局解析失败: {e}")

        n_ok = sum(1 for r in outs if r.ok)
        self.log(f"  [{'OK' if n_ok else '!'}] {disk.name}  GPT 备份完成 "
                 f"({n_ok}/{len(outs)} 项，{human_size(sum(r.real_size for r in outs))})")
        return outs

    def _file_result(self, kind: str, name: str, sub: str, path: str) -> ItemResult:
        r = ItemResult(kind=kind, name=name, sub=sub, path=path)
        try:
            r.real_size = os.path.getsize(path)
            r.sha256 = sha256_file(path, cancel=self.cancel)
            r.ok = True
            r.message = "OK"
        except OSError as e:
            r.ok, r.message = False, str(e)
        return r

    def _verify_tail(self, name: str, tail: str, hdr: GptHeader,
                     outs: list[ItemResult]) -> None:
        """用尾部备份 GPT 与头部交叉验证 —— 两者必须互相指向。"""
        if not (hdr.valid and os.path.exists(tail)):
            return
        try:
            with open(tail, "rb") as f:
                tb = f.read(2 * GPT_SLICE)
        except OSError:
            return
        idx = tb.find(b"EFI PART")
        if idx < 0:
            self.log(f"      {name}: 尾部未找到备份 GPT")
            self._mark_bad(outs, name, "尾部未找到备份 GPT")
            return
        t_my = int.from_bytes(tb[idx + 24:idx + 32], "little")
        t_alt = int.from_bytes(tb[idx + 32:idx + 40], "little")
        if t_my == hdr.alt_lba and t_alt == hdr.my_lba:
            self.log(f"      {name}: head<->tail 交叉一致 (MyLBA={t_my}) | "
                     f"磁盘GUID={hdr.disk_guid}")
        else:
            msg = f"head<->tail 不一致: 尾部 MyLBA={t_my} AltLBA={t_alt}"
            self.log(f"      {name}: {msg}")
            self._mark_bad(outs, name, msg)

    @staticmethod
    def _mark_bad(outs: list[ItemResult], name: str, msg: str):
        for r in outs:
            if r.name == name and r.sub in ("head_1M", "tail_1M"):
                r.ok = False
                r.message = msg

    # ------------------------------------------------------------------ 环境包
    def backup_environment(self) -> ItemResult:
        dest = os.path.join(self.outdir, "data_adb.tar.gz")
        res = ItemResult(kind="TAR", name="data_adb.tar.gz", path=dest)
        self.log("打包 /data/adb root 环境 ...")
        self._emit(phase="env", item="data_adb.tar.gz")

        tar = self.info.caps.tar or "tar"
        dev_tar = f"{DEVICE_STAGE_DIR}/data_adb.tar.gz"

        # ⚠️ 两条必须遵守的写法，都是踩坑换来的：
        #
        # 1) 不要写 `2>/dev/null; true`。
        #    那样确实"永不报错"，但 tar 真失败时也完全看不出来 ——
        #    界面上只剩「环境包未生成」几个字，用户和开发者都无从下手。
        #
        # 2) 所有选项必须排在【文件操作数之前】。
        #    `tar -czf 归档 --exclude=X .` 这种把选项夹在归档名后面的写法，
        #    在 GNU tar 上没问题，但 toybox/bsdtar 可能把 --exclude=X 当成
        #    要打包的文件名而报错。写成 `tar -cz --exclude=X -f 归档 .` 最稳。
        excl = "--exclude=./tmp --exclude=*.sock"
        if self.opt.exclude_busybox:
            excl += " --exclude=./ksu/bin/busybox"
        cmd = (f"mkdir -p {DEVICE_STAGE_DIR} && cd /data/adb && "
               f"rm -f {dev_tar}; "
               f"{tar} -cz {excl} -f {dev_tar} . 2>&1; "
               f"echo __APB_RC=$?; "
               f"if [ -f {dev_tar} ]; then "
               f"echo __APB_SIZE=$(stat -c %s {dev_tar} 2>/dev/null || echo 0); "
               f"else echo __APB_MISSING=1; fi")
        try:
            out = self.adb.su(cmd, timeout=3600)
        except (BackupError, OSError) as e:
            res.ok, res.message = False, f"设备端打包失败: {e}"
            self.log(f"  [X] 环境包失败: {e}")
            return res

        rc_m = re.search(r"__APB_RC=(\d+)", out)
        size_m = re.search(r"__APB_SIZE=(\d+)", out)
        rc_val = int(rc_m.group(1)) if rc_m else -1
        dev_size = int(size_m.group(1)) if size_m else 0

        # tar 的原始输出（去掉我们自己的标记行），失败时原样转给用户看
        noise = [ln.strip() for ln in out.splitlines()
                 if ln.strip() and "__APB_" not in ln]
        detail = " | ".join(noise[-5:])[:300]

        if "__APB_MISSING" in out or dev_size <= 0:
            res.ok = False
            res.message = (f"设备端打包未生成文件（tar 退出码 {rc_val}）"
                           + (f"：{detail}" if detail else "，且无任何输出"))
            self.log(f"  [X] 环境包未生成 —— tar 退出码 {rc_val}")
            for ln in noise[-5:]:
                self.log(f"        {ln}")
            return res

        # ---- 取回本地：暂存+pull 为主，exec-out 流式为备 ----
        #
        # 为什么要有备选：暂存方案的产物落在 /sdcard，而 /sdcard 是 FUSE，
        # 文件属主由 FUSE 自己指派（实测是 u0_a261:media_rw，模式 rw-rw----）。
        # adb pull 是以 shell(uid 2000) 身份读的 —— 既不是属主也不在组里，
        # 能不能读全看 FUSE 当下怎么算。这条链路本质上不可靠，
        # 所以失败时必须有一条不依赖 /sdcard 的路兜底。
        local_size = 0
        err_detail = ""

        try:
            p = _run(self.adb._base() + ["pull", dev_tar, dest],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               creationflags=_CREATE_NO_WINDOW, timeout=7200)
            local_size = os.path.getsize(dest) if os.path.exists(dest) else 0
            if p.returncode != 0 or local_size == 0:
                err_detail = ((p.stderr or b"").decode("utf-8", "replace").strip()
                              or f"adb pull 退出码 {p.returncode}")[:300]
                self.log(f"  [!] 暂存取回失败（{err_detail}），改用流式回传 ...")
            elif local_size != dev_size:
                err_detail = (f"体积不符：设备端 {dev_size}，本地 {local_size}")
                self.log(f"  [!] {err_detail}，改用流式回传 ...")
                local_size = 0
        except (OSError, subprocess.TimeoutExpired) as e:
            err_detail = str(e)[:300]
            self.log(f"  [!] 暂存取回异常（{err_detail}），改用流式回传 ...")
        finally:
            try:
                self.adb.su(f"rm -f {dev_tar}", timeout=30)
            except AdbError:
                pass

        used = "暂存+pull"
        if local_size == 0:
            # 流式：tar 直接写到 stdout，经 exec-out 落到本地，不碰 /sdcard。
            # 2>/dev/null 必须保留 —— su -c 会把 stderr 并进 stdout，
            # 而 stdout 正是要的二进制流（tar 遇到 socket 会打
            # "unknown file type" 警告，混进来就把包毁了）。
            used = "exec-out 流式"
            stream_cmd = (f"cd /data/adb && {tar} -cz {excl} . 2>/dev/null")
            try:
                with open(dest, "wb") as fh:
                    sp = _run(
                        self.adb._base() + ["exec-out", f"su -c '{stream_cmd}'"],
                        stdout=fh, stderr=subprocess.PIPE,
                        creationflags=_CREATE_NO_WINDOW, timeout=7200)
                local_size = os.path.getsize(dest) if os.path.exists(dest) else 0
                if sp.returncode != 0 or local_size == 0:
                    serr = (sp.stderr or b"").decode("utf-8", "replace").strip()[:300]
                    res.ok = False
                    res.message = (f"暂存与流式两条回传路径都失败。"
                                   f"暂存：{err_detail or '未知'}；"
                                   f"流式：{serr or f'退出码 {sp.returncode}'}")
                    self.log(f"  [X] 环境包两条回传路径均失败")
                    self.log(f"        暂存：{err_detail or '未知'}")
                    self.log(f"        流式：{serr or sp.returncode}")
                    return res
            except (OSError, subprocess.TimeoutExpired) as e:
                res.ok = False
                res.message = (f"暂存与流式两条回传路径都失败。"
                               f"暂存：{err_detail or '未知'}；流式：{e}")
                self.log(f"  [X] 环境包两条回传路径均失败: {e}")
                return res

        # gzip 完整性校验 —— 传输被截断时体积可能"看起来对"，sha256 也照样算得出来。
        # 只有真正解一遍才知道包是不是完整的。
        try:
            members = _verify_targz(dest)
        except BackupError as e:
            res.ok = False
            res.message = f"{used}取回后校验失败：{e}"
            self.log(f"  [X] 环境包校验失败: {e}")
            return res

        res.real_size = local_size
        res.sha256 = sha256_file(dest, cancel=self.cancel)
        res.ok, res.message = True, "OK"
        self.log(f"  [OK] data_adb.tar.gz  {human_size(res.real_size)}"
                 f"  含 {members} 个条目  ({used})")
        return res

    # ------------------------------------------------------------------ 主流程
    def run(self, partitions: list[PartitionInfo]) -> list[ItemResult]:
        self._ensure_dirs()
        self._total = len(partitions) + (1 if self.opt.do_env else 0)
        self._done = 0

        self.log(f"===== 备份 {len(partitions)} 个分区 =====")
        for p in partitions:
            self._check_cancel()
            self.results.append(self.backup_partition(p))

        if self.opt.do_gpt:
            self._check_cancel()
            self.log("===== 备份 GPT 分区表 =====")
            self.results.extend(self.backup_gpt())

        if self.opt.do_env:
            self._check_cancel()
            self.log("===== 打包 root 环境 =====")
            self.results.append(self.backup_environment())

        # by-name 映射表
        try:
            self._check_cancel()
            byname = self.info.caps.byname_dir or "/dev/block/by-name"
            mapping = self.adb.su(f"ls -l {byname}/ 2>/dev/null; true", timeout=60)
            target_dir = self.gpt_dir if self.opt.do_gpt else self.outdir
            os.makedirs(target_dir, exist_ok=True)
            bnm = os.path.join(target_dir, "byname_mapping.txt")
            with open(bnm, "w", encoding="utf-8") as f:
                f.write(mapping)
            # 同样登记进 manifest：恢复时靠它把分区名对到设备节点
            self.results.append(
                self._file_result("TXT", "byname_mapping", "", bnm))
            self.log(f"已保存 by-name 映射表（{len(mapping.splitlines())} 行）")
        except (AdbError, OSError) as e:
            self.log(f"[!] by-name 映射表保存失败: {e}")

        # ==== APB_ARCHIVE BEGIN ====
        # ---- 打包（可选，追加在最后；所有分区与校验都已做完）----
        # 失败不影响上面的结果，也不改 run() 的返回值 —— 见 ArchiveSummary 的说明。
        if self.opt.archive_enabled:
            try:
                self.archive = self.archive_output()
            except Exception as e:            # 兜底：打包绝不能让备份算失败
                self.archive = ArchiveSummary(
                    enabled=True, path=self.archive_output_path(),
                    message=f"打包异常：{type(e).__name__}: {e}",
                    error=f"{type(e).__name__}: {e}")
                self.log(f"  [!] 打包阶段异常，已跳过: {self.archive.message}")
        # ==== APB_ARCHIVE END ====
        self._emit(phase="finished")
        return self.results

    def cleanup_device(self):
        self.adb.cleanup_device()
    # ==== APB_ARCHIVE BEGIN ====

    # ------------------------------------------------------------------ 打包
    def archive_output_path(self) -> str:
        """压缩包该放哪 —— 备份目录的**同级**、**同名**，只加一个扩展名。

            ...\\Backups\\Redmi K70\\   →   ...\\Backups\\Redmi K70.zip

        刻意不放进去、也不移动原件：压缩包是**额外**的一份，原件永远原样留着。
        """
        src = os.path.abspath(os.path.normpath(self.outdir))
        return os.path.join(os.path.dirname(src), os.path.basename(src) + ".zip")

    def archive_output(self) -> ArchiveSummary:
        """把整个备份目录压成一个 zip —— 追加在备份流程最后的可选一步。

        【三条不可违背的约束】
        1. 只读备份目录。绝不删除、绝不移动任何原始文件/目录。
        2. 失败**不算**备份失败。备份数据本身是好的，这里只是少了个便利；
           失败原因进日志 + 进 ArchiveSummary，由 GUI / CLI 如实报出来。
        3. 走与备份阶段**同一套**进度事件（phase="archive"），GUI 才能显示。

        【取消了会怎样】
        用户按取消时不再抛 Cancelled，而是让打包停在半路、把结果记成
        「已取消打包」。原因：此时分区镜像与校验**都已经成功完成**，
        若抛 Cancelled，调用方（GUI 工作线程）会走取消分支直接返回，
        连 manifest.txt / README.md / backup_log.txt 都不会再写 ——
        为了一个附属步骤丢掉整份备份的收尾材料，明显不划算。
        """
        out_path = self.archive_output_path()
        summary = ArchiveSummary(enabled=True, path=out_path)

        fmt = (self.opt.archive_format or "zip").strip().lower()
        if fmt != "zip":
            # 只实现了 zip。这里必须说出来，否则用户以为拿到的是 7z。
            self.log(f"  [!] 只支持 zip 打包，忽略配置里的 {fmt!r}，按 zip 处理")
            fmt = "zip"
        summary.fmt = fmt

        level = self.opt.archive_level
        try:
            level = min(9, max(1, int(level)))
        except (TypeError, ValueError):
            level = 6

        src = os.path.abspath(os.path.normpath(self.outdir))
        name = os.path.basename(src)
        self.log(f"===== 打包备份产物（zip, 等级 {level}）=====")
        self.log(f"  源目录 : {src}")
        self.log(f"  压缩包 : {out_path}")

        expects = {"bytes": 0, "files": 0}
        last_emit = {"t": 0.0}

        def on_progress(done_files: int, total_files: int,
                        done_bytes: int, total_bytes: int) -> None:
            # 源目录里有几百个文件时，每个文件都发一次事件会把消息队列灌满，
            # 所以按时间节流到 4 Hz；文件很少时这点延迟完全看不出来。
            now = time.time()
            if done_bytes < total_bytes and now - last_emit["t"] < 0.25:
                return
            last_emit["t"] = now
            expects["bytes"], expects["files"] = total_bytes, total_files
            self._emit(phase="archive", item=name + ".zip",
                       done=done_bytes, expect=total_bytes,
                       files=done_files, total_files=total_files)

        self._emit(phase="archive_start", item=name + ".zip")

        try:
            res = make_archive(src, out_path, level=level,
                               progress_cb=on_progress,
                               cancel_check=self.cancel.is_set)
        except Exception as e:                    # make_archive 承诺不抛，这里只是兜底
            summary.message = f"打包异常：{type(e).__name__}: {e}"
            summary.error = f"{type(e).__name__}: {e}"
            # 同样用 [!] 告警前缀：备份本身是成功的，别让用户看到红色就慌
            self.log(f"  [!] 打包阶段异常，已跳过: {summary.message}")
            self.log(f"      备份数据本身完好，压缩包只是额外的一份，不影响恢复。")
            self._emit(phase="archive_done", item=name + ".zip", ok=False,
                       message=summary.message,
                       path=out_path)
            return summary

        summary.ok = bool(res.ok)
        summary.src_bytes = int(res.src_bytes or 0)
        summary.out_bytes = int(res.out_bytes or 0)
        summary.seconds = float(res.seconds or 0)
        summary.message = res.message or ""
        summary.error = res.error or ""

        if res.ok:
            self.log(f"  [OK] {os.path.basename(out_path)}  "
                     f"{summary.message}  {summary.seconds:.1f}s")
        else:
            # 只是告警，不是备份失败 —— 前缀用 [!] 而不是 [X]，
            # 免得用户在日志里看到红色就以为备份坏了。
            self.log(f"  [!] 打包未完成: {res.message}"
                     + (f"（{res.error}）" if res.error else ""))
            self.log(f"      备份数据本身完好，压缩包只是额外的一份，不影响恢复。")
        if res.path and res.path != out_path:
            self.log(f"  [i] 实际输出路径: {res.path}")

        self._emit(phase="archive_done", item=name + ".zip",
                   ok=summary.ok, message=summary.display,
                   path=out_path, out_bytes=summary.out_bytes,
                   src_bytes=summary.src_bytes)
        return summary

    def archive_add_paths(self, paths: list) -> tuple[bool, str]:
        """把 run() 返回之后才写出来的文件补进已经做好的压缩包里。

        调用时机：调用方写完 manifest.txt / README.md / backup_log.txt 之后。
        （这些文件的内容依赖 run() 的结果，所以不可能赶在打包之前写。）

        未开启打包、或打包没成功时，本方法什么都不做。
        返回 (是否成功, 说明文字) —— 失败同样不影响备份结果。
        """
        if not (self.archive.enabled and self.archive.ok and self.archive.path):
            return False, "未启用打包或打包未成功"
        src = os.path.abspath(os.path.normpath(self.outdir))
        name = os.path.basename(src)
        items = []
        for p in paths:
            if isinstance(p, (tuple, list)) and len(p) == 2:
                local, arc = p
            else:
                local, arc = p, f"{name}/{os.path.basename(p)}"
            items.append((local, arc))
        ok, msg = add_to_archive(self.archive.path, items)
        if not ok:
            self.log(f"  [!] 报告文件未能补进压缩包: {msg}")
        return ok, msg

    # ==== APB_ARCHIVE END ====


# ==============================================================================
#  备份目录命名与冲突消解
# ==============================================================================

DEVICE_MARKER = ".device_info"

# 备份名称留空时使用的默认名。刻意【不】带机型/代号 ——
# 本工具是全设备通用的，名称交给用户自己定，重名由日期后缀消解。
DEFAULT_BACKUP_NAME = "Backup"

# Windows 保留设备名 —— 叫这些名字的文件夹建不出来
_RESERVED_WIN = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

# Windows 文件名非法字符
_ILLEGAL_NAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize_folder_name(name: str, fallback: str = "Backup") -> str:
    """
    把用户输入的备份名称变成安全的文件夹名。

    ⚠️ 这同时是一道【安全防线】：
       用户可能输入 ..\\..\\Windows 之类的内容试图做路径穿越，
       这里把所有路径分隔符与 .. 全部替换掉，确保最终目录一定落在根目录之内。
    """
    if not name:
        return fallback
    s = _ILLEGAL_NAME_CHARS.sub("_", str(name))
    s = s.replace("..", "_")            # 阻断路径穿越
    s = s.strip().strip(".").strip()    # Windows 不允许首尾是点或空格
    s = re.sub(r"\s+", " ", s)          # 折叠连续空白
    if not s:
        return fallback
    if s.upper() in _RESERVED_WIN:
        s = "_" + s
    if len(s) > 80:
        s = s[:80].rstrip(". ")
    return s or fallback


def write_device_marker(outdir: str, meta: dict) -> str:
    """在每个备份目录里留一份设备指纹，供下次判断是否'同一机型'。"""
    path = os.path.join(outdir, DEVICE_MARKER)
    lines = [
        f"codename={meta.get('codename','')}",
        f"serial={meta.get('serial','')}",
        f"model={meta.get('model','')}",
        f"brand={meta.get('brand','')}",
        f"android={meta.get('android','')}",
        f"version={meta.get('version','')}",
        f"created={datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
    ]
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except OSError:
        pass
    return path


def read_device_marker(outdir: str) -> dict:
    """读取设备指纹；没有则返回空 dict。"""
    path = os.path.join(outdir, DEVICE_MARKER)
    out: dict[str, str] = {}
    if not os.path.exists(path):
        # 兼容早期没有 marker 的备份：退而求其次读 manifest 头几行
        man = os.path.join(outdir, "manifest.txt")
        if os.path.exists(man):
            try:
                with open(man, "r", encoding="utf-8") as f:
                    for _ in range(6):
                        line = f.readline()
                        if not line:
                            break
                        if "codename:" in line:
                            out["codename"] = line.split("codename:")[1].strip()
                        if "SN " in line:
                            out["serial"] = line.split("SN ")[1].split()[0]
            except OSError:
                pass
        return out
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                if "=" in line:
                    k, _, v = line.strip().partition("=")
                    out[k.strip()] = v.strip()
    except OSError:
        pass
    return out


def same_device(marker: dict, codename: str, serial: str) -> bool:
    """
    判断某个已有备份目录是否来自同一台设备。

    优先比序列号（唯一）；序列号缺失时退化为比机型代号。
    """
    if not marker:
        return False
    ms, ss = marker.get("serial", ""), serial or ""
    if ms and ss:
        return ms == ss
    mc, cc = marker.get("codename", ""), codename or ""
    return bool(mc and cc and mc == cc)


@dataclass
class ResolvedDir:
    path: str
    base_name: str
    final_name: str
    reason: str          # 人类可读的说明，直接显示在界面上
    collided: bool = False
    same_device_as: str = ""


def resolve_backup_dir(root: str, base_name: str,
                       codename: str = "", serial: str = "",
                       when: Optional[datetime] = None) -> ResolvedDir:
    """
    按以下规则决定本次备份的最终目录：

        1. root/自定义名 不存在            → 直接用它
        2. 已存在，且是【同一台设备】的备份 → 自定义名_YYYYMMDD
        3. 已存在，但是【别的设备】         → 同样加日期，绝不把两台机器混在一个目录
        4. 加日期后仍冲突                  → 再追加 _HHMMSS
        5. 还冲突                          → 追加 _2 / _3 ...

    绝不会覆盖任何已有备份。
    """
    when = when or datetime.now()
    safe = sanitize_folder_name(base_name, fallback="Backup")
    first = os.path.join(root, safe)

    if not os.path.exists(first):
        return ResolvedDir(path=first, base_name=safe, final_name=safe,
                           reason="首次使用该名称")

    marker = read_device_marker(first)
    is_same = same_device(marker, codename, serial)
    prev = marker.get("codename") or marker.get("model") or "未知设备"
    if is_same:
        why = f"检测到同一台设备（{prev}）已用此名称备份过 → 追加日期"
    elif marker:
        why = f"此名称已被其它设备（{prev}）占用 → 追加日期以免混淆"
    else:
        why = "此名称下已有内容但无法识别设备 → 追加日期以免覆盖"

    cand = f"{safe}_{when.strftime('%Y%m%d')}"
    if not os.path.exists(os.path.join(root, cand)):
        return ResolvedDir(path=os.path.join(root, cand), base_name=safe,
                           final_name=cand, reason=why, collided=True,
                           same_device_as=prev if is_same else "")

    cand = f"{safe}_{when.strftime('%Y%m%d_%H%M%S')}"
    if not os.path.exists(os.path.join(root, cand)):
        return ResolvedDir(path=os.path.join(root, cand), base_name=safe,
                           final_name=cand,
                           reason=why + "（同日已备份，再加时间）",
                           collided=True, same_device_as=prev if is_same else "")

    n = 2
    while True:
        cand = f"{safe}_{when.strftime('%Y%m%d_%H%M%S')}_{n}"
        if not os.path.exists(os.path.join(root, cand)):
            return ResolvedDir(path=os.path.join(root, cand), base_name=safe,
                               final_name=cand,
                               reason=why + f"（再加序号 {n}）",
                               collided=True,
                               same_device_as=prev if is_same else "")
        n += 1


def default_backup_root(program_dir: str) -> str:
    """
    默认备份根目录 = 程序目录下的 Backups。

    若程序目录不可写（例如装在 Program Files），自动退回家目录下的
    文档目录，避免用户第一次备份就失败。
    """
    cand = os.path.join(program_dir, "Backups")
    try:
        os.makedirs(cand, exist_ok=True)
        probe = os.path.join(cand, ".write_test")
        with open(probe, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(probe)
        return cand
    except OSError:
        alt = os.path.join(os.path.expanduser("~"), "Documents",
                           "AndroidPartitionBackup")
        try:
            os.makedirs(alt, exist_ok=True)
        except OSError:
            alt = os.path.expanduser("~")
        return alt


# ==============================================================================
#  报告生成
# ==============================================================================

def write_manifest(outdir: str, results: Iterable[ItemResult], meta: dict) -> str:
    lines = [
        "# Android backup manifest",
        f"# device  : {meta.get('display','')}  SN {meta.get('serial','')}",
        f"# codename: {meta.get('codename','')}",
        f"# system  : {meta.get('version','')}  (Android {meta.get('android','')})  "
        f"slot {meta.get('slot','') or '无'}",
        f"# platform: {meta.get('platform','')}",
        f"# storage : {meta.get('storage','')}",
        f"# created : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "# format  : TYPE NAME [SUB] SIZE SHA256",
        "",
    ]
    for r in results:
        state = r.sha256 if r.ok else ("FAILED:" + r.message.replace(" ", "_"))
        lines.append(f"{r.kind} {r.name} {r.sub} {r.real_size} {state}")
    path = os.path.join(outdir, "manifest.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return path


MANIFEST_KINDS = ("PART", "GPT", "TAR", "TXT")


def parse_manifest_line(line: str) -> Optional[dict]:
    """解析 manifest 的一行数据。

    格式:  TYPE NAME [SUB] SIZE SHA256
    —— SUB 可能为空，被 split() 吃掉，所以段数是不定的：

        PART persist  33554432 <sha>          4 段（SUB 为空）
        GPT  sda head_1M 1048576 <sha>        5 段（有 SUB）
        TAR  data_adb.tar.gz  43952010 <sha>  4 段（SUB 为空）

    ⚠️ 老版本这里写的是 `len(parts) >= 5`，结果**所有 SUB 为空的行全部被
    静默跳过** —— 真机那份 manifest 45 条数据只认出 18 条（恰好是 18 个
    GPT 行），27 个 PART 和 1 个 TAR 一个都没认出来，「对比上次备份」
    这个功能对分区一直是失效的。所以这里改成按段数分情况判断。
    """
    parts = line.strip().split()
    if len(parts) < 4 or parts[0] not in MANIFEST_KINDS:
        return None
    kind, name = parts[0], parts[1]
    if len(parts) >= 5:
        sub, size, sha = parts[2], parts[3], parts[4]
    else:
        sub, size, sha = "", parts[2], parts[3]
    if sha.upper().startswith("FAILED:"):
        return None
    try:
        size_i = int(size)
    except ValueError:
        return None
    return {"kind": kind, "name": name, "sub": sub,
            "size": size_i, "sha256": sha.lower()}



def read_manifest(manifest_path: str) -> list:
    """读 manifest，返回条目列表（跳过解析不了的行的同时记下来）。"""
    entries: list = []
    if not os.path.exists(manifest_path):
        return entries
    try:
        with open(manifest_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                s = line.strip()
                if not s or s.startswith("#"):
                    continue
                e = parse_manifest_line(s)
                if e:
                    entries.append(e)
    except OSError:
        pass
    return entries


def load_previous_hashes(manifest_path: str) -> dict:
    out: dict = {}
    for e in read_manifest(manifest_path):
        out[f"{e['kind']}|{e['name']}|{e['sub']}"] = e["sha256"]
    return out


def find_previous_backup(root: str, current: str, tag: str = "") -> Optional[str]:
    """找同设备上一次备份目录。"""
    if not os.path.isdir(root):
        return None
    cands = []
    for name in os.listdir(root):
        full = os.path.join(root, name)
        if not os.path.isdir(full) or os.path.abspath(full) == os.path.abspath(current):
            continue
        if "_Backup_" not in name:
            continue
        if tag and not name.startswith(tag):
            continue
        if not os.path.exists(os.path.join(full, "manifest.txt")):
            continue
        cands.append(full)
    cands.sort()
    return cands[-1] if cands else None


def diff_against_previous(results: list[ItemResult], prev_dir: Optional[str]):
    same, changed, added = [], [], []
    if not prev_dir:
        return same, changed, added
    prev = load_previous_hashes(os.path.join(prev_dir, "manifest.txt"))
    if not prev:
        return same, changed, added
    for r in results:
        if not r.ok:
            continue
        key = f"{r.kind}|{r.name}|{r.sub}"
        if key not in prev:
            added.append(r.label)
        elif prev[key] == r.sha256.lower():
            same.append(r.label)
        else:
            changed.append(r.label)
    return same, changed, added


def _ellipsis(items: list[str], limit: int = 15) -> str:
    if not items:
        return "—"
    if len(items) <= limit:
        return ", ".join(items)
    return ", ".join(items[:limit]) + f" …等 {len(items)} 项"


def write_readme(outdir: str, results: list[ItemResult], meta: dict,
                 prev_dir: Optional[str], selected: list[PartitionInfo]) -> str:
    same, changed, added = diff_against_previous(results, prev_dir)
    ok_n = sum(1 for r in results if r.ok)
    bad_n = len(results) - ok_n
    total = sum(r.real_size for r in results if r.ok)

    L: list[str] = []
    L.append(f"# 安卓分区备份 — {meta.get('stamp','')}")
    L.append("")
    L.append("## 设备信息")
    L.append("")
    L.append("| 项目 | 值 |")
    L.append("|---|---|")
    L.append(f"| 设备 | {meta.get('display','')} |")
    L.append(f"| 代号 | {meta.get('codename','') or '—'} |")
    L.append(f"| 序列号 | {meta.get('serial','')} |")
    L.append(f"| 系统 | {meta.get('version','')} |")
    L.append(f"| Android | {meta.get('android','')}（SDK {meta.get('sdk','')}）|")
    L.append(f"| SoC 平台 | {meta.get('platform','')}（识别得分 {meta.get('platform_score',0)}）|")
    L.append(f"| 存储类型 | {meta.get('storage','')} |")
    L.append(f"| 槽位 | {meta.get('slot','') or '无（A-only 设备）'} |")
    L.append(f"| root | {meta.get('root','')} |")
    L.append(f"| 备份时间 | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} |")
    L.append("")
    L.append("> BL 真实状态请以 `/proc/bootconfig` 为准 —— `getprop` 的")
    L.append("> `ro.boot.verifiedbootstate` 会被模块 resetprop 伪装。")
    L.append("")

    L.append("## 与上次备份的差异")
    L.append("")
    if prev_dir:
        L.append(f"对比基准：`{os.path.basename(prev_dir)}`")
        L.append("")
        L.append("| 项目 | 数量 | 内容 |")
        L.append("|---|---|---|")
        L.append(f"| 未变化 | {len(same)} | {_ellipsis(same)} |")
        L.append(f"| **已变化** | {len(changed)} | {_ellipsis(changed)} |")
        if added:
            L.append(f"| 本次新增 | {len(added)} | {_ellipsis(added)} |")
        if "modemst1" in changed:
            L.append("")
            L.append("> 提示：`modemst1` 由基带在运行时会自行写入，变化属正常现象 ——")
            L.append("> 真正长期稳定的身份副本是 `modemst2` 和 `fsg`。")
    else:
        L.append("（首次备份本设备，无历史记录可对比）")
    L.append("")

    L.append("## 备份内容")
    L.append("")
    L.append("| 类型 | 名称 | 大小 | 校验 |")
    L.append("|---|---|---|---|")
    for r in results:
        loc = {"PART": "img\\", "GPT": "gpt\\"}.get(r.kind, "")
        ext = ".img" if r.kind == "PART" else ""
        mark = "✅" if r.ok else "❌ " + r.message[:40]
        L.append(f"| {r.kind} | {loc}{r.label}{ext} | {human_size(r.real_size)} | "
                 f"{mark} `{r.sha256[:12]}…` |")
    L.append("")
    L.append("## 合计")
    L.append("")
    L.append(f"- 条目数：{len(results)}")
    L.append(f"- 总大小：{human_size(total)}")
    L.append(f"- 校验：**{ok_n} 通过 / {bad_n} 失败**")
    L.append("")

    if selected:
        crit = [p for p in selected if p.tier == 1]
        if crit:
            L.append("## 本次备份的不可再生分区")
            L.append("")
            L.append("| 分区 | 大小 | 为什么不可再生 |")
            L.append("|---|---|---|")
            for p in crit:
                L.append(f"| `{p.name}` | {human_size(p.size)} | {p.reason} |")
            L.append("")

    L.append("## 恢复要点")
    L.append("")
    L.append("```")
    slot = meta.get("slot", "") or ""
    L.append("# 恢复 root（最常见用途，需 fastboot 模式）")
    L.append(f"fastboot flash init_boot img\\init_boot{slot}.img" if slot
             else "fastboot flash init_boot img\\init_boot.img")
    L.append("")
    L.append("# 系统内恢复不可再生分区（需 root）")
    L.append("dd if=img\\persist.img  of=/dev/block/by-name/persist  bs=1M")
    L.append("dd if=img\\fsg.img      of=/dev/block/by-name/fsg      bs=1M")
    L.append("sync && reboot")
    L.append("")
    L.append("# 恢复 GPT（先读 gpt/ 目录下的布局文本，务必确认逻辑扇区大小）")
    L.append(f"sgdisk --load-backup=gpt\\{meta.get('first_disk','sda')}_gpt_sgdisk.bin "
             f"/dev/block/{meta.get('first_disk','sda')}")
    L.append("```")
    L.append("")
    L.append("> ⚠️ 四条铁律")
    L.append("> 1. `persist` / `modemst*` / `efs` / `nvdata` 等**单机绑定**，严禁跨设备恢复")
    L.append("> 2. `secdata` **永远不要恢复旧值** —— 防回滚计数器，写旧值可能触发熔断变砖")
    L.append("> 3. 逻辑扇区不一定是 512 —— 本机是多少看 `gpt/*_layout.txt` 第一行")
    L.append("> 4. 恢复 GPT 前先用 `sgdisk --backup` 给当前 GPT 留底，哪怕它是坏的")
    L.append("")

    path = os.path.join(outdir, "README.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    return path


# ==============================================================================
#  命令行入口（无 GUI 自测 / 自动化用）
# ==============================================================================

def _cli():
    import argparse
    ap = argparse.ArgumentParser(description="安卓分区备份核心引擎（无 GUI 自测）")
    ap.add_argument("--adb", required=True)
    ap.add_argument("--serial", default="")
    ap.add_argument("--out", default=".")
    ap.add_argument("--name", default="",
                    help="备份文件夹名（留空则用默认名 Backup；重名会自动加日期）")
    ap.add_argument("--preset", default="critical")
    ap.add_argument("--partition", action="append", default=[])
    ap.add_argument("--no-gpt", action="store_true")
    ap.add_argument("--env", action="store_true")
    ap.add_argument("--no-verify-device", dest="verify_device",
                    action="store_false", default=True,
                    help="关掉设备端二次校验（默认开启）")
    ap.add_argument("--quiet", action="store_true", help="不显示实时进度条")
    # ==== APB_ARCHIVE BEGIN ====
    ap.add_argument("--archive", action="store_true",
                    help="备份完成后把整个备份目录打包成一个 zip（默认关）")
    ap.add_argument("--archive-level", type=int, default=6, metavar="N",
                    help="zip 压缩等级 1~9（默认 6）")
    # ==== APB_ARCHIVE END ====
    ap.add_argument("--list", action="store_true", help="列出设备/分区/分级后退出")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    devs = Adb.list_devices(args.adb)
    if not devs:
        print("未检测到设备")
        return 1
    serial, state = (args.serial, "device") if args.serial else devs[0]
    print(f"adb      : {Adb.adb_version(args.adb)}")
    print(f"device   : {serial}  状态={state}")
    if state != "device":
        print("设备状态异常，退出")
        return 1

    adb = Adb(args.adb, serial)
    info = adb.probe()
    adb.attach_caps(info.caps)

    print(f"设备     : {info.display}  ({info.codename})")
    print(f"系统     : Android {info.android} / SDK {info.sdk} / {info.version}")
    print(f"槽位     : {info.slot or '无（A-only）'}")
    print(f"root     : {'可用 ' + info.root_context if info.root_ok else '不可用'}"
          f"  ({info.caps.root_backend_name or '未知后端'})")
    print(f"by-name  : {info.caps.byname_dir}")
    print(f"工具     : dd={info.caps.dd} blockdev={info.caps.blockdev} "
          f"sgdisk={info.caps.sgdisk or '缺失'} tar={info.caps.tar or '缺失'}")
    if not info.root_ok:
        print("root 不可用，退出")
        return 1

    parts = adb.list_partitions()
    topo = adb.detect_topology()
    platform, score, scores = detect_platform([p.name for p in parts], info.caps.raw.get("SOC", ""))
    info.platform, info.platform_score = platform, score
    adb.classify_all(parts, platform)

    print(f"存储     : {topo.display}  整盘={[d.name for d in topo.disks]}")
    print(f"平台     : {platform.display}（得分 {score}，明细 {scores}）")
    print(f"分区     : 共 {len(parts)} 个")
    print()

    if args.list:
        print(f"{'分区名':<26}{'大小':>11}  {'级别':<12} 说明")
        print("-" * 100)
        for p in parts:
            print(f"{p.name:<26}{human_size(p.size):>11}  {p.label:<12} {p.reason}")
        return 0

    from partition_profiles import PRESETS, PRESET_EXCLUDE
    preset = next((x for x in PRESETS if x.key == args.preset), PRESETS[0])
    want = ({p.name for p in parts
             if p.tier in preset.tiers and p.name not in PRESET_EXCLUDE}
            if preset.tiers else set())
    if preset.include_root_baseline:
        want |= {p.name for p in parts
                 if p.tier == 2 and p.name not in PRESET_EXCLUDE}
    want |= set(args.partition)

    chosen = [p for p in parts if p.name in want]
    if not chosen:
        print("没有选中任何分区")
        return 1

    stamp = datetime.now().strftime("%Y%m%d")

    # 与 GUI 完全一致的命名与冲突消解逻辑
    resolved = resolve_backup_dir(args.out, args.name or DEFAULT_BACKUP_NAME,
                                  info.codename, info.serial)
    outdir = resolved.path
    print(f"备份目录 : {outdir}")
    print(f"命名说明 : {resolved.reason}")
    try:
        os.makedirs(outdir, exist_ok=False)      # ⚠️ 绝不覆盖
    except FileExistsError:
        print(f"错误：目录已存在 {outdir}")
        return 1
    write_device_marker(outdir, {
        "codename": info.codename, "serial": info.serial, "model": info.model,
        "brand": info.brand, "android": info.android, "version": info.version,
    })

    log_lines: list[str] = []

    # ---- 进度显示 ----
    # 终端里画进度条，非 TTY（重定向到文件）时自动退化为每 10% 一行
    bar = ConsoleProgress(enabled=not args.quiet)
    meter = SpeedMeter()
    t_start = time.time()
    total_bytes = sum(p.size for p in chosen)
    state = {"done_bytes": 0, "cur_size": 0}

    def log(s):
        bar.finish()               # 日志和进度条不能抢同一行
        print("  " + s)
        log_lines.append(s)

    def prog(d):
        phase = d.get("phase")
        if phase == "start":
            state["cur_size"] = d.get("expect", 0)
        elif phase == "item":
            cur = d.get("done", 0)
            exp = d.get("expect", 0)
            spd = meter.sample(cur, time.time())
            bar.update(d.get("item", ""), cur, exp, spd, meter.eta(cur, exp))
        elif phase == "item_done":
            bar.finish()
            state["done_bytes"] += state["cur_size"]
            overall = state["done_bytes"]
            if total_bytes > 0:
                el = time.time() - t_start
                ospd = overall / el if el > 0 else 0.0
                oeta = (total_bytes - overall) / ospd if ospd > 0 else None
                pct = overall * 100.0 / total_bytes
                print(f"      ── 总进度 {pct:5.1f}%   "
                      f"{human_size(overall)} / {human_size(total_bytes)}   "
                      f"平均 {ospd / 1048576:.1f} MB/s   剩 {human_duration(oeta)}")
        else:
            # hash / gpt / env / finished —— 这些阶段没有字节级进度，收掉进度行即可
            bar.finish()

        # ==== APB_ARCHIVE BEGIN ====
        if phase == "archive":
            # 打包阶段有真实的字节进度，可以画条真进度条（不是忙碌指示）
            cur = d.get("done", 0)
            exp = d.get("expect", 0)
            spd = meter.sample(cur, time.time())
            bar.update(d.get("item", "打包"), cur, exp, spd, meter.eta(cur, exp))
        # ==== APB_ARCHIVE END ====
    eng = BackupEngine(
        adb, outdir,
        EngineOptions(do_gpt=not args.no_gpt, do_env=args.env,
                      verify_device_side=args.verify_device),
        info, log_cb=log, progress_cb=prog,
    )
    # ==== APB_ARCHIVE BEGIN ====
    # 打包开关在构造之后补写 —— 这样上面那段 EngineOptions 的构造一行都不用动。
    eng.opt.archive_enabled = args.archive
    eng.opt.archive_format = "zip"
    eng.opt.archive_level = args.archive_level
    # ==== APB_ARCHIVE END ====
    results = eng.run(chosen)
    eng.cleanup_device()
    bar.finish()

    meta = {
        "display": info.display, "serial": info.serial, "codename": info.codename,
        "version": info.version, "android": info.android, "sdk": info.sdk,
        "slot": info.slot, "stamp": stamp, "platform": platform.display,
        "platform_score": score, "storage": topo.display,
        "root": info.caps.root_backend_name or "已 root",
        "first_disk": topo.disks[0].name if topo.disks else "sda",
    }
    write_manifest(outdir, results, meta)
    prev = find_previous_backup(args.out, outdir, tag=info.tag)
    write_readme(outdir, results, meta, prev, chosen)
    with open(os.path.join(outdir, "backup_log.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(log_lines))

    # ==== APB_ARCHIVE BEGIN ====
    # 上面三份报告是 run() 之后才写的，补进压缩包，免得解压出来少了
    # 校验清单与恢复说明（README.md 里正是恢复要点）。补不进去不算失败。
    if eng.archive.enabled and eng.archive.ok:
        eng.archive_add_paths([
            os.path.join(outdir, "manifest.txt"),
            os.path.join(outdir, "README.md"),
            os.path.join(outdir, "backup_log.txt"),
        ])
    # ==== APB_ARCHIVE END ====
    ok = sum(1 for r in results if r.ok)
    print(f"\n完成：{ok}/{len(results)} 通过   →  {outdir}")
    # ==== APB_ARCHIVE BEGIN ====
    # ⚠️ 打包失败**不进**失败计数，只在这里如实报一行 —— 备份数据本身是好的。
    if eng.archive.enabled:
        if eng.archive.ok:
            print(f"打包：{eng.archive.display}"
                  f"   耗时 {eng.archive.seconds:.1f}s")
        else:
            print(f"打包：未完成 —— {eng.archive.message}"
                  f"{('（' + eng.archive.error + '）') if eng.archive.error else ''}")
            print(f"      备份数据完好，压缩包只是额外的一份，不影响恢复。")
    # ==== APB_ARCHIVE END ====
    return 0 if ok == len(results) else 2


if __name__ == "__main__":
    sys.exit(_cli())
