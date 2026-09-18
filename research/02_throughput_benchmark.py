# -*- coding: utf-8 -*-
"""
Spike 2: 找出最快的「流式直传」写法
================================================================
Spike 1 已证明 adb exec-out 二进制可靠，但只有 11.4 MB/s。
本脚本对比几种取数方式与写入路径，为正式程序选定最优实现。

变量：
  取数命令 : dd bs=1M / dd bs=4M / cat / toybox dd
  落盘路径 : Python PIPE 中转  vs  stdout 直接重定向到文件句柄
"""
import subprocess
import hashlib
import time
import os
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

def _find_adb():
    """自动定位 adb：优先本目录/上级的 adb/，其次系统 PATH。"""
    import shutil
    here = os.path.dirname(os.path.abspath(__file__))
    bases = [here]
    for _ in range(4):                 # 向上找 4 层，覆盖各种目录布局
        bases.append(os.path.dirname(bases[-1]))
    for base in bases:
        for rel in (os.path.join("adb", "adb.exe"), os.path.join("adb", "adb"),
                    "adb.exe", "adb"):
            p = os.path.join(base, rel)
            if os.path.isfile(p):
                return p
    return shutil.which("adb") or "adb"


ADB = _find_adb()
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
PART = "persist"
TMP = tempfile.gettempdir()


def dev_sha(part):
    p = subprocess.run([ADB, "shell", f"su -c 'sha256sum /dev/block/by-name/{part}'"],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       creationflags=CREATE_NO_WINDOW)
    return p.stdout.decode("utf-8", "replace").split()[0]


def run_pipe(cmd, path):
    """方式 A：Popen(stdout=PIPE) → Python 读块 → 写文件"""
    t0 = time.time()
    h = hashlib.sha256()
    n = 0
    p = subprocess.Popen([ADB, "exec-out", f"su -c '{cmd}'"],
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                         creationflags=CREATE_NO_WINDOW)
    with open(path, "wb") as f:
        while True:
            chunk = p.stdout.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
            f.write(chunk)
            n += len(chunk)
    p.stdout.close()
    rc = p.wait()
    return n, h.hexdigest(), time.time() - t0, rc


def run_direct(cmd, path):
    """方式 B：stdout 直接重定向到文件句柄（不经 Python）"""
    t0 = time.time()
    with open(path, "wb") as f:
        p = subprocess.Popen([ADB, "exec-out", f"su -c '{cmd}'"],
                             stdout=f, stderr=subprocess.DEVNULL,
                             creationflags=CREATE_NO_WINDOW)
        rc = p.wait()
    n = os.path.getsize(path)
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            c = f.read(4 * 1024 * 1024)
            if not c:
                break
            h.update(c)
    return n, h.hexdigest(), time.time() - t0, rc


def main():
    ref = dev_sha(PART)
    print()
    print(f"基准 sha256 ({PART}) : {ref}")
    print()
    print(f"{'取数命令':<26} {'落盘方式':<18} {'字节':>11} {'耗时':>7} {'速率':>10}  校验")
    print("-" * 92)

    cmds = [
        ("dd bs=1M",   "dd if=/dev/block/by-name/%s bs=1048576 2>/dev/null" % PART),
        ("dd bs=4M",   "dd if=/dev/block/by-name/%s bs=4194304 2>/dev/null" % PART),
        ("dd bs=16M",  "dd if=/dev/block/by-name/%s bs=16777216 2>/dev/null" % PART),
        ("cat",        "cat /dev/block/by-name/%s" % PART),
        ("toybox dd bs=4M", "/system/bin/dd if=/dev/block/by-name/%s bs=4194304 2>/dev/null" % PART),
    ]

    results = []
    for label, cmd in cmds:
        for mode, fn in (("PIPE中转", run_pipe), ("直接重定向", run_direct)):
            path = os.path.join(TMP, "spike2.bin")
            try:
                n, sha, dt, rc = fn(cmd, path)
            except Exception as e:
                print(f"{label:<26} {mode:<18} {'-':>11} {'-':>7} {'-':>10}  ERR {e}")
                continue
            mbps = (n / 1048576) / dt if dt > 0 else 0
            okmark = "OK" if sha == ref else "FAIL"
            print(f"{label:<26} {mode:<18} {n:>11} {dt:>6.2f}s {mbps:>9.1f}MB/s  {okmark}")
            results.append((mode, label, mbps, okmark, dt))
            try:
                os.remove(path)
            except OSError:
                pass
        print()

    print("-" * 92)
    good = [r for r in results if r[3] == "OK"]
    if good:
        good.sort(key=lambda r: -r[2])
        print("最快的可靠组合：")
        for mode, label, mbps, _, dt in good[:4]:
            print(f"   {mbps:6.1f} MB/s   {label:<20} + {mode}")
        best = good[0]
        size_mb = 454
        print()
        print(f"按最快方案估算：454 MB 约需 {size_mb / best[2]:.0f} 秒；"
              f"9 GB (super) 约需 {9216 / best[2] / 60:.1f} 分钟")
    bad = [r for r in results if r[3] != "OK"]
    if bad:
        print()
        print("校验失败（不可用）：")
        for mode, label, _, _, _ in bad:
            print(f"   {label} + {mode}")
    print()


if __name__ == "__main__":
    main()
