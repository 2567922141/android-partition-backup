# -*- coding: utf-8 -*-
"""
Spike: 验证 adb exec-out 的二进制流完整性
================================================================
目的：确认「流式直传」方案是否可行，即：
      adb exec-out su -c 'dd if=分区 ...' 拿到的字节流，
      是否与设备上分区内容逐字节一致。

对比三种取数方式：
  A) adb exec-out  (不分配 PTY —— 预期：干净)
  B) adb shell     (分配 PTY   —— 预期：\n 被转成 \r\n，损坏)
  C) 设备端 sha256sum  (基准真值)

结论决定主程序走方案 A 还是 B。
"""
import subprocess
import hashlib
import time
import os
import sys

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


def adb_bytes(args, timeout=600):
    """执行 adb，返回原始 stdout 字节（绝不用 text 模式，避免 Windows 换行转换）。"""
    p = subprocess.run(
        [ADB] + args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=CREATE_NO_WINDOW,
        timeout=timeout,
    )
    return p.stdout, p.stderr, p.returncode


def adb_text(args, timeout=120):
    out, err, rc = adb_bytes(args, timeout)
    return (out.decode("utf-8", "replace").strip(),
            err.decode("utf-8", "replace").strip(),
            rc)


def probe(part, bs):
    print("=" * 74)
    print(f"分区 {part}   (dd bs={bs})")
    print("=" * 74)

    # ---- 基准：设备端直接算 sha256 ----
    ref, err, rc = adb_text(["shell", f"su -c 'sha256sum /dev/block/by-name/{part}'"])
    if rc != 0 or not ref:
        print(f"  [!] 设备端 hash 失败 rc={rc} err={err}")
        return None
    dev_sha = ref.split()[0]
    print(f"  基准(设备端 sha256)  : {dev_sha}")

    # ---- 方案 A: exec-out ----
    t0 = time.time()
    outA, errA, rcA = adb_bytes(
        ["exec-out", f"su -c 'dd if=/dev/block/by-name/{part} bs={bs} 2>/dev/null'"]
    )
    dtA = time.time() - t0
    shaA = hashlib.sha256(outA).hexdigest()
    print(f"  A) exec-out          : {len(outA):>10} bytes  {dtA:5.2f}s  rc={rcA}")
    print(f"     sha256            : {shaA}")
    print(f"     {'>>> MATCH <<<' if shaA == dev_sha else '>>> MISMATCH <<<'}")

    # ---- 方案 B: shell (PTY) ----
    t0 = time.time()
    outB, errB, rcB = adb_bytes(
        ["shell", f"su -c 'dd if=/dev/block/by-name/{part} bs={bs} 2>/dev/null'"]
    )
    dtB = time.time() - t0
    shaB = hashlib.sha256(outB).hexdigest()
    print(f"  B) shell(PTY)        : {len(outB):>10} bytes  {dtB:5.2f}s  rc={rcB}")
    print(f"     sha256            : {shaB}")
    print(f"     {'>>> MATCH <<<' if shaB == dev_sha else '>>> MISMATCH <<<'}")

    if len(outA) != len(outB):
        print(f"  [!] 两种方式字节数差 {len(outB) - len(outA)} —— PTY 转换的确发生了")
    print()
    return {"part": part, "size": len(outA), "dev": dev_sha,
            "execout": shaA, "shell": shaB, "secA": dtA, "secB": dtB}


def main():
    print()
    print("adb:", ADB)
    ok, err, rc = adb_text(["devices"])
    print("devices:", ok.replace("\n", " | "), f"rc={rc}")
    print()

    results = []
    # secdata 32KB（稳定、极小）→ 快速判定
    r = probe("secdata", 4096)
    if r:
        results.append(r)
    # persist 32MB（稳定、中等）→ 验证吞吐与大数据完整性
    r = probe("persist", 1048576)
    if r:
        results.append(r)

    print("=" * 74)
    print("结论")
    print("=" * 74)
    allA, allB = True, True
    for r in results:
        a = (r["execout"] == r["dev"])
        b = (r["shell"] == r["dev"])
        allA &= a
        allB &= b
        mbps = (r["size"] / 1048576) / r["secA"] if r["secA"] > 0 else 0
        print(f"  {r['part']:<12} exec-out={'OK ' if a else 'FAIL'}  "
              f"shell={'OK ' if b else 'FAIL'}  "
              f"{r['size']/1048576:8.2f} MB  {r['secA']:5.2f}s  {mbps:6.1f} MB/s")

    print()
    if allA:
        print("  >>> exec-out 二进制流【可靠】—— 主程序采用方案 A（流式直传）")
    else:
        print("  >>> exec-out 不可靠 —— 主程序必须改用方案 B（设备端暂存 + pull）")
    if not allB:
        print("  >>> 顺带确认：adb shell 会损坏二进制（符合预期，绝不用于传输）")
    print()


if __name__ == "__main__":
    main()
