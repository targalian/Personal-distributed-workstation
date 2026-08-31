"""
自愈闭环 (F4.2) — 修复动作执行、自动守护、写动作配额与熔断护栏。

拆分自 station_controller.py (iter-74 Phase 2)。诊断产出的修复建议在此
落地为真实动作 (check_peer/rotate_key/switch_pool), 每次执行落盘 heal_log
并 WS 广播; 写动作受每日配额与连败熔断双护栏约束。
"""
import time

from .logger import get_logger

logger = get_logger("station")


class StationSelfHealMixin:
    """自愈动作执行与自动守护。

    依赖 StationController 提供的属性: db / state / cfg / model_router /
    _auto_heal_* 状态字典。
    """

    # ── iter-50: F4.2 自动自愈守护 (修复环节自动化) ──────────
    # iter-60: 全自动闭环 — rotate_key/switch_pool 升级为真实修复写动作

    _AUTO_HEAL_ACTIONS = {"check_peer", "rotate_key", "switch_pool"}  # retry_or_switch 需人工
    _AUTO_HEAL_WRITE_ACTIONS = {"rotate_key", "switch_pool"}  # 写动作: 每日配额 + 熔断护栏

    def run_heal_action(self, action: str, category: str = "") -> dict:
        """iter-49: 执行一个自愈动作并落盘 heal_log + WS 广播。

        仅安全的只读动作注册为可自动执行; 未注册动作统一返回
        manual_required (需人工介入)。诊断规则 action 标识映射:
        check_peer ← timeout/connection, probe_balances ← auth/rate_limit。
        返回: {action, category, result: ok/failed/manual_required, detail}
        """
        action = (action or "").strip()
        category = (category or "").strip()
        handlers = {
            "check_peer": self._heal_check_peer,
            "probe_balances": self._heal_probe_balances,
            # iter-60 (F4.2 全自动闭环): 真实修复写动作 — 探测后自动处置失效/耗尽池
            "rotate_key": self._heal_rotate_key,
            "switch_pool": self._heal_switch_pool,
        }
        handler = handlers.get(action)
        if handler is None:
            record = {"category": category, "action": action,
                      "result": "manual_required",
                      "detail": "动作未注册为可自动执行, 需人工介入"}
        else:
            try:
                detail = handler()
                record = {"category": category, "action": action,
                          "result": "ok", "detail": str(detail)[:500]}
            except Exception as e:
                record = {"category": category, "action": action,
                          "result": "failed", "detail": str(e)[:300]}
        try:
            self.db.save_heal_record(record)
        except Exception:
            pass
        try:
            from .event_bus import publish_event
            publish_event("heal_action", record)
        except Exception:
            pass
        logger.info("[Heal] 自愈动作执行: %s/%s -> %s",
                    action, category, record["result"])
        return record

    def _heal_check_peer(self) -> str:
        """check_peer: 向所有已知设备发送 UDP 发现探测包 (只读)。"""
        if not self.discovery:
            return "发现服务未启动, 跳过探测"
        devices = self.discovery.list_devices() or []
        probed = 0
        for d in devices:
            ip = d.get("ip", "") if isinstance(d, dict) else ""
            if not ip:
                continue
            try:
                self.discovery.probe_ip(ip)
                probed += 1
            except Exception:
                pass
        return f"已向 {probed}/{len(devices)} 台已知设备发送探测包"

    def _heal_probe_balances(self) -> str:
        """probe_balances: 触发资源池余额探测 (R2, 验证密钥/额度状态)。"""
        from .model_resources import probe_balances_global
        data = probe_balances_global(timeout=10.0)
        if data.get("error"):
            raise RuntimeError(str(data["error"])[:200])
        probed = int(data.get("probed") or 0)
        supported = int(data.get("supported") or 0)
        if supported == 0 and probed == 0:
            return "无可探测余额的资源池 (未配置 api_key 或不支持余额查询)"
        return f"余额探测完成: 探测 {probed}/{supported} 个支持余额查询的资源池"

    def _heal_rotate_key(self) -> str:
        """iter-60: rotate_key (auth 类错误) — 探测密钥有效性, 失效池自动暂停。

        探测结果 error 命中 401/unauthorized/invalid/forbidden 的池
        → status=paused (路由剔除), 让流量自动落到其他可用池;
        无失效池则仅报告探测完成。
        """
        from .model_resources import (probe_balances_global,
                                      set_pool_status_global)
        data = probe_balances_global(timeout=10.0)
        if data.get("error"):
            raise RuntimeError(str(data["error"])[:200])
        results = data.get("results") or {}
        paused: list[str] = []
        for rid, res in results.items():
            err = str(res.get("error") or "").lower()
            if any(k in err for k in ("401", "403", "unauthorized",
                                      "invalid", "forbidden")):
                if set_pool_status_global(rid, "paused"):
                    paused.append(rid)
        if paused:
            return (f"已暂停 {len(paused)} 个密钥失效池: "
                    f"{', '.join(paused)}")
        return (f"探测完成: 未发现密钥失效池 "
                f"(探测 {int(data.get('probed') or 0)} 个)")

    def _heal_switch_pool(self) -> str:
        """iter-60: switch_pool (rate_limit 类错误) — 探测余额, 耗尽池剔除路由。

        probe_balances 内部已自动将 balance<=0 池标记 exhausted;
        本动作探测后核对可用池数量, 全部耗尽则升级人工介入。
        """
        from .model_resources import probe_balances_global
        data = probe_balances_global(timeout=10.0)
        if data.get("error"):
            raise RuntimeError(str(data["error"])[:200])
        results = data.get("results") or {}
        ok_pools: list[str] = []
        for rid, res in results.items():
            bal = res.get("balance")
            if bal is not None and float(bal) > 0:
                ok_pools.append(rid)
        if not ok_pools:
            raise RuntimeError(
                "所有可探测池余额均为 0, 自动切换失败: 需人工充值/更换密钥")
        return (f"切换完成: {len(ok_pools)} 个可用池 "
                f"(探测 {int(data.get('probed') or 0)} 个)")

    def _heal_guard(self) -> None:
        """iter-60: 惰性初始化护栏状态 (兼容测试 __new__ 轻量构造)。"""
        self.__dict__.setdefault("_auto_heal_daily", {})
        self.__dict__.setdefault("_auto_heal_fused", {})
        self.__dict__.setdefault("_auto_heal_fail_streak", {})

    def _auto_heal_once(self) -> dict:
        """iter-50: 单轮自动自愈扫描 — 诊断缓冲错误, 对安全动作自动执行 (冷却去重)。

        仅处理 action 属于 _AUTO_HEAL_ACTIONS 的 findings (retry_or_switch 需人工跳过);
        同类别冷却期内跳过防风暴。iter-60: 写动作附加每日配额与连续失败
        熔断护栏, 熔断在错误消失后自动复位。返回本轮执行摘要
        (供守护线程与手动端点复用)。
        """
        self._heal_guard()
        obs = self.cfg.observability
        state = self._auto_heal_state
        state["runs"] += 1
        state["last_run"] = time.time()
        summary = {"enabled": bool(obs.auto_heal_enabled),
                   "actions_run": [], "skipped_cooldown": [],
                   "skipped_manual": 0, "skipped_quota": [],
                   "skipped_fused": []}
        if not obs.auto_heal_enabled:
            state["last_actions"] = []
            return summary
        try:
            from .error_tracker import error_tracker
            diag = error_tracker.diagnose(window_records=200)
        except Exception:
            return summary
        findings = diag.get("findings") or []
        now = time.time()
        # iter-60: 熔断自动复位 — 类别不再出现在本轮 findings → 解除熔断/清失败计数
        active_cats = {str(f.get("category", "")) for f in findings}
        for cat in list(self._auto_heal_fused.keys()):
            if cat not in active_cats:
                self._auto_heal_fused.pop(cat, None)
                self._auto_heal_fail_streak.pop(cat, None)
                logger.info("[AutoHeal] 类别 %s 错误消失, 熔断复位", cat)
        daily_limit = max(0, int(obs.auto_heal_daily_limit or 0))
        today_key = time.strftime("%Y-%m-%d")
        daily = self._auto_heal_daily.setdefault(today_key, {})
        for f in findings:
            action = str(f.get("action", ""))
            category = str(f.get("category", ""))
            if action not in self._AUTO_HEAL_ACTIONS:
                summary["skipped_manual"] += 1
                continue
            last = self._auto_heal_last.get(category, 0.0)
            if now - last < obs.auto_heal_cooldown:
                summary["skipped_cooldown"].append(category)
                continue
            # iter-60: 写动作护栏 — 熔断 / 每日配额
            if action in self._AUTO_HEAL_WRITE_ACTIONS:
                if category in self._auto_heal_fused:
                    summary["skipped_fused"].append(
                        {"category": category,
                         "reason": self._auto_heal_fused[category]})
                    continue
                if daily.get(category, 0) >= daily_limit:
                    summary["skipped_quota"].append(category)
                    continue
            rec = self.run_heal_action(action, category)
            self._auto_heal_last[category] = now
            if action in self._AUTO_HEAL_WRITE_ACTIONS:
                daily[category] = daily.get(category, 0) + 1
                if rec.get("result") != "ok":
                    streak = self._auto_heal_fail_streak.get(category, 0) + 1
                    self._auto_heal_fail_streak[category] = streak
                    if streak >= 2:
                        self._auto_heal_fused[category] = (
                            f"连续 {streak} 次执行失败, 已熔断待人工介入")
                        logger.warning("[AutoHeal] 类别 %s 熔断: %s",
                                       category, self._auto_heal_fused[category])
                else:
                    self._auto_heal_fail_streak[category] = 0
            summary["actions_run"].append(
                {"category": category, "action": rec.get("action"),
                 "result": rec.get("result")})
        state["last_actions"] = summary["actions_run"]
        return summary

    def _auto_heal_loop(self):
        """iter-50: 自动自愈守护线程 — 按观测配置周期扫描 (异常隔离)。"""
        while True:
            try:
                obs = self.cfg.observability
                interval = max(30.0, float(obs.auto_heal_interval or 300.0))
            except Exception:
                interval = 300.0
            time.sleep(interval)
            try:
                self._auto_heal_once()
            except Exception as e:
                logger.warning("[AutoHeal] 自动自愈扫描异常 (no-op): %s", e)

    def get_auto_heal_status(self) -> dict:
        """iter-50: 自动自愈守护状态 (端点 /api/errors/heal/status)。

        iter-60: 扩展写动作护栏状态 — 每日配额消耗/熔断表 (跨重启不保留,
        内存态即可: 重启后错误风暴重新评估)。
        """
        self._heal_guard()
        obs = self.cfg.observability
        state = self._auto_heal_state
        today_key = time.strftime("%Y-%m-%d")
        return {"enabled": bool(obs.auto_heal_enabled),
                "interval": float(obs.auto_heal_interval or 300.0),
                "cooldown": float(obs.auto_heal_cooldown or 600.0),
                "daily_limit": max(0, int(obs.auto_heal_daily_limit or 0)),
                "runs": state["runs"], "last_run": state["last_run"],
                "last_actions": state["last_actions"],
                "daily_counts": dict(
                    self._auto_heal_daily.get(today_key, {})),
                "fused": dict(self._auto_heal_fused)}
