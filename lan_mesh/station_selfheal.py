"""
自愈闭环 (F4.2) — 修复动作执行、自动守护、写动作配额与熔断护栏。

拆分自 station_controller.py (iter-74 Phase 2)。诊断产出的修复建议在此
落地为真实动作 (check_peer/rotate_key/switch_pool), 每次执行落盘 heal_log
并 WS 广播; 写动作受每日配额与连败熔断双护栏约束。
"""
import time

from .logger import get_logger

logger = get_logger("station")


from typing import Any

class StationSelfHealMixin:
    """自愈动作执行与自动守护。

    依赖 StationController 提供的属性: db / state / cfg / model_router /
    _auto_heal_* 状态字典。
    """

    # ── iter-50: F4.2 自动自愈守护 (修复环节自动化) ──────────
    # iter-60: 全自动闭环 — rotate_key/switch_pool 升级为真实修复写动作

    _AUTO_HEAL_ACTIONS = {"check_peer", "rotate_key", "switch_pool"}  # retry_or_switch 需人工
    _AUTO_HEAL_WRITE_ACTIONS = {"rotate_key", "switch_pool"}  # 写动作: 每日配额 + 熔断护栏

    def run_heal_action(self, action: str, category: str='') -> dict:
        return call_component(self, StationHealingService, 'run_heal_action', action=action, category=category)

    def _heal_check_peer(self) -> str:
        return call_component(self, StationHealingService, '_heal_check_peer')

    def _heal_probe_balances(self) -> str:
        return call_component(self, StationHealingService, '_heal_probe_balances')

    def _heal_rotate_key(self) -> str:
        return call_component(self, StationHealingService, '_heal_rotate_key')

    def _heal_switch_pool(self) -> str:
        return call_component(self, StationHealingService, '_heal_switch_pool')

    def _heal_guard(self) -> None:
        return call_component(self, StationHealingService, '_heal_guard')

    def _auto_heal_once(self) -> dict:
        return call_component(self, StationHealingService, '_auto_heal_once')

    def _auto_heal_loop(self) -> Any:
        return call_component(self, StationHealingService, '_auto_heal_loop')

    def get_auto_heal_status(self) -> dict:
        return call_component(self, StationHealingService, 'get_auto_heal_status')


from typing import Any
from .component_service import call_component
from .station_healing_service import StationHealingService
