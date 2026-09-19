# Android Partition Backup

[![中文](https://img.shields.io/badge/README-%E4%B8%AD%E6%96%87-d73a49?style=flat-square)](README.md) [![English](https://img.shields.io/badge/README-English-0969da?style=flat-square&logo=readthedocs&logoColor=white)](README.en.md)

> 🌐 **This page is the English version.** Prefer Chinese? → **[阅读中文文档 (README.md)](README.md)**

[![Vibe Coding](https://img.shields.io/badge/Vibe%20Coding-100%25-ff69b4?style=flat-square)](#-about-this-project)
[![Made with AI](https://img.shields.io/badge/Made%20with-AI%20pair%20programming-8a2be2?style=flat-square)](#-about-this-project)
[![Python](https://img.shields.io/badge/Python-3.8%2B%20%7C%20%E9%9B%B6%E4%BE%9D%E8%B5%96-3776ab?style=flat-square)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/Platform-Android%20(arm64)-3ddc84?style=flat-square)](#)
[![License](https://img.shields.io/badge/License-GPLv3-blue?style=flat-square)](LICENSE)

**In one sentence**: back up the partitions on your Android phone that would be gone forever if lost — with a single click, onto your computer.

**Works on any device** — it is not tied to any particular model; any rooted Android phone will do. The platform (Qualcomm / MediaTek / Samsung / Unisoc / Tensor) and each partition's value are **inferred automatically from semantics**, so there is no per-device config to write.

> 🌐 **Language note** — the GUI and every message it prints are currently **Chinese only**.
> This English README documents the tool in full, but the mock-ups and log samples below
> keep the **real Chinese strings verbatim**, so you can match them against what actually
> appears on screen. An English UI translation would be a welcome contribution.

---

## ⚡ About This Project

> **This is a 100% Vibe Coding project.**

Why the project was started: to back up the important partitions of an Android phone.

The project was completed by DeepSeek.

***AI can make mistakes. This is for vibe coding learning purposes only — please do not use it in industrial production or other critical industries. Any losses are your own responsibility!!!***

> ⚠️ **Please be sure to read [`DISCLAIMER.md`](DISCLAIMER.md) before use** — writing to a partition is the only way to brick a device, and restore operations carry extremely high risk.

| Stage | What the AI did | What the human did |
|---|---|---|
| **Research** | Searched GitHub for similar projects, confirmed that a "GUI multi-select backup tool" was a gap in the market | Set the direction of the requirements and scoped the features |
| **Feasibility check** | Wrote a spike script to measure binary integrity of `adb exec-out` vs `adb shell` — found that the PTY corrupts 76739 bytes of 32 MB of data into `\r\n` | Provided a real device (Redmi K70) for testing |
| **Performance tuning** | Compared 4 data-fetch methods × 2 write paths, chose the best combination at 14.8 MB/s | — |
| **Coding** | Three modules, roughly 3000 lines, zero third-party dependencies | Reviewed round by round, made the calls on trade-offs |
| **Code review** | Self-audit surfaced 4 real defects (including the classic Tkinter trap of reading variables from a worker thread) | — |
| **Automated tests** | 4 test suites, 87 assertions in total, including a progress-bar test that specifically covers the TTY branch (**used for development-time regression only — not shipped with the repo**) | — |
| **⭐ Manual testing** | Used human feedback to pinpoint root causes, fix them, and verify by regression | **A human ran the tests by hand**: walked the full GUI backup flow on a real device, and verified portable-version portability on a different computer (a VM) |
| **On-device validation** | Compared byte-for-byte against a manual backup with sha256, 18/18 identical | Ran end-to-end on a real device and signed off on the results |

**But keep it in perspective**: AI makes mistakes too. Real-device testing in the **early** days already caught defects the AI had written:

  1. A/B slot suffixes were not stripped, leaving 40+ partitions "unclassified"
  2. Whole-disk symlinks in by-name were treated as partitions
  3. The rule tables did not cover everything
  4. There was a risk of backup failure

Then, at the **human on-device acceptance** stage, 3 more defects surfaced that were subtler still and completely outside the reach of automated tests — see the next section 👇

### 🧑🔬 Testing is done by AI and humans **together**

The automated tests were written by the AI (4 suites / 87 assertions, **development-time only — not shipped with the repo**), but **manual testing was not a formality — it was the main bug-catcher**.

The 3 defects below were **not covered by a single one** of the AI's 87 assertions; every one of them was exposed by **a human testing by hand**:

| # | What the human did | The defect it exposed |
|---|---|---|
| 1 | Ran the **complete GUI backup flow** on a real device | The GPT tail backup of all 6 disks **failed**, yet the UI showed only "passed 20/26 items". The root cause was treating a Windows local path as a device path — a code path the AI's regression tests had never actually exercised |
| 2 | Ran the full portable version **on a different computer** (a VM) | The `/data/adb` environment bundle failed (39/40). The true behaviour of the portable package in an **unfamiliar environment** can only be verified by switching machines |
| 3 | Examined the UI feedback on failure | When the environment bundle failed, **the reason was invisible** — just a red X → this led to the addition of streaming output and integrity verification |

> 📌 **Conclusion**: automated tests prevent regressions, but they **cannot find the "the AI assumed it works that way" class of mistake**.
> What makes this tool safe to hand to others is that **a human actually went and used it**.

**All code has been validated on a real device (Redmi K70). On other phones, please assess the risk yourself before using it to back up important data (correct operation on other phones is not guaranteed).**

---

## 1. Quick Start

### Requirements

- **Python 3.9+** and **PySide6** (`python -m pip install PySide6`)
- **ADB** — download it from [platform-tools](https://developer.android.com/tools/releases/platform-tools),
  put it at `adb/adb.exe` (Windows) or `adb/adb` (Linux/macOS), or just add it to your system PATH
- A **rooted** Android phone (Magisk / KernelSU / APatch all work)

> This repository does not include the ADB binary (an 8 MB binary in git history can never be slimmed down again), so please download it yourself.
>
> 💡 **Don't want to install Python and PySide6?** Use the **portable build** from the releases page — it bundles its own Python runtime and Qt,
> so you just unzip it and double-click. Nothing to install.

### Running

```bash
git clone https://github.com/2567922141/android-partition-backup.git
cd android-partition-backup

# Put the adb from platform-tools here (pick one)
#   A. Copy it to adb/adb.exe
#   B. Add it to your system PATH
#   C. Do nothing — the program finds it on PATH automatically

python -m pip install PySide6   # the one and only third-party dependency
python backup_gui_qt.py         # launch the GUI
```

Linux / macOS users can use the bundled launch script directly (it picks a Python that has PySide6):

```bash
./run.sh
```

Command-line version (no GUI, good for scripting):

```bash
python backup_core.py --adb <path-to-adb> --preset critical --out <output-dir>
python backup_core.py --adb <path-to-adb> --list        # just list the partitions and their tiers
```

### Preparing the phone

1. Settings → About phone → tap "Build number" 7 times to enable Developer options
2. Developer options → turn on **USB debugging**
3. Connect the phone to the computer with a data cable; when "Allow USB debugging" pops up → **check "Always allow" and confirm**
4. **Unlock the phone screen** (while the screen is locked, adb is often blocked by the system)
5. Make sure the phone is rooted, and grant **Shell** permission in your root manager

The program detects the device state automatically and tells you in the UI which step you are stuck on.

---

## 2. What Gets Backed Up — Four Tiers

The program does not look at the model; it assigns tiers automatically **based on the semantics of the partition name**:

| Tier | Meaning | Typical partitions | What happens if lost |
|---|---|---|---|
| **★ Tier 1 Non-regenerable** | Contains device-unique data | `persist` `modemst1/2` `fsg` `nvdata` `efs` `devinfo` `frp` `secdata` | **Permanent damage — not even official firmware can bring it back** |
| **Tier 2 root baseline** | System partitions you have patched | `init_boot_a/b` `boot_a/b` `vbmeta*` `dtbo*` | Only the unpatched version is official; root state is lost |
| **Tier 3 Rebuildable from firmware** | Present in the official flash package | `system` `vendor` `abl` `modem` `super` | Just reflash the official package |
| **Tier 4 Low value** | Vendor backup slots / logs / alignment | `bk*` `ALIGN*` `logfs` `oops` `rawdump` | Meaningless |

**Presets** (switchable with one click in the UI):

| Preset | Contents | Typical size |
|---|---|---|
| **Critical partitions (recommended)** | Tier 1 only | ~80 MB |
| **Critical + root baseline** | Tier 1 + Tier 2 (default) | ~1 GB |
| All valuable partitions | Tier 1+2+3 | A few GB to tens of GB |
| All partitions | Everything | Up to 200 GB+ |

> 📚 **The tiering rules have been externally verified**: the partition list in this document matches, item for item, the list independently compiled by [onfix.cn "Guide to Backing Up Phone Baseband/Firmware with Commands"](https://onfix.cn/course/4780?bid=1&mid=28642)
> (Qualcomm `fsg/fsc/modemst1/modemst2`; MediaTek `nvram/nvdata/nvcfg/persist/protect1/protect2/seccfg`).
> That source also corroborates the point that "MediaTek's by-name paths differ from Qualcomm's and require multi-path probing".

> ⚠️ `userdata` (personal data, can reach hundreds of GB), `super` (dynamic partition container), `metadata` (encryption metadata)
> are **never checked automatically by a preset** — you have to select them by hand, to avoid a "select all" casually dragging along hundreds of GB.

---

## 3. Where Backup Files Go

```
android-partition-backup-portable\
└── Backups\                        ← created automatically inside the program folder
    ├── MyBackup\                     ← the name you chose
    │   ├── .device_info            ← device fingerprint (used to detect "the same device")
    │   ├── README.md               ← auto-generated: device info + diff + file listing
    │   ├── manifest.txt            ← per-item SHA256
    │   ├── backup_log.txt          ← full log of this run
    │   ├── img\                    ← partition images
    │   └── gpt\                    ← GPT tables (head / tail / sgdisk format / layout text)
    └── MyBackup_20260918\            ← same name again → date appended
```

### Naming and Conflict Rules

You **customise** the name in the UI (leave it blank to use the default name `Backup`). When names collide, they are resolved in the following order, and **an existing backup is never overwritten**:

| Situation | Result |
|---|---|
| The name is not yet used | `MyBackup` |
| It exists and is the **same device** | `MyBackup_20260918` ← date appended |
| It exists but is a **different device** | `MyBackup_20260918` ← date appended as well, so two machines are never mixed together |
| Backed up again on the same day | `MyBackup_20260918_153000` ← hour/minute/second appended |
| Still a conflict | `MyBackup_20260918_153000_2` ← sequence number appended |

**The UI previews the final path in real time** and tells you before the backup why that name was chosen.

> 🛡️ Custom names go through safety sanitisation (`..\..\Windows` → `____Windows`),
> so a backup always lands inside the root directory you specified.

---

## 4. 🛡️ Safety Statement

**This tool performs read-only export only.**

| Item | Description |
|---|---|
| Read / write | For device partitions there is **only** the read `dd if=<partition>`; **no code path anywhere in the program writes to a device partition** |
| Writes on the device | The only writes are temporary files under `/sdcard/.apb_tmp` and `/data/local/tmp`, **deleted immediately after use** |
| Overwriting existing backups | **Impossible** — directories are created with `exist_ok=False`, and name clashes are renamed automatically |
| Command injection | Partition names are validated against the whitelist regex `^[A-Za-z0-9_.\-]{1,64}$` |
| Path traversal | Custom names are filtered for `/ \ ..` and illegal Windows characters |
| Hang protection | A stalled transfer is aborted automatically after 120 seconds (so a hung adb cannot cause an endless wait)|
| Cancel | Cancellable at any time; partition files already completed are kept |

**No restore function is provided** — writing to a partition is the only way to brick a device. Reference commands are written only into the `README.md` generated inside each **backup directory**; run them by hand if you ever need them.

This project absolutely does not leak any personal information.
---

## 5. Verification

The point of verification is **not** to compute more hashes — it is to make sure
**each layer catches a failure mode the previous layer cannot**. So the checkpoints
are deliberately placed at *different points* along the data path:

```
Device flash ──①device-side hash──▶ adb transfer ──②PC-side hash──▶ disk
                closes the transfer loop        confirms the write
```

> Running sha256 / sha1 / md5 in parallel is **not** "multiple layers" — they read
> the same bytes and catch the same class of error. **Only a different position
> counts as a layer.**

### ① Device-side hash (on by default) ⭐

The device computes its own `sha256sum /dev/block/by-name/<partition>` first, and it
is compared against the bytes received on the PC.

This is the **only** layer that can prove **the bytes on the device equal the bytes
on your disk**. Head/tail sampling, double reads, a second transfer path — everything
they could catch, this already catches.

**It costs essentially nothing**: UFS sequential reads run at 1–2 GB/s while adb
transfers typically run at 30–150 MB/s — so for the same partition the check takes
only a few percent of the transfer time.

Partitions above **1 GiB** are skipped automatically (by then you are dealing with
regenerable `super` / `userdata`).

| Partition | Size | Device-side check |
|---|---|---|
| `secdata` | 32 KB | ✅ checked |
| `frp` | 512 KB | ✅ checked |
| `modemst1` / `modemst2` / `fsg` | 8 MB | ✅ checked |
| `devinfo` | 16 MB | ✅ checked |
| `persist` | 32 MB | ✅ checked |
| `recovery_a` / `boot_a` | 100–192 MB | ✅ checked |
| `super` | 9 GB | skipped |
| `userdata` | 226 GB | skipped |

**Every non-regenerable partition falls below the threshold** — the ones that
matter most are always verified.

To turn it off: untick "设备端二次校验" in the GUI, or pass `--no-verify-device`.

> If the device fails to produce a hash for any reason (no `sha256sum`, insufficient
> permissions…), the program does **not** treat it as a failure — but it says so
> explicitly in the log and reports the unverified count in the completion dialog.
> It never pretends a check happened.

### ② PC-side hash + exact byte count

### Why must `exec-out` be used instead of `shell`?

`adb shell` allocates a PTY, which converts `\n` in the binary stream into `\r\n`, **silently corrupting the data**.

Measured data (a 32 MB persist partition):

| Method | Bytes received | SHA256 |
|---|---|---|
| `exec-out` | 33554432 | ✅ Exactly matches the device |
| `shell (PTY)` | **33631171** (76739 bytes extra) | ❌ No match at all |

---

## 6. Interface and Progress Display

### GUI

The window is four panels top to bottom, with **device info and the ADB switch right at the top**:

```
① 设备状态 ──────────────────────────────────────────────────────────
  ●  已连接  6f06de3d                          [刷新设备] [检查 Root]
     代号 vermeer · 识别为 高通 Qualcomm（得分 16） · root ✓ KernelSU

  系统 Android 16（SDK 36）  系统版本 OS3.0.307.0.WNKCNXM
  芯片 骁龙 8 Gen 2（SM8550）· 高通          平台 kalama
  内核 5.15.194-android13-8-00019-gf4321180a397-ab15212794
  架构 arm64-v8a             内存 14.8 GB      屏幕 1440x3200
  槽位 _a（当前系统槽）       补丁 2026-08-01   Root ✓ uid=0（u:r:ksu:s0）

  ADB 服务: ● 运行中  [停止]   退出本程序时会自动停止 ADB 服务
──────────────────────────────────────────────────────────────────────

④ 进度与日志 ────────────────────────────────────────────────
  persist      [████████████░░░░░░░░░░░░░░]   48.2%
               15.4 MB / 32.0 MB   11.9 MB/s   剩 2s
  总计 5/13 项 [██████░░░░░░░░░░░░░░░░░░░░]   36.0%
               36.0 MB / 100.0 MB   7.2 MB/s   剩 8s
  ┌──────────────────────────────────────────────────────┐
  │ [21:19:10] ===== 备份 13 个分区 =====                 │  ← 紫色标题
  │ [21:19:10] 开始 persist  (32.0 MB)  [★不可再生]       │
  │ [21:19:13]   [OK] persist  32.0 MB  2.7s  11.9MB/s   │  ← 绿色成功
  │ [21:19:14]   [!] 流式失败，回退到设备端暂存模式 ...    │  ← 黄色告警
  │ [21:19:20]   [X] modemst1 失败: 传输停滞超过 120 秒   │  ← 红色错误
  └──────────────────────────────────────────────────────┘
```
*(the panel titles and field names are Chinese-only in the app; see the
[language note](#-about-this-project))*

#### Device info panel

Every field is read straight from the system (`getprop` + `uname` +
`/proc/meminfo` + `wm size`) in a single round trip. Nothing on the device
is modified.

| Field | Source | Notes |
|---|---|---|
| 系统 System | `ro.build.version.release` / `.sdk` | e.g. `Android 16（SDK 36）` |
| 系统版本 OS build | `ro.build.version.incremental` | e.g. `OS3.0.307.0.WNKCNXM` |
| 芯片 Chip | `ro.soc.model` / `ro.soc.manufacturer` | authoritative on Android 12+ |
| 平台 Platform | `ro.board.platform` | Qualcomm internal codename, e.g. `kalama` |
| 内核 Kernel | `uname -r` | |
| 架构 ABI | `ro.product.cpu.abi` | e.g. `arm64-v8a` |
| 内存 RAM | `/proc/meminfo` MemTotal | |
| 屏幕 Screen | `wm size` | physical resolution |
| 槽位 Slot | `ro.boot.slot_suffix` | active slot on A/B devices |
| 补丁 Patch | `ro.build.version.security_patch` | |
| Root | `su -c id` | includes the SELinux context |

> 🔍 **The chip name is never guessed.** `ro.soc.model` is used whenever present;
> a *very small* board-codename table (`kalama → Snapdragon 8 Gen 2`) is consulted
> only as a fallback, and the raw codename is always shown alongside so you can
> verify it. Anything unavailable renders as `—` — never invented.
>
> ⚠️ **"系统版本 / OS build" comes from `ro.build.version.incremental`, not
> `ro.build.display.id`.** On Xiaomi/Redmi the latter is the **AOSP build ID**
> (measured: `BP2A.250605.031.A3`), which is *not* the version shown under
> Settings → About phone — we got this wrong at first and only real-device
> verification caught it. Likewise the kernel comes from `uname -r`: some ROMs
> expose a two-component `ro.kernel.version` like `5.15`, and letting it win
> would leave you with just `5.15` on screen.
>
> 📐 The row is laid out with `FlowLayout` (a custom flow layout), so it **reflows when the window gets
> narrow** instead of dropping fields (3 rows at 1060 px on the real device,
> 4 at 760 px; measured, no overflow). Kernel / RAM / screen go through `shell`
> rather than `su` — they need no root, so **these show even on an unrooted device**.

**Four design points behind the progress bars / ETA**:

| Feature | Description |
|---|---|
| **Two progress bars** | Top = current partition, bottom = overall (including every selected partition + GPT + environment bundle)|
| **Dual ETA** | Time remaining for the current partition + time remaining overall |
| **Smoothed speed** | An exponential moving average rather than an instantaneous value — otherwise the numbers jump around wildly |
| **Window title in sync** | The title bar shows `备份中 36%`, so you can check the taskbar when minimised and still know the progress |
| **Phase switching** | During transfer it is a percentage bar; for SHA256 verification, GPT reads and tar packing it automatically switches to a back-and-forth scrolling bar |
| **Levelled log colouring** | Purple = phase heading, green = success, red = failure, yellow = warning, grey = detail |

> ⚠️ For colour decisions, **prefix markers take precedence over keywords**. A warning like `[!] 流式失败，回退到设备端暂存模式`
> that contains the word "failure" (though the fallback actually succeeded) would be mis-coloured red if keyword matching ran first.

### Command-line version

```sh
python backup_core.py --adb <path-to-adb> --preset critical --out <output-dir>
```

In the terminal it is a single-line refreshing progress bar (`\r` overwrite, no screen spam):

```
  persist              [████████████░░░░░░░░░░░░░░]  48.2%   15.4 MB/32.0 MB   11.9 MB/s  剩 2s
      ── 总进度  36.0%   36.0 MB / 100.0 MB   平均 7.2 MB/s   剩 8s
```

**It degrades automatically when the output is redirected to a file** — logging only one line per 10% crossed, so that thousands of
`\r` characters are not stuffed into the log file. Add `--quiet` to turn progress output off entirely.

---

### Responsive layout (window resizing)

No element is ever clipped — **at any window size, on any resolution, under any DPI scaling**.

| Mechanism | What it solves |
|---|---|
| **Window size measured from content and screen** | No more hard-coded `1060x820`. On startup the real content requirement is measured, then clamped to the available screen area |
| **Scrollable canvas as a safety net** | When the window is smaller than the content, a scrollbar appears so every control stays reachable |
| **Automatic reflow of horizontal rows** | Presets, bulk-action buttons and option switches wrap onto the next line in a narrow window instead of being pushed out of view |
| **Dual scrollbars on the table and the log** | In a narrow window you can scroll horizontally, so columns and long log lines are never cut off |

> **Why Tk's `pack(side="left")` was not an option** — when the container is too narrow, `pack` pushes the **later widgets straight out of the visible area**, with no error and no warning. The symptom is "some buttons disappear after I make the window narrower".
> **Why Tk's `grid` was not an option either** — `grid` **shares column widths across the whole container**: after wrapping, a wide widget occupying column 0 widens that column, and **every other row shifts right as well**. Offsetting the column index by row number does not help either, because row 1 still has to start after the columns used by row 0.
> The final design: a hand-written flow layout (`FlowFrame` in the Tk version, `FlowLayout` in the Qt version — both compute their own wrapping coordinates) plus a scrolling fallback (`ScrollHost` in the Tk version, `QScrollArea` in the Qt version).
>
> 📌 **Since v2.0.0**: Qt's layout system is simply far more robust than Tk's, and `QScrollArea` is a native widget rather than a hand-rolled Canvas solution.
> The Tk-era war story above is kept because it explains **why this project insists on managing its own line wrapping** instead of leaving it to a layout manager.

### ADB server switch

The UI has a row: **`ADB 服务: ● 运行中  [停止]  退出本程序时会自动停止 ADB 服务`** —
**in the "① 设备状态" panel at the very top**, right next to *refresh device* / *check root*, so it is always within reach.

| Control | What it does |
|---|---|
| Status text | Shows in real time whether the adb server is up (polled every 2.5 s via a millisecond-level port probe) |
| `[启动]` / `[停止]` | Start or stop the adb server manually |
| Auto-stop on exit | **Unconditional, no setting needed** — runs `adb kill-server` when you close the window |

**Why this switch exists** — the adb server (listening on `5037`) is a **shared** long-lived process (Android Studio and scrcpy use the very same one). So this tool never kills it while running (only when you explicitly press "stop"), but it **always stops it on exit**, leaving no leftover process behind.

> ⚠️ **Pressing "stop" also pauses device detection** — otherwise the polling thread's next `adb devices` would immediately bring the server back up, and it would look like "stop does nothing". Press "start" to resume.

#### What happens when you close the window

```
1. poll_stop.set()        tell background threads to stop issuing adb calls
2. kill_live_children()   kill any adb child processes still running
3. adb kill-server        stop the server (unconditional, and must come after 2)
4. wait for threads (<=0.6s), then destroy()
```

> ❗ **Why it has to clean up after itself** — adb child processes are **separate processes**; Python exiting does not take them along. If an adb call is in flight when you close the window (device polling, reading the partition table, or a backup in progress), that `adb.exe` becomes an orphan left behind in Task Manager.
> Measured (Windows / Python 3.14): after `Popen(["adb","wait-for-device"])`, letting the interpreter exit leaves that adb process **still alive**.
> In the implementation, `backup_core.py` keeps a child-process registry: all seven `subprocess.run` calls go through `_run()` (which registers the PID), the two `Popen` sites were given registration too, and an `atexit` hook acts as a backstop — so no exit path can leave an orphan.

## 7. FAQ

### "No device detected"

The UI shows exactly which step you are stuck on:

| UI message | Meaning | What to do |
|---|---|---|
| 未检测到设备 | adb sees no device at all | Try another data cable (one that can carry data) / install the USB driver |
| **未授权** | You did not tap "Allow" on the phone | Look at the phone screen and tap "Allow USB debugging"; if that fails, unplug and replug |
| **离线 (offline)** | The connection is unstable | Unplug and replug, or restart the phone |
| root 不可用 | `su` was denied | Grant Shell permission in Magisk/KernelSU/APatch |

### Chinese text shows up as boxes

Very rare. If it happens, your system is missing a Chinese font — installing "Microsoft YaHei" is enough.

### Backups are slow

Measured at roughly **12-15 MB/s** (limited by USB and adb). For reference:

| Contents | Size | Time |
|---|---|---|
| Critical partitions (Tier 1)| ~80 MB | About 10 seconds |
| Critical + root baseline | ~1 GB | About 1.5 minutes |
| `super` | 9 GB | About 12 minutes |
| `userdata` | 226 GB | About 4.5 hours |

### Changing the backup location

The "backup root directory" can be changed in the UI. The default is the `Backups\` folder in the program directory;
if the program is installed in a read-only location such as Program Files, it automatically falls back to the "Documents" folder in your home directory.

---

## 8. Technical Architecture (for future maintainers)

```
backup_gui_qt.py       the GUI (PySide6 / Qt6) — display and interaction only, **the default interface since v2.0.0**
  ├─ device poll thread polls `adb devices` every 1.5 s
  ├─ message queue       the only channel from worker thread → main thread
  └─ QTimer message pump the main thread drains the queue and repaints every 80 ms

backup_gui.py          【旧版】the legacy GUI (Tkinter), the 1.x-era implementation, kept as a fallback
  └─ feature-aligned with the Qt version, but noticeably stutters when a large window is resized (an architectural cause — see below)

backup_core.py         the core engine (no GUI dependency; runs standalone from the CLI)
  ├─ Adb                adb.exe wrapper + capability probing
  ├─ BackupEngine       backup flow orchestration
  ├─ GPT parser         parses GPT in pure Python, without relying on the device-side sgdisk
  └─ naming/conflict    sanitize_folder_name / resolve_backup_dir

partition_profiles.py  platform signatures + the four-tier partition rules (pure data)
```

### Four Inviolable Design Constraints

1. **Never touch GUI objects from a worker thread**
   Every call such as `QLineEdit.text()` / `QCheckBox.isChecked()` must be completed on the main thread and only then passed to the worker —
   otherwise Qt simply crashes (`QObject: Cannot create children for a parent in a different thread`) or segfaults at random.
   Qt is stricter than Tkinter here: **across a thread boundary you can only pass data, never widgets**.

2. **Why v2.0.0 moved from Tkinter to Qt**
   On Windows, Tk creates **a real HWND for every single widget**. This program has 114 widgets nested 8 levels deep, so every window resize made Windows re-adjust and repaint all of them — measured at **69 ms per resize**, and while dragging, the event queue could never catch up.
   Eight Tk-side optimisations were tried (flattening the hierarchy, freezing relayout, dropping the scrollbars, moving the content out of the Canvas…), and **none of them helped**, because this was not a matter of badly written code — it was decided by Tk's architecture.
   Qt widgets do not own a separate HWND; resizing is a repaint rather than a window rebuild — measured at **13.7 ms per resize, 4–5× faster**.

3. **A/B slot suffixes must be stripped before rule matching**
   The rule table is written with unslotted names (`abl`), while on the device it is actually called `abl_a`.
   Not stripping the suffix leaves a large number of partitions "unclassified". See `classify()`.

4. **GPT offsets must be computed in Python**
   The shell's `[ ]` comparison **overflows 32 bits** into a negative number for values like 253 GB,
   causing the tail GPT to be silently skipped. Python is natively 64-bit and has no such problem.

### How to Support a New Platform

Add one platform profile to `PLATFORMS` in `partition_profiles.py`,
then add `(fnmatch pattern, description)` entries to rule tables such as `TIER1_RULES` — **no other code needs to change**.

---

## 9. Versions

| Item | Value |
|---|---|
| Tool version | **2.0.0** |
| Core version | 1.0.0 |
| Profile library version | 1.0.0 |
| Dependencies | **PySide6** (the GUI) + the Python standard library. The core engine, `backup_core.py`, still has **zero third-party dependencies** and runs standalone from the CLI |
| Portable package size | About 152 MB (including the Python runtime + PySide6 + ADB)|

### Changelog

| Version | Changes |
|---|---|
| **2.0.0** | **The GUI was migrated from Tkinter to PySide6 (Qt6)** — which finally kills the window-drag stutter at the root. Tk gives every widget its own HWND, so with 114 widgets nested 8 levels deep each resize cost 69 ms and eight Tk-side optimisations all failed; Qt measures 13.7 ms per resize, **4–5× faster**, and dragging finally keeps up with the cursor. Feature-for-feature identical to 1.1.1, with `backup_gui.py` (the Tk version) kept as a fallback. The package grew from 50 MB to 152 MB because it now bundles Qt |
| 1.1.1 | **Device-side hashing is now on by default** — backups automatically compare against the phone's current partition via `sha256sum` (the only layer that proves the device bytes equal the disk bytes); partitions of 1 GiB or more are skipped automatically, and anything left unverified is reported honestly in the log and the dialog. **Fixed "diff against previous backup" being silently dead for partitions** (manifest lines with an empty `sub` were skipped wholesale by a `len(parts) >= 5` test, so only 18 of 45 entries were recognised). **`*_layout.txt` and `byname_mapping.txt` are now in the manifest** (they had no hash protection before). **New detailed device-info bar at the top** (system / chip / version / platform / kernel / ABI / RAM / screen / slot / patch / root, wrapping automatically in narrow windows); **ADB server switch moved to the top**; **ADB server now stops unconditionally on exit**; **fixed `adb.exe` being orphaned after closing the window**; **fixed window-drag stutter** (146 ms → 54 ms per frame, the limit of what Tk-side optimisation could reach); **fixed UI elements being hidden after resizing** — now a responsive layout |
| 1.1.0 | Fixed all six LUNs' GPT tail backups failing (a local path was used as a device path); fixed the byte stream being polluted by stderr; fixed two whitelist validation gaps |

---

## 10. License

This project is released under the **GNU General Public License v3.0 (GPL-3.0)**.

| Item | Description |
|---|---|
| License | **GPL-3.0** — full text in [`LICENSE`](LICENSE) |
| SPDX identifier | `GPL-3.0` |
| Additional notice | [`DISCLAIMER.md`](DISCLAIMER.md) — a risk notice that **does not modify or replace** the GPL terms |

**You are free to**: use, modify and distribute this software, **including for commercial purposes**.

**But you must**:

- 📌 Keep the copyright notice and the original license text
- 🔓 **Derivative works must also be open-sourced under GPL-3.0** (copyleft — this is the biggest difference between GPL and MIT)
- 📝 Modified versions must state what was changed and the date

**This software comes with no warranty of any kind** — see sections 15–17 of [`LICENSE`](LICENSE).

---

## 11. Research & References

Before writing any code I searched GitHub for comparable projects and looked up public sources for the partition lists. This section documents both.

### Comparable projects

The survey turned up **8 related projects**, all of which I went through. The conclusion: existing solutions are **either device-side CLIs or PC command-line tools** — none offers "PC GUI + multi-select partitions + one-click backup + automatic verification".

They are listed here both to document the survey and to **help you pick the right tool** — if one of them fits your use case better, just use it.

| Project | Language | Form | Notes |
|---|---|---|---|
| [Magisk-Modules-Alt-Repo/backup](https://github.com/Magisk-Modules-Alt-Repo/backup) | Shell | Device-side CLI | Partition backup without a custom recovery; the most popular of the group |
| [RuslanUC/pyAdbBackup](https://github.com/RuslanUC/pyAdbBackup) | Python | PC CLI | Closest in scope to this tool, but has no GUI |
| [zzzee6/Geek_Toolbox](https://github.com/zzzee6/Geek_Toolbox) | Shell | Device-side | Partition backup/restore under KernelSU / Magisk |
| [Skecthware/Android-Firmware-Dumper](https://github.com/Skecthware/Android-Firmware-Dumper) | — | Device-side | A/B slot detection + MD5 verification + TAR packaging |
| [SysAdminDoc/Devicer](https://github.com/SysAdminDoc/Devicer) | C# | Windows WPF | **Has a GUI**, but Samsung-oriented and mixes many features |
| [lopestom/android-partition-backup](https://github.com/lopestom/android-partition-backup) | Python | CLI | Plain ADB + root |
| [Bruh938/Samsung-Partition-Backup-Tool](https://github.com/Bruh938/Samsung-Partition-Backup-Tool) | Python | CLI | Samsung only |
| [VioletChann/cy-android-toolkit](https://github.com/VioletChann/cy-android-toolkit) | — | All-in-one toolbox | ADB / Fastboot / Magisk suite |

### Where the partition tiering rules come from

The tiering rules were not invented from thin air — **public sources were checked first, then verified on a real device**. The following sources directly shaped the rule tables in `partition_profiles.py`:

| Source | Contribution |
|---|---|
| [onfix.cn, "Guide to Backing Up Phone Baseband/Firmware with Commands"](https://onfix.cn/course/4780?bid=1&mid=28642) | Qualcomm `fsg` `fsc` `modemst1` `modemst2`; MediaTek `nvram` `nvdata` `nvcfg` `persist` `protect1` `protect2` `seccfg`. The Tier 1 rules match this list **item for item** |
| android-hilfe.de — Samsung SM-N986B (Note 20 Ultra) IMEI repair thread | The Samsung `efs` / `sec_efs` partition pair |
| [CamsShaft/arbitrary-R-W-for-steady-and-param-partitions](https://github.com/CamsShaft/arbitrary-R-W-for-steady-and-param-partitions) | Existence of the Samsung `steady` / `param` partitions |

The following entries **could not be traced to a public source**. They are kept as domain knowledge and flagged in the `partition_profiles.py` comments:

> Unisoc `prodnv` `nvitem` `wcnmodem` `splloader`; Samsung `up_param`; Tensor `ldfw`

Those rules **never cause a false match when the naming pattern does not apply**, but if they are wrong for your device, please open an issue.

### Statement

> ⚠️ **This tool does not use, copy, or adapt any code from the projects listed above.**
> The three `.py` files total roughly 3,000 lines and are **entirely hand-written**, with **zero third-party dependencies** — you never need to `pip install` anything.
> The projects and sources above were used **for research and for cross-checking the rules only**; the implementation, the tiering thresholds, and the inference logic are all original work.
