# -*- coding: utf-8 -*-
"""
GUI 冒烟测试 —— 无人值守地验证界面能构建、能渲染、能安全退出。

它做的事：
  1. 创建 Tk 根窗口与完整界面
  2. 跑事件循环直到设备信息就绪（或超时）
  3. 检查关键控件存在、状态合理
  4. 模拟勾选 / 排序 / 预设切换 / 过滤等交互
  5. 干净地关闭

任何异常都以非零退出码结束，便于自动化判定。
用法： python _tests/smoke_gui.py
"""
import os
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import tkinter as tk                       # noqa: E402

import backup_gui                          # noqa: E402
from backup_gui import BackupApp           # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
    if not cond:
        FAILURES.append(name)


def main():
    print("=" * 70)
    print("GUI 冒烟测试")
    print("=" * 70)

    root = tk.Tk()
    root.withdraw()                        # 不真的弹窗，避免干扰

    print("\n[1] 构建界面 ...")
    app = BackupApp(root)
    check("BackupApp 实例化", app is not None)
    check("找到 adb", bool(app.adb_path), app.adb_path or "(未找到)")

    print("\n[2] 关键控件存在性 ...")
    for attr in ("tree", "log", "pb_item", "pb_all", "btn_start", "btn_cancel",
                 "btn_open", "lbl_dev", "lbl_dot", "lbl_sum", "lbl_result",
                 "lbl_cur", "lbl_status", "lbl_preview", "out_var", "name_var",
                 "lbl_item_pct", "lbl_all_pct", "lbl_all_name"):
        check(f"控件 {attr}", hasattr(app, attr) and getattr(app, attr) is not None)

    print("\n[3] 表格列配置 ...")
    cols = app.tree["columns"]
    check("5 列", len(cols) == 5, str(cols))
    check("含 chk 列", "chk" in cols)

    print("\n[4] 等待设备信息就绪 ...")
    t0 = time.time()
    while time.time() - t0 < 40:
        root.update()
        if app.partitions:
            break
        time.sleep(0.1)
    check("事件循环未崩溃", True)
    check("已获取分区列表", bool(app.partitions),
          f"{len(app.partitions)} 个分区，耗时 {time.time()-t0:.1f}s")
    print(f"       设备探测耗时 {time.time()-t0:.1f}s")

    print("\n[5] 设备状态判定 ...")
    print(f"       设备标签: {app.lbl_dev.cget('text')}")
    print(f"       状态栏  : {app.lbl_status.cget('text')}")
    if app.partitions:
        check("已排除整盘", not any(
            p.name in ("sda", "sdb", "sdc", "sdd", "sde", "sdf") for p in app.partitions))
        bad = [p.name for p in app.partitions if p.tier == 0]
        check("无未分类分区", len(bad) == 0, f"未分类 {len(bad)} 个: {bad[:5]}")

        print("\n[6] 预设切换 ...")
        for key in ("critical", "critical+root", "all-useful", "everything", "custom"):
            app.preset_var.set(key)
            app._apply_preset()
            print(f"       {key:<16} -> 已选 {len(app.checked)} 项")
        app.preset_var.set("critical")
        app._apply_preset()
        check("critical 只含 Tier1",
              all(p.tier == 1 for p in app.partitions if p.name in app.checked))
        check("critical 不含 userdata", "userdata" not in app.checked)
        check("critical 不含 super", "super" not in app.checked)

        print("\n[7] 勾选与批量操作 ...")
        app._bulk("all")
        n_all = len(app.checked)
        app._bulk("none")
        check("全不选清空", len(app.checked) == 0, f"全选时 {n_all} 项")
        app._bulk("invert")
        check("反选后有内容", len(app.checked) > 0, f"{len(app.checked)} 项")
        app._bulk("none")
        app._bulk("critical")
        check("仅不可再生 = Tier1",
              all(p.tier == 1 for p in app.partitions if p.name in app.checked))

        print("\n[8] 排序 ...")
        for k in ("name", "size", "tier"):
            app._sort_by(k)
        check("三种排序均无异常", True)

        print("\n[9] 过滤与隐藏 ...")
        app.filter_var.set("boot")
        root.update()
        vis = app._visible()
        check("过滤生效", all("boot" in p.name.lower() or "boot" in (p.reason or "").lower()
                              for p in vis), f"{len(vis)} 项")
        app.filter_var.set("")
        app.hide_low.set(True)
        app._render_rows()
        check("隐藏低价值生效", not any(p.tier == 4 for p in app._visible()))
        app.hide_low.set(False)
        app._render_rows()

        print("\n[10] 命名预览 ...")
        app.name_var.set("")
        app._preview_path()
        prev_empty = app.lbl_preview.cget("text")
        check("留空时预览不含机型代号",
              (not code) or (code not in prev_empty), prev_empty[:70])
        app.name_var.set("我的备份")
        app._preview_path()
        prev_named = app.lbl_preview.cget("text")
        check("自定义名出现在预览里", "我的备份" in prev_named, prev_named[:70])
        app.name_var.set("..\\..\\Windows")
        app._preview_path()
        prev_evil = app.lbl_preview.cget("text")
        check("恶意名被净化", "..\\" not in prev_evil and "..\\\\" not in prev_evil,
              prev_evil[:70])
        app.name_var.set("")
    else:
        print("       (设备未连接，跳过依赖设备列表的检查)")

    print("\n[11] 进度显示逻辑（喂合成进度事件，不依赖真实备份）...")
    MB = 1024 * 1024
    app._t_start = time.time() - 5.0          # 假装已经跑了 5 秒
    app._total_bytes = 100 * MB
    app._done_bytes = 20 * MB
    app._cur_size = 0
    app._meter = backup_gui.SpeedMeter()

    app._on_progress({"phase": "start", "item": "persist", "expect": 32 * MB})
    root.update()
    check("start 阶段归零", float(app.pb_item.cget("value")) == 0.0,
          str(app.pb_item.cget("value")))

    # 推进到一半，多喂几次让平滑器有样本
    for done in (4 * MB, 8 * MB, 12 * MB, 16 * MB):
        app._on_progress({"phase": "item", "item": "persist",
                          "done": done, "expect": 32 * MB})
        root.update()
    check("当前项进度条到 50%", abs(float(app.pb_item.cget("value")) - 50.0) < 0.1,
          str(app.pb_item.cget("value")))
    item_txt = app.lbl_item_pct.cget("text")
    check("当前项显示速度与 ETA", "MB/s" in item_txt and "剩" in item_txt, item_txt)

    all_txt = app.lbl_all_pct.cget("text")
    # 已完成 20MB + 当前项 16MB = 36MB / 100MB
    check("总体进度含当前项", "36.0 MB" in all_txt, all_txt)
    check("总体显示 ETA", "剩" in all_txt, all_txt)
    check("窗口标题带百分比", "%" in root.title(), root.title())

    app._on_progress({"phase": "hash", "item": "persist"})
    root.update()
    check("校验阶段切滚动条",
          str(app.pb_item.cget("mode")) == "indeterminate",
          str(app.pb_item.cget("mode")))

    app._on_progress({"phase": "item_done", "item": "persist", "done": 1, "total": 13})
    root.update()
    check("总计项数更新", "1/13" in app.lbl_all_name.cget("text"),
          app.lbl_all_name.cget("text"))
    check("item_done 累加已完成字节",
          abs(app._done_bytes - 52 * MB) < 1, str(app._done_bytes))

    app._on_progress({"phase": "gpt", "item": "sda"})
    root.update()
    check("GPT 阶段切滚动条",
          str(app.pb_item.cget("mode")) == "indeterminate")
    check("GPT 阶段标签正确", "GPT" in app.lbl_cur.cget("text"),
          app.lbl_cur.cget("text"))

    print("\n[12] 日志分级着色 ...")
    # 第 4 条是回归用例：它同时含"失败"字样和 [!] 前缀，
    # 必须判成告警而不是错误 —— 前缀标记的优先级高于关键词启发。
    for text, want in (
            ("===== 备份 13 个分区 =====", "head"),
            ("  [OK] persist  32.0 MB", "ok"),
            ("  [X] persist 失败: 超时", "err"),
            ("  [!] 流式失败，回退到设备端暂存模式 ...", "warn"),   # ← 回归用例
            ("  [i] 设备无 sgdisk，跳过结构化备份", "warn"),
            ("  [X] 校验失败 设备=abc 本地=def", "err"),
            ("  已保存 by-name 映射表（134 行）", "dim"),
            ("  普通信息行", ""),
    ):
        got = app._log_level(text)
        check(f"级别 {want or '默认'}: {text.strip()[:30]!r}", got == want,
              f"得到 {got!r}")
    app._log("  [OK] 测试成功行")
    app._log("  [X] 测试失败行")
    root.update()
    content = app.log.get("1.0", "end")
    check("日志确实写入了", "测试成功行" in content and "测试失败行" in content)

    print("\n[13] 关闭窗口 ...")
    app.poll_stop.set()
    root.update()
    root.destroy()
    check("窗口已销毁", True)

    print("\n" + "=" * 70)
    if FAILURES:
        print(f"结果：{len(FAILURES)} 项失败")
        for f in FAILURES:
            print(f"   - {f}")
        return 1
    print("结果：全部通过 ✅")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(3)
