# -*- coding: utf-8 -*-
"""
/data/adb 环境包（data_adb.tar.gz）单元测试
================================================================================
这一项曾经在真实使用中静默失败过 —— 界面上只显示「data_adb.tar.gz 失败」，
看不出是打包挂了还是取回挂了。根因是代码把两条路径的错误都吞掉了：

    f"{tar} -czf ... 2>/dev/null; true"     # tar 的报错丢弃 + 强制返回成功
    subprocess.run([... "pull" ...])        # 返回值从未检查

所以这里的测试重点不是"能不能打包"（那要真机），而是**失败时能不能说清楚**：
  1. _verify_targz 必须能认出截断/空/垃圾包 —— 这是唯一能抓传输截断的手段
  2. 生成的 tar 命令必须满足 su() 的约束（不能含单引号）
  3. 命令里所有选项必须排在文件操作数之前
"""
import io
import os
import shutil
import sys
import tarfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from backup_core import (                      # noqa: E402
    BackupError, DEVICE_STAGE_DIR, _verify_targz,
)

# ⚠️ 不能放 tempfile.mkdtemp()。
#    某些受限环境（含本项目的开发沙箱）$env:TEMP 下无法写入文件，
#    会抛 PermissionError: [Errno 13]。放仓库内的临时目录最稳。
TMP_ROOT = os.path.join(os.path.dirname(HERE), "_test_tmp")

PASS, FAIL = 0, 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK  ] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}   {detail}")


def make_targz(path, names=("a", "b", "c")):
    with tarfile.open(path, "w:gz") as tf:
        for n in names:
            b = n.encode()
            ti = tarfile.TarInfo(n)
            ti.size = len(b)
            tf.addfile(ti, io.BytesIO(b))
    return path


def main():
    tmp = TMP_ROOT
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp, exist_ok=True)
    try:
        print("=" * 78)
        print("一、_verify_targz —— 完整性判定")
        print("=" * 78)

        good = make_targz(os.path.join(tmp, "good.tar.gz"))
        try:
            n = _verify_targz(good)
            check("正常包能通过并数出条目数", n == 3, f"得到 {n}，期望 3")
        except BackupError as e:
            check("正常包能通过并数出条目数", False, str(e))

        # ---- 截断：这是最要命的一种。gzip 是流式格式，截断后
        #      体积可能看着"差不多"，sha256 也照样算得出来。
        raw = open(good, "rb").read()
        trunc = os.path.join(tmp, "trunc.tar.gz")
        with open(trunc, "wb") as f:
            f.write(raw[:len(raw) // 2])
        try:
            _verify_targz(trunc)
            check("截断包必须被拒绝", False, "居然通过了")
        except BackupError:
            check("截断包必须被拒绝", True)

        # ---- 只砍掉最后 20 字节（模拟传输尾部丢失，最隐蔽的一种）
        tail = os.path.join(tmp, "tailcut.tar.gz")
        with open(tail, "wb") as f:
            f.write(raw[:-20])
        try:
            _verify_targz(tail)
            check("只丢尾部 20 字节也必须被拒绝", False, "居然通过了")
        except BackupError:
            check("只丢尾部 20 字节也必须被拒绝", True)

        # ---- 空文件
        empty = os.path.join(tmp, "empty.tar.gz")
        open(empty, "wb").close()
        try:
            _verify_targz(empty)
            check("空文件必须被拒绝", False, "居然通过了")
        except BackupError:
            check("空文件必须被拒绝", True)

        # ---- 非 gzip 的垃圾数据（比如误把 stderr 文字写进了文件）
        junk = os.path.join(tmp, "junk.tar.gz")
        with open(junk, "wb") as f:
            f.write(b"tar: unknown file type '140000'\ntar: had errors\n" * 50)
        try:
            _verify_targz(junk)
            check("混入 stderr 文字的垃圾文件必须被拒绝", False, "居然通过了")
        except BackupError:
            check("混入 stderr 文字的垃圾文件必须被拒绝", True)

        # ---- 真实大小的包（557 个条目那种）不该被 500000 防呆上限误伤
        big = make_targz(os.path.join(tmp, "big.tar.gz"),
                         [f"f{i}" for i in range(2000)])
        try:
            n = _verify_targz(big)
            check("2000 条目的大包不被防呆上限误伤", n == 2000, f"得到 {n}")
        except BackupError as e:
            check("2000 条目的大包不被防呆上限误伤", False, str(e))

        print()
        print("=" * 78)
        print("二、设备端 tar 命令的约束")
        print("=" * 78)

        check("暂存目录是 /sdcard/.apb_tmp",
              DEVICE_STAGE_DIR == "/sdcard/.apb_tmp", DEVICE_STAGE_DIR)

        # su() 会把命令包进 su -c '...'，命令自身含单引号会把引号配平破坏掉，
        # Adb.su() 会直接抛 BackupError。这里复刻一遍构造逻辑做静态检查。
        tar = "/system/bin/tar"
        excl = "--exclude=./tmp --exclude=*.sock --exclude=./ksu/bin/busybox"
        dev_tar = f"{DEVICE_STAGE_DIR}/data_adb.tar.gz"
        cmd = (f"mkdir -p {DEVICE_STAGE_DIR} && cd /data/adb && "
               f"rm -f {dev_tar}; "
               f"{tar} -cz {excl} -f {dev_tar} . 2>&1; "
               f"echo __APB_RC=$?; "
               f"if [ -f {dev_tar} ]; then "
               f"echo __APB_SIZE=$(stat -c %s {dev_tar} 2>/dev/null || echo 0); "
               f"else echo __APB_MISSING=1; fi")

        check("命令中不含单引号（su -c 的硬约束）", "'" not in cmd)
        check("打包前先删除旧产物（防拉到上一次的残包）",
              f"rm -f {dev_tar}" in cmd)
        check("回传 tar 的真实退出码", "__APB_RC=$?" in cmd)
        check("回传产物体积用于交叉比对", "__APB_SIZE=" in cmd)
        check("产物缺失时有明确标记", "__APB_MISSING=1" in cmd)

        # 选项必须全部排在 `.` 之前。写成 `tar -czf 归档 --exclude=X .`
        # 是把选项夹在归档名后面，toybox 可能把 --exclude=X 当成待打包文件。
        #
        # 注意：不能直接 cmd.index(" -f ") —— 前面 `rm -f {dev_tar}` 里
        # 也有个 "-f"，会先匹配上。必须先把 tar 那一段单独切出来。
        seg = cmd[cmd.index(f"{tar} "):]
        seg = seg[:seg.index(";")]
        i_operand = seg.rindex(" .")           # 唯一的文件操作数
        i_archive = seg.index("-f ")           # 归档名（-f 的下一项）
        i_last_opt = max(seg.rindex("--exclude="), seg.index("-cz"))
        check("所有选项都排在 -f 归档名之前",
              i_last_opt < i_archive,
              f"最后选项 @{i_last_opt}，-f @{i_archive}")
        check("文件操作数 '.' 排在归档名之后",
              i_operand > i_archive,
              f"操作数 @{i_operand}，归档 @{i_archive}")
        check("选项里不再出现旧的 '-czf 归档 选项' 写法",
              "-czf" not in seg)

        print()
        print("=" * 78)
        print(f"结果：{PASS} 通过 / {FAIL} 失败")
        print("=" * 78)
        return 1 if FAIL else 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
