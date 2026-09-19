# -*- coding: utf-8 -*-
<#
================================================================================
  构建发布包
================================================================================
  产出两个 zip，覆盖「目标电脑有没有 Python」两种情况：

     ① android-partition-backup-v2.0.0-portable.zip
        含 Python 运行时 + ADB。任何 Windows 10/11 x64 解压即用，零依赖。

     ② android-partition-backup-v2.0.0-script.zip
        只有 .py + ADB。适合本机已装 Python 的场景，体积小一个数量级。

  用法： powershell -ExecutionPolicy Bypass -File _build_release.ps1
================================================================================
#>

$ErrorActionPreference = "Stop"

$Here    = Split-Path -Parent $MyInvocation.MyCommand.Path
$ToolDir = $Here
$WsRoot  = Split-Path (Split-Path $Here -Parent) -Parent
$PortDir = Join-Path $WsRoot "android-partition-backup-portable"      # 已构建好的便携版
$Ver     = "2.0.0"
$RelRoot = Join-Path $WsRoot "发布包"                                  # 各版本一个子目录
$RelDir  = Join-Path $RelRoot "v$Ver"                                  # 本次的输出目录

function Say($m, $c = "Gray") { Write-Host $m -ForegroundColor $c }
function MB($p) {
    if (-not (Test-Path $p)) { return 0 }
    return [math]::Round(((Get-ChildItem $p -Recurse -File -EA SilentlyContinue |
        Measure-Object -Property Length -Sum).Sum) / 1MB, 1)
}

Say ""
Say "================================================================" Cyan
Say "  构建发布包 v$Ver" Cyan
Say "================================================================" Cyan
Say ""

if (-not (Test-Path $PortDir)) { throw "找不到便携版目录，请先运行 _build_portable.ps1" }
# 逐个删除并容忍占用。**只清理本次版本自己的子目录** —— 发布包根目录下
# 其它版本的子目录必须原样保留，那是历史归档。
# 整目录 Remove-Item -Recurse -Force 遇到任何一个被占用的文件就会抛
# IOException 并直接中断构建 —— 而发布包很容易正被资源管理器预览、
# 或被聊天工具/压缩软件打开着。删不掉的就留着，交给下面的
# Resolve-WritablePath 换名输出。
if (Test-Path $RelDir) {
    Get-ChildItem $RelDir -Recurse -Force |
        Sort-Object FullName -Descending |
        ForEach-Object { Remove-Item $_.FullName -Recurse -Force -ErrorAction SilentlyContinue }
}
New-Item -ItemType Directory -Force -Path $RelDir | Out-Null

# 目标文件被占用时换一个带 _new 后缀的名字，不要中断构建
function Resolve-WritablePath([string]$path) {
    if (-not (Test-Path $path)) { return $path }
    try {
        $fs = [System.IO.File]::Open($path, 'Open', 'ReadWrite', 'None')
        $fs.Close()
        return $path
    } catch {
        $dir  = Split-Path $path -Parent
        $base = [System.IO.Path]::GetFileNameWithoutExtension($path)
        $ext  = [System.IO.Path]::GetExtension($path)
        $alt  = Join-Path $dir "$base`_new$ext"
        Say ("  [!] {0} 正被其它程序占用，改写入 {1}" -f `
             (Split-Path $path -Leaf), (Split-Path $alt -Leaf)) Yellow
        return $alt
    }
}

# 优先用 7-Zip（更快、压缩率更高），没有就退回系统自带 Compress-Archive
$SevenZip = $null
foreach ($c in @("$env:ProgramFiles\7-Zip\7z.exe",
                 "${env:ProgramFiles(x86)}\7-Zip\7z.exe",
                 "$env:LOCALAPPDATA\Programs\7-Zip\7z.exe")) {
    if (Test-Path $c) { $SevenZip = $c; break }
}
if (-not $SevenZip) {
    $w = Get-Command 7z -ErrorAction SilentlyContinue
    if ($w) { $SevenZip = $w.Source }
}
Say ("  压缩工具: " + $(if ($SevenZip) { "7-Zip ($SevenZip)" } else { "系统自带 Compress-Archive" }))
Say ""

# ==============================================================================
#  ① 脚本版
# ==============================================================================
Say "[1/3] 构建脚本版 ..." Cyan
# ⚠️ 暂存目录必须放在工作区内 —— 本机沙箱禁止在系统 TEMP 下创建子目录
$ScriptPkg = Join-Path $RelDir "_stage_script"
$appDir = Join-Path $ScriptPkg "app"
New-Item -ItemType Directory -Force -Path $appDir | Out-Null

foreach ($f in @("backup_gui_qt.py", "backup_gui.py", "backup_core.py", "partition_profiles.py")) {
    Copy-Item (Join-Path $ToolDir $f) $appDir -Force
}
Copy-Item (Join-Path $ToolDir "README.md") $ScriptPkg -Force

# ADB 也一起带上，省得用户自己配
$adbDir = Join-Path $ScriptPkg "adb"
New-Item -ItemType Directory -Force -Path $adbDir | Out-Null
$AdbSrc = Join-Path $WsRoot "adb"
foreach ($f in @("adb.exe", "AdbWinApi.dll", "AdbWinUsbApi.dll", "NOTICE.txt")) {
    $s = Join-Path $AdbSrc $f
    if (Test-Path $s) { Copy-Item $s $adbDir -Force }
}
Say ("      程序 + adb  ({0} MB)" -f (MB $ScriptPkg))

# ---- 启动器（纯 ASCII，避免 cmd.exe 编码坑）----
$bat = @'
@echo off
cd /d "%~dp0"
title Android Partition Backup

set "PYEXE="
for %%C in (pythonw.exe pyw.exe python.exe py.exe) do (
    if not defined PYEXE (
        where %%C >nul 2>&1 && set "PYEXE=%%C"
    )
)

if not defined PYEXE (
    echo.
    echo  [ERROR] Python not found on this computer.
    echo.
    echo  This build requires Python 3.9+ and PySide6 already installed.
    echo    Download : https://www.python.org/downloads/
    echo    Remember to tick "Add python.exe to PATH" during setup.
    echo.
    echo  No Python? Use the FULL PORTABLE build instead -
    echo  it bundles its own Python and needs nothing installed.
    echo.
    pause
    exit /b 1
)

echo  Using: %PYEXE%
%PYEXE% -c "import PySide6" 2>nul
if errorlevel 1 (
    echo.
    echo  [ERROR] PySide6 not found.
    echo.
    echo  This build needs PySide6 in addition to Python:
    echo      %PYEXE% -m pip install PySide6
    echo.
    echo  No Python/PySide6? Use the FULL PORTABLE build instead -
    echo  it bundles everything and needs nothing installed.
    echo.
    pause
    exit /b 1
)
start "" %PYEXE% "app\backup_gui_qt.py"
exit /b 0
'@
[System.IO.File]::WriteAllText((Join-Path $ScriptPkg "启动备份工具.bat"), $bat,
                               (New-Object System.Text.ASCIIEncoding))

$batDbg = @'
@echo off
cd /d "%~dp0"
title Android Partition Backup - DEBUG

echo ============================================================
echo  DEBUG MODE - console stays open, all errors visible
echo ============================================================
echo.
where python >nul 2>&1 && python --version
echo.
python "app\backup_gui_qt.py"
echo.
echo ------------------------------------------------------------
echo Exited with code %ERRORLEVEL%
echo ------------------------------------------------------------
pause
'@
[System.IO.File]::WriteAllText((Join-Path $ScriptPkg "调试启动(显示错误).bat"), $batDbg,
                               (New-Object System.Text.ASCIIEncoding))

$readme = @'
脚本版说明
================================================================================
本版本【需要电脑上已安装 Python 3.9 或更高版本，以及 PySide6】。

如果你不想装 Python，请改用「完整便携版」——
那个版本自带 Python 运行时与 Tcl/Tk，解压双击即可，什么都不用装。

需要什么
    Python 3.9+，并且要有 PySide6（图形界面框架）
    下载 Python: https://www.python.org/downloads/
    安装时务必勾选 "Add python.exe to PATH"
    再装 PySide6: 在命令行运行  python -m pip install PySide6

怎么用
    双击「启动备份工具.bat」
    如果没反应或闪退，双击「调试启动(显示错误).bat」看具体报错

已经自带的东西
    adb\  —— 完整的 ADB，不需要你另外装 platform-tools
    程序本体在 app\ 下，三个 .py 文件，零第三方依赖

常见问题
    Q: 提示 Python not found
    A: Python 没装，或者装的时候没勾 "Add python.exe to PATH"。
       重装一次并勾上，或者直接用完整便携版。

    Q: 提示 No module named PySide6
    A: 没装图形界面框架。在命令行运行：
          python -m pip install PySide6
       装完再启动即可。（约 70 MB，装一次就够）
'@
[System.IO.File]::WriteAllText((Join-Path $ScriptPkg "使用说明.txt"), $readme,
                               (New-Object System.Text.UTF8Encoding $true))
Say "      启动器 + 说明"

# ---- Linux / macOS 启动脚本 ----
# ⚠️ 必须用 LF 换行且不加 BOM，否则 Linux 上 shebang 会解析失败
$sh = @'
#!/bin/sh
# 安卓分区备份工具 - Linux / macOS 启动脚本
cd "$(dirname "$0")" || exit 1

PY=""
for c in python3 python; do
    if command -v "$c" >/dev/null 2>&1; then
        if "$c" -c "import PySide6" >/dev/null 2>&1; then PY="$c"; break; fi
    fi
done

if [ -z "$PY" ]; then
    echo "错误：找不到带 PySide6 的 Python 3。"
    echo "  任意平台 : python3 -m pip install PySide6"
    exit 1
fi

if [ ! -x "adb/adb" ] && ! command -v adb >/dev/null 2>&1; then
    echo "提示：未找到 adb。请安装 android-tools / platform-tools，"
    echo "      或把对应平台的 adb 放进本目录的 adb/ 下。"
fi

exec "$PY" "app/backup_gui_qt.py" "$@"
'@
$sh = $sh -replace "`r`n", "`n"
[System.IO.File]::WriteAllText((Join-Path $ScriptPkg "run.sh"), $sh,
                               (New-Object System.Text.UTF8Encoding $false))
Say "      启动器 + 说明 + run.sh(Linux/macOS)"

# ==============================================================================
#  打包
# ==============================================================================
function New-Zip($src, $dst, $tmpName) {
    if (Test-Path $dst) { Remove-Item $dst -Force }
    if ($SevenZip) {
        & $SevenZip a -tzip -mx=9 -bso0 -bsp0 "$dst" "$src\*" | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "7z 压缩失败: $src" }
    } else {
        Compress-Archive -Path "$src\*" -DestinationPath $dst -CompressionLevel Optimal -Force
    }
    return [math]::Round((Get-Item $dst).Length / 1MB, 1)
}

Say ""
Say "[2/3] 压缩脚本版 ..." Cyan
$zipScript = Resolve-WritablePath (Join-Path $RelDir "android-partition-backup-v$Ver-script.zip")
$sz1 = New-Zip $ScriptPkg $zipScript
Say ("      {0}  ({1} MB)" -f (Split-Path $zipScript -Leaf), $sz1) Green
Remove-Item $ScriptPkg -Recurse -Force -EA SilentlyContinue

Say ""
Say "[3/3] 压缩完整便携版（47 MB，需要一点时间）..." Cyan
$zipFull = Resolve-WritablePath (Join-Path $RelDir "android-partition-backup-v$Ver-portable.zip")
$sz2 = New-Zip $PortDir $zipFull
Say ("      {0}  ({1} MB)" -f (Split-Path $zipFull -Leaf), $sz2) Green

# ==============================================================================
#  校验：把 zip 解开确认能跑
# ==============================================================================
Say ""
Say "校验：解压后能否正常运行 ..." Cyan
$verifyDir = Join-Path $RelDir "_verify"
New-Item -ItemType Directory -Force -Path $verifyDir | Out-Null
try {
    if ($SevenZip) {
        & $SevenZip x -o"$verifyDir" -bso0 -bsp0 $zipFull | Out-Null
    } else {
        Expand-Archive -Path $zipFull -DestinationPath $verifyDir -Force
    }
    $rtPy = Join-Path $verifyDir "runtime\python.exe"
    if (-not (Test-Path $rtPy)) { throw "解压后找不到 runtime\python.exe" }

    $probe = Join-Path $verifyDir "_probe.py"
    @'
import os, sys
rt  = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(rt, "app"))
if not os.path.abspath(sys.prefix).lower().startswith(os.path.abspath(rt).lower()):
    print("FAIL isolation"); sys.exit(1)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import subprocess, hashlib, ctypes, queue, threading, dataclasses
import PySide6
from PySide6 import QtCore, QtWidgets
import backup_core, partition_profiles, backup_gui_qt
adb = backup_gui_qt.find_adb()
print("UNZIP_OK|py=%s|Qt=%s|PySide=%s|app=%s|adb=%s" % (
    sys.version.split()[0], QtCore.qVersion(), PySide6.__version__,
    backup_gui_qt.APP_VERSION,
    os.path.relpath(adb, rt) if adb else "NOT_FOUND"))
'@ | Set-Content -Path $probe -Encoding UTF8
    $out = & $rtPy $probe 2>&1
    $code = $LASTEXITCODE
    $txt = ($out | Out-String).Trim()
    if ($code -ne 0 -or $txt -notmatch "UNZIP_OK") {
        Say "      $txt" Red
        throw "解压后的便携版自检失败"
    }
    Say ("      $txt") Green

    # 再确认脚本版的 .py 齐全
    if ($SevenZip) { & $SevenZip x -o"$verifyDir\_s" -bso0 -bsp0 $zipScript | Out-Null }
    else { Expand-Archive -Path $zipScript -DestinationPath "$verifyDir\_s" -Force }
    foreach ($f in @("app\backup_gui_qt.py", "app\backup_gui.py", "app\backup_core.py",
                     "app\partition_profiles.py", "adb\adb.exe",
                     "启动备份工具.bat", "使用说明.txt", "run.sh")) {
        if (-not (Test-Path (Join-Path "$verifyDir\_s" $f))) { throw "脚本版缺少 $f" }
    }
    # run.sh 必须无 BOM 且全 LF，否则 Linux 上跑不起来
    $shBytes = [System.IO.File]::ReadAllBytes((Join-Path "$verifyDir\_s" "run.sh"))
    if ($shBytes.Length -ge 3 -and $shBytes[0] -eq 0xEF -and $shBytes[1] -eq 0xBB) {
        throw "run.sh 带了 BOM，Linux 会报错"
    }
    if ($shBytes -contains 0x0D) { throw "run.sh 含 CR 字符，Linux 会报错" }
    Say "      脚本版内容齐全（run.sh 编码与换行校验通过）" Green
} finally {
    Remove-Item $verifyDir -Recurse -Force -EA SilentlyContinue
}

# ==============================================================================
Say ""
Say "================================================================" Green
Say "  发布包就绪" Green
Say "================================================================" Green
Say "  版本目录: $RelDir"
Say "  发布根目录: $RelRoot"
Say ""

# 清掉暂存与校验目录，只留两个 zip
foreach ($d in @($ScriptPkg, $verifyDir)) {
    if (Test-Path $d) { Remove-Item $d -Recurse -Force -EA SilentlyContinue }
}

# 放一份选型说明在发布包里，省得以后忘了哪个是哪个
$pick = @"
安卓分区备份工具 v$Ver — 该用哪个？
================================================================================

【完整便携版】  android-partition-backup-v$Ver-portable.zip
    自带 Python 运行时(3.14.7 + Tcl/Tk 9.0) 与 ADB。
    目标电脑【不需要装任何东西】，解压 → 双击「启动备份工具.bat」即可。
    适合：给别人用 / 装到 U 盘随身带 / 电脑上没有 Python。
    要求：Windows 10 或 11，64 位。

【脚本版】      android-partition-backup-v$Ver-script.zip
    只有 .py 源码 + ADB，【需要目标电脑已装 Python 3.9+ 与 PySide6】。
    体积小 5 倍，且跨平台 —— 附了 run.sh，Linux / macOS 也能跑。
    适合：自己的电脑已经装了 Python / 想改代码 / 想省空间。

--------------------------------------------------------------------------------
为什么没有单个 .exe？
    打包 exe 需要 PyInstaller，而本机构建时网络不通（代理未运行，
    镜像站也连不上），无法下载。不过这不影响使用：

    完整便携版里的 runtime\python.exe 就是解释器本身，
    配上启动器效果与 exe 完全等同 —— 一样是解压双击，一样不装任何东西。

    而且 exe 在这里反而有个隐患：自解压类 exe 通常解压到临时目录，
    备份文件也会跟着落到临时目录里，重启就被清理掉了。
    用 zip 解压到你选定的位置，备份才会稳稳地待在原地。

--------------------------------------------------------------------------------
备份文件会存到哪？
    解压目录下的 Backups\ 子文件夹里。
    所以解压位置要选一个长期保留、空间够的地方（别放临时目录）。

    同一台设备同名再备份会自动加日期，绝不会覆盖已有备份。
"@
[System.IO.File]::WriteAllText((Join-Path $RelDir "WHICH-VERSION.txt"), $pick,
                               (New-Object System.Text.UTF8Encoding $true))

# 列本次版本目录的产物 —— 必须放在 WHICH-VERSION.txt 写完之后，
# 否则清单里会漏掉它。（踩过一次）
Say ""
Say "  本版本目录产物:" Yellow
Get-ChildItem $RelDir | Sort-Object Name | ForEach-Object {
    Say ("    {0,-46} {1,7} MB" -f $_.Name, [math]::Round($_.Length/1MB,1))
}

# 打印发布包根目录下已归档的所有版本，一眼能看出攒了哪些
$versions = @(Get-ChildItem $RelRoot -Directory -EA SilentlyContinue |
              Sort-Object Name -Descending)
if ($versions.Count -gt 1) {
    Say ""
    Say "  发布包内已归档的版本:" Yellow
    foreach ($v in $versions) {
        $n = @(Get-ChildItem $v.FullName -File -EA SilentlyContinue).Count
        $s = (Get-ChildItem $v.FullName -File -EA SilentlyContinue |
              Measure-Object Length -Sum).Sum
        $mark = if ($v.Name -eq "v$Ver") { " <- 本次" } else { "" }
        Say ("    {0,-10} {1} 个文件  {2,7:N2} MB{3}" -f $v.Name, $n, ($s/1MB), $mark)
    }
}

Say ""
Say "  选哪个？" Yellow
Say "    · 目标电脑【没装 Python】 → 完整便携版（解压双击，什么都不用装）"
Say "    · 目标电脑【已装 Python】 → 脚本版（体积小 5 倍，且支持 Linux/macOS）"
Say ""
Say "  系统要求: 便携版 Windows 10/11 x64；脚本版 任意有 Python 3.9+ 与 PySide6 的平台" Yellow
Say ""
