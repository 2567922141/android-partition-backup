# -*- coding: utf-8 -*-
"""
安卓分区备份工具 —— 图形界面
================================================================================
依赖：Python 3.8+ 标准库（tkinter）+ 同目录的 backup_core.py / partition_profiles.py
不依赖任何第三方包。

【线程模型 —— Tkinter 铁律】
    主线程：只跑 Tk 事件循环与界面更新
    工作线程：所有 adb 调用、文件 IO、哈希计算
    两者之间只通过 queue.Queue 通信，主线程用 root.after() 轮询队列。
    绝不在工作线程里碰任何 tkinter 控件 —— 否则会随机崩溃。
================================================================================
"""

from __future__ import annotations

import os
import queue
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
from datetime import datetime
from tkinter import filedialog, messagebox, ttk
from typing import Optional

# 允许脚本以任意工作目录启动
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backup_core import (          # noqa: E402
    Adb, AdbError, BackupEngine, BackupError, Cancelled, DeviceInfo,
    EngineOptions, PartitionInfo, human_size, human_duration, SpeedMeter,
    find_previous_backup, write_manifest, write_readme, CORE_VERSION,
    default_backup_root, resolve_backup_dir, sanitize_folder_name,
    write_device_marker, read_device_marker, DEFAULT_BACKUP_NAME,
    kill_live_children,
    adb_server_running, adb_start_server, adb_stop_server,
    describe_soc, parse_sysinfo,
)
from partition_profiles import (   # noqa: E402
    PRESETS, PRESET_EXCLUDE, PLATFORM_GENERIC, PROFILES_VERSION,
    detect_platform, classify,
)

APP_TITLE = "安卓分区备份工具"
APP_VERSION = "1.1.1"

# 顶部设备信息栏显示的字段 —— 顺序即显示顺序。
# 用 FlowFrame 铺，窗口窄了会自动折行，不会丢字段。
#
# ⚠️ 实测坑：ro.build.display.id 在小米上是 **AOSP 构建号**（BP2A.250605.031.A3），
#    不是用户看到的系统版本号。真正的版本号在 ro.build.version.incremental
#    （= OS3.0.307.0.WNKCNXM），也就是 DeviceInfo.version。
DEVICE_INFO_FIELDS = (
    "系统", "系统版本", "芯片", "平台", "内核", "架构",
    "内存", "屏幕", "槽位", "补丁", "Root",
)

CHECK_ON = "☑"
CHECK_OFF = "☐"

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


# ==============================================================================
#  环境相关小工具
# ==============================================================================

def find_adb() -> str:
    """
    按优先级查找 adb.exe：
      1) 脚本同目录 / 上一级的 adb/adb.exe
      2) 脚本同目录直接放的 adb.exe
      3) 系统 PATH
      4) 常见安装位置
    """
    here = os.path.dirname(os.path.abspath(__file__))
    cands = []
    for base in (here, os.path.dirname(here), os.path.dirname(os.path.dirname(here))):
        cands.append(os.path.join(base, "adb", "adb.exe"))
        cands.append(os.path.join(base, "adb.exe"))
        cands.append(os.path.join(base, "platform-tools", "adb.exe"))
    for c in cands:
        if os.path.isfile(c):
            return c
    from shutil import which
    w = which("adb")
    if w:
        return w
    for c in (r"C:\platform-tools\adb.exe",
              r"C:\adb\adb.exe",
              os.path.expanduser(r"~\platform-tools\adb.exe")):
        if os.path.isfile(c):
            return c
    return ""


def pick_font() -> str:
    """挑一个系统里存在的中文字体，避免界面出现方块。"""
    try:
        import tkinter.font as tkfont
        families = set(tkfont.families())
    except Exception:
        return "TkDefaultFont"
    for f in ("Microsoft YaHei UI", "Microsoft YaHei", "微软雅黑",
              "PingFang SC", "Noto Sans CJK SC", "SimHei", "SimSun"):
        if f in families:
            return f
    return "TkDefaultFont"


def enable_dpi_awareness():
    """Windows 高分屏下让文字不发虚。"""
    if os.name != "nt":
        return
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


# ==============================================================================
#  响应式布局构件
# ==============================================================================

#: 期望窗口尺寸（屏幕够大时用这个）
DEF_W, DEF_H = 1060, 900
#: 允许缩到的最小尺寸。比这更小也不会丢控件 —— 滚动条会接管（见 ScrollHost）
MIN_W, MIN_H = 760, 520


class FlowFrame(ttk.Frame):
    """一排控件，窗口变窄时自动折行。

    【为什么不能用 pack(side="left")】
        pack 在容器宽度不足时，会把**排在后面的控件直接挤出可视区**，
        既不报错也不提示 —— 这就是「调整窗口大小后有些元素消失」的根因。

    【为什么也不能用 grid】
        grid 的列宽是**整个容器共享**的：折行后若某一行的宽控件占了第 0 列，
        这一列就会被撑宽，**其它行也跟着右移**。把列号按行号错开也没用 ——
        第 1 行仍然要排在第 0 行那些列之后（实测越界 142px）。
        所以这里用 place 自己算坐标，各行完全独立。

    用法::

        row = FlowFrame(parent)
        row.pack(fill="x")
        row.add(ttk.Button(row, text="全选", command=...))
        row.add(ttk.Button(row, text="反选", command=...), gap=14)

    注意：控件必须用本行的对象做 parent（`ttk.Button(row, ...)`）。
    """

    def __init__(self, master, gap=10, row_gap=6, **kw):
        super().__init__(master, **kw)
        self._gap = gap           # 同一行内相邻控件的间距
        self._row_gap = row_gap   # 折行后行与行之间的竖直间距
        self._items = []          # [(widget, gap_before)]
        self._last_w = -1
        self._last_h = -1
        self._pending = False     # 是否有排队中的重排
        self._boxes = {}          # widget -> (x, y, w, h)，用来跳过无变化的 place
        self.bind("<Configure>", self._on_configure)

    def add(self, widget, gap=None):
        """把控件登记进本行 —— 由本行统一负责它的位置。"""
        self._items.append((widget, self._gap if gap is None else gap))
        self._last_w = -1
        self._schedule()
        return widget

    def add_all(self, widgets, gap=None):
        for w in widgets:
            self.add(w, gap)
        return widgets

    def refresh(self):
        """子控件文本变了（宽度随之变化）之后重新测量排布。

        ⚠️ 必须先 update_idletasks：改完 -text 之后 Tk 要等到下一次几何计算
        才会更新 winfo_reqwidth，直接量会量到**旧值** —— 表现就是值被截成两
        三个字符（量到的是原来那个 "—" 的宽度）。
        """
        self._last_w = -1
        self._boxes.clear()
        self.update_idletasks()
        self._settle()

    def _on_configure(self, event):
        # 宽度变了才重排；高度变化不处理（否则会自己触发自己）
        if abs(event.width - self._last_w) >= 2:
            self._last_w = event.width
            self._schedule()

    # ------------------------------------------------------------ 性能
    #
    # 【性能】拖拽时 <Configure> 每帧会触发多次，若每次都立刻重排，同一步
    # 拖拽里会把一样的位置算好几遍。这里合并到下一个 idle 只做一次。
    def _schedule(self):
        if not self._pending:
            self._pending = True
            self.after_idle(self._settle)

    def _settle(self):
        self._pending = False
        self._reflow(self._last_w if self._last_w > 1 else None)

    def _reflow(self, width=None):
        if not self._items:
            return
        if width is None or width <= 1:
            width = self.winfo_width()
        if width <= 1:
            # 还没映射 —— 先摆成一行，等真正的 <Configure> 再折
            width = (sum(w.winfo_reqwidth() for w, _ in self._items)
                     + self._gap * max(0, len(self._items) - 1))

        x = y = 0
        row_h = 0
        for w, gap in self._items:
            need = w.winfo_reqwidth()
            h = max(w.winfo_reqheight(), 1)
            g = 0 if x == 0 else gap
            if x > 0 and x + g + need > width:
                y += row_h + self._row_gap     # 放不下 → 换行
                x = 0
                g = 0
                row_h = 0
            box = (x + g, y, need, h)
            # 位置没变就别再 place 一次 —— 拖拽时绝大多数控件是原地不动的
            if self._boxes.get(w) != box:
                self._boxes[w] = box
                w.place(x=box[0], y=box[1], width=need, height=h)
            x += g + need
            row_h = max(row_h, h)
        # place 不会把尺寸传给父容器，高度得自己报
        total = y + row_h
        if total != self._last_h:
            self._last_h = total
            self.configure(height=total)


class ScrollHost(ttk.Frame):
    """把整个界面装进一个可滚动画布 —— 「绝不丢控件」的兜底保险。

    两种情形都照顾到：

    * 窗口比内容**大** → 内容撑满画布，`expand=True` 的区块（分区表、日志）
      自动吃掉多余空间；
    * 窗口比内容**小** → 出现竖直滚动条，任何元素都够得着。
      哪怕在 1366x768 的老笔记本上、或者系统 DPI 放大到 200%，
      也不会有控件被永久裁掉。

    滚动条只在真正需要时才出现（内容比画布高）。
    """

    # 拖拽窗口时，最后一次尺寸变化过去多久才真正重排（毫秒）。
    # 调大 → 更跟手但内容归位更迟；调小 → 内容跟得紧但容易卡。
    # 120ms 是「手一停就归位」与「拖拽全程不重排」之间的平衡点。
    _IDLE_MS = 120

    def __init__(self, master, **kw):
        super().__init__(master, **kw)
        bg = "SystemButtonFace"
        try:
            bg = ttk.Style().lookup("TFrame", "background") or bg
        except Exception:
            pass

        self.canvas = tk.Canvas(self, highlightthickness=0, borderwidth=0,
                                background=bg)
        self.vsb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.vsb.set)
        self.canvas.pack(side="left", fill="both", expand=True)

        self.inner = ttk.Frame(self.canvas)
        self._win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self._sb_visible = False
        self._win_w = -1        # 上次设定的内容区宽度
        self._win_h = -1        # 上次设定的内容区高度
        self._scrollregion = None
        self._job = None        # 排队中的布局任务
        self._ever_settled = False

        self.inner.bind("<Configure>", self._on_inner)
        self.canvas.bind("<Configure>", self._on_canvas)

    # ---------------------------------------------------------------- 内部
    #
    # 【性能】拖拽窗口时 <Configure> 会以每帧多次的频率触发。若每次都立刻
    # 重算布局，一步拖拽会把同样的活干十几遍 —— 实测每步 130ms，明显卡顿。
    # 所以这里一律「只标脏、不干活」，把真正的工作合并到下一个 idle 做一次。
    def _on_inner(self, _event=None):
        """内容区尺寸变了 —— 先标脏，由 _settle 统一收敛。

        注意：不能只靠 canvas 的 <Configure>。折行发生在 canvas resize
        **之后**，那时 inner 的尺寸往往没变、不会再触发 <Configure>，
        高度就会永远停在按旧 reqheight 算出的值，末尾状态栏再也映射不出来。
        所以这里也必须标脏，多收敛一轮。
        """
        self._mark_dirty()

    def _on_canvas(self, _event=None):
        self._mark_dirty()

    def _mark_dirty(self):
        """排一次布局 —— 但不在拖拽当中排。

        【性能】实测：让内容跟随窗口宽度重排一次要 **约 140ms**。这不是本程序
        写得烂 —— 一个只放 80 个 ttk 控件的空白窗口，同样的重排也要 75ms。
        ttk 控件是 Tcl 实现的，每个控件每次重排约 1ms，就是 Tk 的地板价。
        而拖拽窗口每秒会产生几十次 <Configure>，逐次重排的话事件队列永远
        追不上，手感就是「拖不动」。

        所以改成**尾随去抖**：每来一次尺寸变化就把待办往后推 _IDLE_MS。
        连续拖拽期间一次都不执行（窗口边框照常跟手，由系统直接拉伸），
        手一停下来立刻排一次，内容随即归位。
        """
        if not self._ever_settled:
            # 开窗第一次布局要快，不等待
            if self._job is None:
                self._job = self.after_idle(self._settle)
            return
        if self._job is not None:
            try:
                self.after_cancel(self._job)
            except Exception:
                pass
        self._job = self.after(self._IDLE_MS, self._settle)

    def _settle(self, budget=3):
        """把布局推到不动点。

        每轮只做**真正有变化**的事，结果缓存在 _win_w / _win_h /
        _scrollregion 里 —— 没有变化的轮次立刻收敛退出，不会空转。
        """
        self._job = None
        self._ever_settled = True
        if self._layout_once() and budget > 0:
            self.after_idle(lambda: self._settle(budget - 1))

    def _layout_once(self) -> bool:
        ch = self.canvas.winfo_height()
        cw = self.canvas.winfo_width()
        if ch <= 1 or cw <= 1:
            return False                    # 还没映射，别下结论

        # 高度 = max(画布高度, 内容需求高度)：
        #   内容矮 → 撑满画布，让 expand 区块（分区表 / 日志）长大
        #   内容高 → 保持需求高度，交给滚动条
        want_w = cw
        want_h = max(ch, self.inner.winfo_reqheight())

        # ⚠️ 宽和高必须**一次** itemconfigure 给完。分两次的话，给宽度会先
        # 触发一轮内容子树的 <Configure> 级联，给高度再触发一轮 —— 实测
        # 重排耗时直接翻倍（140ms vs 75ms）。合并后仍然会收敛：这一轮用的是
        # 旧宽度算出的 reqheight，级联结束后会再排一轮修正。
        opts = {}
        if want_w != self._win_w:
            opts["width"] = want_w
        if want_h != self._win_h:
            opts["height"] = want_h
        changed = bool(opts)
        if changed:
            self._win_w, self._win_h = want_w, want_h
            self.canvas.itemconfigure(self._win, **opts)

        # scrollregion 直接用算好的尺寸，省掉 bbox("all") 那次 Tcl 往返
        region = (0, 0, want_w, want_h)
        if region != self._scrollregion:
            self._scrollregion = region
            self.canvas.configure(scrollregion=region)

        self._sync_scrollbar(ch)
        return changed

    def _sync_scrollbar(self, ch=None):
        """按需显隐竖滚动条。

        不会来回抖动：藏起来 → 画布变宽 → 内容只会更矮或不变；
        亮出来 → 画布变窄 → 内容只会更高或不变。两个方向都是单调的。
        """
        ch = ch if ch is not None else self.canvas.winfo_height()
        if ch <= 1:
            return
        need = self.inner.winfo_reqheight() > ch + 1
        if need == self._sb_visible:
            return
        self._sb_visible = need
        if need:
            self.vsb.pack(side="right", fill="y")
        else:
            self.vsb.pack_forget()

    def _on_wheel(self, event):
        if not self._sb_visible:
            return
        self.canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")

    def bind_wheel(self, widget=None):
        """给内容区挂滚轮事件。

        表格（Treeview）与日志（Text）自带滚动，跳过 —— 否则滚轮会同时
        滚它们和整个页面，手感很怪。
        """
        widget = widget if widget is not None else self.inner
        if isinstance(widget, (ttk.Treeview, tk.Text)):
            return
        widget.bind("<MouseWheel>", self._on_wheel, add="+")
        for child in widget.winfo_children():
            self.bind_wheel(child)


# ==============================================================================
#  主窗口
# ==============================================================================

class BackupApp:

    def __init__(self, root: tk.Tk):
        self.root = root
        self.adb_path = find_adb()
        self.adb: Optional[Adb] = None
        self.info: Optional[DeviceInfo] = None
        self.partitions: list[PartitionInfo] = []
        self.checked: set[str] = set()
        self.rows: dict[str, str] = {}           # 分区名 -> treeview iid
        self._item_of: dict[str, PartitionInfo] = {}

        # 线程通信
        self.msg_q: queue.Queue = queue.Queue()
        self.poll_stop = threading.Event()
        self.poll_paused = threading.Event()
        self.cancel_evt = threading.Event()
        self.worker: Optional[threading.Thread] = None
        self.poll_thread: Optional[threading.Thread] = None
        self._last_devs: list = []
        self._probing = False

        self.font = pick_font()
        self._setup_style()
        self._build_ui()
        self._start_poll_thread()
        self.root.after(80, self._pump)

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------ 样式
    def _setup_style(self):
        st = ttk.Style()
        try:
            st.theme_use("vista")
        except tk.TclError:
            try:
                st.theme_use("clam")
            except tk.TclError:
                pass
        base = (self.font, 10)
        st.configure(".", font=base)
        st.configure("Head.TLabel", font=(self.font, 15, "bold"))
        st.configure("Sub.TLabel", font=(self.font, 9), foreground="#666")
        st.configure("Ok.TLabel", foreground="#1a7f37", font=(self.font, 10, "bold"))
        st.configure("Warn.TLabel", foreground="#b54708", font=(self.font, 10, "bold"))
        st.configure("Err.TLabel", foreground="#b42318", font=(self.font, 10, "bold"))
        st.configure("Big.TButton", font=(self.font, 11, "bold"), padding=(16, 8))
        st.configure("Treeview", rowheight=24, font=(self.font, 10))
        st.configure("Treeview.Heading", font=(self.font, 10, "bold"))

    # ------------------------------------------------------------------ 布局
    def _build_ui(self):
        self.root.title(f"{APP_TITLE} v{APP_VERSION}")

        # 整个界面装进可滚动画布 —— 窗口再小也不会把控件裁掉
        self.host = ScrollHost(self.root)
        self.host.pack(fill="both", expand=True)

        outer = ttk.Frame(self.host.inner, padding=10)
        outer.pack(fill="both", expand=True)

        self._build_device_panel(outer)
        self._build_partition_panel(outer)
        self._build_option_panel(outer)
        self._build_action_panel(outer)
        self._build_progress_panel(outer)
        self._build_statusbar(outer)

        self.host.bind_wheel()
        # 等所有控件算出自然尺寸后再定窗口大小，否则量到的是半成品
        self.root.after_idle(self._fit_window)

    def _fit_window(self):
        """按「内容实际需求」和「屏幕可用区」决定初始窗口大小。

        旧版写死 `geometry("1060x820")` + `minsize(900, 700)`：
        一旦系统 DPI 放大、或字体与内容变多，实际需要的尺寸就会超过 820，
        而 pack 在空间不足时是**从末尾开始裁**的 —— 状态栏、进度条、日志
        依次消失，且不给任何提示。这里改成实测后按需取值。
        """
        self.root.update_idletasks()
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()

        need_w = self.host.inner.winfo_reqwidth() + 4
        need_h = self.host.inner.winfo_reqheight() + 4

        max_w = max(MIN_W, sw - 40)      # 给窗口边框留余量
        max_h = max(MIN_H, sh - 90)      # 给任务栏和标题栏留余量
        w = min(max(need_w, DEF_W), max_w)
        h = min(max(need_h, DEF_H), max_h)

        x = max(0, (sw - w) // 2)
        y = max(0, (sh - h) // 2 - 20)
        self.root.geometry(f"{w}x{h}+{x}+{y}")

        # 最小尺寸故意允许小于内容 —— 那种情况下滚动条接管，元素依然够得着，
        # 比「锁死一个很大的 minsize 导致小屏上窗口超出屏幕」要好得多。
        self.root.minsize(min(MIN_W, max_w), min(MIN_H, max_h))

    # ---------------------------------------------------------- ① 设备状态
    def _build_device_panel(self, parent):
        f = ttk.LabelFrame(parent, text=" ① 设备状态 ", padding=10)
        f.pack(fill="x", pady=(0, 8))

        row = ttk.Frame(f)
        row.pack(fill="x")
        self.lbl_dot = ttk.Label(row, text="●", font=(self.font, 16), foreground="#999")
        self.lbl_dot.pack(side="left", padx=(0, 8))

        col = ttk.Frame(row)
        col.pack(side="left", fill="x", expand=True)
        self.lbl_dev = ttk.Label(col, text="正在检测设备 ...", font=(self.font, 12, "bold"))
        self.lbl_dev.pack(anchor="w")
        self.lbl_devsub = ttk.Label(col, text="", style="Sub.TLabel")
        self.lbl_devsub.pack(anchor="w")

        btns = ttk.Frame(row)
        btns.pack(side="right")
        self.btn_refresh = ttk.Button(btns, text="刷新设备", command=self._force_refresh)
        self.btn_refresh.pack(side="left", padx=3)
        self.btn_root = ttk.Button(btns, text="检查 Root", command=self._probe_now)
        self.btn_root.pack(side="left", padx=3)

        # ---- 详细信息：一排排 `名称 值`，宽度不够自动折行 ----
        # 用 FlowFrame 而不是 grid：grid 的列宽是整个容器共享的，某个值特别长
        # 会把整列撑宽、带着其它行一起右移（见 README 里那段说明）。
        self.info_grid = FlowFrame(f, gap=20, row_gap=4)
        self.info_grid.pack(fill="x", pady=(9, 0))
        self._info_labels = {}
        for key in DEVICE_INFO_FIELDS:
            cell = ttk.Frame(self.info_grid)
            ttk.Label(cell, text=key, style="Sub.TLabel").pack(side="left")
            val = ttk.Label(cell, text="—", anchor="w")
            val.pack(side="left", padx=(6, 0))
            self.info_grid.add(cell)
            self._info_labels[key] = val

        # ---- ADB 服务开关（放在顶部，方便随手启停）----
        # 服务端是**多个工具共用**的常驻进程（Android Studio / scrcpy 也在用
        # 同一个）。所以这里给出显式开关；退出时则**无条件**停掉它，不留残留。
        srv = FlowFrame(f, gap=12, row_gap=6)
        srv.pack(fill="x", pady=(9, 0))
        self.srv_row = srv
        self.lbl_srv = ttk.Label(srv, text="ADB 服务: 检测中 ...")
        self.btn_srv = ttk.Button(srv, text="启动", width=8, command=self._toggle_server)
        srv.add(self.lbl_srv)
        srv.add(self.btn_srv, gap=8)
        srv.add(ttk.Label(srv, text="退出本程序时会自动停止 ADB 服务",
                          style="Sub.TLabel"), gap=16)
        self._srv_running = None
        self._srv_busy = False

        self.root.after(400, self._refresh_server_state)
        self.root.after(2500, self._tick_server_state)

    def _set_device_details(self, info: Optional[DeviceInfo]):
        """把设备详情填进顶部那一排 `名称 值`。取不到的显示 "—"。"""
        if not hasattr(self, "_info_labels"):
            return

        def g(v):
            return (str(v).strip() if v else "") or "—"

        android = g(info.android if info else "")
        sdk = (info.sdk if info else "") or ""
        if android != "—" and sdk:
            android = f"Android {android}（SDK {sdk}）"
        elif android != "—":
            android = f"Android {android}"

        soc = describe_soc(info) if info else ""
        root = ""
        if info and info.root_ok:
            root = "✓ uid=0" + (f"（{info.root_context}）" if info.root_context else "")
        elif info:
            root = "✗ 不可用"

        slot = (info.slot if info else "") or ""
        if slot:
            slot = f"{slot}（当前系统槽）"

        vals = {
            "系统": android,
            "系统版本": g(info.version if info else ""),
            "芯片": g(soc),
            "平台": g(info.board if info else ""),
            "内核": g(info.kernel if info else ""),
            "架构": g(info.abi if info else ""),
            "内存": g(info.ram if info else ""),
            "屏幕": g(info.screen if info else ""),
            "槽位": slot or "—",
            "补丁": g(info.security_patch if info else ""),
            "Root": root or "—",
        }
        for key, lbl in self._info_labels.items():
            lbl.configure(text=vals.get(key, "—"))
        self.info_grid.refresh()        # 文字宽度变了，重新折行

    # ---------------------------------------------------------- ② 分区选择
    def _build_partition_panel(self, parent):
        f = ttk.LabelFrame(parent, text=" ② 选择要备份的分区 ", padding=10)
        f.pack(fill="both", expand=True, pady=(0, 8))

        top = FlowFrame(f, gap=8)
        top.pack(fill="x", pady=(0, 6))
        top.add(ttk.Label(top, text="预设方案:"))
        self.preset_var = tk.StringVar(value="critical+root")
        for p in PRESETS:
            top.add(ttk.Radiobutton(top, text=p.display, value=p.key,
                                    variable=self.preset_var,
                                    command=self._apply_preset))

        top2 = FlowFrame(f, gap=6, row_gap=6)
        top2.pack(fill="x", pady=(0, 6))
        top2.add(ttk.Button(top2, text="全选", width=6,
                            command=lambda: self._bulk("all")))
        top2.add(ttk.Button(top2, text="全不选", width=7,
                            command=lambda: self._bulk("none")))
        top2.add(ttk.Button(top2, text="反选", width=6,
                            command=lambda: self._bulk("invert")))
        top2.add(ttk.Button(top2, text="仅不可再生", width=11,
                            command=lambda: self._bulk("critical")), gap=14)

        top2.add(ttk.Label(top2, text="过滤:"), gap=14)
        self.filter_var = tk.StringVar()
        self.filter_var.trace_add("write", lambda *_: self._render_rows())
        top2.add(ttk.Entry(top2, textvariable=self.filter_var, width=18))

        self.hide_low = tk.BooleanVar(value=True)
        top2.add(ttk.Checkbutton(top2, text="隐藏低价值分区", variable=self.hide_low,
                                 command=self._render_rows), gap=14)
        top2.add(ttk.Label(top2, text="（勾选/取消：点击左侧方框，或选中行按空格）",
                           style="Sub.TLabel"), gap=14)

        # ---- 表格 ----
        wrap = ttk.Frame(f)
        wrap.pack(fill="both", expand=True)
        wrap.rowconfigure(0, weight=1)
        wrap.columnconfigure(0, weight=1)

        cols = ("chk", "name", "size", "tier", "note")
        self.tree = ttk.Treeview(wrap, columns=cols, show="headings",
                                 selectmode="extended", height=6)
        heads = [("chk", "选", 44, "center"), ("name", "分区名", 210, "w"),
                 ("size", "大小", 100, "e"), ("tier", "级别", 100, "center"),
                 ("note", "说明", 520, "w")]
        for key, text, width, anchor in heads:
            self.tree.heading(key, text=text,
                              command=lambda k=key: self._sort_by(k))
            self.tree.column(key, width=width, anchor=anchor, minwidth=60,
                             stretch=(key == "note"))

        # 竖直 + 水平双滚动条：窗口很窄时横向可以拖，列不会被切掉
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        hsb = ttk.Scrollbar(wrap, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")

        self.tree.bind("<Button-1>", self._on_tree_click)
        self.tree.bind("<space>", self._on_space)
        self.tree.tag_configure("t1", background="#fff4e5")
        self.tree.tag_configure("t2", background="#eef6ff")
        self.tree.tag_configure("t4", foreground="#999999")

        self.lbl_sum = ttk.Label(f, text="已选 0 项，合计 0 B", font=(self.font, 10, "bold"))
        self.lbl_sum.pack(anchor="w", pady=(6, 0))

    # ---------------------------------------------------------- ③ 选项
    def _build_option_panel(self, parent):
        f = ttk.LabelFrame(parent, text=" ③ 输出位置与选项 ", padding=10)
        f.pack(fill="x", pady=(0, 8))

        # ---- 备份根目录 ----
        r1 = ttk.Frame(f)
        r1.pack(fill="x", pady=(0, 6))
        ttk.Label(r1, text="备份根目录:", width=12).pack(side="left")
        self.out_var = tk.StringVar(value=self._default_outdir())
        self.out_var.trace_add("write", lambda *_: self._preview_path())
        ttk.Entry(r1, textvariable=self.out_var).pack(side="left", fill="x",
                                                      expand=True, padx=6)
        ttk.Button(r1, text="浏览...", command=self._choose_out).pack(side="left")
        ttk.Button(r1, text="恢复默认", width=9,
                   command=self._reset_outdir).pack(side="left", padx=(6, 0))

        # ---- 自定义备份名称 ----
        r2 = ttk.Frame(f)
        r2.pack(fill="x", pady=(0, 6))
        ttk.Label(r2, text="备份名称:", width=12).pack(side="left")
        self.name_var = tk.StringVar(value="")
        self.name_var.trace_add("write", lambda *_: self._preview_path())
        ttk.Entry(r2, textvariable=self.name_var).pack(side="left", fill="x",
                                                       expand=True, padx=6)
        ttk.Label(r2, text="留空则用默认名 Backup", style="Sub.TLabel").pack(side="left")

        # ---- 最终路径实时预览 ----
        r3 = ttk.Frame(f)
        r3.pack(fill="x", pady=(0, 8))
        ttk.Label(r3, text="最终路径:", width=12).pack(side="left")
        self.lbl_preview = ttk.Label(r3, text="", style="Sub.TLabel", anchor="w")
        self.lbl_preview.pack(side="left", fill="x", expand=True)

        # ---- 开关 ----
        r4 = FlowFrame(f, gap=14, row_gap=6)
        r4.pack(fill="x")
        self.opt_gpt = tk.BooleanVar(value=True)
        self.opt_env = tk.BooleanVar(value=False)
        self.opt_devverify = tk.BooleanVar(value=False)
        self.opt_fallback = tk.BooleanVar(value=True)

        r4.add(ttk.Checkbutton(r4, text="备份 GPT 分区表", variable=self.opt_gpt))
        r4.add(ttk.Checkbutton(r4, text="打包 /data/adb 环境", variable=self.opt_env))
        r4.add(ttk.Checkbutton(r4, text="设备端二次校验（慢，最严格）",
                               variable=self.opt_devverify))
        r4.add(ttk.Checkbutton(r4, text="失败自动回退", variable=self.opt_fallback))

        self.root.after(300, self._preview_path)

    # ---------------------------------------------------------- ADB 服务
    def _tick_server_state(self):
        """每 2.5 秒看一眼 adb 服务端状态（别的程序也可能把它启停）。"""
        if not self.poll_stop.is_set():
            self._refresh_server_state()
        self.root.after(2500, self._tick_server_state)

    def _refresh_server_state(self):
        """刷新「ADB 服务」那一行的显示。"""
        if not hasattr(self, "lbl_srv") or self._srv_busy:
            return
        running = adb_server_running()
        # 服务端被停掉时轮询也一定处于暂停 —— 把它体现在文字里，
        # 否则用户会以为「设备检测坏了」。
        paused = self.poll_paused.is_set() and not running
        state = (running, paused)
        if state == self._srv_running:
            return                      # 没变就别白刷（省一次 refresh）
        self._srv_running = state
        if running:
            self.lbl_srv.configure(text="ADB 服务: ● 运行中", foreground="#1a7f37")
            self.btn_srv.configure(text="停止", state="normal")
        else:
            self.lbl_srv.configure(
                text="ADB 服务: ○ 已停止" + ("（设备检测已暂停）" if paused else ""),
                foreground="#b42318")
            self.btn_srv.configure(text="启动", state="normal")
        self.srv_row.refresh()          # 文字变宽了要重新排

    def _toggle_server(self):
        """启停 adb 服务端 —— 要跑 adb，所以放工作线程。"""
        if self._srv_busy:
            return
        want_stop = bool(self._srv_running and self._srv_running[0])
        if want_stop and self.worker and self.worker.is_alive():
            messagebox.showwarning(APP_TITLE, "备份正在进行，不能停止 ADB 服务。")
            return
        self._srv_busy = True
        self.btn_srv.configure(state="disabled",
                               text="停止中" if want_stop else "启动中")
        self.srv_row.refresh()

        def work():
            try:
                if want_stop:
                    # ⚠️ 必须先暂停设备轮询。否则轮询线程下一轮的 `adb devices`
                    # 会立刻把服务端又拉起来 —— 用户看到的就是「点了停止没反应」。
                    self.poll_paused.set()
                    ok, msg = adb_stop_server(self.adb_path)
                    verb = "停止" if ok else "停止失败"
                    if not ok:
                        self.poll_paused.clear()     # 没停成，把轮询放回去
                else:
                    ok, msg = adb_start_server(self.adb_path)
                    verb = "启动" if ok else "启动失败"
                    if ok:
                        self.poll_paused.clear()
                self.msg_q.put(("log", f"[ADB 服务] {verb}：{msg}"))
            except Exception as e:
                self.msg_q.put(("log", f"[ADB 服务] 操作异常：{e}"))
            finally:
                self._srv_busy = False
                self._srv_running = None        # 强制下一轮重画
                self.msg_q.put(("srv_changed", None))

        threading.Thread(target=work, name="adb-server", daemon=True).start()

    # ---------------------------------------------------------- ④ 操作
    def _build_action_panel(self, parent):
        f = FlowFrame(parent, gap=8, row_gap=8)
        self.action_row = f
        f.pack(fill="x", pady=(0, 8))
        self.btn_start = ttk.Button(f, text="▶  开始备份", style="Big.TButton",
                                    command=self._start_backup, state="disabled")
        f.add(self.btn_start)
        self.btn_cancel = ttk.Button(f, text="✕  取消", style="Big.TButton",
                                     command=self._cancel_backup, state="disabled")
        f.add(self.btn_cancel)
        self.btn_open = ttk.Button(f, text="打开输出目录", command=self._open_outdir,
                                   state="disabled")
        f.add(self.btn_open, gap=10)

        self.lbl_result = ttk.Label(f, text="", font=(self.font, 11, "bold"))
        f.add(self.lbl_result, gap=14)

    # ---------------------------------------------------------- ⑤ 进度
    def _build_progress_panel(self, parent):
        f = ttk.LabelFrame(parent, text=" ④ 进度与日志 ", padding=10)
        f.pack(fill="both", expand=True)

        # ---- 当前分区 ----
        # 标签宽度刻意收窄：两个 34 字符宽的标签在窄窗口里会把进度条挤没
        r1 = ttk.Frame(f)
        r1.pack(fill="x")
        self.lbl_cur = ttk.Label(r1, text="就绪", width=16, anchor="w")
        self.lbl_cur.pack(side="left")
        self.pb_item = ttk.Progressbar(r1, mode="determinate", maximum=100)
        self.pb_item.pack(side="left", fill="x", expand=True, padx=8)
        self.lbl_item_pct = ttk.Label(r1, text="", width=36, anchor="e",
                                      font=("Consolas", 9))
        self.lbl_item_pct.pack(side="left")

        # ---- 总计 ----
        r2 = ttk.Frame(f)
        r2.pack(fill="x", pady=(4, 8))
        self.lbl_all_name = ttk.Label(r2, text="总计", width=16, anchor="w")
        self.lbl_all_name.pack(side="left")
        self.pb_all = ttk.Progressbar(r2, mode="determinate", maximum=100)
        self.pb_all.pack(side="left", fill="x", expand=True, padx=8)
        self.lbl_all_pct = ttk.Label(r2, text="", width=36, anchor="e",
                                     font=("Consolas", 9))
        self.lbl_all_pct.pack(side="left")

        # ---- 日志 ----
        wrap = ttk.Frame(f)
        wrap.pack(fill="both", expand=True)
        wrap.rowconfigure(0, weight=1)
        wrap.columnconfigure(0, weight=1)
        # height 只作为**自然高度下限**；窗口变大时靠 expand 长高，
        # 窗口变小时由 ScrollHost 兜底。故意取小 —— 自然高度越小，
        # 小屏幕上「一打开就全都看得见」的概率越高。
        self.log = tk.Text(wrap, height=5, wrap="none", font=("Consolas", 9),
                           background="#1e1e1e", foreground="#d4d4d4",
                           insertbackground="#d4d4d4", padx=6, pady=4)
        lsb = ttk.Scrollbar(wrap, orient="vertical", command=self.log.yview)
        hsb = ttk.Scrollbar(wrap, orient="horizontal", command=self.log.xview)
        self.log.configure(yscrollcommand=lsb.set, xscrollcommand=hsb.set,
                           state="disabled")
        self.log.grid(row=0, column=0, sticky="nsew")
        lsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")

        # 日志分级着色 —— 一眼能从瀑布里挑出失败项
        self.log.tag_configure("head", foreground="#c586c0",
                               font=("Consolas", 9, "bold"))
        self.log.tag_configure("ok",   foreground="#4ec9b0")
        self.log.tag_configure("err",  foreground="#f48771",
                               font=("Consolas", 9, "bold"))
        self.log.tag_configure("warn", foreground="#dcdcaa")
        self.log.tag_configure("info", foreground="#569cd6")
        self.log.tag_configure("dim",  foreground="#808080")

    def _build_statusbar(self, parent):
        bar = ttk.Frame(parent)
        bar.pack(fill="x", pady=(6, 0))
        self.lbl_status = ttk.Label(bar, text="启动中 ...", style="Sub.TLabel")
        self.lbl_status.pack(side="left")
        adb_txt = self.adb_path or "未找到 adb.exe"
        ttk.Label(bar, text=f"adb: {adb_txt}", style="Sub.TLabel").pack(side="right")

    # ======================================================================
    #  工具方法
    # ======================================================================

    def _default_outdir(self) -> str:
        """
        默认备份根目录 = 程序目录下的 Backups。

        便携版布局是 <包根>/app/backup_gui.py，此时备份应落在 <包根>/Backups
        而不是 <包根>/app/Backups —— 所以见到名为 app 的父目录就往上提一层。

        程序目录不可写时（例如装在 Program Files）自动退回家目录，
        避免用户第一次备份就失败。
        """
        here = os.path.dirname(os.path.abspath(__file__))
        if os.path.basename(here).lower() == "app":
            here = os.path.dirname(here)
        return default_backup_root(here)

    def _reset_outdir(self):
        self.out_var.set(self._default_outdir())
        self._preview_path()

    def _current_name(self) -> str:
        """取用户填写的名称；留空则用通用默认名（不含机型）。"""
        n = self.name_var.get().strip()
        return n if n else DEFAULT_BACKUP_NAME

    def _preview_path(self):
        """实时显示本次备份会落到哪个目录，以及为什么是这个名字。"""
        if not hasattr(self, "lbl_preview"):
            return
        root = self.out_var.get().strip()
        if not root:
            self.lbl_preview.configure(text="（请先选择备份根目录）")
            return
        code = self.info.codename if self.info else ""
        sn = self.info.serial if self.info else ""
        try:
            r = resolve_backup_dir(root, self._current_name(), code, sn)
        except Exception as e:
            self.lbl_preview.configure(text=f"（无法预览：{e}）")
            return
        arrow = "  ⟵  " + r.reason if r.collided else "  ⟵  该名称尚未使用"
        self.lbl_preview.configure(text=r.path + arrow)

    @staticmethod
    def _log_level(text: str) -> str:
        """
        从日志文本推断级别。

        刻意不改 BackupEngine 的 log_cb 签名 —— 核心层不该知道界面想怎么着色，
        保持 core / gui 解耦。代价只是这里多几行 if。

        ⚠️ 顺序很关键：**前缀标记优先于关键词启发**。
           像 "  [!] 流式失败，回退到设备端暂存模式 ..." 这种带"失败"字样的
           告警（回退后其实成功了），若先做关键词匹配就会被误染成红色错误。
        """
        t = text.lstrip()
        # ---- 第一优先：显式前缀标记（最明确的信号）----
        if t.startswith("====="):
            return "head"
        if t.startswith("[OK]"):
            return "ok"
        if t.startswith("[X]"):
            return "err"
        if t.startswith("[!]") or t.startswith("[i]"):
            return "warn"
        # ---- 第二优先：无标记时才退回关键词启发 ----
        if "失败" in t or "错误" in t:
            return "err"
        if t.startswith("  ") or t.startswith("已保存") or t.startswith("探测到"):
            return "dim"
        return ""

    def _log(self, text: str, tag: str = ""):
        ts = datetime.now().strftime("%H:%M:%S")
        if not tag:
            tag = self._log_level(text)
        line = f"[{ts}] {text}\n"
        self.log.configure(state="normal")
        if tag:
            self.log.insert("end", line, tag)
        else:
            self.log.insert("end", line)
        self.log.see("end")
        self.log.configure(state="disabled")

    def _status(self, text: str):
        self.lbl_status.configure(text=text)

    def _set_result(self, text: str, style: str = ""):
        """更新底部结果标签。

        文本宽度会变（"" → "✅ 全部通过 13/13"），而 FlowFrame 用的是绝对
        定位、不会自动重排，所以必须主动 refresh 一次。
        """
        if style:
            self.lbl_result.configure(text=text, style=style)
        else:
            self.lbl_result.configure(text=text)
        self.action_row.refresh()

    def _choose_out(self):
        d = filedialog.askdirectory(title="选择备份根目录",
                                    initialdir=self.out_var.get() or os.getcwd())
        if d:
            self.out_var.set(os.path.normpath(d))
            self._preview_path()

    def _open_outdir(self):
        """优先打开本次备份目录；还没备份过就打开备份根目录。"""
        d = getattr(self, "_outdir", "") or self.out_var.get()
        if not os.path.isdir(d):
            d = self.out_var.get()
        if not os.path.isdir(d):
            messagebox.showinfo(APP_TITLE, "目录还不存在，先做一次备份吧。")
            return
        try:
            if os.name == "nt":
                os.startfile(d)          # noqa: S606
            elif sys.platform == "darwin":
                subprocess.Popen(["open", d])
            else:
                subprocess.Popen(["xdg-open", d])
        except Exception as e:
            messagebox.showwarning(APP_TITLE, f"无法打开目录：{e}")

    # ======================================================================
    #  设备轮询线程
    # ======================================================================

    def _start_poll_thread(self):
        t = threading.Thread(target=self._poll_loop, name="device-poll", daemon=True)
        self.poll_thread = t
        t.start()

    def _poll_loop(self):
        while not self.poll_stop.is_set():
            try:
                if not self.poll_paused.is_set() and not self._probing:
                    devs = Adb.list_devices(self.adb_path) if self.adb_path else []
                    if devs != self._last_devs:
                        self._last_devs = devs
                        self.msg_q.put(("devices", devs))
                        if devs and devs[0][1] == "device":
                            self._probing = True
                            self.msg_q.put(("status", "正在读取设备信息 ..."))
                            try:
                                adb = Adb(self.adb_path, devs[0][0])
                                info = adb.probe()
                                adb.attach_caps(info.caps)
                                parts = []
                                platform, score = PLATFORM_GENERIC, 0
                                if info.root_ok:
                                    parts = adb.list_partitions()
                                    platform, score, _ = detect_platform(
                                        [p.name for p in parts])
                                    adb.classify_all(parts, platform)
                                self.msg_q.put(
                                    ("info", (adb, info, parts, platform, score)))
                            except Exception as e:
                                self.msg_q.put(("log", f"读取设备信息失败: {e}"))
                            finally:
                                self._probing = False
            except Exception as e:
                self.msg_q.put(("log", f"轮询异常: {e}"))
            time.sleep(1.5)

    def _force_refresh(self):
        self._last_devs = []
        self.lbl_dev.configure(text="正在检测设备 ...")
        self.lbl_dot.configure(foreground="#999")
        self.lbl_devsub.configure(text="")
        self._status("已请求刷新")

    def _probe_now(self):
        self._force_refresh()

    # ======================================================================
    #  消息泵（主线程）
    # ======================================================================

    def _pump(self):
        try:
            while True:
                kind, payload = self.msg_q.get_nowait()
                if kind == "devices":
                    self._on_devices(payload)
                elif kind == "info":
                    self._on_info(*payload)
                elif kind == "log":
                    self._log(str(payload))
                elif kind == "status":
                    self._status(str(payload))
                elif kind == "progress":
                    self._on_progress(payload)
                elif kind == "srv_changed":
                    self._refresh_server_state()
                elif kind == "done":
                    self._on_done(payload)
        except queue.Empty:
            pass
        except Exception as e:
            self._log(f"[界面异常] {e}")
        self.root.after(80, self._pump)

    def _on_devices(self, devs):
        if not devs:
            self.lbl_dot.configure(foreground="#b42318")
            self.lbl_dev.configure(text="未检测到设备")
            self.lbl_devsub.configure(
                text="请确认：USB 已连接 / 已开启 USB 调试 / 手机上已点“允许” / 屏幕已解锁")
            self.btn_start.configure(state="disabled")
            self.adb, self.info, self.partitions = None, None, []
            self._set_device_details(None)
            self._render_rows()
            return
        serial, state = devs[0]
        if state == "device":
            self.lbl_dot.configure(foreground="#1a7f37")
            self.lbl_dev.configure(text=f"已连接  {serial}")
            self.lbl_devsub.configure(text="正在读取设备信息 ...")
        elif state == "unauthorized":
            self.lbl_dot.configure(foreground="#b54708")
            self.lbl_dev.configure(text=f"未授权  {serial}")
            self.lbl_devsub.configure(text="请在手机屏幕上点击“允许 USB 调试”，然后重新插拔")
            self.btn_start.configure(state="disabled")
        elif state == "offline":
            self.lbl_dot.configure(foreground="#b54708")
            self.lbl_dev.configure(text=f"离线  {serial}")
            self.lbl_devsub.configure(text="连接异常，建议重新插拔 USB 线")
            self.btn_start.configure(state="disabled")
        else:
            self.lbl_dot.configure(foreground="#b54708")
            self.lbl_dev.configure(text=f"{state}  {serial}")
            self.btn_start.configure(state="disabled")

    def _on_info(self, adb, info: DeviceInfo, parts: list, platform, score: int):
        self.adb, self.info = adb, info

        info.platform, info.platform_score = platform, score
        adb.classify_all(parts, platform)

        self.partitions = parts
        self._set_device_details(info)

        # 概要行只说「识别结果」—— 具体型号/系统/芯片都在上面那排详情里了
        root_txt = ("✓ " + info.caps.root_backend_name) if info.root_ok else "✗"
        sub = (f"代号 {info.codename or '未知'} · 识别为 {platform.display}"
               f"（得分 {score}） · root {root_txt}")
        if not info.root_ok:
            self.lbl_dot.configure(foreground="#b42318")
            self.lbl_devsub.configure(text=sub + "  ← root 不可用，无法读取分区")
            self.btn_start.configure(state="disabled")
            self._log("root 不可用：请在 root 管理器里给 Shell/ADB 授权后点“刷新设备”")
        else:
            self.lbl_dev.configure(text=f"{info.display}   ({info.serial})")
            self.lbl_devsub.configure(text=sub)
            self._log(f"设备就绪：{info.display} / {info.codename} / {platform.display} "
                      f"(得分 {score})")
            self._log(f"能力：by-name={info.caps.byname_dir}  "
                      f"sgdisk={'有' if info.caps.has_sgdisk else '无（用内置解析器）'}  "
                      f"root={info.caps.root_backend_name or '未知'}")
            self._status(f"共发现 {len(parts)} 个分区")
        self._apply_preset()
        self._render_rows()

    # ======================================================================
    #  分区列表
    # ======================================================================

    def _apply_preset(self):
        key = self.preset_var.get()
        preset = next((p for p in PRESETS if p.key == key), None)
        if preset is None or key == "custom":
            return
        # PRESET_EXCLUDE 里的分区（userdata / super / metadata）永不自动勾选，
        # 避免"全选"顺手带上 226 GB 的 userdata
        self.checked = {p.name for p in self.partitions
                        if p.tier in preset.tiers and p.name not in PRESET_EXCLUDE}
        if preset.include_root_baseline:
            self.checked |= {p.name for p in self.partitions
                             if p.tier == 2 and p.name not in PRESET_EXCLUDE}
        self._render_rows()

    def _visible(self) -> list[PartitionInfo]:
        kw = self.filter_var.get().strip().lower()
        out = []
        for p in self.partitions:
            if self.hide_low.get() and p.tier == 4:
                continue
            if kw and kw not in p.name.lower() and kw not in (p.reason or "").lower():
                continue
            out.append(p)
        return out

    def _render_rows(self):
        self.tree.delete(*self.tree.get_children())
        self.rows.clear()
        self._item_of.clear()

        for p in self._visible():
            mark = CHECK_ON if p.name in self.checked else CHECK_OFF
            tag = f"t{p.tier}" if p.tier in (1, 2, 4) else ""
            iid = self.tree.insert("", "end", values=(
                mark, p.name, human_size(p.size), p.label, p.reason), tags=(tag,))
            self.rows[p.name] = iid
            self._item_of[p.name] = p
        self._update_sum()

    def _update_sum(self):
        chosen = [p for p in self.partitions if p.name in self.checked]
        total = sum(p.size for p in chosen)
        crit = sum(1 for p in chosen if p.tier == 1)
        self.lbl_sum.configure(
            text=f"已选 {len(chosen)} 项，合计 {human_size(total)}"
                 f"（其中不可再生 {crit} 个）")
        ready = bool(self.adb and self.info and self.info.root_ok
                     and chosen and self.worker is None)
        self.btn_start.configure(state="normal" if ready else "disabled")

    # ---------------------------------------------------------- 交互
    def _on_tree_click(self, event):
        if self.tree.identify("region", event.x, event.y) != "cell":
            return
        if self.tree.identify_column(event.x) != "#1":
            return
        iid = self.tree.identify_row(event.y)
        if iid:
            self._toggle_iid(iid)

    def _on_space(self, _event):
        for iid in self.tree.selection():
            self._toggle_iid(iid)
        return "break"

    def _toggle_iid(self, iid):
        vals = self.tree.item(iid, "values")
        if not vals:
            return
        name = vals[1]
        if name in self.checked:
            self.checked.discard(name)
            self.tree.item(iid, values=(CHECK_OFF,) + tuple(vals[1:]))
        else:
            self.checked.add(name)
            self.tree.item(iid, values=(CHECK_ON,) + tuple(vals[1:]))
        self._update_sum()
        self.preset_var.set("custom")

    def _bulk(self, mode):
        vis = self._visible()
        if mode == "all":
            self.checked |= {p.name for p in vis}
        elif mode == "none":
            self.checked -= {p.name for p in vis}
        elif mode == "invert":
            for p in vis:
                if p.name in self.checked:
                    self.checked.discard(p.name)
                else:
                    self.checked.add(p.name)
        elif mode == "critical":
            self.checked |= {p.name for p in vis if p.tier == 1}
        if mode != "critical":
            self.preset_var.set("custom")
        self._render_rows()

    def _sort_by(self, key):
        rev = getattr(self, "_sort_rev", {}).get(key, False)
        self._sort_rev = getattr(self, "_sort_rev", {})
        self._sort_rev[key] = not rev
        if key == "name":
            self.partitions.sort(key=lambda p: p.name, reverse=rev)
        elif key == "size":
            self.partitions.sort(key=lambda p: p.size, reverse=not rev)
        elif key == "tier":
            self.partitions.sort(key=lambda p: (p.tier or 9), reverse=rev)
        self._render_rows()

    # ======================================================================
    #  备份
    # ======================================================================

    def _start_backup(self):
        if not (self.adb and self.info):
            messagebox.showwarning(APP_TITLE, "设备未就绪")
            return
        chosen = [p for p in self.partitions if p.name in self.checked]
        if not chosen:
            messagebox.showwarning(APP_TITLE, "请至少勾选一个分区")
            return

        total = sum(p.size for p in chosen)

        out_root = self.out_var.get().strip()
        if not out_root:
            messagebox.showwarning(APP_TITLE, "请先选择备份根目录")
            return
        try:
            os.makedirs(out_root, exist_ok=True)
        except OSError as e:
            messagebox.showerror(APP_TITLE, f"无法创建备份根目录：\n{out_root}\n\n{e}")
            return

        # 冲突消解：同名且同机型 → 追加日期；再冲突 → 追加时间/序号。
        # 这一步只做只读探测，不会创建任何东西。
        resolved = resolve_backup_dir(out_root, self._current_name(),
                                      self.info.codename, self.info.serial)
        outdir = resolved.path

        warn = ""
        if total > 20 * 1024 ** 3:
            warn = "\n\n⚠️ 所选内容超过 20 GB，请确认目标磁盘空间充足。"
        elif total > 5 * 1024 ** 3:
            warn = "\n\n⚠️ 所选内容较大，请确认磁盘空间。"

        note = (f"\n命名说明：{resolved.reason}\n" if resolved.collided
                else "\n命名说明：该名称尚未使用，直接创建。\n")
        if not messagebox.askokcancel(
                APP_TITLE,
                f"即将备份 {len(chosen)} 个分区，合计 {human_size(total)}。\n\n"
                f"备份目录：\n{outdir}\n"
                f"{note}\n"
                f"本工具只做只读导出，不写入设备任何分区。{warn}"):
            return

        try:
            os.makedirs(outdir, exist_ok=False)      # ⚠️ 绝不覆盖已有目录
        except FileExistsError:
            messagebox.showerror(APP_TITLE,
                                 f"目录已存在，请换个名称：\n{outdir}")
            return
        except OSError as e:
            messagebox.showerror(APP_TITLE, f"无法创建备份目录：\n{e}")
            return

        # 立刻写下设备指纹 —— 这样即使本次备份中途失败，
        # 下次同名备份也能正确识别出"这是同一台设备"并加日期。
        write_device_marker(outdir, {
            "codename": self.info.codename, "serial": self.info.serial,
            "model": self.info.model, "brand": self.info.brand,
            "android": self.info.android, "version": self.info.version,
        })

        self.cancel_evt.clear()
        self.poll_paused.set()
        self._outdir = outdir
        self._chosen = chosen
        self._log_lines = []

        # 进度追踪状态（主线程持有，工作线程只通过消息队列喂数据）
        self._t_start = time.time()
        self._total_bytes = total
        self._done_bytes = 0
        self._cur_size = 0
        self._meter = SpeedMeter()

        self.pb_item.stop()
        self.pb_item.configure(mode="determinate", value=0)
        self.pb_all.configure(value=0)
        self.lbl_item_pct.configure(text="")
        self.lbl_all_pct.configure(text=f"0 / {human_size(total)}")
        self.lbl_all_name.configure(text="总计 0/0 项")

        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

        # ⚠️ 所有 tkinter 变量必须在【主线程】读完再传给工作线程。
        #    在工作线程里调用 BooleanVar.get() / StringVar.get() 是未定义行为，
        #    可能导致随机死锁或崩溃 —— 这是 Tkinter 最经典的坑之一。
        opts = EngineOptions(
            do_gpt=self.opt_gpt.get(),
            do_env=self.opt_env.get(),
            verify_device_side=self.opt_devverify.get(),
            allow_fallback=self.opt_fallback.get(),
        )
        out_root = self.out_var.get()

        self.worker = threading.Thread(
            target=self._backup_worker,
            args=(chosen, outdir, opts, out_root),
            name="backup", daemon=True)
        self.worker.start()

        self.btn_start.configure(state="disabled")
        self.btn_cancel.configure(state="normal")
        self.btn_open.configure(state="disabled")
        self._set_result("")
        self._status("备份进行中 ...")

    def _backup_worker(self, chosen, outdir, opts: EngineOptions, out_root: str):
        """
        备份工作线程。

        ⚠️ 本函数运行在【非主线程】，因此：
            · 绝不能访问任何 tkinter 控件或 Variable（所需的值已由主线程读好传入）
            · 只能通过 self.msg_q 与主线程通信
        """
        def log(s):
            self._log_lines.append(s)
            self.msg_q.put(("log", s))

        def prog(d):
            self.msg_q.put(("progress", d))

        try:
            eng = BackupEngine(
                self.adb, outdir, opts, self.info,
                log_cb=log, progress_cb=prog, cancel=self.cancel_evt,
            )
            results = eng.run(chosen)
            eng.cleanup_device()

            platform, score, _ = detect_platform([p.name for p in self.partitions])
            topo = self.adb.detect_topology()
            meta = {
                "display": self.info.display, "serial": self.info.serial,
                "codename": self.info.codename, "version": self.info.version,
                "android": self.info.android, "sdk": self.info.sdk,
                "slot": self.info.slot, "stamp": datetime.now().strftime("%Y%m%d"),
                "platform": platform.display, "platform_score": score,
                "storage": topo.display,
                "root": self.info.caps.root_backend_name or "已 root",
                "first_disk": topo.disks[0].name if topo.disks else "sda",
            }
            write_manifest(outdir, results, meta)
            prev = find_previous_backup(out_root, outdir, tag=self.info.tag)
            write_readme(outdir, results, meta, prev, chosen)
            with open(os.path.join(outdir, "backup_log.txt"), "w",
                      encoding="utf-8") as f:
                f.write("\n".join(self._log_lines))

            self.msg_q.put(("done", {"ok": True, "results": results,
                                     "outdir": outdir, "prev": prev}))
        except Cancelled:
            self.msg_q.put(("done", {"ok": False, "cancelled": True, "outdir": outdir}))
        except Exception as e:
            self.msg_q.put(("log", f"[严重错误] {type(e).__name__}: {e}"))
            self.msg_q.put(("done", {"ok": False, "error": str(e), "outdir": outdir}))

    def _cancel_backup(self):
        if self.worker and self.worker.is_alive():
            if messagebox.askyesno(APP_TITLE, "确定要取消备份吗？\n已完成的文件会保留。"):
                self.cancel_evt.set()
                self._status("正在取消 ...")
                self.btn_cancel.configure(state="disabled")

    def _set_indeterminate(self, label: str, detail: str):
        """把当前项进度条切成来回滚动模式（校验/打包这类没有字节进度的阶段）。"""
        self.lbl_cur.configure(text=label)
        self.pb_item.stop()
        self.pb_item.configure(mode="indeterminate")
        self.pb_item.start(14)
        self.lbl_item_pct.configure(text=detail)

    def _on_progress(self, d: dict):
        phase = d.get("phase")

        if phase == "start":
            self._cur_size = d.get("expect", 0)
            self.pb_item.stop()
            self.pb_item.configure(mode="determinate", value=0)
            self.lbl_cur.configure(text=d.get("item", ""))
            self.lbl_item_pct.configure(
                text=f"0 B / {human_size(self._cur_size)}")

        elif phase == "item":
            name = d.get("item", "")
            cur = d.get("done", 0)
            exp = d.get("expect", 0)
            if str(self.pb_item.cget("mode")) != "determinate":
                self.pb_item.stop()
                self.pb_item.configure(mode="determinate")
            self.pb_item.configure(value=(cur / exp * 100) if exp else 0)

            # 速度用平滑器 —— 瞬时值会让进度条上的数字乱跳
            spd = self._meter.sample(cur, time.time())
            eta = self._meter.eta(cur, exp)
            self.lbl_cur.configure(text=name)
            # 紧凑写法：两个标签都是 36 列宽，原来的 " / " 与 " MB/s " 会超出被截断
            self.lbl_item_pct.configure(
                text=f"{human_size(cur)}  {spd / 1048576:.1f}MB/s  "
                     f"剩{human_duration(eta)}")
            self._update_overall(cur)

        elif phase == "hash":
            self._set_indeterminate(f"校验 {d.get('item','')}", "正在计算 SHA256 ...")

        elif phase == "item_done":
            self._done_bytes += self._cur_size
            self._cur_size = 0
            self._update_overall(0)
            self.lbl_all_name.configure(
                text=f"总计 {d.get('done', 0)}/{d.get('total', 1)} 项")

        elif phase == "gpt":
            self._set_indeterminate(f"GPT 分区表 {d.get('item','')}", "读取分区表 ...")

        elif phase == "env":
            self._set_indeterminate("打包 /data/adb 环境", "tar 打包中 ...")

    def _update_overall(self, cur_item_done: int):
        """刷新总体进度条、总体速度与 ETA，并把百分比同步到窗口标题。"""
        total = getattr(self, "_total_bytes", 0)
        if total <= 0:
            return
        overall = self._done_bytes + cur_item_done
        pct = min(100.0, overall * 100.0 / total)
        self.pb_all.configure(value=pct)

        el = time.time() - getattr(self, "_t_start", time.time())
        ospd = (overall / el) if el > 0 else 0.0
        oeta = ((total - overall) / ospd) if ospd > 0 else None
        self.lbl_all_pct.configure(
            text=f"{human_size(overall)}/{human_size(total)}  "
                 f"{ospd / 1048576:.1f}MB/s  剩{human_duration(oeta)}")
        # 窗口最小化 / 被遮挡时，任务栏上也能看到进度
        self.root.title(f"{APP_TITLE} — 备份中 {pct:.0f}%")

    def _on_done(self, r: dict):
        self.worker = None
        self.poll_paused.clear()
        self.btn_start.configure(state="normal")
        self.btn_cancel.configure(state="disabled")
        self.btn_open.configure(state="normal")
        try:
            self.pb_item.stop()
            self.pb_item.configure(mode="determinate", value=100)
        except tk.TclError:
            pass

        if r.get("cancelled"):
            self.root.title(f"{APP_TITLE} v{APP_VERSION} — 已取消")
        else:
            self.pb_all.configure(value=100)
            self.root.title(f"{APP_TITLE} v{APP_VERSION}")

        if r.get("cancelled"):
            self._set_result("已取消", "Warn.TLabel")
            self._status("备份已取消")
            return
        if not r.get("ok"):
            self._set_result("失败", "Err.TLabel")
            self._status(f"备份失败：{r.get('error','未知错误')}")
            messagebox.showerror(APP_TITLE, f"备份失败：\n{r.get('error','未知错误')}")
            return

        results = r["results"]
        ok = sum(1 for x in results if x.ok)
        bad = len(results) - ok
        total = sum(x.real_size for x in results if x.ok)
        if bad == 0:
            self._set_result(f"✅ 全部通过 {ok}/{len(results)}", "Ok.TLabel")
        else:
            self._set_result(f"⚠️ {ok} 通过 / {bad} 失败", "Warn.TLabel")
        self._status(f"完成：{ok}/{len(results)} 通过，共 {human_size(total)}")

        prev = r.get("prev")
        msg = (f"备份完成\n\n通过 {ok} / 共 {len(results)} 项\n"
               f"总大小 {human_size(total)}\n\n输出目录：\n{r['outdir']}")
        if prev:
            msg += f"\n\n已与上次备份对比：\n{os.path.basename(prev)}"
        if bad:
            failed = [x.label for x in results if not x.ok][:10]
            msg += f"\n\n失败项：\n" + "\n".join(failed)
            messagebox.showwarning(APP_TITLE, msg)
        else:
            messagebox.showinfo(APP_TITLE, msg)

    # ======================================================================
    #  退出
    # ======================================================================

    def _on_close(self):
        """关窗 —— 必须把 adb 子进程一并收拾干净。

        【为什么不能只 destroy()】adb 子进程是**独立进程**，Python 退出不会
        顺带把它们带走。关窗时若正有一次 adb 调用在飞（设备轮询、读分区表、
        或一次正在进行的备份），那个 adb.exe 就会变成孤儿留在任务管理器里 ——
        用户看到的现象就是「程序关了还有进程占着」。

        实测（Windows / Python 3.14）：`Popen(["adb","wait-for-device"])` 之后
        直接让解释器退出，该 adb 进程依然存活。

        所以按顺序做三件事：
          ① 举旗，让后台线程别再发起新的 adb 调用
          ② 给一小段收尾时间，然后把还活着的 adb 子进程全部 kill
          ③ 等后台线程真正退出（它们会因 ② 立刻从阻塞里醒来）
        """
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno(APP_TITLE, "备份正在进行，确定退出吗？"):
                return
            self.cancel_evt.set()

        # ① 举旗
        self.poll_stop.set()

        # ② 收尾 + 清子进程。给 0.35 秒让正常的调用自己跑完（大多数 adb
        #    命令几十毫秒就返回了），剩下的强杀。
        self._drain_ui(0.35)
        leaked = kill_live_children()
        if leaked:
            self._log(f"[退出] 已回收 {leaked} 个仍在运行的 adb 子进程")

        # ③ 停掉 adb 服务端 —— **无条件执行**，不给用户选择。
        #    必须排在 ② 之后：它自己也是一次 adb 调用，放前面会被刚装好的
        #    清理逻辑误杀，服务端反而停不掉。
        try:
            ok, msg = adb_stop_server(self.adb_path)
            self._log(f"[退出] 停止 ADB 服务：{'成功' if ok else '未成功'}（{msg}）")
            self._drain_ui(0.05)
        except Exception as e:
            try:
                self._log(f"[退出] 停止 ADB 服务失败：{e}")
            except Exception:
                pass

        # ④ 等后台线程退出。这两个都是 daemon 线程，本来就会随进程结束 ——
        #    等它们只是为了收尾干净，所以上限给得很短，别让用户点完 X 还等。
        deadline = time.monotonic() + 0.6
        while time.monotonic() < deadline:
            alive = [t for t in (self.worker, getattr(self, "poll_thread", None))
                     if t is not None and t.is_alive()]
            if not alive:
                break
            if not self._drain_ui(0.05):
                break

        self.root.destroy()

    def _drain_ui(self, seconds: float) -> bool:
        """在等待期间继续跑事件循环，避免界面假死。返回 False 表示窗口已没了。"""
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            try:
                self.root.update_idletasks()
                self.root.update()
            except tk.TclError:
                return False
            time.sleep(0.01)
        return True


# ==============================================================================
#  入口
# ==============================================================================

def main():
    enable_dpi_awareness()
    root = tk.Tk()
    try:
        root.call("tk", "scaling", 1.25)
    except tk.TclError:
        pass
    app = BackupApp(root)
    if not app.adb_path:
        root.after(300, lambda: messagebox.showerror(
            APP_TITLE,
            "未找到 adb.exe。\n\n"
            "请把本工具放在包含 adb 目录的位置，\n"
            "或把 platform-tools 加入系统 PATH 后重启本程序。"))
    root.mainloop()


if __name__ == "__main__":
    main()
