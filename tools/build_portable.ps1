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
# 兼容两种布局：脚本与源码同目录（开发目录），或脚本在 tools/ 下（仓库布局）
$ToolDir = if ((Split-Path $Here -Leaf) -eq "tools") { Split-Path $Here -Parent } else { $Here }
$WsRoot  = Split-Path $ToolDir -Parent
$PkgRoot = Join-Path $WsRoot "安卓分区备份工具_便携版"

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
foreach ($f in @("backup_gui.py", "backup_core.py", "partition_profiles.py")) {
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
foreach ($f in @("backup_gui.py", "backup_core.py", "partition_profiles.py")) {
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

# 3d Tcl/Tk 运行时（tkinter 必需）
$tclDst = Join-Path $rt "tcl"
robocopy (Join-Path $PyHome "tcl") $tclDst /E /NFL /NDL /NJH /NJS /NP /R:1 /W:1 | Out-Null
Say "      tcl/  ($(MB $tclDst) MB)"

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
import backup_core, partition_profiles, backup_gui

# 3) adb 是否随包携带
adb = backup_gui.find_adb()
if not adb:
    print("FAIL adb not found"); sys.exit(1)

print("PORTABLE_OK|python=%s|Tk=%s|app=%s|adb=%s" % (
    sys.version.split()[0], tkinter.TkVersion, backup_gui.APP_VERSION,
    os.path.relpath(adb, pkg)))
'@
[System.IO.File]::WriteAllText($probePath, $probe, (New-Object System.Text.UTF8Encoding $false))

$out = & (Join-Path $rt "python.exe") $probePath 2>&1
$code = $LASTEXITCODE
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

start "" "runtime\pythonw.exe" "app\backup_gui.py"
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
"runtime\python.exe" "app\backup_gui.py"
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
Copy-Item (Join-Path $ToolDir "README.md") $PkgRoot -Force -ErrorAction SilentlyContinue

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
