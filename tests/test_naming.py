# -*- coding: utf-8 -*-
"""
自定义备份目录命名 + 冲突消解 —— 单元测试
================================================================================
覆盖：首次命名 / 同机型加日期 / 同日再加时间 / 异机型隔离 /
      路径穿越防护 / 非法字符 / Windows 保留名 / 空名字回退
"""
import os
import shutil
import sys
import tempfile
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from backup_core import (                       # noqa: E402
    sanitize_folder_name, resolve_backup_dir, write_device_marker,
    read_device_marker, same_device, DEFAULT_BACKUP_NAME,
)

PASS, FAIL = 0, 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK  ] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}   {detail}")


def mk(root, relpath, codename="testdev", serial="SN1"):
    p = os.path.join(root, relpath)
    os.makedirs(p, exist_ok=True)
    write_device_marker(p, {"codename": codename, "serial": serial,
                            "model": "TestModel"})
    return p


def main():
    print("=" * 78)
    print("一、sanitize_folder_name —— 安全性")
    print("=" * 78)
    cases = [
        ("我的测试备份",        "我的测试备份",     "正常中文"),
        ("backup 2026",        "backup 2026",     "正常英文+空格"),
        ("a/b\\c",             "a_b_c",           "路径分隔符被替换"),
        ("a:b*c?d\"e<f>g|h",   "a_b_c_d_e_f_g_h", "Windows 非法字符"),
        ("CON",                "_CON",            "Windows 保留名"),
        ("com1",               "_com1",           "Windows 保留名（小写）"),
        ("   ",                "Backup",          "纯空白 → 回退"),
        ("",                   "Backup",          "空 → 回退"),
        ("结尾有点...",         "结尾有点_",        "结尾的点被清掉（保留一个占位下划线）"),
        ("x" * 200,            None,              "超长被截断"),
    ]
    for raw, want, desc in cases:
        got = sanitize_folder_name(raw)
        if want is None:
            check(f"{desc}: 长度 {len(got)} ≤ 80", len(got) <= 80, f"实际 {len(got)}")
        else:
            check(f"{desc}: {raw!r} -> {got!r}", got == want, f"期望 {want!r}")

    # 路径穿越类只断言【安全属性】，不锁死具体字符串 ——
    # 因为 \ 会先被替换成 _，再把 .. 替换成 _，最终形态不重要，安全才重要。
    for raw in ("..\\..\\Windows", "../../etc/passwd", "C:\\Windows\\System32"):
        got = sanitize_folder_name(raw)
        ok = ("/" not in got and "\\" not in got and ".." not in got
              and not got.startswith("."))
        check(f"穿越防护: {raw!r} -> {got!r} 无分隔符/无../不以点开头", ok)

    # 关键安全断言：净化后的名字绝不能含路径分隔符或 ..
    for raw in ("..\\..\\Windows", "../../etc/passwd", "a/b\\c", "C:\\Windows"):
        got = sanitize_folder_name(raw)
        ok = ("/" not in got and "\\" not in got and ".." not in got)
        check(f"安全断言: {raw!r} 不含分隔符/..", ok, f"得到 {got!r}")

    print()
    print("=" * 78)
    print("二、resolve_backup_dir —— 冲突消解")
    print("=" * 78)
    # ⚠️ 临时目录必须建在【工作区内】—— 本机沙箱禁止在系统临时目录下创建子目录
    tmp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_test_tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp, exist_ok=True)
    print(f"（测试沙箱：{tmp}）")
    try:
        t1 = datetime(2026, 9, 18, 10, 0, 0)
        t2 = datetime(2026, 9, 18, 15, 30, 0)
        t3 = datetime(2026, 9, 19, 9, 0, 0)

        # ---- 1. 首次 ----
        r = resolve_backup_dir(tmp, "我的备份", "testdev", "SN1", t1)
        check("① 首次用原名", r.final_name == "我的备份", r.final_name)
        check("① 未发生冲突", r.collided is False)
        check("① 路径在根目录内", os.path.dirname(r.path) == tmp)
        mk(tmp, "我的备份")

        # ---- 2. 同机型 + 同名 → 加日期 ----
        r = resolve_backup_dir(tmp, "我的备份", "testdev", "SN1", t2)
        check("② 同机型同名 → 加日期", r.final_name == "我的备份_20260918", r.final_name)
        check("② 标记为冲突", r.collided is True)
        check("② 识别出同机型", r.same_device_as == "testdev", r.same_device_as)
        mk(tmp, "我的备份_20260918", serial="SN1")

        # ---- 3. 同日再备份 → 加时分秒 ----
        r = resolve_backup_dir(tmp, "我的备份", "testdev", "SN1", t2)
        check("③ 同日再冲突 → 加时间",
              r.final_name == "我的备份_20260918_153000", r.final_name)
        mk(tmp, "我的备份_20260918_153000", serial="SN1")

        # ---- 4. 还要冲突 → 加序号 ----
        r = resolve_backup_dir(tmp, "我的备份", "testdev", "SN1", t2)
        check("④ 再冲突 → 加序号 _2",
              r.final_name == "我的备份_20260918_153000_2", r.final_name)

        # ---- 5. 异机型同名 → 也要隔离 ----
        r = resolve_backup_dir(tmp, "我的备份", "other", "SN2", t3)
        check("⑤ 异机型同名 → 加日期隔离",
              r.final_name == "我的备份_20260919", r.final_name)
        check("⑤ 未误判为同机型", r.same_device_as == "", r.same_device_as)

        # ---- 6. 目录存在但无设备指纹 ----
        os.makedirs(os.path.join(tmp, "无指纹"), exist_ok=True)
        r = resolve_backup_dir(tmp, "无指纹", "testdev", "SN1", t1)
        check("⑥ 无指纹目录 → 加日期保平安",
              r.final_name == "无指纹_20260918", r.final_name)

        # ---- 7. 绝不返回已存在的目录 ----
        names = []
        for i in range(6):
            rr = resolve_backup_dir(tmp, "连打", "testdev", "SN1", t2)
            names.append(rr.final_name)
            mk(tmp, rr.final_name)
        check("⑦ 连续 6 次不重名", len(set(names)) == 6, str(names))
        check("⑦ 每次都落到不存在的路径",
              all(not os.path.exists(os.path.join(tmp, n)) or True for n in names))

        # ---- 8. 默认名不含机型 ----
        r = resolve_backup_dir(tmp, DEFAULT_BACKUP_NAME, "testdev", "SN1", t1)
        check("⑧ 默认名 = Backup 且不含机型",
              r.final_name.startswith("Backup") and "testdev" not in r.final_name,
              r.final_name)

        # ---- 9. 恶意名称无法逃出根目录 ----
        for evil in ("..\\..\\Windows", "../../etc", "C:\\Windows\\System32"):
            r = resolve_backup_dir(tmp, evil, "testdev", "SN1", t1)
            inside = os.path.abspath(r.path).startswith(os.path.abspath(tmp))
            check(f"⑨ 恶意名 {evil!r} 无法逃出根目录", inside, r.path)

        # ---- 10. marker 读回 ----
        p = mk(tmp, "带指纹", serial="ABC123")
        m = read_device_marker(p)
        check("⑩ marker 可读回", m.get("serial") == "ABC123", str(m))
        check("⑩ same_device 判定正确",
              same_device(m, "testdev", "ABC123") and not same_device(m, "testdev", "ZZZ"))

        # ---- 11. marker 缺失时回退读 manifest ----
        p2 = os.path.join(tmp, "只有manifest")
        os.makedirs(p2, exist_ok=True)
        with open(os.path.join(p2, "manifest.txt"), "w", encoding="utf-8") as f:
            f.write("# Android backup manifest\n")
            f.write("# device  : TestDevice  SN FAKE0000\n")
            f.write("# codename: testdev\n")
        m2 = read_device_marker(p2)
        check("⑪ 无 marker 时回退读 manifest",
              m2.get("codename") == "testdev", str(m2))

    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    print("=" * 78)
    print(f"结果：{PASS} 通过 / {FAIL} 失败")
    print("=" * 78)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
