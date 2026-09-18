# Android Partition Backup

[中文](README.md) | **English**

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
| **Automated tests** | 4 test suites, 87 assertions in total, including a progress-bar test that specifically covers the TTY branch | — |
| **⭐ Manual testing** | Used human feedback to pinpoint root causes, fix them, and verify by regression | **A human ran the tests by hand**: walked the full GUI backup flow on a real device, and verified portable-version portability on a different computer (a VM) |
| **On-device validation** | Compared byte-for-byte against a manual backup with sha256, 18/18 identical | Ran end-to-end on a real device and signed off on the results |

**But keep it in perspective**: AI makes mistakes too. Real-device testing in the **early** days already caught defects the AI had written:

  1. A/B slot suffixes were not stripped, leaving 40+ partitions "unclassified"
  2. Whole-disk symlinks in by-name were treated as partitions
  3. The rule tables did not cover everything
  4. There was a risk of backup failure

Then, at the **human on-device acceptance** stage, 3 more defects surfaced that were subtler still and completely outside the reach of automated tests — see the next section 👇

### 🧑🔬 Testing is done by AI and humans **together**

The automated tests were written by the AI (4 suites / 87 assertions), but **manual testing was not a formality — it was the main bug-catcher**.

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

- **Python 3.8+** (including tkinter, which the official installer bundles by default)
- **ADB** — download it from [platform-tools](https://developer.android.com/tools/releases/platform-tools),
  put it at `adb/adb.exe` (Windows) or `adb/adb` (Linux/macOS), or just add it to your system PATH
- A **rooted** Android phone (Magisk / KernelSU / APatch all work)

> This repository does not include the ADB binary (an 8 MB binary in git history can never be slimmed down again), so please download it yourself.

### Running

```bash
git clone https://github.com/2567922141/android-partition-backup.git
cd android-partition-backup   # or whatever you cloned it as

# Put the adb from platform-tools here (pick one)
#   A. Copy it to adb/adb.exe
#   B. Add it to your system PATH
#   C. Do nothing — the program finds it on PATH automatically

python backup_gui.py         # launch the GUI
```

Linux / macOS users can use the bundled launch script directly (it picks a Python that has tkinter):

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

**No restore function is provided** — writing to a partition is the only way to brick a device; please use a manual procedure together with the instructions in `README.md`.

This project absolutely does not leak any personal information.
---

## 5. Verification

For every partition it backs up, the program will:

1. **Transfer**: `adb exec-out` **redirects** the output of `dd` **straight into** a local file
2. **Byte-count check**: the number of bytes received must be **exactly identical** to the partition size reported by `blockdev --getsize64`
3. **Local SHA256**: streaming hash computed over the file as it is written
4. **GPT structure validation** (for GPT items): pure Python parsing of the MBR `55AA` + GPT `EFI PART` signatures,
   plus a head ↔ tail cross-check (the two must point at each other)
5. **Optional second check on the device**: when enabled, the device additionally computes its own `sha256sum` for comparison against the local one

> If the byte counts do not match, it automatically **falls back** to the slower but more robust "stage on the device + adb pull" path and retries.

### Why must `exec-out` be used instead of `shell`?

`adb shell` allocates a PTY, which converts `\n` in the binary stream into `\r\n`, **silently corrupting the data**.

Measured data (a 32 MB persist partition):

| Method | Bytes received | SHA256 |
|---|---|---|
| `exec-out` | 33554432 | ✅ Exactly matches the device |
| `shell (PTY)` | **33631171** (76739 bytes extra) | ❌ No match at all |

---

## 6. Progress Display

### GUI

```
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

## 8. Four Iron Rules for Restoring

> 1. **Device-bound**: `persist` / `modemst*` / `efs` / `nvdata` and the like contain device-unique data —
>    **restoring them across devices is strictly forbidden**, as it can break the fingerprint, cause signal anomalies, or even give two phones conflicting IMEIs
> 2. **Never restore an old value to `secdata`** — it is a one-way anti-rollback counter, and writing an old value may trip the fuse and brick the device
> 3. **The logical sector size is not necessarily 512** — Qualcomm UFS is usually 4096.
>    Check the first line of `gpt\<disk>_layout.txt` for your own device, and compute offsets from it when running `dd` by hand
> 4. **Keep a copy before restoring GPT** — even if the current GPT is broken, back it up first with `sgdisk --backup`

### Restoring root (the most common case)

```sh
fastboot flash init_boot img\init_boot_a.img
```

### Restoring non-regenerable partitions from within the system

```sh
dd if=img\persist.img  of=/dev/block/by-name/persist  bs=1M
dd if=img\fsg.img      of=/dev/block/by-name/fsg      bs=1M
dd if=img\modemst1.img of=/dev/block/by-name/modemst1 bs=1M
sync && reboot
```

### Restoring GPT

```sh
sgdisk --load-backup=gpt\sda_gpt_sgdisk.bin /dev/block/sda
sgdisk --move-second-header /dev/block/sda
blockdev --rereadpt /dev/block/sda
```

The `README.md` in every backup directory includes these commands together with the actual partition names of that device.

---

## 9. Technical Architecture (for future maintainers)

```
backup_gui.py          the GUI (Tkinter) — display and interaction only
  ├─ device poll thread polls `adb devices` every 1.5 s
  ├─ message queue       the only channel from worker thread → main thread
  └─ root.after pump     the main thread drains the queue and repaints every 80 ms

backup_core.py         the core engine (no GUI dependency; runs standalone from the CLI)
  ├─ Adb                adb.exe wrapper + capability probing
  ├─ BackupEngine       backup flow orchestration
  ├─ GPT parser         parses GPT in pure Python, without relying on the device-side sgdisk
  └─ naming/conflict    sanitize_folder_name / resolve_backup_dir

partition_profiles.py  platform signatures + the four-tier partition rules (pure data)
```

### Three Inviolable Design Constraints

1. **Never touch tkinter from a worker thread**
   Every call such as `BooleanVar.get()` must be completed on the main thread and only then passed to the worker —
   otherwise you get random deadlocks or crashes. This is the most classic Tkinter pitfall.

2. **A/B slot suffixes must be stripped before rule matching**
   The rule table is written with unslotted names (`abl`), while on the device it is actually called `abl_a`.
   Not stripping the suffix leaves a large number of partitions "unclassified". See `classify()`.

3. **GPT offsets must be computed in Python**
   The shell's `[ ]` comparison **overflows 32 bits** into a negative number for values like 253 GB,
   causing the tail GPT to be silently skipped. Python is natively 64-bit and has no such problem.

### How to Support a New Platform

Add one platform profile to `PLATFORMS` in `partition_profiles.py`,
then add `(fnmatch pattern, description)` entries to rule tables such as `TIER1_RULES` — **no other code needs to change**.

---

## 10. Versions

| Item | Value |
|---|---|
| Tool version | 1.1.0 |
| Core version | 1.0.0 |
| Profile library version | 1.0.0 |
| Dependencies | Python standard library only (tkinter), **zero third-party packages** |
| Portable package size | About 50 MB (including the Python runtime + ADB)|

---

## 11. License

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
