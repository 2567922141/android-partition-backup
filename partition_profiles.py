# -*- coding: utf-8 -*-
"""
分区画像库 —— 平台特征、分级规则、预设方案
================================================================================
本模块是纯数据 + 纯函数，不做任何 IO，方便单独审查与扩充。

【设计原则】
    不硬编码任何具体机型的分区清单。
    分区按【语义】分成 4 级，适用于任意 Android 设备：

        Tier 1  ★不可再生   —— 含设备唯一数据，丢了官方固件也救不回来
        Tier 2  root 基准   —— 你修补过的系统分区，官方只有未修补版
        Tier 3  固件可重建   —— 官方刷机包里就有，备份价值低
        Tier 4  低价值      —— 厂商备份槽 / 日志 / 对齐填充，基本无意义

【如何扩充】
    在 TIER1/2/3/4_RULES 里加一条 (fnmatch 模式, 说明) 即可，
    无需改动任何其他代码。
================================================================================
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from typing import Iterable, Optional

PROFILES_VERSION = "1.0.0"


# ==============================================================================
#  平台特征
# ==============================================================================
#  ⚠️ 数据可信度分级 —— 请勿把"未核实"当成"错误"，但也别当成"已确认"。
#
#  【已外部核实】2026-09-18，两个以上独立来源：
#
#    高通 Qualcomm  fsg / fsc / modemst1 / modemst2
#       来源: onfix.cn《手机字库基带使用命令备份指南》
#             https://onfix.cn/course/4780?bid=1&mid=28642      (Redmi K70 至尊版)
#             https://onfix.cn/course/4780?bid=1&mid=29847      (Redmi Note 15 Pro+ 5G)
#       原文: "高通机型：fsg，fsc，modemst1，modemst2"
#
#    联发科 MediaTek  nvram / nvdata / nvcfg / protect1 / protect2 / seccfg
#       来源: 同上两页（其中 Note 15 Pro+ 本身就是联发科机型）
#       原文: "联发科机型：nvram，nvdata，nvcfg，persist，protect1，protect2，seccfg"
#
#    三星 Samsung  efs / sec_efs
#       来源: android-hilfe.de SM-N986B(Note 20 Ultra) IMEI 修复帖标题即
#             "ohne gespeicherte efs oder sec_efs"（无 efs 或 sec_efs 备份）
#    三星 Samsung  param / steady
#       来源: github.com/CamsShaft/arbitrary-R-W-for-steady-and-param-partitions
#             描述: "...reading from and writing to the steady and param
#                    partitions in most flagship Samsung devices"
#
#  【领域知识，未找到公开来源核实】—— 保留，因为模式不匹配时不会误伤：
#    展锐 Unisoc   prodnv / nvitem / wcnmodem / splloader
#    三星 Samsung  up_param
#    Tensor        ldfw
#
#  该来源另外印证了三个设计决策：
#    · 联发科机型的 by-name 路径与高通不同
#     （高通 bootdevice/by-name；联发科 by-name）
#      → 证明 BYNAME_CANDIDATES 多路径探测是必要的
#    · 教程的备份脚本明确排除 userdata 与 cache
#      → 与本文件 PRESET_EXCLUDE 的设计一致
#    · 教程警告"不要恢复他人备份，会刷进别人串码导致不能解 BL"
#      → 与 README 恢复铁律第 1 条（严禁跨设备恢复）一致
# ==============================================================================

@dataclass(frozen=True)
class Platform:
    key: str
    display: str
    baseband: str
    # 用于从分区名反推平台的"签名分区"
    signatures: tuple[str, ...]
    notes: str = ""


PLATFORMS: tuple[Platform, ...] = (
    Platform(
        key="qualcomm", display="高通 Qualcomm", baseband="高通基带",
        signatures=("modemst1", "modemst2", "fsg", "tz", "hyp", "abl", "xbl",
                    "devcfg", "qupfw", "cmnlib", "keymaster"),
        notes="IMEI/NV 走 modemst1+modemst2+fsg 三副本，另有 persist 存校准数据",
    ),
    Platform(
        key="mediatek", display="联发科 MediaTek", baseband="联发科基带",
        signatures=("nvdata", "nvram", "nvcfg", "protect1", "protect2",
                    "preloader", "md1img", "seccfg", "proinfo", "spmfw"),
        notes="IMEI/NV 走 nvdata + nvram + protect1/2，校准数据多在 persist",
    ),
    Platform(
        key="samsung", display="三星 Samsung", baseband="Exynos/高通",
        signatures=("efs", "sec_efs", "param", "steady", "up_param",
                    "keystore", "sec_efs_backup"),
        notes="IMEI 在 efs/sec_efs，另有 param 与 steady 两个设备专属分区",
    ),
    Platform(
        key="unisoc", display="紫光展锐 Unisoc", baseband="展锐基带",
        signatures=("prodnv", "nvitem", "wcnmodem", "splloader", "prodnv_backup"),
        notes="NV 数据在 prodnv / nvitem，可能有 prodnv_backup 冗余副本",
    ),
    Platform(
        key="tensor", display="Google Tensor", baseband="三星基带(Exynos Modem)",
        signatures=("modemst1", "modemst2", "fsg", "efs", "ldfw"),
        notes="Pixel 上同时存在高通风格 modemst 与三星风格 efs，两者都要保",
    ),
)

PLATFORM_GENERIC = Platform(
    key="generic", display="未识别（通用规则）", baseband="未知",
    signatures=(), notes="未能识别 SoC 平台，按通用关键词分级",
)


def detect_platform(partition_names: Iterable[str],
                    prop_hints: str = "") -> tuple[Platform, int, dict[str, int]]:
    """
    从分区名推断平台。

    为什么以分区名为主、props 为辅：
        ro.board.platform 之类的属性会被厂商改名甚至被模块 resetprop 伪装，
        而分区名是内核枚举出来的物理事实，几乎无法造假。

    返回 (平台, 得分, 各平台得分明细)
    """
    names = {n.lower() for n in partition_names}
    joined = " ".join(sorted(names))
    hint = (prop_hints or "").lower()

    scores: dict[str, int] = {}
    for pf in PLATFORMS:
        s = 0
        for sig in pf.signatures:
            if sig in names:
                s += 3                       # 精确命中分区名，权重高
            elif sig in joined:
                s += 1                       # 子串命中（如 modemst1 命中 modemst）
        # props 提示作为加分项，权重刻意压低
        for token in ("qcom", "msm", "snapdragon", "sm8", "sm7", "sdm", "kalama",
                      "taro", "pineapple", "cape", "vermeer", "sun", "peridot"):
            if token in hint and pf.key == "qualcomm":
                s += 2
        for token in ("mediatek", "mt6", "mt8", "dimensity", "helio"):
            if token in hint and pf.key == "mediatek":
                s += 2
        for token in ("exynos", "s5e", "samsung"):
            if token in hint and pf.key == "samsung":
                s += 2
        for token in ("unisoc", "ums", "sc9", "sprd", "spreadtrum"):
            if token in hint and pf.key == "unisoc":
                s += 2
        for token in ("tensor", "gs101", "gs201", "zuma", "ripslinger", "husky"):
            if token in hint and pf.key == "tensor":
                s += 2
        scores[pf.key] = s

    best_key = max(scores, key=lambda k: scores[k]) if scores else "generic"
    best_score = scores.get(best_key, 0)
    if best_score < 3:                        # 证据不足就不硬猜
        return PLATFORM_GENERIC, best_score, scores
    for pf in PLATFORMS:
        if pf.key == best_key:
            return pf, best_score, scores
    return PLATFORM_GENERIC, best_score, scores


# ==============================================================================
#  分区分级规则
# ==============================================================================
#  每条规则: (fnmatch 模式, 人类可读的原因)
#  匹配是大小写不敏感的；同一个分区命中多条时，取【级别数字最小】的那条
#  （即 Tier 1 优先于 Tier 2，Tier 2 优先于 Tier 3 ...）
# ==============================================================================

TIER1_RULES: tuple[tuple[str, str], ...] = (
    # ---- 通用 Android ----
    ("persist",           "指纹/屏幕/摄像头/传感器校准 + WiFi·BT MAC"),
    ("persist*",          "校准数据变体"),
    ("devinfo",           "BL 解锁戳记 / 保修标志"),
    ("secdata",           "防回滚单向计数器（永远不要恢复旧值）"),
    ("frp",               "恢复出厂保护（防盗锁）"),
    ("keystore",          "Android 密钥库"),
    ("countrycode",       "国家/地区码"),
    ("uefivarstore",      "UEFI 变量存储"),
    ("apdp",              "调试策略"),
    ("apdpb",             "调试策略（副本）"),
    # ---- 高通 ----
    ("modemst*",          "基带 NV / IMEI（高通，主+备）"),
    ("fsg",               "基带 Golden Copy 黄金副本（高通）"),
    ("fsc",               "基带缓存（高通，由 modemst 派生）"),
    # ---- 联发科 ----
    ("nvdata",            "基带 NV 数据（联发科）"),
    ("nvdata*",           "基带 NV 数据（联发科）"),
    ("nvram",             "基带 NVRAM（联发科）"),
    ("nvcfg",             "基带 NV 配置（联发科）"),
    ("nvmem",             "NV 存储（联发科）"),
    ("nvitem",            "NV 项（展锐/联发科）"),
    ("protect1",          "安全保护分区（联发科）"),
    ("protect2",          "安全保护分区（联发科）"),
    ("seccfg",            "安全配置（联发科）"),
    ("proinfo",           "产品信息（联发科）"),
    # ---- 三星 ----
    ("efs",               "IMEI / 射频校准（三星）"),
    ("efs*",              "IMEI / 射频校准（三星）"),
    ("sec_efs",           "EFS 备份（三星）"),
    ("param",             "启动参数（三星）"),
    ("up_param",          "参数分区（三星）"),
    ("steady",            "校准/稳态数据（三星）"),
    # ---- 紫光展锐 ----
    ("prodnv",            "产品 NV（展锐）"),
    ("prodnv*",           "产品 NV 备份（展锐）"),
    ("wcn*",              "无线连接校准（展锐）"),
)

TIER2_RULES: tuple[tuple[str, str], ...] = (
    ("init_boot",         "⭐ root 关键 —— 修补过的版本官方包里没有"),
    ("init_boot*",        "⭐ root 关键"),
    ("boot",              "内核 + ramdisk"),
    ("boot*",             "内核 + ramdisk"),
    ("vendor_boot",       "vendor ramdisk"),
    ("vendor_boot*",      "vendor ramdisk"),
    ("dtbo",              "设备树叠加层"),
    ("dtbo*",             "设备树叠加层"),
    ("vbmeta",            "验证启动元数据（改机后与官方版不同）"),
    ("vbmeta*",           "验证启动元数据"),
    ("recovery",          "恢复模式"),
    ("recovery*",         "恢复模式"),
    ("lk",                "Little Kernel 引导（联发科）"),
    ("lk*",               "Little Kernel 引导（联发科）"),
)

TIER4_RULES: tuple[tuple[str, str], ...] = (
    ("bk*",               "厂商预留的备份槽，无独立价值"),
    ("ALIGN*",            "分区对齐填充"),
    ("align*",            "分区对齐填充"),
    ("ssd",               "对齐/占位分区"),
    ("logfs",             "日志文件系统"),
    ("log*",              "日志分区"),
    ("oops",              "崩溃日志"),
    ("rawdump",           "原始转储区"),
    ("rescue",            "救援分区"),
    ("dbg",               "调试分区"),
    ("mtdblk",            "块设备元数据"),
    ("ffu",               "固件更新缓冲"),
    ("switch",            "开关标志位"),
    ("*_dump",            "转储分区"),
    ("minidump",          "小型转储"),
    ("crash*",            "崩溃记录"),
    ("blackbox",          "黑盒日志"),
    ("spmfw",             "电源管理固件（联发科，可重建）"),
)

TIER3_RULES: tuple[tuple[str, str], ...] = (
    ("system",            "系统分区，官方包可重建"),
    ("system*",           "系统分区，官方包可重建"),
    ("vendor",            "厂商分区，官方包可重建"),
    ("vendor*",           "厂商分区，官方包可重建"),
    ("product",           "产品分区，官方包可重建"),
    ("product*",          "产品分区，官方包可重建"),
    ("odm",               "ODM 分区，官方包可重建"),
    ("odm*",              "ODM 分区，官方包可重建"),
    ("system_ext",        "系统扩展，官方包可重建"),
    ("my_*",              "厂商定制分区"),
    ("super",             "动态分区容器（内含 system/vendor/product）"),
    ("metadata",          "加密元数据（恢复旧值可能导致无法解密）"),
    ("misc",              "启动控制块，官方包可重建"),
    ("modem",             "基带固件，官方包可重建"),
    ("modem*",            "基带固件，官方包可重建"),
    ("mdtp*",             "Modem 调试服务"),
    ("dsp",               "数字信号处理器固件"),
    ("bluetooth",         "蓝牙固件"),
    ("abl",               "Android 引导程序"),
    ("xbl*",              "Extensible Bootloader"),
    ("tz",                "TrustZone 固件"),
    ("hyp",               "Hypervisor 固件"),
    ("aop*",              "Always-On 处理器固件"),
    ("devcfg",            "设备配置"),
    ("qupfw",             "QUP 固件"),
    ("cmnlib*",           "公共库固件"),
    ("keymaster",         "密钥管理固件"),
    ("imagefv",           "镜像验证"),
    ("uefi*",             "UEFI 固件"),
    ("shrm",              "系统硬件资源管理"),
    ("cpucp",             "CPU 控制处理器"),
    ("featenabler",       "特性开关"),
    ("qweslicstore",      "QWE 许可存储"),
    ("xbl_ramdump",       "XBL 转储"),
    ("multiimgoem",       "多镜像 OEM"),
    ("rticmpdata",        "运行时完整性数据"),
    ("qmcs",              "QMC 服务"),
    ("mdtpsecapp*",       "Modem 调试安全应用"),
    ("preloader",         "预引导程序（联发科）"),
    ("md1img",            "Modem 镜像（联发科）"),
    ("scp*",              "协处理器固件（联发科）"),
    ("sspm*",             "安全系统电源管理（联发科）"),
    ("mcupm*",            "MCU 电源管理（联发科）"),
    ("pi_img",            "平台完整性镜像"),
    ("dpm*",              "数据保护管理"),
    ("gz*",               "Granule 保护（联发科）"),
    ("tee*",              "可信执行环境"),
    ("vbmeta_system",     "系统验证元数据"),

    # ---- 补充：实机测试后补齐的常见分区（2026-09-18 首轮实机跑出的未分类项）----
    ("abl*",              "Android 引导程序（A/B 双槽）"),
    ("tz*",               "TrustZone 固件"),
    ("hyp*",              "Hypervisor 固件"),
    ("devcfg*",           "设备配置"),
    ("keymaster*",        "密钥管理固件"),
    ("qupfw*",            "QUP 固件"),
    ("cpucp*",            "CPU 控制处理器"),
    ("shrm*",             "系统硬件资源管理"),
    ("featenabler*",      "特性开关"),
    ("multiimgoem*",      "多镜像 OEM"),
    ("multiimgqti*",      "多镜像 QTI"),
    ("qweslicstore*",     "QWE 许可存储"),
    ("rticmpdata*",       "运行时完整性数据"),
    ("dsp*",              "数字信号处理器固件"),
    ("bluetooth*",        "蓝牙固件"),
    ("imagefv*",          "镜像验证"),
    ("aop*",              "Always-On 处理器固件"),
    ("userdata",          "个人数据与隐私 —— 体积可达数百 GB，请按需手动勾选"),
    ("splash",            "开机画面"),
    ("spunvm",            "SPU 虚拟机固件"),
    ("storsec",           "存储安全固件"),
    ("tzsc",              "TrustZone 安全配置"),
    ("ddr",               "DDR 训练参数（缺失时引导程序会重建）"),
    ("dip",               "显示参数"),
    ("mem",               "内存保留区"),
    ("opconfig",          "运营商配置"),
    ("mbnconfig",         "基带配置"),
    ("mdcompress",        "基带压缩固件"),
    ("cdt",               "平台配置表（Configuration Data Table）"),
    ("gsort",             "排序/索引数据"),
    ("limits*",           "温度/功耗限制表"),
    ("toolsfv",           "工具固件"),
    ("connsec",           "连接安全配置"),
)


# ==============================================================================
#  分级结果
# ==============================================================================

TIER_LABELS = {
    1: "★不可再生",
    2: "root基准",
    3: "固件可重建",
    4: "低价值",
    0: "未分类",
}

# 级别优先级：数字越小越"重要"。决定同一分区命中多条规则时谁胜出。
# 刻意让 Tier 4（低价值）压过 Tier 3（固件）—— 一个明显的日志/备份槽
# 不该被当成"有用的固件分区"。
TIER_ORDER = {1: 1, 2: 2, 4: 3, 3: 4, 0: 5}


@dataclass(frozen=True)
class Classification:
    tier: int
    label: str
    reason: str
    platform_key: str = ""

    @property
    def is_critical(self) -> bool:
        return self.tier == 1


def classify(name: str, platform: Optional[Platform] = None) -> Classification:
    """
    按语义给分区分级。命中多条规则时取优先级最高的（见 TIER_ORDER）。

    ⚠️ 关键细节：匹配前必须先尝试【去掉 A/B 槽位后缀】。
       规则表是按无槽位名字写的（abl / tz / hyp），
       而设备上实际叫 abl_a / tz_b —— 不剥后缀会导致大量分区"未分类"。
    """
    candidates = [name.lower()]
    base, slot = split_slot(name)
    if slot and base.lower() != name.lower():
        candidates.append(base.lower())

    hits: list[tuple[int, str]] = []
    for cand in candidates:
        for tier, rules in ((1, TIER1_RULES), (2, TIER2_RULES),
                            (4, TIER4_RULES), (3, TIER3_RULES)):
            for pattern, reason in rules:
                if fnmatch.fnmatch(cand, pattern.lower()):
                    hits.append((tier, reason))
                    break
        if hits:
            break                      # 原始名一旦命中就不再回退到剥后缀的名字

    if not hits:
        return Classification(tier=0, label=TIER_LABELS[0],
                              reason="未匹配任何已知规则",
                              platform_key=platform.key if platform else "")

    hits.sort(key=lambda h: TIER_ORDER[h[0]])
    tier, reason = hits[0]
    return Classification(tier=tier, label=TIER_LABELS[tier], reason=reason,
                          platform_key=platform.key if platform else "")


# ==============================================================================
#  槽位辅助
# ==============================================================================

def split_slot(name: str) -> tuple[str, str]:
    """
    拆分分区名与槽位后缀。
        boot_a -> ("boot", "_a")
        boot   -> ("boot", "")
    """
    m = re.match(r"^(.*?)(_a|_b|_A|_B)$", name)
    if m:
        return m.group(1), m.group(2).lower()
    return name, ""


def expand_slot(pattern: str, slot: str) -> list[str]:
    """
    把不带槽位的模式展开成当前设备的实际分区名。
        expand_slot("boot", "_a") -> ["boot_a", "boot"]
        expand_slot("boot", "")   -> ["boot"]
    """
    if not slot or slot == "_":
        return [pattern]
    return [pattern + slot, pattern]


# ==============================================================================
#  预设方案
# ==============================================================================

@dataclass(frozen=True)
class Preset:
    key: str
    display: str
    description: str
    tiers: tuple[int, ...]
    include_root_baseline: bool = False


PRESETS: tuple[Preset, ...] = (
    Preset("critical", "关键分区（推荐）",
           "只备份 Tier 1 不可再生分区 —— 体积小、价值最高，每次 OTA 后跑一次",
           tiers=(1,), include_root_baseline=False),
    Preset("critical+root", "关键 + root 基准",
           "Tier 1 + Tier 2，含修补过的 init_boot/boot/vbmeta，可完整还原当前 root 状态",
           tiers=(1, 2), include_root_baseline=True),
    Preset("all-useful", "全部有价值分区",
           "Tier 1 + 2 + 3（不含低价值槽），体积可能很大",
           tiers=(1, 2, 3), include_root_baseline=True),
    Preset("everything", "全部分区",
           "勾选所有分区，包含 9GB 的 super 与 200GB+ 的 userdata —— 请确认磁盘空间",
           tiers=(1, 2, 3, 4, 0), include_root_baseline=True),
    Preset("custom", "自定义", "手动勾选，不套用预设", tiers=()),
)


def apply_preset(preset_key: str, partitions: Iterable, slot: str) -> list[str]:
    """
    根据预设返回应当勾选的分区名列表。

    partitions 需要是带 .name / .tier 属性的对象序列（PartitionInfo）。
    """
    preset = next((p for p in PRESETS if p.key == preset_key), None)
    if preset is None or preset.key == "custom":
        return []

    picked: list[str] = []
    for p in partitions:
        tier = getattr(p, "tier", 0)
        if tier in preset.tiers and p.name not in PRESET_EXCLUDE:
            picked.append(p.name)
    return picked


# 这些分区体积巨大或含隐私，预设方案【永不自动勾选】——
# 用户仍可在列表里手动勾上，只是不会被预设"顺手带上"。
PRESET_EXCLUDE: frozenset = frozenset({
    "userdata",     # 个人数据，可达数百 GB
    "super",        # 动态分区容器，官方固件可完整重建
    "metadata",     # 加密元数据，恢复旧值可能导致无法解密
})


# ==============================================================================
#  存储拓扑相关常量
# ==============================================================================

# 整盘设备节点的命名规则（用于把 /dev/block 下的整盘与分区区分开）
WHOLE_DISK_PATTERNS = (
    r"^sd[a-z]+$",          # SCSI/UFS LUN
    r"^mmcblk\d+$",         # eMMC / SD
    r"^mmcblk\d+boot\d+$",  # eMMC boot 分区（独立块设备）
    r"^mmcblk\d+rpmb$",     # eMMC RPMB
    r"^nvme\d+n\d+$",       # NVMe
    r"^vd[a-z]+$",          # virtio
    r"^ubiblock\d+$",       # UBI
)

WHOLE_DISK_RE = re.compile("|".join(WHOLE_DISK_PATTERNS))

# by-name 目录的候选路径（按现代到古老排序）
BYNAME_CANDIDATES: tuple[str, ...] = (
    "/dev/block/by-name",
    "/dev/block/bootdevice/by-name",
    "/dev/block/platform/soc/1d84000.ufshc/by-name",
    "/dev/block/platform/soc/4804000.ufshc/by-name",
    "/dev/block/platform/*/by-name",
    "/dev/block/platform/*/*/by-name",
    "/dev/block/platform/*/*/*/by-name",
)

# 动态分区（Android 10+ 逻辑分区）所在目录
MAPPER_DIR = "/dev/block/mapper"

# root 后端特征目录
ROOT_BACKENDS: tuple[tuple[str, str, str], ...] = (
    ("/data/adb/ksu",  "kernelsu", "KernelSU"),
    ("/data/adb/magisk", "magisk", "Magisk"),
    ("/data/adb/ap",   "apatch",   "APatch"),
)


def new_partition_dirs() -> tuple[str, ...]:
    """分区名与人类描述的对照，供 README 生成用。"""
    return ()
