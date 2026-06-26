"""
主机评级系统 — 基于硬件配置自动计算能力等级

职责:
1. 根据 CPU/内存/磁盘计算综合得分 (0~100)
2. 映射到 S/A/B/C/D 五级评级
3. 生成人类可读的评级摘要

评级用于:
- Station Director 向 Secretary/Planner 提供资源池筛选
- 项目规划时按难度匹配对应等级的主机
"""
from dataclasses import dataclass


# ── 评级阈值 ──────────────────────────────────────────────────

TIER_THRESHOLDS = [
    ("S", 80),   # 旗舰: 8核+/16GB+/500GB+
    ("A", 60),   # 高性能: 6核/8GB/200GB+
    ("B", 40),   # 中等: 4核/8GB/100GB+
    ("C", 20),   # 基础: 2核/4GB/50GB+
    ("D", 0),    # 入门: 1核/2GB
]

# 评级顺序 (用于比较)
TIER_ORDER = {"S": 0, "A": 1, "B": 2, "C": 3, "D": 4}


@dataclass
class HostRating:
    """主机评级结果。"""
    tier: str = "D"            # S/A/B/C/D
    score: int = 0             # 综合得分 (0~100)
    summary: str = ""          # 人类可读摘要 (如 "6核/8GB/200GB - A级")

    def to_dict(self) -> dict:
        return {
            "tier": self.tier,
            "score": self.score,
            "summary": self.summary,
        }


def rate_host(cpu_count: int, memory_mb: int, disk_gb: int) -> HostRating:
    """根据硬件配置计算评级。

    评分规则 (满分 100):
    - CPU 核数:  min(cores * 5, 35)     # 最多 35 分 (7 核封顶)
    - 内存 GB:   min(mem_gb * 3, 35)    # 最多 35 分 (~12GB 封顶)
    - 磁盘 GB:   min(disk_gb * 0.1, 30) # 最多 30 分 (300GB 封顶)

    Args:
        cpu_count: CPU 核心数
        memory_mb: 总内存 (MB)
        disk_gb: 总磁盘 (GB)

    Returns:
        HostRating 包含 tier、score、summary
    """
    # CPU 分 (最多 35)
    cpu_score = min(cpu_count * 5, 35)

    # 内存分 (最多 35)
    mem_gb = memory_mb / 1024.0
    mem_score = min(mem_gb * 3, 35)

    # 磁盘分 (最多 30)
    disk_score = min(disk_gb * 0.1, 30)

    total = int(cpu_score + mem_score + disk_score)

    # 映射等级
    tier = "D"
    for t, threshold in TIER_THRESHOLDS:
        if total >= threshold:
            tier = t
            break

    # 生成摘要
    summary = f"{cpu_count}核/{int(mem_gb)}GB/{disk_gb}GB - {tier}级"

    return HostRating(tier=tier, score=total, summary=summary)


def tier_meets(actual: str, required: str) -> bool:
    """检查实际评级是否满足要求。

    Args:
        actual: 实际评级 (S/A/B/C/D)
        required: 最低要求评级

    Returns:
        True 如果 actual >= required
    """
    return TIER_ORDER.get(actual, 99) <= TIER_ORDER.get(required, 99)


def min_tier_for_difficulty(difficulty: str) -> str:
    """根据任务难度返回最低主机评级要求。

    Args:
        difficulty: 任务难度 L1/L2/L3/L4

    Returns:
        最低评级 (S/A/B/C/D)
    """
    mapping = {
        "L1": "D",   # 简单任务: 任意主机
        "L2": "C",   # 一般任务: 基础以上
        "L3": "B",   # 复杂任务: 中等以上
        "L4": "A",   # 旗舰任务: 高性能以上
    }
    return mapping.get(difficulty, "D")
