# -*- coding: utf-8 -*-
<#
================================================================================
  跑全部测试
================================================================================
  用法：
      powershell -ExecutionPolicy Bypass -File tools\run_tests.ps1

  为什么要跑【两轮】：
      测试会真实创建 Backups\ 目录。第一轮跑完后目录就存在了，
      第二轮便会走进「同名冲突 → 追加日期」这条分支。
      之前一个 flaky 测试正是只在第二轮才失败 —— 只跑一轮会漏掉。
================================================================================
#>

$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = if ((Split-Path $Here -Leaf) -eq "tools") { Split-Path $Here -Parent } else { $Here }
Set-Location $Root

function Say($m, $c = "Gray") { Write-Host $m -ForegroundColor $c }

# ---- 找 Python ----
$Py = $null
foreach ($c in @("python", "python3")) {
    $cmd = Get-Command $c -ErrorAction SilentlyContinue
    if ($cmd) {
        & $c -c "import tkinter" 2>$null
        if ($LASTEXITCODE -eq 0) { $Py = $c; break }
    }
}
if (-not $Py) {
    Say "找不到带 tkinter 的 Python。请先安装 Python 3.8+（官方安装包默认含 tkinter）。" Red
    exit 1
}
$ver = (& $Py --version 2>&1)
Say ""
Say "================================================================" Cyan
Say "  安卓分区备份工具 —— 测试" Cyan
Say "================================================================" Cyan
Say "  Python : $ver  ($((Get-Command $Py).Source))"
Say "  目录   : $Root"
Say ""

# ---- 非交互环境要禁用分页，否则脚本会挂住 ----
$env:PYTHONIOENCODING = "utf-8"

# ---- 兼容两种目录布局 ----
# 开发目录用 _tests\ + smoke_gui.py，仓库发布布局用 tests\ + test_gui_smoke.py。
# 两种布局都能跑，省得每次同步还要改名。
function Find-Test([string[]]$candidates) {
    foreach ($c in $candidates) { if (Test-Path $c) { return $c } }
    return $null
}

$suites = @(
    @{ name = "命名与冲突消解"
       path = Find-Test @("tests\test_naming.py", "_tests\test_naming.py")
       rounds = 1 }
    @{ name = "进度条组件"
       path = Find-Test @("tests\test_progress.py", "_tests\test_progress.py")
       rounds = 1 }
    @{ name = "环境包完整性校验"
       path = Find-Test @("tests\test_env_tar.py", "_tests\test_env_tar.py")
       rounds = 1 }
    # GUI 冒烟跑两轮 —— 第二轮必然触发 Backups\ 同名冲突分支
    @{ name = "图形界面 + 进度状态机"
       path = Find-Test @("tests\test_gui_smoke.py", "_tests\smoke_gui.py")
       rounds = 2 }
)

$totalFail = 0
foreach ($s in $suites) {
    if (-not (Test-Path $s.path)) {
        Say ("  [!] 找不到 {0}，跳过" -f $s.path) Yellow
        continue
    }
    for ($i = 1; $i -le $s.rounds; $i++) {
        $tag = if ($s.rounds -gt 1) { "$($s.name)（第 $i/$($s.rounds) 轮）" } else { $s.name }
        $out = & $Py $s.path 2>&1
        $code = $LASTEXITCODE

        $fails = $out | Select-String "\[FAIL\]"
        $crash = $out | Select-String "Traceback \(most recent call last\)"
        $res   = $out | Select-String "结果：" | Select-Object -Last 1

        if ($crash) {
            Say ("  [崩溃] {0}" -f $tag) Red
            $out | Select-Object -Last 12 | ForEach-Object { Say "         $_" DarkGray }
            $totalFail++
        } elseif ($fails) {
            Say ("  [失败] {0}   （{1} 项）" -f $tag, $fails.Count) Red
            $fails | ForEach-Object { Say "         $($_.Line.Trim())" DarkGray }
            $totalFail++
        } elseif ($code -ne 0) {
            Say ("  [异常退出 code=$code] {0}" -f $tag) Red
            $out | Select-Object -Last 8 | ForEach-Object { Say "         $_" DarkGray }
            $totalFail++
        } else {
            $line = if ($res) { $res.Line.Trim() } else { "通过" }
            Say ("  [OK]   {0,-34} {1}" -f $tag, $line) Green
        }
    }
}

# ---- 清理测试残留（否则下次跑会一直走冲突分支，且污染仓库）----
foreach ($d in @("Backups", "tests\_test_tmp", "_tests\_test_tmp",
                 "__pycache__", "tests\__pycache__", "research\__pycache__")) {
    if (Test-Path $d) { Remove-Item $d -Recurse -Force -ErrorAction SilentlyContinue }
}

Say ""
Say "================================================================" $(if ($totalFail) { "Red" } else { "Green" })
if ($totalFail) {
    Say "  $totalFail 个测试套件有问题" Red
} else {
    Say "  全部通过 ✅" Green
}
Say "================================================================" $(if ($totalFail) { "Red" } else { "Green" })
Say ""
exit $totalFail
