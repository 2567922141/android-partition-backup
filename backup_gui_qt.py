# -*- coding: utf-8 -*-
"""
安卓分区备份工具 —— 图形界面（PySide6 / Qt6 版）
================================================================================
这是 Tk 版 backup_gui.py 的**逐功能移植版**：功能、文案、线程模型、退出顺序
全部与 Tk 版一致，只把界面层从 tkinter 换成 PySide6。

【版本线】
    Tk 版 backup_gui.py   = 1.1.1（冻结，保留作回退方案）
    Qt 版 本文件          = 2.0.0（界面框架完整重写）
    两份文件并存，功能对等，靠版本号区分。

【为什么换 Qt】
    Tk 在 Windows 上给每个控件创建一个真实 HWND。本界面有 114 个控件、嵌套 8 层，
    每次改变窗口大小 Windows 都要重新调整/重绘这 114 个原生窗口，实测 69 ms/次，
    拖拽窗口边框时明显卡顿。Qt 的控件不占独立原生窗口，同样规模的界面
    实测每帧只要十几毫秒（见交付报告里的实测数字）。

【线程模型 —— Qt 铁律】
    主线程：只跑 Qt 事件循环与界面更新
    工作线程：所有 adb 调用、文件 IO、哈希计算
    两者之间只通过 queue.Queue 通信，主线程用 QTimer 每 80 ms 轮询队列。
    绝不在工作线程里碰任何 Qt 控件 —— 否则会随机崩溃。

【与 Tk 版的差异（只有这几处，都是为了适配 Qt 的机制，不是功能改动）】
    · 预设方案沿用 Tk 版的**单选按钮**（Radiobutton），不是下拉框 —— 以
      backup_gui.py 为准。
    · 高分屏适配由 Qt6 自动完成（Per-Monitor DPI Aware v2）。刻意**不**调用
      SetProcessDpiAwareness：手工设成 System DPI Aware 反而会让 Qt 拿不到
      Per-Monitor V2，在跨屏拖动时更糊。
    · ScrollHost 用 QScrollArea(widgetResizable) 实现，语义与 Tk 版一致：
      窗口比内容大 → 内容撑满、可伸缩区块（分区表 / 日志）吃掉多余空间；
      窗口比内容小 → 出现竖直滚动条，任何元素都够得着，绝不静默裁切。
    · 日志窗用 QPlainTextEdit（QTextCharFormat 着色），分级规则与 Tk 版逐字一致。

【依赖】
    PySide6 6.x + 同目录的 backup_core.py / partition_profiles.py
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
from datetime import datetime
from typing import Optional

APP_TITLE = "安卓分区备份工具"
# ⚠️ 两条版本线，不要搞混：
#   · backup_gui.py（Tk 版）    = 1.1.1  —— 保留作为回退方案，版本号冻结
#   · backup_gui_qt.py（本文件）= 2.0.0  —— 界面框架从 Tk 换到 Qt6 的完整重写
# 两者功能对等、可并存，但版本号必须区分，免得用户报障时分不清跑的是哪个。
APP_VERSION = "2.1.0"


# ==============================================================================
#  PySide6 定位
#
#  这个文件要同时活在两种部署形态里：
#    ① 开发态   —— 脚本在 <工作区>\06_脚本工具\安卓分区备份工具\，PySide6 在 <工作区>\_qtlib\
#    ② 便携发布态 —— PySide6 被打进便携包自己的目录（runtime\Lib\site-packages\ 之类）
#  所以**绝不硬编码某一个绝对路径**，一律按优先级去找。
# ==============================================================================

def _script_dir() -> str:
    """本文件所在目录。用 __file__ 而不是 os.getcwd() —— 用户可能从任何目录启动。"""
    return os.path.dirname(os.path.abspath(__file__))


def _pyside6_dir_candidates() -> list:
    """从脚本目录向上 5 级找 _qtlib。

    依次是：<脚本目录>\\_qtlib、<..>\\_qtlib、<..\\..>\\_qtlib、
            <..\\..\\..>\\_qtlib、<..\\..\\..\\..>\\_qtlib
    """
    out = []
    base = _script_dir()
    for _ in range(5):
        out.append(os.path.join(base, "_qtlib"))
        parent = os.path.dirname(base)
        if parent == base:          # 到根了
            break
        base = parent
    return out


def _no_pyside6_error(msg: str) -> None:
    """PySide6 找不到时，用最原始的方式把话说清楚（此时还没有 Qt 可用）。"""
    try:
        sys.stderr.write(msg + "\n")
        sys.stderr.flush()
    except Exception:
        pass
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, msg, APP_TITLE, 0x10)
        except Exception:
            pass
    raise SystemExit(1)


def _ensure_pyside6() -> None:
    """保证 `import PySide6` 能用。必须在任何 `from PySide6...` 之前调用。"""
    # ① 已经装好 / 已经在 sys.path 上 —— 直接用
    try:
        import PySide6  # noqa: F401
        return
    except ImportError:
        pass

    tried = []
    import_error = ""
    for cand in _pyside6_dir_candidates():
        tried.append(cand)
        if not os.path.isfile(os.path.join(cand, "PySide6", "__init__.py")):
            continue
        if cand not in sys.path:
            sys.path.insert(0, cand)
        try:
            import PySide6  # noqa: F401
            return
        except ImportError as e:          # 目录在、但装得不对（例如缺 shiboken6）
            import_error = f"{cand}：{e}"
            continue

    lines = [
        "未找到可用的 PySide6（Qt6 界面库），本程序无法启动。",
        "",
        "已经找过这些位置：",
        "  · 直接 import PySide6（当前 sys.path）",
    ]
    lines += [f"  · {p}" for p in tried]
    lines += [
        "",
        "每个位置上，目录里应当有 PySide6\\__init__.py，例如：",
        "    <某个目录>\\_qtlib\\PySide6\\__init__.py",
        "",
        "开发态可以直接用工作区里的 _qtlib（把 PySide6 解包进去即可）；",
        "也可以安装到当前 Python：",
        f"    {sys.executable} -m pip install PySide6",
    ]
    if import_error:
        lines += ["", f"补充：找到一个候选目录但导入失败 —— {import_error}"]
    _no_pyside6_error("\n".join(lines))


_ensure_pyside6()

from PySide6.QtCore import (          # noqa: E402
    QEvent, QPoint, QRect, QSize, Qt, QTimer,
)
from PySide6.QtGui import (           # noqa: E402
    QBrush, QColor, QFont, QFontDatabase, QFontMetrics, QPalette,
    QTextCharFormat, QTextCursor,
)
from PySide6.QtWidgets import (       # noqa: E402
    QAbstractItemView, QApplication, QButtonGroup, QCheckBox, QFileDialog,
    QFrame, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLayout, QLineEdit,
    QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QRadioButton,
    QScrollArea, QSizePolicy, QStyledItemDelegate, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout, QWidget,
)

# 允许脚本以任意工作目录启动 —— 与 Tk 版完全一致。
# 便携版从 <包根>\app\ 加载本文件时，backup_core 就在同一个 app\ 目录里，
# 靠这一句就能 import 到。
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

# 顶部设备信息栏显示的字段 —— 顺序即显示顺序。
# 用 FlowWidget 铺，窗口窄了会自动折行，不会丢字段。
#
# ⚠️ 实测坑（与 Tk 版同）：ro.build.display.id 在小米上是 **AOSP 构建号**
#    （BP2A.250605.031.A3），不是用户看到的系统版本号。真正的版本号在
#    ro.build.version.incremental（= OS3.0.307.0.WNKCNXM），即 DeviceInfo.version。
DEVICE_INFO_FIELDS = (
    "系统", "系统版本", "芯片", "平台", "内核", "架构",
    "内存", "屏幕", "槽位", "补丁", "Root",
)

# Tk 版用文本符号当勾选框；Qt 用 setCheckState() 画原生勾选框，
# 这两个常量保留只是为了和 Tk 版对照方便。
CHECK_ON = "☑"
CHECK_OFF = "☐"

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

#: 期望窗口尺寸（屏幕够大时用这个）
DEF_W, DEF_H = 1060, 900
#: 允许缩到的最小尺寸。比这更小也不会丢控件 —— QScrollArea 会接管。
MIN_W, MIN_H = 760, 520

# 文案用色 —— 与 Tk 版 _setup_style() 里的取值逐字一致
COLOR_SUB = "#666666"
COLOR_OK = "#1a7f37"
COLOR_WARN = "#b54708"
COLOR_ERR = "#b42318"
COLOR_DIM = "#999999"

# 日志分级配色 —— 与 Tk 版 log.tag_configure 逐字一致
LOG_COLORS = {
    "head": "#c586c0",
    "ok":   "#4ec9b0",
    "err":  "#f48771",
    "warn": "#dcdcaa",
    "info": "#569cd6",
    "dim":  "#808080",
}
LOG_BOLD_TAGS = ("head", "err")
LOG_BG = "#1e1e1e"
LOG_FG = "#d4d4d4"

#: 分区表列定义（键, 标题, 宽度, 对齐）—— 与 Tk 版 heads 逐字一致
TREE_COLUMNS = (
    ("chk",  "选",     44,  Qt.AlignmentFlag.AlignHCenter),
    ("name", "分区名", 210, Qt.AlignmentFlag.AlignLeft),
    ("size", "大小",   100, Qt.AlignmentFlag.AlignRight),
    ("tier", "级别",   100, Qt.AlignmentFlag.AlignHCenter),
    ("note", "说明",   520, Qt.AlignmentFlag.AlignLeft),
)


# ==============================================================================
#  环境相关小工具（与 Tk 版逐字一致）
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
    """挑一个系统里存在的中文字体，避免界面出现方块。

    必须在 QApplication 建好之后调用（QFontDatabase 需要 GUI 应用实例）。
    """
    try:
        families = set(QFontDatabase.families())
    except Exception:
        return ""
    for f in ("Microsoft YaHei UI", "Microsoft YaHei", "微软雅黑",
              "PingFang SC", "Noto Sans CJK SC", "SimHei", "SimSun"):
        if f in families:
            return f
    return ""


# ==============================================================================
#  响应式布局构件
# ==============================================================================

class FlowLayout(QLayout):
    """一排控件，容器变窄时自动折行。

    【为什么不能用 QHBoxLayout】
        水平布局在宽度不足时会把控件压扁或挤出可视区，既不报错也不提示 ——
        这就是「调整窗口大小后有些元素消失」的根因。

    【为什么也不能用 QGridLayout】
        grid 的列宽是**整个容器共享**的：折行后若某一行的宽控件占了第 0 列，
        这一列就会被撑宽，**其它行也跟着右移**（Tk 版实测越界 142px）。
        所以这里用 QLayout 自己算坐标，各行完全独立。

    这是 Qt 官方 Flow Layout 示例的结构（addItem / count / itemAt / takeAt /
    expandingDirections / hasHeightForWidth / heightForWidth / setGeometry /
    sizeHint / minimumSize 全实现），另加两处必要的改动：

      · **支持逐项前置间距**（Tk 版 FlowFrame.add(w, gap=...) 的语义）
      · **单项比整行还宽时，把它压到行宽**。否则一个超长标签（例如内核版本
        字符串，实测 642 px）会伸出容器右边缘 —— 这个 bug 在 Tk 版同样存在，
        Qt 版这里顺手修掉了。
    """

    def __init__(self, parent=None, gap: int = 10, row_gap: int = 6):
        super().__init__(parent)
        self._items: list = []          # QLayoutItem
        self._gaps: list = []           # 每项**之前**的水平间距
        self._gap = gap                 # 同一行内相邻控件的默认间距
        self._row_gap = row_gap         # 折行后行与行之间的竖直间距
        self.setContentsMargins(0, 0, 0, 0)

    # ------------------------------------------------------------ Qt 要求的接口
    def addItem(self, item):            # noqa: N802
        self._items.append(item)
        self._gaps.append(self._gap)

    def addWidget(self, w, gap=None):   # noqa: N802
        """把控件登记进本行。gap 是它**前面**要留的间距（对应 Tk 的 add(w, gap)）。"""
        super().addWidget(w)
        if self._items:
            self._gaps[-1] = self._gap if gap is None else gap
        return w

    def add_widget(self, w, gap=None):
        return self.addWidget(w, gap)

    def count(self):
        return len(self._items)

    def itemAt(self, index):            # noqa: N802
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def takeAt(self, index):            # noqa: N802
        if 0 <= index < len(self._items):
            self._gaps.pop(index)
            return self._items.pop(index)
        return None

    def expandingDirections(self):      # noqa: N802
        return Qt.Orientation(0)

    def hasHeightForWidth(self):        # noqa: N802
        return True

    def heightForWidth(self, width):    # noqa: N802
        return self._do_layout(QRect(0, 0, max(0, width), 0), True)

    def setGeometry(self, rect):        # noqa: N802
        super().setGeometry(rect)
        self._do_layout(rect, False)

    def sizeHint(self):                 # noqa: N802
        return self._natural_size()

    def minimumSize(self):              # noqa: N802
        size = QSize()
        for it in self._items:
            size = size.expandedTo(it.minimumSize())
        return size

    # ------------------------------------------------------------------ 自己用
    def _natural_size(self) -> QSize:
        """全部控件排成一行所需尺寸（对应 Tk 的 reqwidth / reqheight）。"""
        w = h = 0
        for i, it in enumerate(self._items):
            s = it.sizeHint()
            if i:
                w += self._gaps[i]
            w += s.width()
            h = max(h, s.height())
        return QSize(w, h)

    def _do_layout(self, rect: QRect, test_only: bool) -> int:
        """与 Tk 版 FlowFrame._reflow() 同一套坐标算法。返回占用的总高度。"""
        x = y = row_h = 0
        width = rect.width()
        for i, it in enumerate(self._items):
            s = it.sizeHint()
            need = s.width()
            h = max(s.height(), 1)
            g = 0 if x == 0 else self._gaps[i]
            if x > 0 and x + g + need > width:
                y += row_h + self._row_gap       # 放不下 → 换行
                x = 0
                g = 0
                row_h = 0
            # 单项比整行还宽 → 压到行宽，绝不伸出容器右边缘
            w = min(need, width) if width > 0 else need
            if not test_only:
                it.setGeometry(QRect(rect.x() + x + g, rect.y() + y, w, h))
            x += g + w
            row_h = max(row_h, h)
        return y + row_h


class FlowWidget(QFrame):
    """装了 FlowLayout 的容器。用法::

        row = FlowWidget(parent, gap=10, row_gap=6)
        row.add(QPushButton("全选"), gap=8)

    注意：控件必须用本行做 parent（`QPushButton("全选", row)`），或用
    `row.add(...)` —— 由本行统一负责它的位置。
    """

    def __init__(self, parent=None, gap: int = 10, row_gap: int = 6):
        super().__init__(parent)
        self._flow = FlowLayout(self, gap, row_gap)
        self.setFrameShape(QFrame.Shape.NoFrame)
        sp = QSizePolicy(QSizePolicy.Policy.MinimumExpanding,
                         QSizePolicy.Policy.Minimum)
        # ⭐ heightForWidth 必须显式打开，否则父布局不会来问「这么宽时你要多高」，
        #    折行后的高度就算错 —— 表现是折行内容被裁掉。
        sp.setHeightForWidth(True)
        self.setSizePolicy(sp)

    def flow(self) -> FlowLayout:
        return self._flow

    def add(self, widget, gap=None):
        self._flow.addWidget(widget, gap)
        return widget

    def add_all(self, widgets, gap=None):
        for w in widgets:
            self.add(w, gap)
        return widgets

    def refresh(self):
        """子控件文本变了（宽度随之变化）之后重新测量排布。

        对应 Tk 版的 update_idletasks() + _settle()：先把本级与所有祖先的布局
        标记为脏，让「折行 → 高度变化 → 父容器重新分配空间」这条链传导上去。
        """
        self._flow.invalidate()
        self.updateGeometry()
        p = self.parentWidget()
        while p is not None:
            lay = p.layout()
            if lay is not None:
                lay.invalidate()
            p.updateGeometry()
            p = p.parentWidget()

    # ⭐ 宽度报 0：折行容器的「自然宽度」没有意义（全排一行会非常宽，
    #    把窗口初始尺寸算坏）。Tk 版里 FlowFrame 是用 place 摆的，
    #    reqwidth 同样几乎不参与父容器计算 —— 这里对齐这个语义。
    def sizeHint(self):                 # noqa: N802
        return QSize(0, self._flow._natural_size().height())

    def minimumSizeHint(self):          # noqa: N802
        return QSize(0, self._flow.minimumSize().height())

    def heightForWidth(self, w):        # noqa: N802
        return self._flow.heightForWidth(w)


class _RowHeightDelegate(QStyledItemDelegate):
    """把表格行高固定成 Tk 版 Treeview 的 rowheight=24。"""

    def __init__(self, height: int, parent=None):
        super().__init__(parent)
        self._h = height

    def sizeHint(self, option, index):  # noqa: N802
        s = super().sizeHint(option, index)
        return QSize(s.width(), self._h)


class _PartitionTree(QTreeWidget):
    """只多一件事：按空格切换选中行的勾选状态（对应 Tk 版的 <space> 绑定）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.space_handler = None

    def keyPressEvent(self, event):     # noqa: N802
        if event.key() == Qt.Key.Key_Space and self.space_handler is not None:
            self.space_handler()
            event.accept()
            return
        super().keyPressEvent(event)


# ==============================================================================
#  极小的 tk 变量替身
#
#  Tk 版用 StringVar / BooleanVar，并且有一条铁律：**只能在主线程读写**。
#  Qt 里这些值直接存在控件上，语义一样（读写都发生在主线程），这里只是保留
#  原来的 .get() / .set() 写法，让两个版本的代码能逐行对照。
# ==============================================================================

class _TextVar:
    """对应 tk.StringVar，背后是一个 QLineEdit。"""

    def __init__(self, entry: QLineEdit):
        self._entry = entry

    def get(self) -> str:
        return self._entry.text()

    def set(self, value: str):
        self._entry.setText("" if value is None else str(value))


class _BoolVar:
    """对应 tk.BooleanVar，背后是一个 QCheckBox。"""

    def __init__(self, box: QCheckBox):
        self._box = box

    def get(self) -> bool:
        return self._box.isChecked()

    def set(self, value: bool):
        self._box.setChecked(bool(value))


class _ChoiceVar:
    """对应 tk.StringVar + Radiobutton 组，背后是一组 QRadioButton。

    ⚠️ get() 必须回读**真正被选中的那个按钮**。只记一个 _value 字段是不够的：
       用户点按钮时走的是 Qt 的 toggled 信号，字段不会被更新，
       结果 _apply_preset() 会拿到上一次的值 —— 表现是「点了预设没反应」。
    """

    def __init__(self, buttons: dict, default: Optional[str] = None):
        self._buttons = buttons
        self._value = default or ""
        if default and default in buttons:
            buttons[default].setChecked(True)

    def get(self) -> str:
        for key, btn in self._buttons.items():
            if btn.isChecked():
                return key
        return self._value

    def set(self, key: str):
        if key not in self._buttons:
            return
        self._value = key
        btn = self._buttons[key]
        if not btn.isChecked():
            btn.setChecked(True)


# ==============================================================================
#  主窗口
# ==============================================================================

class MainWindow(QWidget):
    """主窗口。只多一件事：把「用户点了 X」交给 BackupApp 按顺序收尾。

    ⚠️ 收尾返回 False（用户在「备份正在进行，确定退出吗？」里选了“否”）时
       必须 event.ignore()，否则窗口照样关掉 —— Tk 版是 return 后什么都不做。
    """

    def __init__(self, owner):
        super().__init__()
        self._owner = owner

    def closeEvent(self, event):        # noqa: N802
        owner = self._owner
        if owner is None or owner._closing:
            event.accept()
            return
        if owner._on_close():
            event.accept()
        else:
            event.ignore()


def install_qt_chinese(app: "QApplication") -> bool:
    """让 Qt 自带对话框（QMessageBox 的确定/取消、QFileDialog 等）显示中文。

    不装的话按钮是英文的 `OK` / `Cancel` —— Tk 版走的是系统对话框所以本来就是
    中文，换到 Qt 后这里必须自己补，否则中文界面上会蹦出两个英文按钮。

    翻译文件 (`qtbase_zh_CN.qm`) 随 PySide6 一起发布，位于
    `PySide6/translations/`；便携版里也必须一并带上（打包脚本已包含）。

    找不到就静默跳过 —— 宁可按钮是英文，也不能因此起不来。
    可重复调用（已装过就跳过）。
    """
    if getattr(app, "_apb_i18n_done", False):
        return True
    try:
        from PySide6.QtCore import QLibraryInfo, QTranslator
        td = QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)
        for name in ("qtbase_zh_CN", "qt_zh_CN"):
            tr = QTranslator(app)
            # 先按「文件名 + 目录」加载；某些便携布局下 QLibraryInfo 解析不到，
            # 再退回显式绝对路径试一次。
            ok = tr.load(name, td) or tr.load(os.path.join(td, name + ".qm"))
            if ok:
                app.installTranslator(tr)
                app._apb_i18n_done = True
                return True
    except Exception:
        pass
    return False


class BackupApp:

    def __init__(self, app: Optional[QApplication] = None):
        self.app = app if app is not None else QApplication.instance()
        if self.app is None:
            raise RuntimeError("创建 BackupApp 之前必须先创建 QApplication")
        # 无论从 main() 还是被别处直接实例化，都保证 Qt 自带对话框是中文
        install_qt_chinese(self.app)

        self.adb_path = find_adb()
        self.adb: Optional[Adb] = None
        self.info: Optional[DeviceInfo] = None
        self.partitions: list = []
        self.checked: set = set()
        self.rows: dict = {}                     # 分区名 -> QTreeWidgetItem
        self._item_of: dict = {}

        # 线程通信
        self.msg_q: queue.Queue = queue.Queue()
        self.poll_stop = threading.Event()
        self.poll_paused = threading.Event()
        self.cancel_evt = threading.Event()
        self.worker: Optional[threading.Thread] = None
        self.poll_thread: Optional[threading.Thread] = None
        self._last_devs: list = []
        self._probing = False

        # 界面状态
        self._closing = False
        self._building = False
        self._suppress_item_changed = False
        self._srv_running = None
        self._srv_busy = False
        self._outdir = ""
        self._chosen: list = []
        self._log_lines: list = []
        self._total_bytes = 0
        self._done_bytes = 0
        self._cur_size = 0
        self._t_start = time.time()
        self._meter = SpeedMeter()
        self._sort_rev: dict = {}

        self.font = pick_font()
        self._setup_style()
        self._build_ui()
        self._start_poll_thread()

        # 消息泵：QTimer 每 80 ms 在主线程消费一次队列（对应 root.after(80, _pump)）
        self._pump_timer = QTimer(self.window)
        self._pump_timer.setInterval(80)
        self._pump_timer.timeout.connect(self._pump)
        self._pump_timer.start()

    # ------------------------------------------------------------------ 样式
    def _setup_style(self):
        """字体与 DPI：Qt6 自己就是 Per-Monitor V2 aware，这里只定基准字体。"""
        base = QFont()
        if self.font:
            base.setFamily(self.font)
        base.setPointSize(10)
        self.app.setFont(base)

    def _f(self, size: int, bold: bool = False, family: Optional[str] = None) -> QFont:
        f = QFont()
        f.setFamily(family or self.font or self.app.font().family())
        f.setPointSize(size)
        f.setBold(bold)
        return f

    @staticmethod
    def _mono_font(size: int = 9, bold: bool = False) -> QFont:
        return QFont("Consolas", size, QFont.Weight.Bold if bold else QFont.Weight.Normal)

    @staticmethod
    def _fixed_chars(label: QLabel, chars: int):
        """模拟 Tk 的 width=<字符数>：按字体平均字符宽固定标签宽度。

        与 Tk 略有不同的一点：宽度取 max(实际文本宽, N 个字符宽)，
        所以文字永远不会被截掉（Tk 的 -width 会截）。
        """
        fm = label.fontMetrics()
        w = max(fm.horizontalAdvance(label.text()), fm.averageCharWidth() * chars)
        label.setFixedWidth(w)

    def _char_min_width(self, widget, chars: int):
        """模拟 Tk Button 的 width=<字符数>：给按钮一个统一的最小宽度。"""
        fm = widget.fontMetrics()
        widget.setMinimumWidth(fm.averageCharWidth() * chars + 16)

    @staticmethod
    def _color(widget, color: str):
        """给控件上色。

        ⚠️ 用 QPalette 而不是 setStyleSheet("color: ...")。
           一旦给某个控件设了样式表，Qt 就会给它（以及它的子树）换上
           QStyleSheetStyle，每次重绘都要多绕一大圈；本界面有二十多个带色
           标签，实测这份开销直接体现在 resize 的每帧耗时里。
           QPalette 只是换个颜色，绘制路径和默认完全一样。
        """
        pal = widget.palette()
        c = QColor(color)
        pal.setColor(QPalette.ColorRole.WindowText, c)
        pal.setColor(QPalette.ColorRole.Text, c)
        widget.setPalette(pal)

    def _sub_label(self, text: str, parent=None) -> QLabel:
        lbl = QLabel(text, parent)
        self._color(lbl, COLOR_SUB)
        lbl.setFont(self._f(9))
        return lbl

    # ------------------------------------------------------------------ 布局
    def _build_ui(self):
        # ⚠️ 建界面期间会有一串 setChecked / setText，它们会同步触发 toggled /
        #    textChanged。此时控件还没建全（例如分区表格还没创建），回调里访问
        #    就会 AttributeError。所以整段用 _building 罩住，只做构建不谈响应。
        self._building = True
        self.window = MainWindow(self)
        self.window.setWindowTitle(f"{APP_TITLE} v{APP_VERSION}")
        self.window.setMinimumSize(MIN_W, MIN_H)

        root_lay = QVBoxLayout(self.window)
        root_lay.setContentsMargins(0, 0, 0, 0)
        root_lay.setSpacing(0)

        # 整个界面装进可滚动区 —— 窗口再小也不会把控件裁掉（Tk 版的 ScrollHost）
        self.host = QScrollArea(self.window)
        self.host.setWidgetResizable(True)
        self.host.setFrameShape(QFrame.Shape.NoFrame)
        self.host.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.host.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        root_lay.addWidget(self.host)

        self.inner = QWidget()                 # 对应 Tk 的 host.inner
        self.host.setWidget(self.inner)
        outer = QVBoxLayout(self.inner)
        outer.setContentsMargins(10, 10, 10, 10)
        outer.setSpacing(0)
        self.outer = outer

        self._build_device_panel(self.inner, outer)
        outer.addSpacing(8)
        self._build_partition_panel(self.inner, outer)
        outer.addSpacing(8)
        self._build_option_panel(self.inner, outer)
        outer.addSpacing(8)
        self._build_action_panel(self.inner, outer)
        outer.addSpacing(8)
        self._build_progress_panel(self.inner, outer)
        outer.addSpacing(6)
        self._build_statusbar(self.inner, outer)

        self._building = False

        # 等所有控件算出自然尺寸后再定窗口大小，否则量到的是半成品
        QTimer.singleShot(0, self._fit_window)
        # ADB 服务状态：先 400ms 后刷一次，之后每 2.5 秒一次
        QTimer.singleShot(400, self._refresh_server_state)
        self._srv_timer = QTimer(self.window)
        self._srv_timer.setInterval(2500)
        self._srv_timer.timeout.connect(self._tick_server_state)
        self._srv_timer.start()
        # 最终路径预览：早先绑定的时候设备信息还没到，稍后再刷一次
        QTimer.singleShot(300, self._preview_path)

    def _fit_window(self):
        """按「内容实际需求」和「屏幕可用区」决定初始窗口大小。

        对应 Tk 版的 _fit_window()：旧版写死 `geometry("1060x820")` +
        `minsize(900, 700)`，一旦系统 DPI 放大或内容变多，实际需要的尺寸就会
        超过 820，而 pack 在空间不足时是从末尾开始裁的 —— 状态栏、进度条、
        日志依次消失，且不给任何提示。这里改成实测后按需取值。

        Qt 版多一步：高度必须在**最终选定的宽度**下量 —— 折行容器的高度依赖
        宽度，用自然宽度量出来的高度会偏小。
        """
        lay = self.inner.layout()
        need_w = max(self.inner.sizeHint().width(),
                     self.inner.minimumSizeHint().width()) + 4

        screen = self.window.screen() or self.app.primaryScreen()
        avail = screen.availableGeometry()
        max_w = max(MIN_W, avail.width() - 40)      # 给窗口边框留余量
        max_h = max(MIN_H, avail.height() - 90)     # 给任务栏和标题栏留余量

        w = min(max(need_w, DEF_W), max_w)
        if lay is not None and lay.hasHeightForWidth():
            need_h = lay.heightForWidth(w) + 4
        else:
            need_h = self.inner.sizeHint().height() + 4
        h = min(max(need_h, DEF_H), max_h)

        # 最小尺寸故意允许小于内容 —— 那种情况下滚动条接管，元素依然够得着，
        # 比「锁死一个很大的 minsize 导致小屏上窗口超出屏幕」要好得多。
        self.window.setMinimumSize(min(MIN_W, max_w), min(MIN_H, max_h))

        x = max(0, avail.x() + (avail.width() - w) // 2)
        y = max(0, avail.y() + (avail.height() - h) // 2 - 20)
        self.window.setGeometry(x, y, w, h)

    # ---------------------------------------------------------- ① 设备状态
    def _build_device_panel(self, parent, lay):
        f = QGroupBox(" ① 设备状态 ", parent)
        self.panel_device = f
        lay.addWidget(f)
        v = QVBoxLayout(f)
        v.setContentsMargins(10, 10, 10, 10)
        v.setSpacing(0)

        row = QWidget(f)
        v.addWidget(row)
        rh = QHBoxLayout(row)
        rh.setContentsMargins(0, 0, 0, 0)
        rh.setSpacing(8)

        self.lbl_dot = QLabel("●", row)
        self.lbl_dot.setFont(self._f(16))
        self._color(self.lbl_dot, COLOR_DIM)
        rh.addWidget(self.lbl_dot)

        col = QWidget(row)
        rh.addWidget(col, 1)
        cv = QVBoxLayout(col)
        cv.setContentsMargins(0, 0, 0, 0)
        cv.setSpacing(0)
        self.lbl_dev = QLabel("正在检测设备 ...", col)
        self.lbl_dev.setFont(self._f(12, bold=True))
        # 允许被压缩：QLabel 默认的最小宽度 = 整段文字的宽度，会被父布局当成
        # 「低于这个宽度就排不下」，把整个内容区的最小宽度顶上去 ——
        # 表现就是窗口一拖窄就冒出横向滚动条。这些文字行本来就该让位。
        self.lbl_dev.setMinimumWidth(1)
        cv.addWidget(self.lbl_dev)
        self.lbl_devsub = self._sub_label("", col)
        self.lbl_devsub.setMinimumWidth(1)
        # 概要行结尾可能是要紧的警告（“… ← root 不可用，无法读取分区”），
        # 所以这里让它折行而不是裁掉。
        self.lbl_devsub.setWordWrap(True)
        cv.addWidget(self.lbl_devsub)

        btns = QWidget(row)
        rh.addWidget(btns, 0, Qt.AlignmentFlag.AlignTop)
        bh = QHBoxLayout(btns)
        bh.setContentsMargins(0, 0, 0, 0)
        bh.setSpacing(6)
        self.btn_refresh = QPushButton("刷新设备", btns)
        self.btn_refresh.clicked.connect(self._force_refresh)
        bh.addWidget(self.btn_refresh)
        self.btn_root = QPushButton("检查 Root", btns)
        self.btn_root.clicked.connect(self._probe_now)
        bh.addWidget(self.btn_root)

        # ---- 详细信息：一排排 `名称 值`，宽度不够自动折行 ----
        # 用 FlowWidget 而不是 QGridLayout：grid 的列宽是整个容器共享的，某个值
        # 特别长会把整列撑宽、带着其它行一起右移。
        self.info_grid = FlowWidget(f, gap=20, row_gap=4)
        v.addSpacing(9)
        v.addWidget(self.info_grid)
        self._info_labels = {}
        for key in DEVICE_INFO_FIELDS:
            cell = QWidget(self.info_grid)
            ch = QHBoxLayout(cell)
            ch.setContentsMargins(0, 0, 0, 0)
            ch.setSpacing(6)
            ch.addWidget(self._sub_label(key, cell))
            val = QLabel("—", cell)
            ch.addWidget(val)
            self.info_grid.add(cell)
            self._info_labels[key] = val

        # ---- ADB 服务开关（放在顶部，方便随手启停）----
        # 服务端是**多个工具共用**的常驻进程（Android Studio / scrcpy 也在用
        # 同一个）。所以这里给出显式开关；退出时则**无条件**停掉它，不留残留。
        srv = FlowWidget(f, gap=12, row_gap=6)
        v.addSpacing(9)
        v.addWidget(srv)
        self.srv_row = srv
        self.lbl_srv = QLabel("ADB 服务: 检测中 ...", srv)
        srv.add(self.lbl_srv)
        self.btn_srv = QPushButton("启动", srv)
        self._char_min_width(self.btn_srv, 8)
        self.btn_srv.clicked.connect(self._toggle_server)
        srv.add(self.btn_srv, gap=8)
        srv.add(self._sub_label("退出本程序时会自动停止 ADB 服务", srv), gap=16)

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
            lbl.setText(vals.get(key, "—"))
        self.info_grid.refresh()        # 文字宽度变了，重新折行

    # ---------------------------------------------------------- ② 分区选择
    def _build_partition_panel(self, parent, lay):
        f = QGroupBox(" ② 选择要备份的分区 ", parent)
        self.panel_partition = f
        lay.addWidget(f, 1)
        v = QVBoxLayout(f)
        v.setContentsMargins(10, 10, 10, 10)
        v.setSpacing(0)

        # ---- 预设方案（Tk 版是单选按钮，这里照搬）----
        top = FlowWidget(f, gap=8)
        self.top = top
        self.preset_row = top
        v.addWidget(top)
        top.add(QLabel("预设方案:", top))
        self._preset_buttons = {}
        for p in PRESETS:
            btn = QRadioButton(p.display, top)
            btn.setToolTip(p.description)
            btn.toggled.connect(
                lambda checked, k=p.key: self._on_preset_toggled(checked, k))
            self._preset_buttons[p.key] = btn
            top.add(btn)
        self.preset_var = _ChoiceVar(self._preset_buttons, "critical+root")

        # ---- 批量按钮 / 过滤 / 隐藏低价值 ----
        top2 = FlowWidget(f, gap=6, row_gap=6)
        self.top2 = top2
        self.bulk_row = top2
        v.addSpacing(6)
        v.addWidget(top2)
        b_all = QPushButton("全选", top2)
        self._char_min_width(b_all, 6)
        b_all.clicked.connect(lambda: self._bulk("all"))
        top2.add(b_all)
        b_none = QPushButton("全不选", top2)
        self._char_min_width(b_none, 7)
        b_none.clicked.connect(lambda: self._bulk("none"))
        top2.add(b_none)
        b_inv = QPushButton("反选", top2)
        self._char_min_width(b_inv, 6)
        b_inv.clicked.connect(lambda: self._bulk("invert"))
        top2.add(b_inv)
        b_crit = QPushButton("仅不可再生", top2)
        self._char_min_width(b_crit, 11)
        b_crit.clicked.connect(lambda: self._bulk("critical"))
        top2.add(b_crit, gap=14)

        top2.add(QLabel("过滤:", top2), gap=14)
        self.filter_entry = QLineEdit(top2)
        self.filter_entry.setMinimumWidth(120)
        self.filter_entry.textChanged.connect(lambda *_: self._render_rows())
        self.filter_var = _TextVar(self.filter_entry)
        top2.add(self.filter_entry)

        self.chk_hide_low = QCheckBox("隐藏低价值分区", top2)
        self.chk_hide_low.setChecked(True)
        self.chk_hide_low.toggled.connect(lambda *_: self._render_rows())
        self.hide_low = _BoolVar(self.chk_hide_low)
        top2.add(self.chk_hide_low, gap=14)
        top2.add(self._sub_label("（勾选/取消：点击左侧方框，或选中行按空格）", top2),
                 gap=14)

        # ---- 表格 ----
        v.addSpacing(6)
        self.tree = _PartitionTree(f)
        self.tree.space_handler = self._on_space
        self.tree.setColumnCount(len(TREE_COLUMNS))
        self.tree.setHeaderLabels([c[1] for c in TREE_COLUMNS])
        self.tree.setRootIsDecorated(False)
        self.tree.setUniformRowHeights(True)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.setItemDelegate(_RowHeightDelegate(24, self.tree))
        self.tree.setMinimumHeight(80)          # 允许被压小（否则会把窗口最小高度顶上去）
        header = self.tree.header()
        header.setSectionsClickable(True)
        header.setStretchLastSection(True)
        for i, (_key, _text, width, align) in enumerate(TREE_COLUMNS):
            self.tree.setColumnWidth(i, width)
            self.tree.headerItem().setTextAlignment(i, align)
            # 单元格对齐要设在 QTreeWidgetItem 上（QTreeWidget 自己没有
            # setTextAlignment），所以每一行在 _render_rows() 里逐列设置。
        header.sectionClicked.connect(self._sort_by)
        self.tree.itemChanged.connect(self._on_item_changed)
        v.addWidget(self.tree, 1)

        self.lbl_sum = QLabel("已选 0 项，合计 0 B", f)
        self.lbl_sum.setFont(self._f(10, bold=True))
        self.lbl_sum.setMinimumWidth(1)
        v.addSpacing(6)
        v.addWidget(self.lbl_sum)

    # ---------------------------------------------------------- ③ 选项
    def _build_option_panel(self, parent, lay):
        f = QGroupBox(" ③ 输出位置与选项 ", parent)
        self.panel_option = f
        lay.addWidget(f)
        v = QVBoxLayout(f)
        v.setContentsMargins(10, 10, 10, 10)
        v.setSpacing(0)

        # ---- 备份根目录 ----
        r1 = QWidget(f)
        v.addWidget(r1)
        h1 = QHBoxLayout(r1)
        h1.setContentsMargins(0, 0, 0, 0)
        h1.setSpacing(6)
        lbl1 = QLabel("备份根目录:", r1)
        self._fixed_chars(lbl1, 12)
        h1.addWidget(lbl1)
        self.out_entry = QLineEdit(self._default_outdir(), r1)
        self.out_entry.textChanged.connect(lambda *_: self._preview_path())
        self.out_var = _TextVar(self.out_entry)
        h1.addWidget(self.out_entry, 1)
        b_browse = QPushButton("浏览...", r1)
        b_browse.clicked.connect(self._choose_out)
        h1.addWidget(b_browse)
        b_reset = QPushButton("恢复默认", r1)
        self._char_min_width(b_reset, 9)
        b_reset.clicked.connect(self._reset_outdir)
        h1.addWidget(b_reset)

        # ---- 自定义备份名称 ----
        v.addSpacing(6)
        r2 = QWidget(f)
        v.addWidget(r2)
        h2 = QHBoxLayout(r2)
        h2.setContentsMargins(0, 0, 0, 0)
        h2.setSpacing(6)
        lbl2 = QLabel("备份名称:", r2)
        self._fixed_chars(lbl2, 12)
        h2.addWidget(lbl2)
        self.name_entry = QLineEdit("", r2)
        self.name_entry.textChanged.connect(lambda *_: self._preview_path())
        self.name_var = _TextVar(self.name_entry)
        h2.addWidget(self.name_entry, 1)
        h2.addWidget(self._sub_label("留空则用默认名 Backup", r2))

        # ---- 最终路径实时预览 ----
        v.addSpacing(6)
        r3 = QWidget(f)
        v.addWidget(r3)
        h3 = QHBoxLayout(r3)
        h3.setContentsMargins(0, 0, 0, 0)
        h3.setSpacing(6)
        lbl3 = QLabel("最终路径:", r3)
        self._fixed_chars(lbl3, 12)
        h3.addWidget(lbl3)
        self.lbl_preview = self._sub_label("", r3)
        self.lbl_preview.setMinimumWidth(1)     # 同上：别让长路径顶住最小宽度
        self.lbl_preview.setWordWrap(True)      # 路径太长就折行，不裁掉
        h3.addWidget(self.lbl_preview, 1)

        # ---- 开关 ----
        v.addSpacing(8)
        r4 = FlowWidget(f, gap=14, row_gap=6)
        self.opt_row = r4
        v.addWidget(r4)
        self.chk_gpt = QCheckBox("备份 GPT 分区表", r4)
        self.chk_gpt.setChecked(True)
        self.opt_gpt = _BoolVar(self.chk_gpt)
        r4.add(self.chk_gpt)
        self.chk_env = QCheckBox("打包 /data/adb 环境", r4)
        self.chk_env.setChecked(False)
        self.opt_env = _BoolVar(self.chk_env)
        r4.add(self.chk_env)
        # 默认开 —— 设备端算一遍 sha256sum 跟本地比对，是唯一能证明
        # 「设备上的字节 == 硬盘上的字节」的一层。对关键分区（几十 MB）
        # 耗时只有零点几秒；超过 1 GB 的分区引擎会自动跳过。
        self.chk_devverify = QCheckBox("设备端二次校验（推荐；≥1GB 自动跳过）", r4)
        self.chk_devverify.setChecked(True)
        self.opt_devverify = _BoolVar(self.chk_devverify)
        r4.add(self.chk_devverify)
        self.chk_fallback = QCheckBox("失败自动回退", r4)
        self.chk_fallback.setChecked(True)
        self.opt_fallback = _BoolVar(self.chk_fallback)
        r4.add(self.chk_fallback)
        # ==== APB_ARCHIVE BEGIN ====
        # 备份全部跑完（含校验）之后，额外把整个备份目录压成一个 zip，
        # 放在备份目录的**同级**、**同名**（Backups\Redmi K70\ → Backups\Redmi K70.zip），
        # 原始文件夹原封不动地保留。打包失败不影响备份结果。
        self.chk_archive = QCheckBox("备份完成后打包为压缩包", r4)
        self.chk_archive.setChecked(False)   # 默认关 —— 别让每次备份都白多花时间
        self.chk_archive.setToolTip(
            "把整个备份目录压成一个同名的 zip，放在它的同级目录里。\n"
            "原始文件夹原样保留，不会删除也不会移动。\n"
            "压缩包用的是 zip（Windows 双击即可打开），打包失败不影响备份结果。")
        self.opt_archive = _BoolVar(self.chk_archive)
        r4.add(self.chk_archive)
        # ==== APB_ARCHIVE END ====

    # ---------------------------------------------------------- ADB 服务
    def _tick_server_state(self):
        """每 2.5 秒看一眼 adb 服务端状态（别的程序也可能把它启停）。"""
        if not self.poll_stop.is_set():
            self._refresh_server_state()

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
            return                      # 没变就别白刷
        self._srv_running = state
        if running:
            self.lbl_srv.setText("ADB 服务: ● 运行中")
            self._color(self.lbl_srv, COLOR_OK)
            self.btn_srv.setText("停止")
            self.btn_srv.setEnabled(True)
        else:
            self.lbl_srv.setText(
                "ADB 服务: ○ 已停止" + ("（设备检测已暂停）" if paused else ""))
            self._color(self.lbl_srv, COLOR_ERR)
            self.btn_srv.setText("启动")
            self.btn_srv.setEnabled(True)
        self.srv_row.refresh()          # 文字变宽了要重新排

    def _toggle_server(self):
        """启停 adb 服务端 —— 要跑 adb，所以放工作线程。"""
        if self._srv_busy:
            return
        want_stop = bool(self._srv_running and self._srv_running[0])
        if want_stop and self.worker and self.worker.is_alive():
            self._mb_warn(APP_TITLE, "备份正在进行，不能停止 ADB 服务。")
            return
        self._srv_busy = True
        self.btn_srv.setEnabled(False)
        self.btn_srv.setText("停止中" if want_stop else "启动中")
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
    def _build_action_panel(self, parent, lay):
        f = FlowWidget(parent, gap=8, row_gap=8)
        self.action_row = f
        lay.addWidget(f)
        self.btn_start = QPushButton("▶  开始备份", f)
        self.btn_start.setFont(self._f(11, bold=True))
        self.btn_start.setMinimumHeight(38)
        self.btn_start.setEnabled(False)
        self.btn_start.clicked.connect(self._start_backup)
        f.add(self.btn_start)
        self.btn_cancel = QPushButton("✕  取消", f)
        self.btn_cancel.setFont(self._f(11, bold=True))
        self.btn_cancel.setMinimumHeight(38)
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.clicked.connect(self._cancel_backup)
        f.add(self.btn_cancel)
        self.btn_open = QPushButton("打开输出目录", f)
        self.btn_open.setEnabled(False)
        self.btn_open.clicked.connect(self._open_outdir)
        f.add(self.btn_open, gap=10)

        self.lbl_result = QLabel("", f)
        self.lbl_result.setFont(self._f(11, bold=True))
        f.add(self.lbl_result, gap=14)

    # ---------------------------------------------------------- ⑤ 进度
    def _build_progress_panel(self, parent, lay):
        f = QGroupBox(" ④ 进度与日志 ", parent)
        self.panel_progress = f
        lay.addWidget(f, 1)
        v = QVBoxLayout(f)
        v.setContentsMargins(10, 10, 10, 10)
        v.setSpacing(0)

        # ---- 当前分区 ----
        # 标签宽度刻意收窄：两个 34 字符宽的标签在窄窗口里会把进度条挤没
        r1 = QWidget(f)
        v.addWidget(r1)
        h1 = QHBoxLayout(r1)
        h1.setContentsMargins(0, 0, 0, 0)
        h1.setSpacing(8)
        self.lbl_cur = QLabel("就绪", r1)
        self._fixed_chars(self.lbl_cur, 16)
        h1.addWidget(self.lbl_cur)
        self.pb_item = QProgressBar(r1)
        self.pb_item.setRange(0, 100)
        self.pb_item.setValue(0)
        self.pb_item.setTextVisible(False)
        # ⚠️ 必须显式给小一点的最小宽度。QProgressBar 的默认 minimumSizeHint
        #    在 Qt6 里相当大（实测两行加起来把「进度与日志」面板的最小宽度
        #    顶到 797px），窗口一拖到 760 就必然冒出横向滚动条。
        #    进度条本来就是该吃掉宽度变化的那一个控件。
        self.pb_item.setMinimumWidth(120)
        h1.addWidget(self.pb_item, 1)
        self.lbl_item_pct = QLabel("", r1)
        self.lbl_item_pct.setFont(self._mono_font(9))
        self.lbl_item_pct.setAlignment(Qt.AlignmentFlag.AlignRight |
                                       Qt.AlignmentFlag.AlignVCenter)
        self._fixed_chars(self.lbl_item_pct, 36)
        h1.addWidget(self.lbl_item_pct)

        # ---- 总计 ----
        v.addSpacing(4)
        r2 = QWidget(f)
        v.addWidget(r2)
        h2 = QHBoxLayout(r2)
        h2.setContentsMargins(0, 0, 0, 0)
        h2.setSpacing(8)
        self.lbl_all_name = QLabel("总计", r2)
        self._fixed_chars(self.lbl_all_name, 16)
        h2.addWidget(self.lbl_all_name)
        self.pb_all = QProgressBar(r2)
        self.pb_all.setRange(0, 100)
        self.pb_all.setValue(0)
        self.pb_all.setTextVisible(False)
        self.pb_all.setMinimumWidth(120)        # 同上
        h2.addWidget(self.pb_all, 1)
        self.lbl_all_pct = QLabel("", r2)
        self.lbl_all_pct.setFont(self._mono_font(9))
        self.lbl_all_pct.setAlignment(Qt.AlignmentFlag.AlignRight |
                                      Qt.AlignmentFlag.AlignVCenter)
        self._fixed_chars(self.lbl_all_pct, 36)
        h2.addWidget(self.lbl_all_pct)

        # ---- 日志 ----
        v.addSpacing(8)
        # 最小高度故意取小 —— 自然高度越小，小屏幕上「一打开就全都看得见」
        # 的概率越高；同时也不会把窗口的最小高度顶上去。
        self.log = QPlainTextEdit(f)
        self.log.setReadOnly(True)
        self.log.setUndoRedoEnabled(False)
        self.log.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.log.setFont(self._mono_font(9))
        self.log.setMinimumHeight(60)
        pal = self.log.palette()
        pal.setColor(QPalette.ColorRole.Base, QColor(LOG_BG))
        pal.setColor(QPalette.ColorRole.Text, QColor(LOG_FG))
        self.log.setPalette(pal)
        v.addWidget(self.log, 1)
        self._log_formats = self._make_log_formats()

    def _make_log_formats(self) -> dict:
        """日志分级着色 —— 一眼能从瀑布里挑出失败项。取值与 Tk 版逐字一致。"""
        out = {}
        for tag, color in LOG_COLORS.items():
            fmt = QTextCharFormat()
            fmt.setForeground(QColor(color))
            if tag in LOG_BOLD_TAGS:
                fmt.setFontWeight(QFont.Weight.Bold)
            out[tag] = fmt
        return out

    def _build_statusbar(self, parent, lay):
        bar = QWidget(parent)
        lay.addWidget(bar)
        h = QHBoxLayout(bar)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(8)
        self.lbl_status = self._sub_label("启动中 ...", bar)
        self.lbl_status.setMinimumWidth(1)
        h.addWidget(self.lbl_status, 1)
        adb_txt = self.adb_path or "未找到 adb.exe"
        self.lbl_adb = self._sub_label(f"adb: {adb_txt}", bar)
        # adb 的完整路径很长，让它裁掉但把全文留在 tooltip 里 ——
        # 否则这一条会把状态栏（进而是整个窗口）的最小宽度顶到 768px。
        self.lbl_adb.setMinimumWidth(1)
        self.lbl_adb.setToolTip(adb_txt)
        h.addWidget(self.lbl_adb)

    # ======================================================================
    #  消息框（对应 Tk 的 messagebox，返回语义一致）
    # ======================================================================

    def _mb_warn(self, title: str, text: str):
        QMessageBox.warning(self.window, title, text)

    def _mb_info(self, title: str, text: str):
        QMessageBox.information(self.window, title, text)

    def _mb_error(self, title: str, text: str):
        QMessageBox.critical(self.window, title, text)

    def _mb_ask_yes(self, title: str, text: str) -> bool:
        btn = QMessageBox.question(
            self.window, title, text,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        return btn == QMessageBox.StandardButton.Yes

    def _mb_ask_ok(self, title: str, text: str) -> bool:
        btn = QMessageBox.question(
            self.window, title, text,
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Ok)
        return btn == QMessageBox.StandardButton.Ok

    # ======================================================================
    #  工具方法
    # ======================================================================

    def _default_outdir(self) -> str:
        """
        默认备份根目录 = 程序目录下的 Backups。

        便携版布局是 <包根>/app/backup_gui_qt.py，此时备份应落在 <包根>/Backups
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
            self.lbl_preview.setText("（请先选择备份根目录）")
            return
        code = self.info.codename if self.info else ""
        sn = self.info.serial if self.info else ""
        try:
            r = resolve_backup_dir(root, self._current_name(), code, sn)
        except Exception as e:
            self.lbl_preview.setText(f"（无法预览：{e}）")
            return
        arrow = "  ⟵  " + r.reason if r.collided else "  ⟵  该名称尚未使用"
        self.lbl_preview.setText(r.path + arrow)

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
        line = f"[{ts}] {text}"
        # 空格式 = 「不指定任何属性」，从而跟随控件的默认字体与前景色。
        # 若直接传 None，Qt 会沿用光标当前的字符格式 —— 上一行是红色的话，
        # 这一行会被一起染红。
        fmt = self._log_formats.get(tag) if tag else None
        if fmt is None:
            fmt = QTextCharFormat()
        cur = self.log.textCursor()
        cur.movePosition(QTextCursor.MoveOperation.End)
        cur.insertText(line + "\n", fmt)
        self.log.setTextCursor(cur)
        self.log.ensureCursorVisible()
        sb = self.log.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _status(self, text: str):
        self.lbl_status.setText(text)

    def _set_result(self, text: str, style: str = ""):
        """更新底部结果标签。

        文本宽度会变（"" → "✅ 全部通过 13/13"），而 FlowLayout 用的是绝对
        定位、不会自动重排，所以必须主动 refresh 一次。
        """
        self.lbl_result.setText(text)
        if style == "Ok.TLabel":
            self.lbl_result.setFont(self._f(10, bold=True))
            self._color(self.lbl_result, COLOR_OK)
        elif style == "Warn.TLabel":
            self.lbl_result.setFont(self._f(10, bold=True))
            self._color(self.lbl_result, COLOR_WARN)
        elif style == "Err.TLabel":
            self.lbl_result.setFont(self._f(10, bold=True))
            self._color(self.lbl_result, COLOR_ERR)
        self.action_row.refresh()

    def _choose_out(self):
        d = QFileDialog.getExistingDirectory(
            self.window, "选择备份根目录", self.out_var.get() or os.getcwd())
        if d:
            self.out_var.set(os.path.normpath(d))
            self._preview_path()

    def _open_outdir(self):
        """优先打开本次备份目录；还没备份过就打开备份根目录。"""
        d = getattr(self, "_outdir", "") or self.out_var.get()
        if not os.path.isdir(d):
            d = self.out_var.get()
        if not os.path.isdir(d):
            self._mb_info(APP_TITLE, "目录还不存在，先做一次备份吧。")
            return
        try:
            if os.name == "nt":
                os.startfile(d)          # noqa: S606
            elif sys.platform == "darwin":
                subprocess.Popen(["open", d])
            else:
                subprocess.Popen(["xdg-open", d])
        except Exception as e:
            self._mb_warn(APP_TITLE, f"无法打开目录：{e}")

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
        self.lbl_dev.setText("正在检测设备 ...")
        self._color(self.lbl_dot, COLOR_DIM)
        self.lbl_devsub.setText("")
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

    def _on_devices(self, devs):
        if not devs:
            self._color(self.lbl_dot, COLOR_ERR)
            self.lbl_dev.setText("未检测到设备")
            self.lbl_devsub.setText(
                "请确认：USB 已连接 / 已开启 USB 调试 / 手机上已点“允许” / 屏幕已解锁")
            self.btn_start.setEnabled(False)
            self.adb, self.info, self.partitions = None, None, []
            self._set_device_details(None)
            self._render_rows()
            return
        serial, state = devs[0]
        if state == "device":
            self._color(self.lbl_dot, COLOR_OK)
            self.lbl_dev.setText(f"已连接  {serial}")
            self.lbl_devsub.setText("正在读取设备信息 ...")
        elif state == "unauthorized":
            self._color(self.lbl_dot, COLOR_WARN)
            self.lbl_dev.setText(f"未授权  {serial}")
            self.lbl_devsub.setText("请在手机屏幕上点击“允许 USB 调试”，然后重新插拔")
            self.btn_start.setEnabled(False)
        elif state == "offline":
            self._color(self.lbl_dot, COLOR_WARN)
            self.lbl_dev.setText(f"离线  {serial}")
            self.lbl_devsub.setText("连接异常，建议重新插拔 USB 线")
            self.btn_start.setEnabled(False)
        else:
            self._color(self.lbl_dot, COLOR_WARN)
            self.lbl_dev.setText(f"{state}  {serial}")
            self.btn_start.setEnabled(False)

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
            self._color(self.lbl_dot, COLOR_ERR)
            self.lbl_devsub.setText(sub + "  ← root 不可用，无法读取分区")
            self.btn_start.setEnabled(False)
            self._log("root 不可用：请在 root 管理器里给 Shell/ADB 授权后点“刷新设备”")
        else:
            self.lbl_dev.setText(f"{info.display}   ({info.serial})")
            self.lbl_devsub.setText(sub)
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

    def _on_preset_toggled(self, checked: bool, key: str):
        """用户点了某个预设方案的单选按钮。

        对应 Tk 版 Radiobutton 的 command= —— Tk 只在**用户点击**时触发，
        程序里改变量不触发。所以建界面期间（_building）和取消选中都要忽略。
        """
        if not checked or self._building:
            return
        if key == "custom":
            return
        self._apply_preset()

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

    def _visible(self) -> list:
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
        # 建界面期间「隐藏低价值分区」默认是勾上的，那一句 setChecked 会同步
        # 触发 toggled → 本函数，而分区表格还没创建出来。
        if not hasattr(self, "tree"):
            return
        self._suppress_item_changed = True
        try:
            self.tree.clear()
            self.rows.clear()
            self._item_of.clear()

            for p in self._visible():
                item = QTreeWidgetItem([
                    "", p.name, human_size(p.size), p.label, p.reason])
                item.setCheckState(
                    0, Qt.CheckState.Checked if p.name in self.checked
                    else Qt.CheckState.Unchecked)
                # 列对齐（选/级别居中、大小右对齐，与 Tk 版 heads 的 anchor 一致）
                for c, col in enumerate(TREE_COLUMNS):
                    item.setTextAlignment(c, col[3])
                # 行配色：t1 橙 / t2 蓝 / t4 灰（与 Tk 版 tag_configure 一致）
                if p.tier in (1, 2, 4):
                    if p.tier == 1:
                        bg = QBrush(QColor("#fff4e5"))
                    elif p.tier == 2:
                        bg = QBrush(QColor("#eef6ff"))
                    else:
                        bg = None
                    if bg is not None:
                        for c in range(len(TREE_COLUMNS)):
                            item.setBackground(c, bg)
                    if p.tier == 4:
                        fg = QBrush(QColor(COLOR_DIM))
                        for c in range(len(TREE_COLUMNS)):
                            item.setForeground(c, fg)
                self.tree.addTopLevelItem(item)
                self.rows[p.name] = item
                self._item_of[p.name] = p
        finally:
            self._suppress_item_changed = False
        self._update_sum()

    def _update_sum(self):
        chosen = [p for p in self.partitions if p.name in self.checked]
        total = sum(p.size for p in chosen)
        crit = sum(1 for p in chosen if p.tier == 1)
        self.lbl_sum.setText(
            f"已选 {len(chosen)} 项，合计 {human_size(total)}"
            f"（其中不可再生 {crit} 个）")
        ready = bool(self.adb and self.info and self.info.root_ok
                     and chosen and self.worker is None)
        self.btn_start.setEnabled(bool(ready))

    # ---------------------------------------------------------- 交互
    def _on_item_changed(self, item, column):
        """用户在表格里点了勾选框（对应 Tk 版的 _on_tree_click）。"""
        if self._suppress_item_changed or column != 0:
            return
        name = item.text(1)
        if not name:
            return
        if item.checkState(0) == Qt.CheckState.Checked:
            self.checked.add(name)
        else:
            self.checked.discard(name)
        self._update_sum()
        self.preset_var.set("custom")

    def _on_space(self):
        for item in self.tree.selectedItems():
            self._toggle_item(item)
        return

    def _toggle_item(self, item):
        name = item.text(1)
        if not name:
            return
        if name in self.checked:
            self.checked.discard(name)
            state = Qt.CheckState.Unchecked
        else:
            self.checked.add(name)
            state = Qt.CheckState.Checked
        self._suppress_item_changed = True
        try:
            item.setCheckState(0, state)
        finally:
            self._suppress_item_changed = False
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

    def _sort_by(self, section: int):
        """点表头排序（与 Tk 版 _sort_by 同一套升降序规则）。"""
        key = TREE_COLUMNS[section][0] if 0 <= section < len(TREE_COLUMNS) else ""
        rev = self._sort_rev.get(key, False)
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
            self._mb_warn(APP_TITLE, "设备未就绪")
            return
        chosen = [p for p in self.partitions if p.name in self.checked]
        if not chosen:
            self._mb_warn(APP_TITLE, "请至少勾选一个分区")
            return

        total = sum(p.size for p in chosen)

        out_root = self.out_var.get().strip()
        if not out_root:
            self._mb_warn(APP_TITLE, "请先选择备份根目录")
            return
        try:
            os.makedirs(out_root, exist_ok=True)
        except OSError as e:
            self._mb_error(APP_TITLE, f"无法创建备份根目录：\n{out_root}\n\n{e}")
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
        if not self._mb_ask_ok(
                APP_TITLE,
                f"即将备份 {len(chosen)} 个分区，合计 {human_size(total)}。\n\n"
                f"备份目录：\n{outdir}\n"
                f"{note}\n"
                f"本工具只做只读导出，不写入设备任何分区。{warn}"):
            return

        try:
            os.makedirs(outdir, exist_ok=False)      # ⚠️ 绝不覆盖已有目录
        except FileExistsError:
            self._mb_error(APP_TITLE, f"目录已存在，请换个名称：\n{outdir}")
            return
        except OSError as e:
            self._mb_error(APP_TITLE, f"无法创建备份目录：\n{e}")
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

        self.pb_item.setRange(0, 100)
        self.pb_item.setValue(0)
        self.pb_all.setValue(0)
        self.lbl_item_pct.setText("")
        self.lbl_all_pct.setText(f"0 / {human_size(total)}")
        self.lbl_all_name.setText("总计 0/0 项")

        self.log.clear()

        # ⚠️ 所有界面控件的值必须在【主线程】读完再传给工作线程。
        #    在工作线程里读控件状态是未定义行为 —— Tk 版里这是最经典的坑，
        #    Qt 同样如此（Qt 控件不是线程安全的）。
        opts = EngineOptions(
            do_gpt=self.opt_gpt.get(),
            do_env=self.opt_env.get(),
            verify_device_side=self.opt_devverify.get(),
            allow_fallback=self.opt_fallback.get(),
            # ==== APB_ARCHIVE BEGIN ====
            # 勾选框的值在【主线程】读好再传进去（见上面的铁律）
            archive_enabled=self.opt_archive.get(),
            archive_format="zip",
            archive_level=6,
            # ==== APB_ARCHIVE END ====
        )
        out_root = self.out_var.get()

        self.worker = threading.Thread(
            target=self._backup_worker,
            args=(chosen, outdir, opts, out_root),
            name="backup", daemon=True)
        self.worker.start()

        self.btn_start.setEnabled(False)
        self.btn_cancel.setEnabled(True)
        self.btn_open.setEnabled(False)
        self._set_result("")
        self._status("备份进行中 ...")

    def _backup_worker(self, chosen, outdir, opts: EngineOptions, out_root: str):
        """
        备份工作线程。

        ⚠️ 本函数运行在【非主线程】，因此：
            · 绝不能访问任何 Qt 控件（所需的值已由主线程读好传入）
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

            # ==== APB_ARCHIVE BEGIN ====
            # manifest / README / backup_log 都是 run() 返回之后才写出来的，
            # 补进已经做好的压缩包，免得解压出来少了校验清单与恢复说明。
            # 补不进去不算失败 —— 原件都在，压缩包只是额外的一份。
            if eng.archive.enabled and eng.archive.ok:
                eng.archive_add_paths([
                    os.path.join(outdir, "manifest.txt"),
                    os.path.join(outdir, "README.md"),
                    os.path.join(outdir, "backup_log.txt"),
                ])
            # ==== APB_ARCHIVE END ====

            self.msg_q.put(("done", {"ok": True, "results": results,
                                     "outdir": outdir, "prev": prev,
                                     # ==== APB_ARCHIVE BEGIN ====
                                     # 打包结果一并交给主线程显示（跨线程只传数据）
                                     "archive": eng.archive,
                                     # ==== APB_ARCHIVE END ====
                                     }))
        except Cancelled:
            self.msg_q.put(("done", {"ok": False, "cancelled": True, "outdir": outdir}))
        except Exception as e:
            self.msg_q.put(("log", f"[严重错误] {type(e).__name__}: {e}"))
            self.msg_q.put(("done", {"ok": False, "error": str(e), "outdir": outdir}))

    def _cancel_backup(self):
        if self.worker and self.worker.is_alive():
            if self._mb_ask_yes(APP_TITLE, "确定要取消备份吗？\n已完成的文件会保留。"):
                self.cancel_evt.set()
                self._status("正在取消 ...")
                self.btn_cancel.setEnabled(False)

    def _set_indeterminate(self, label: str, detail: str):
        """把当前项进度条切成来回滚动模式（校验/打包这类没有字节进度的阶段）。"""
        self.lbl_cur.setText(label)
        self.pb_item.setRange(0, 0)          # 0/0 = 忙碌指示（Tk 的 indeterminate）
        self.lbl_item_pct.setText(detail)

    def _on_progress(self, d: dict):
        phase = d.get("phase")

        if phase == "start":
            self._cur_size = d.get("expect", 0)
            self.pb_item.setRange(0, 100)
            self.pb_item.setValue(0)
            self.lbl_cur.setText(d.get("item", ""))
            self.lbl_item_pct.setText(f"0 B / {human_size(self._cur_size)}")

        elif phase == "item":
            name = d.get("item", "")
            cur = d.get("done", 0)
            exp = d.get("expect", 0)
            if self.pb_item.maximum() == 0 and self.pb_item.minimum() == 0:
                self.pb_item.setRange(0, 100)
            self.pb_item.setValue(int(cur / exp * 100) if exp else 0)

            # 速度用平滑器 —— 瞬时值会让进度条上的数字乱跳
            spd = self._meter.sample(cur, time.time())
            eta = self._meter.eta(cur, exp)
            self.lbl_cur.setText(name)
            # 紧凑写法：两个标签都是 36 列宽，原来的 " / " 与 " MB/s " 会超出被截断
            self.lbl_item_pct.setText(
                f"{human_size(cur)}  {spd / 1048576:.1f}MB/s  "
                f"剩{human_duration(eta)}")
            self._update_overall(cur)

        elif phase == "hash":
            self._set_indeterminate(f"校验 {d.get('item','')}", "正在计算 SHA256 ...")

        elif phase == "item_done":
            self._done_bytes += self._cur_size
            self._cur_size = 0
            self._update_overall(0)
            self.lbl_all_name.setText(
                f"总计 {d.get('done', 0)}/{d.get('total', 1)} 项")

        elif phase == "gpt":
            self._set_indeterminate(f"GPT 分区表 {d.get('item','')}", "读取分区表 ...")

        elif phase == "env":
            self._set_indeterminate("打包 /data/adb 环境", "tar 打包中 ...")
        # ==== APB_ARCHIVE BEGIN ====
        elif phase == "archive_start":
            self._set_indeterminate("打包备份产物", "准备压缩 ...")
        elif phase == "archive":
            # 打包阶段有真实的字节进度（引擎自己遍历目录算出来的），
            # 所以可以用确定进度条，不必挂个忙碌指示。
            exp = d.get("expect", 0)
            cur = d.get("done", 0)
            if exp > 0:
                if self.pb_item.maximum() == 0 and self.pb_item.minimum() == 0:
                    self.pb_item.setRange(0, 100)
                self.pb_item.setValue(int(cur / exp * 100))
                tf = d.get("total_files", 0)
                extra = f"  {d.get('files', 0)}/{tf} 个文件" if tf else ""
                self.lbl_item_pct.setText(
                    f"{human_size(cur)} / {human_size(exp)}{extra}")
            else:
                self._set_indeterminate("打包备份产物", "压缩中 ...")
            self.lbl_cur.setText("打包")
        elif phase == "archive_done":
            self.pb_item.setRange(0, 100)
            self.pb_item.setValue(100 if d.get("ok") else 0)
        # ==== APB_ARCHIVE END ====

    def _update_overall(self, cur_item_done: int):
        """刷新总体进度条、总体速度与 ETA，并把百分比同步到窗口标题。"""
        total = getattr(self, "_total_bytes", 0)
        if total <= 0:
            return
        overall = self._done_bytes + cur_item_done
        pct = min(100.0, overall * 100.0 / total)
        self.pb_all.setValue(int(pct))

        el = time.time() - getattr(self, "_t_start", time.time())
        ospd = (overall / el) if el > 0 else 0.0
        oeta = ((total - overall) / ospd) if ospd > 0 else None
        self.lbl_all_pct.setText(
            f"{human_size(overall)}/{human_size(total)}  "
            f"{ospd / 1048576:.1f}MB/s  剩{human_duration(oeta)}")
        # 窗口最小化 / 被遮挡时，任务栏上也能看到进度
        self.window.setWindowTitle(f"{APP_TITLE} — 备份中 {pct:.0f}%")

    def _on_done(self, r: dict):
        self.worker = None
        self.poll_paused.clear()
        self.btn_start.setEnabled(True)
        self.btn_cancel.setEnabled(False)
        self.btn_open.setEnabled(True)
        try:
            self.pb_item.setRange(0, 100)
            self.pb_item.setValue(100)
        except RuntimeError:
            pass

        if r.get("cancelled"):
            self.window.setWindowTitle(f"{APP_TITLE} v{APP_VERSION} — 已取消")
        else:
            self.pb_all.setValue(100)
            self.window.setWindowTitle(f"{APP_TITLE} v{APP_VERSION}")

        if r.get("cancelled"):
            self._set_result("已取消", "Warn.TLabel")
            self._status("备份已取消")
            return
        if not r.get("ok"):
            self._set_result("失败", "Err.TLabel")
            self._status(f"备份失败：{r.get('error','未知错误')}")
            self._mb_error(APP_TITLE, f"备份失败：\n{r.get('error','未知错误')}")
            return

        results = r["results"]
        ok = sum(1 for x in results if x.ok)
        bad = len(results) - ok
        total = sum(x.real_size for x in results if x.ok)
        # 设备端校验的战果 —— 让用户看得见这一层到底跑了没跑
        dv_ok = sum(1 for x in results if x.device_verified is True)
        dv_no = sum(1 for x in results if x.device_verified is False)
        if bad == 0:
            self._set_result(f"✅ 全部通过 {ok}/{len(results)}", "Ok.TLabel")
        else:
            self._set_result(f"⚠️ {ok} 通过 / {bad} 失败", "Warn.TLabel")
        self._status(f"完成：{ok}/{len(results)} 通过，共 {human_size(total)}")
        if dv_ok:
            self._log(f"  其中 {dv_ok} 项与设备端哈希核对一致", "ok")
        if dv_no:
            self._log(f"  [!] {dv_no} 项没能做设备端校验（设备端没算出哈希）", "warn")

        # ==== APB_ARCHIVE BEGIN ====
        # 打包环节的结果（在 run() 最后做的；没勾选时为「未启用」）
        arc = r.get("archive")
        arc_msg = ""
        if arc is not None and getattr(arc, "enabled", False):
            if arc.ok:
                self._log(f"  [OK] 压缩包 {arc.path}", "ok")
                self._log(f"       {arc.message}   耗时 {arc.seconds:.1f}s", "dim")
                self._status(f"完成：{ok}/{len(results)} 通过，共 {human_size(total)}"
                             f"，压缩包 {human_size(arc.out_bytes)}")
                arc_msg = (f"\n\n压缩包：\n{arc.path}\n"
                           f"{arc.message}（耗时 {arc.seconds:.1f}s）")
            else:
                self._log(f"  [!] 打包未完成：{arc.message}"
                          + (f"（{arc.error}）" if arc.error else ""), "warn")
                self._log("       备份数据完好，压缩包只是额外的一份，不影响恢复。",
                          "warn")
                arc_msg = (f"\n\n⚠️ 压缩包未生成：{arc.message}\n"
                           f"备份数据完好，不影响恢复。")
        # ==== APB_ARCHIVE END ====

        prev = r.get("prev")
        msg = (f"备份完成\n\n通过 {ok} / 共 {len(results)} 项\n"
               f"总大小 {human_size(total)}")
        if dv_ok:
            msg += f"\n设备端校验：{dv_ok} 项哈希一致 ✅"
        if dv_no:
            msg += f"\n⚠️ {dv_no} 项未做设备端校验"
        msg += f"\n\n输出目录：\n{r['outdir']}"
        # ==== APB_ARCHIVE BEGIN ====
        msg += arc_msg
        # ==== APB_ARCHIVE END ====
        if prev:
            msg += f"\n\n已与上次备份对比：\n{os.path.basename(prev)}"
        if bad:
            failed = [x.label for x in results if not x.ok][:10]
            msg += f"\n\n失败项：\n" + "\n".join(failed)
            self._mb_warn(APP_TITLE, msg)
        else:
            self._mb_info(APP_TITLE, msg)

    # ======================================================================
    #  退出
    # ======================================================================

    def _on_close(self) -> bool:
        """关窗 —— 必须把 adb 子进程一并收拾干净。返回 True 表示真的关。

        【为什么不能只关窗口】adb 子进程是**独立进程**，Python 退出不会顺带把
        它们带走。关窗时若正有一次 adb 调用在飞（设备轮询、读分区表、或一次
        正在进行的备份），那个 adb.exe 就会变成孤儿留在任务管理器里 ——
        用户看到的现象就是「程序关了还有进程占着」。

        实测（Windows / Python 3.14）：`Popen(["adb","wait-for-device"])` 之后
        直接让解释器退出，该 adb 进程依然存活。

        所以按顺序做四件事：
          ① 举旗，让后台线程别再发起新的 adb 调用
          ② 给一小段收尾时间，然后把还活着的 adb 子进程全部 kill
          ③ 停掉 adb 服务端（**无条件**，且必须在 ② 之后 —— 它自己也是一次
             adb 调用，放前面会被 ② 刚装好的清理逻辑误杀）
          ④ 等后台线程真正退出（它们会因 ② 立刻从阻塞里醒来），然后关窗

        ⚠️ 四步的顺序不能动。顺带一提，②③ 之间的分界就是「先杀子进程、
           再停服务端」，反过来写会导致服务端停不掉。
        """
        if self._closing:
            return True

        # 先问，再动旗子 —— 用户选「否」时要能原样退回去（Tk 版同样是 return）
        if self.worker and self.worker.is_alive():
            if not self._mb_ask_yes(APP_TITLE, "备份正在进行，确定退出吗？"):
                return False
            self.cancel_evt.set()
        self._closing = True

        # ① 举旗
        self.poll_stop.set()

        # ② 收尾 + 清子进程。给 0.35 秒让正常的调用自己跑完（大多数 adb
        #    命令几十毫秒就返回了），剩下的强杀。
        self._drain_ui(0.35)
        leaked = kill_live_children()
        if leaked:
            self._log(f"[退出] 已回收 {leaked} 个仍在运行的 adb 子进程")

        # ③ 停掉 adb 服务端 —— 无条件执行，不给用户选择。
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

        try:
            self._pump_timer.stop()
            self._srv_timer.stop()
        except Exception:
            pass
        self.window.close()
        self.app.quit()
        return True

    def _drain_ui(self, seconds: float) -> bool:
        """在等待期间继续跑事件循环，避免界面假死。返回 False 表示应用已没了。

        对应 Tk 版的 root.update_idletasks() + root.update()；Qt 里就是
        QApplication.processEvents()。**只有在主线程调用才安全**。
        """
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            try:
                QApplication.processEvents()
            except Exception:
                return False
            time.sleep(0.01)
        return True


# ==============================================================================
#  入口
# ==============================================================================

def main():
    # Qt6 自带 Per-Monitor V2 DPI 感知，不需要像 Tk 版那样手工调
    # SetProcessDpiAwareness —— 手工设成 System DPI Aware 反而更糊。
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName(APP_TITLE)
    app.setApplicationDisplayName(APP_TITLE)
    app.setQuitOnLastWindowClosed(True)
    # 让 Qt 自带对话框（确定/取消、目录选择）也走中文 —— 找不到就静默跳过。
    # 这一句在 BackupApp.__init__ 里还会再兜一次，两处都调用是幂等的。
    install_qt_chinese(app)

    gui = BackupApp(app)
    gui.window.show()
    if not gui.adb_path:
        QTimer.singleShot(300, lambda: QMessageBox.critical(
            gui.window, APP_TITLE,
            "未找到 adb.exe。\n\n"
            "请把本工具放在包含 adb 目录的位置，\n"
            "或把 platform-tools 加入系统 PATH 后重启本程序。"))
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
