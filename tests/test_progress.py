# -*- coding: utf-8 -*-
"""
进度显示组件单元测试
================================================================================
覆盖 ConsoleProgress 的两条分支（TTY / 非 TTY）、SpeedMeter 平滑逻辑、
human_duration 边界值。

为什么必须专门测 TTY 分支：
    在 CI / 重定向环境下 stdout 不是 TTY，代码会自动走"每 10% 一行"的降级路径，
    真正画进度条的那段代码永远不会被执行 —— 没有测试就等于没写过。
"""
import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from backup_core import (                      # noqa: E402
    ConsoleProgress, SpeedMeter, human_duration, human_size,
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


def main():
    print("=" * 78)
    print("一、human_duration 边界值")
    print("=" * 78)
    for val, want in ((None, "--"), (-1, "--"), (float("nan"), "--"),
                      (float("inf"), "--"), (0, "0s"), (45, "45s"),
                      (60, "1m00s"), (125, "2m05s"), (3600, "1h00m"),
                      (7385, "2h03m")):
        got = human_duration(val)
        check(f"{val!r} -> {got!r}", got == want, f"期望 {want!r}")

    print()
    print("=" * 78)
    print("二、ConsoleProgress —— TTY 分支（真正画进度条）")
    print("=" * 78)
    buf = io.StringIO()
    bar = ConsoleProgress(stream=buf, force_tty=True)
    check("识别为 TTY", bar.tty is True)

    bar.update("persist", 0, 100 * 1024 * 1024, 0.0, None)
    bar.update("persist", 50 * 1024 * 1024, 100 * 1024 * 1024,
               12.5 * 1048576, 4.0)
    bar.finish()
    out = buf.getvalue()

    check("用 \\r 覆盖刷新（不刷屏）", out.count("\r") == 2, f"\\r 出现 {out.count('\r')} 次")
    check("画出了进度条方块", "█" in out and "░" in out)
    check("显示百分比", " 50.0%" in out, repr(out[-90:]))
    check("显示已传/总量", "50.0 MB" in out and "100.0 MB" in out)
    check("显示速度", "12.5 MB/s" in out)
    check("显示剩余时间", "剩 4s" in out)
    check("finish 后换行", out.endswith("\n"))

    print()
    print("=" * 78)
    print("三、ConsoleProgress —— 非 TTY 分支（降级为每 10% 一行）")
    print("=" * 78)
    buf2 = io.StringIO()
    bar2 = ConsoleProgress(stream=buf2)          # StringIO 没有 isatty → 非 TTY
    check("识别为非 TTY", bar2.tty is False)

    lines_seen = []
    for pct in range(0, 101):
        before = buf2.getvalue().count("\n")
        bar2.update("modemst1", pct, 100, 5 * 1048576, 1.0)
        after = buf2.getvalue().count("\n")
        if after > before:
            lines_seen.append(pct)
    out2 = buf2.getvalue()

    check("绝不输出 \\r", "\r" not in out2, f"发现 {out2.count(chr(13))} 个")
    check("输出行数 ≤ 12（每 10% 一行）", len(lines_seen) <= 12,
          f"输出 {len(lines_seen)} 行，触发点 {lines_seen}")
    check("第 0% 有输出", lines_seen and lines_seen[0] == 0, str(lines_seen))
    check("包含百分比文本", "%" in out2)
    check("非 TTY 不画方块", "█" not in out2)

    print()
    print("=" * 78)
    print("四、ConsoleProgress —— 关闭开关")
    print("=" * 78)
    buf3 = io.StringIO()
    bar3 = ConsoleProgress(stream=buf3, force_tty=True, enabled=False)
    bar3.update("x", 50, 100, 1.0, 1.0)
    bar3.finish()
    check("enabled=False 时完全静默", buf3.getvalue() == "", repr(buf3.getvalue()))

    print()
    print("=" * 78)
    print("五、ConsoleProgress —— 边界与异常输入")
    print("=" * 78)
    buf4 = io.StringIO()
    bar4 = ConsoleProgress(stream=buf4, force_tty=True)
    bar4.update("zero", 0, 0)                    # total=0 必须不崩（除零）
    check("total=0 不崩溃也不输出", buf4.getvalue() == "", repr(buf4.getvalue()))
    bar4.update("over", 200, 100, 1.0, None)     # done > total
    check("done>total 被夹到 100%", "100.0%" in buf4.getvalue())
    bar4.finish()
    bar4.finish()                                # 重复 finish 必须安全
    check("重复 finish 不报错", True)

    print()
    print("=" * 78)
    print("六、SpeedMeter 平滑逻辑")
    print("=" * 78)
    m = SpeedMeter(alpha=0.5)
    r0 = m.sample(10 * 1048576, 1.0)             # 第一个样本：只建立基准
    check("首样本速度为 0（无历史）", r0 == 0.0, str(r0))

    r1 = m.sample(20 * 1048576, 2.0)             # 1 秒传 10MB
    check("第二样本约 10 MB/s", abs(r1 - 10 * 1048576) < 1, str(r1))

    r2 = m.sample(62 * 1048576, 3.0)             # 这次 1 秒传 42MB，应被平滑拉低
    check("突增被平滑（不直接跳到 42MB/s）",
          10 * 1048576 < r2 < 42 * 1048576, f"{r2/1048576:.1f} MB/s")

    eta = m.eta(62 * 1048576, 100 * 1048576)
    check("ETA = 剩余字节 / 速度",
          eta is not None and abs(eta - (38 * 1048576 / r2)) < 0.01,
          str(eta))
    check("已完成时 ETA 为 None", m.eta(100, 100) is None)
    check("速度未知时 ETA 为 None", SpeedMeter().eta(0, 100) is None)

    # 时间倒流 / 重复采样应被忽略而不是产生负速度
    m2 = SpeedMeter()
    m2.sample(1000, 5.0)
    m2.sample(2000, 4.0)                         # dt < 0
    check("时间倒流不产生负速度", m2.rate >= 0, str(m2.rate))

    print()
    print("=" * 78)
    print(f"结果：{PASS} 通过 / {FAIL} 失败")
    print("=" * 78)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
