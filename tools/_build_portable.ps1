# -*- coding: utf-8 -*-
<#
================================================================================
  构建「安卓分区备份工具 便携版」
================================================================================
  把三样东西打进一个自包含目录，拷到任何 Windows 电脑都能直接双击运行：
      · 程序本体（3 个 .py）
      · ADB（adb.exe + 2 个必需 DLL）
      · Python 精简运行时（含 tkinter / Tcl-Tk）

  构建原则：宁可多带几个无关模块，也不能缺依赖 —— 缺一个模块用户就白屏。
  因此只做「确定无用」的裁剪（test / idlelib / site-packages 等），不做精细依赖分析。

  用法： powershell -ExecutionPolicy Bypass -File _build_portable.ps1
================================================================================
#>

$ErrorActionPreference = "Stop"

$Here    = Split-Path -Parent $MyInvocation.MyCommand.Path     # ...\安卓分区备份工具
$ToolDir = $Here
$WsRoot  = Split-Path (Split-Path $Here -Parent) -Parent       # 工作区根
$PkgRoot = Join-Path $WsRoot "android-partition-backup-portable"

$PyHome  = "C:\Python314"
$AdbSrc  = Join-Path $WsRoot "adb"

function Say($m, $c = "Gray") { Write-Host $m -ForegroundColor $c }
function MB($p) {
    if (-not (Test-Path $p)) { return "0" }
    $s = (Get-ChildItem $p -Recurse -File -ErrorAction SilentlyContinue |
          Measure-Object -Property Length -Sum).Sum
    return [math]::Round($s / 1MB, 1)
}

Say ""
Say "================================================================" Cyan
Say "  构建便携版" Cyan
Say "================================================================" Cyan
Say "  源程序  : $ToolDir"
Say "  输出包  : $PkgRoot"
Say "  Python  : $PyHome"
Say "  ADB     : $AdbSrc"
Say ""

# ---------------------------------------------------------------- 前置检查
foreach ($f in @("backup_gui_qt.py", "backup_gui.py", "backup_core.py", "archive_pack.py", "partition_profiles.py")) {
    if (-not (Test-Path (Join-Path $ToolDir $f))) { throw "缺少程序文件: $f" }
}
foreach ($f in @("adb.exe", "AdbWinApi.dll", "AdbWinUsbApi.dll")) {
    if (-not (Test-Path (Join-Path $AdbSrc $f))) { throw "缺少 ADB 依赖: $f" }
}
if (-not (Test-Path (Join-Path $PyHome "python.exe"))) { throw "找不到 Python: $PyHome" }

# ---------------------------------------------------------------- 清理旧包
if (Test-Path $PkgRoot) {
    Say "  清理旧的便携包 ..." Yellow
    Remove-Item $PkgRoot -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $PkgRoot | Out-Null

# ---------------------------------------------------------------- 1. 程序本体
Say "[1/5] 复制程序本体 ..." Cyan
$appDir = Join-Path $PkgRoot "app"
New-Item -ItemType Directory -Force -Path $appDir | Out-Null
foreach ($f in @("backup_gui_qt.py", "backup_gui.py", "backup_core.py", "archive_pack.py", "partition_profiles.py")) {
    Copy-Item (Join-Path $ToolDir $f) $appDir -Force
    Say "      $f"
}

# ---------------------------------------------------------------- 2. ADB
Say "[2/5] 复制 ADB ..." Cyan
$adbDir = Join-Path $PkgRoot "adb"
New-Item -ItemType Directory -Force -Path $adbDir | Out-Null
foreach ($f in @("adb.exe", "AdbWinApi.dll", "AdbWinUsbApi.dll", "NOTICE.txt")) {
    $src = Join-Path $AdbSrc $f
    if (Test-Path $src) { Copy-Item $src $adbDir -Force }
}
Say "      adb.exe + AdbWinApi.dll + AdbWinUsbApi.dll  ($(MB $adbDir) MB)"

# ---------------------------------------------------------------- 3. Python 运行时
Say "[3/5] 构建 Python 精简运行时（这一步最慢，约 1 分钟）..." Cyan
$rt = Join-Path $PkgRoot "runtime"
New-Item -ItemType Directory -Force -Path $rt | Out-Null

# 3a 解释器与核心 DLL
foreach ($f in @("python.exe", "pythonw.exe", "python3.dll", "python314.dll",
                 "vcruntime140.dll", "vcruntime140_1.dll")) {
    $src = Join-Path $PyHome $f
    if (Test-Path $src) { Copy-Item $src $rt -Force } else { Say "      [!] 缺少 $f" Yellow }
}
Say "      解释器: python.exe / pythonw.exe / python314.dll"

# 3b DLLs 目录（含 _tkinter.pyd 与 Tcl/Tk 的 dll）—— 排除测试用扩展
$dllDst = Join-Path $rt "DLLs"
robocopy (Join-Path $PyHome "DLLs") $dllDst /E /XF _testclinic.pyd _testclinic_limited.pyd `
    _testbuffer.pyd _testcapi.pyd _testimportmultiple.pyd _testinternalcapi.pyd `
    _testmultiphase.pyd _testsinglephase.pyd /NFL /NDL /NJH /NJS /NP /R:1 /W:1 | Out-Null
Say "      DLLs/ (含 _tkinter.pyd + tcl90.dll + tcl9tk90.dll)  ($(MB $dllDst) MB)"

# 3c Lib 目录 —— 只排除确定无用的
$libDst = Join-Path $rt "Lib"
robocopy (Join-Path $PyHome "Lib") $libDst /E `
    /XD test tests __pycache__ site-packages idlelib ensurepip venv distutils lib2to3 turtledemo `
    /XF *.pyc *.pyo /NFL /NDL /NJH /NJS /NP /R:1 /W:1 | Out-Null
Say "      Lib/  ($(MB $libDst) MB)"

# 3d Tcl/Tk 运行时（tkinter 必需）—— 仅作备用界面，Qt 版才是主界面
$tclDst = Join-Path $rt "tcl"
robocopy (Join-Path $PyHome "tcl") $tclDst /E /NFL /NDL /NJH /NJS /NP /R:1 /W:1 | Out-Null
Say "      tcl/  ($(MB $tclDst) MB)"

# 3d-2 PySide6（Qt 主界面必需）—— 装进 runtime\Lib\site-packages\，
#      这样便携运行时直接 import PySide6 就能用，不需要任何 sys.path 手法。
#      搬运时顺手剥掉 QtWidgets 应用绝对用不到的部分（约省 110 MB）。
$qtSrc = Join-Path $WsRoot "_qtlib"
if (-not (Test-Path (Join-Path $qtSrc "PySide6"))) {
    throw "找不到 PySide6：$qtSrc\PySide6`n请在任意能联网的机器上执行：`n    python -m pip install --target `"$qtSrc`" PySide6`n（只需 pyside6-essentials + shiboken6，不必装 Addons；装完把整个 _qtlib 目录拷到工作区根即可）"
}
$spDst = Join-Path $rt "Lib\site-packages"
New-Item -ItemType Directory -Force -Path $spDst | Out-Null

foreach ($pkgName in @("PySide6", "shiboken6")) {
    robocopy (Join-Path $qtSrc $pkgName) (Join-Path $spDst $pkgName) /E `
        /NFL /NDL /NJH /NJS /NP /R:1 /W:1 | Out-Null
}
Say "      PySide6/ + shiboken6/ 复制完成"

# 剥掉 QtWidgets 应用不需要的：QML/Quick 全家桶、Designer、Qt 命令行工具、
# SQL 驱动、软件 OpenGL 回退。保留 Qt Core/Gui/Widgets/Svg/Network/PrintSupport。
$qtDst = Join-Path $spDst "PySide6"
$cutDirs = @("qml", "include", "glue", "scripts", "typesystems",
             "plugins\sqldrivers", "plugins\qmltooling", "plugins\designer")
$cutFiles = @("opengl32sw.dll", "Qt6Quick*", "Qt6Qml*", "Qt6Designer*", "QtDesigner*",
              "QtQml*", "QtQuick*", "Qt6UiTools*", "QtUiTools*", "Qt6Test*", "QtTest*",
              "Qt6Sql*", "QtSql*", "Qt6Help*", "QtHelp*", "Qt6Pdf*", "QtPdf*",
              "Qt6Spatial*", "Qt6Charts*", "Qt6Multimedia*", "Qt6RemoteObjects*",
              "Qt6SerialPort*", "Qt6Scxml*", "Qt6StateMachine*", "Qt6Bluetooth*",
              "Qt6Nfc*", "Qt6Positioning*", "Qt6Location*", "Qt6WebSockets*",
              "Qt6WebChannel*", "Qt6Concurrent*", "*.exe")
$freed = 0
foreach ($d in $cutDirs) {
    $p = Join-Path $qtDst $d
    if (Test-Path $p) {
        $freed += (Get-ChildItem $p -Recurse -File -ErrorAction SilentlyContinue |
                   Measure-Object -Property Length -Sum).Sum
        Remove-Item $p -Recurse -Force -ErrorAction SilentlyContinue
    }
}
foreach ($pat in $cutFiles) {
    Get-ChildItem $qtDst -Filter $pat -File -ErrorAction SilentlyContinue | ForEach-Object {
        $freed += $_.Length
        Remove-Item $_.FullName -Force -ErrorAction SilentlyContinue
    }
}
Say "      PySide6/  ($(MB $qtDst) MB，已剥掉 $([math]::Round($freed/1MB,1)) MB)"

# 3d-3 建一个空的 lib\fonts 目录。
#      Qt6 不再自带字体（用系统的），但找不到这个目录时会往 stderr 打一条
#      「QFontDatabase: Cannot find font directory ...」警告。那条警告本身无害，
#      却会让 PowerShell 把原生命令的 stderr 当成 ErrorRecord，从而中断构建。
#      目录存在即可让它闭嘴。
$qtFonts = Join-Path $qtDst "lib\fonts"
New-Item -ItemType Directory -Force -Path $qtFonts | Out-Null

# 3e 关键依赖自检
Say "      依赖自检:"
$need = @(
    "DLLs\_tkinter.pyd", "DLLs\tcl90.dll", "DLLs\tcl9tk90.dll",
    "Lib\tkinter\__init__.py", "Lib\tkinter\ttk.py",
    "Lib\subprocess.py", "Lib\hashlib.py", "Lib\threading.py",
    "Lib\queue.py", "Lib\dataclasses.py", "Lib\fnmatch.py",
    "Lib\ctypes\__init__.py", "Lib\encodings\utf_8.py",
    "tcl\tk9.0"
)
$missing = @()
foreach ($n in $need) {
    if (Test-Path (Join-Path $rt $n)) { Say "        OK  $n" DarkGray }
    else { Say "        缺失 $n" Red; $missing += $n }
}
if ($missing.Count -gt 0) { throw "运行时缺少关键依赖，打包中止" }

# 3f 真正的验收 —— 让便携运行时自己去 import 一遍整个程序。
#     比猜路径可靠得多：路径对不对不重要，能跑起来才算数。
Say "      功能自检（用便携运行时实际 import 整个程序）..."
$probePath = Join-Path $rt "_selftest.py"
$probe = @'
import os, sys
rt  = os.path.dirname(os.path.abspath(__file__))
pkg = os.path.dirname(rt)
sys.path.insert(0, os.path.join(pkg, "app"))

# 1) 隔离性：绝不能用到系统 Python
if not os.path.abspath(sys.prefix).lower().startswith(os.path.abspath(rt).lower()):
    print("FAIL isolation: prefix =", sys.prefix); sys.exit(1)
for p in sys.path:
    if p and "Python3" in p and os.path.abspath(rt).lower() not in p.lower():
        print("FAIL leaked path:", p); sys.exit(1)

# 2) 依赖是否齐全
import tkinter, tkinter.ttk, tkinter.filedialog, tkinter.messagebox, tkinter.font
import subprocess, hashlib, ctypes, queue, threading, dataclasses
import fnmatch, re, shutil, json, tempfile, traceback, argparse

# 2b) Qt 主界面 —— 无头模式下导入，确认 PySide6 真的随包可用
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import PySide6
from PySide6 import QtCore, QtGui, QtWidgets

import backup_core, partition_profiles
import backup_gui_qt

# 2c) Qt 版能真的把窗口建起来吗（无头）—— 比单纯 import 可靠得多
_qapp = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
_app = backup_gui_qt.BackupApp()
# BackupApp 本身不是 QWidget，它内部持有 self.window
_win = _app if isinstance(_app, QtWidgets.QWidget) else None
if _win is None:
    for _n in ("window", "root", "win", "main_window", "ui"):
        _c = getattr(_app, _n, None)
        if isinstance(_c, QtWidgets.QWidget):
            _win = _c; break
if _win is None:
    _cands = [v for v in vars(_app).values() if isinstance(v, QtWidgets.QWidget)]
    _win = (_cands or [None])[0]
if _win is None:
    print("FAIL qt-window: BackupApp 里找不到主窗口"); sys.exit(1)

_win.resize(1060, 900)
_win.show()
for _ in range(14):
    _qapp.processEvents()
_n = len(_win.findChildren(QtWidgets.QWidget))
if _n < 40:
    print("FAIL qt-widgets: only %d widgets" % _n); sys.exit(1)
_win.close()
for _ in range(6):
    _qapp.processEvents()

# 3) adb 是否随包携带
adb = backup_gui_qt.find_adb() if hasattr(backup_gui_qt, "find_adb") else None
if not adb:
    import backup_gui
    adb = backup_gui.find_adb()
if not adb:
    print("FAIL adb not found"); sys.exit(1)

print("PORTABLE_OK|python=%s|Qt=%s|PySide=%s|Tk=%s|widgets=%d|app=%s|adb=%s" % (
    sys.version.split()[0], QtCore.qVersion(), PySide6.__version__, tkinter.TkVersion,
    _n, backup_gui_qt.APP_VERSION, os.path.relpath(adb, pkg)))
'@
[System.IO.File]::WriteAllText($probePath, $probe, (New-Object System.Text.UTF8Encoding $false))

# ⚠️ 原生命令往 stderr 写东西时，`2>&1` 会让 PowerShell 把它包成 ErrorRecord；
#    在 $ErrorActionPreference="Stop" 下这会直接中断脚本（Qt 的字体警告就踩过这个坑）。
#    所以这里临时放宽，只认退出码与 stdout 里的标记。
$__eap = $ErrorActionPreference
$ErrorActionPreference = "Continue"
$out = & (Join-Path $rt "python.exe") $probePath 2>&1
$code = $LASTEXITCODE
$ErrorActionPreference = $__eap
Remove-Item $probePath -Force -ErrorAction SilentlyContinue
$outText = ($out | Out-String).Trim()
if ($code -ne 0 -or $outText -notmatch "PORTABLE_OK") {
    Say "        $outText" Red
    throw "便携运行时功能自检失败（退出码 $code）"
}
Say "        $outText" Green

# ---------------------------------------------------------------- 4. 启动器
Say "[4/5] 生成启动器 ..." Cyan

$batMain = @'
@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0"
title Android Partition Backup Tool

if not exist "runtime\pythonw.exe" (
    echo.
    echo [ERROR] runtime\pythonw.exe not found.
    echo         The portable package is incomplete.
    echo.
    pause
    exit /b 1
)
if not exist "adb\adb.exe" (
    echo.
    echo [WARN] adb\adb.exe not found - the tool will try system PATH instead.
    echo.
)

start "" "runtime\pythonw.exe" "app\backup_gui_qt.py"
exit /b 0
'@
[System.IO.File]::WriteAllText((Join-Path $PkgRoot "启动备份工具.bat"), $batMain,
                               (New-Object System.Text.ASCIIEncoding))

$batDebug = @'
@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0"
title Android Partition Backup Tool - DEBUG (console visible)

echo ============================================================
echo  DEBUG MODE - console stays open, all errors are visible
echo ============================================================
echo.
"runtime\python.exe" "app\backup_gui_qt.py"
echo.
echo ------------------------------------------------------------
echo Program exited with code %ERRORLEVEL%
echo ------------------------------------------------------------
pause
'@
[System.IO.File]::WriteAllText((Join-Path $PkgRoot "调试启动(显示错误).bat"), $batDebug,
                               (New-Object System.Text.ASCIIEncoding))
Say "      启动备份工具.bat        （正常使用，无控制台窗口）"
Say "      调试启动(显示错误).bat  （出问题时用，能看到报错）"

# ---------------------------------------------------------------- 5. 说明
Say "[5/5] 生成说明文件 ..." Cyan
# ⚠️ DISCLAIMER.md 必须一起带上：README.md 顶部那条警告直接指向它，
#    漏掉的话用户解压后照着 README 去找这个文件会找不到。（踩过一次）
foreach ($f in @("README.md", "DISCLAIMER.md")) {
    Copy-Item (Join-Path $ToolDir $f) $PkgRoot -Force -ErrorAction SilentlyContinue
}

# ---------------------------------------------------------------- 结果
Say ""
Say "================================================================" Green
Say "  打包完成" Green
Say "================================================================" Green
Say "  位置: $PkgRoot"
Say ""
Say "  目录结构:"
Get-ChildItem $PkgRoot | Sort-Object Name | ForEach-Object {
    if ($_.PSIsContainer) { Say ("    {0,-22} {1,8} MB" -f ($_.Name + "\"), (MB $_.FullName)) }
    else                  { Say ("    {0,-22} {1,8} KB" -f $_.Name, [math]::Round($_.Length/1KB,1)) }
}
Say ""
Say ("  合计: {0} MB" -f (MB $PkgRoot)) Green
Say ""
