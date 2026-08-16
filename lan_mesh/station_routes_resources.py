"""
Station 资源与密钥路由 — 模型资源池/配置向导/密钥同步/事件与角色卡 (P1 #2 拆分产物)

Secretary 激活后可用 (secrets/fetch 与 sync-all 除外):
  - R1 资源池汇总/用量记录 (单条+批量幂等) / R6 成本分摊 / R2 余额探测
  - R4 资源配置向导 (读/保存+热重载+F1 全网对齐)
  - S3 密钥拉取 / S1 密钥接收 (自愈收敛) / F1 全网密钥对齐
  - Key 测试 / R7 预警检查 / R5 轮换方案
  - M5 事件查询 / M6 角色卡摘要
"""
import threading

from fastapi import APIRouter, HTTPException, Request

from .event_bus import publish_event, recent_events
from .logger import get_logger
from .station_routes_common import (
    _heal_mesh_token_from, check_secretary, get_mesh_auth_token,
)

logger = get_logger("station_api")


def build_resource_routes(controller) -> APIRouter:
    """Secretary 资源/密钥域路由。"""
    router = APIRouter()

    # ── R1: 模型资源管理 ──

    @router.get("/api/resources")
    async def get_model_resources():
        """查询模型资源池汇总 (额度/已用/剩余/状态)。未启用时返回空列表。"""
        check_secretary(controller)
        from .model_resources import resource_summary
        return resource_summary()

    @router.post("/api/resources/usage")
    async def record_model_usage(payload: dict):
        """记录 LLM 调用消耗 (模型 → 资源池自动匹配)。

        单条: {model, input_tokens, output_tokens, usage_id?,
            task_id?, project_id?}
        批量 (R3 Worker 上报): {records: [{usage_id, model,
            input_tokens, output_tokens, task_id?, project_id?}, ...]}
            — usage_id 幂等去重。
        """
        check_secretary(controller)
        from .model_resources import record_usage_global
        records = payload.get("records")
        if isinstance(records, list) and records:
            recorded = duplicate = 0
            for rec in records:
                if not isinstance(rec, dict):
                    continue
                res = record_usage_global(
                    rec.get("model", ""),
                    rec.get("input_tokens", 0),
                    rec.get("output_tokens", 0),
                    usage_id=str(rec.get("usage_id", "")),
                    task_id=str(rec.get("task_id", "")),
                    project_id=str(rec.get("project_id", "")),
                )
                if res.get("duplicate"):
                    duplicate += 1
                elif res.get("tracked"):
                    recorded += 1
            publish_event("usage_reported",
                          {"total": len(records), "recorded": recorded,
                           "duplicate": duplicate})
            return {"batch": True, "total": len(records),
                    "recorded": recorded, "duplicate": duplicate}
        return record_usage_global(
            payload.get("model", ""),
            payload.get("input_tokens", 0),
            payload.get("output_tokens", 0),
            usage_id=str(payload.get("usage_id", "")),
            task_id=str(payload.get("task_id", "")),
            project_id=str(payload.get("project_id", "")),
        )

    @router.get("/api/resources/cost")
    async def get_cost_by_task(limit: int = 100):
        """R6: 成本分摊聚合 — 按 task_id 分组统计调用次数/token/金额。

        无归因记录 task_id 为空串 (前端展示为「未归因」)。
        """
        check_secretary(controller)
        if not controller.db:
            return {"enabled": False, "items": []}
        try:
            items = controller.db.query_cost_by_task(
                limit=max(1, min(int(limit or 100), 500)))
        except Exception:
            items = []  # 旧库未迁移等异常 → 降级空列表
        return {"enabled": True, "items": items}

    @router.post("/api/resources/probe")
    async def probe_model_balances():
        """触发资源池服务商余额自动探测 (R2)。

        调用各家余额 API (SiliconFlow/DeepSeek/Moonshot/智谱), 结果
        缓存并附到 /api/resources 汇总; 未支持的服务商返回引导提示。
        """
        check_secretary(controller)
        from .model_resources import probe_balances_global
        return probe_balances_global()

    # ── R4: 资源配置向导 (UI 化, 免手工编辑 yaml) ──────────────

    @router.get("/api/resources/config")
    async def get_resources_config():
        """读取资源配置 (UI 配置向导): 当前配置 + 模型目录 + 探测能力。"""
        check_secretary(controller)
        from pathlib import Path
        from .balance_probe import UNSUPPORTED_HINTS, supported_providers
        from .config import load_model_pool
        from .model_resources import read_config_data
        target = Path(__file__).parent / "resources.yaml"
        cfg = read_config_data(target)
        catalog: dict = {}
        try:
            pool = load_model_pool()
            for e in (pool.models or []):
                catalog.setdefault(e.provider, []).append(e.id)
        except Exception:
            pass
        return {
            "exists": cfg["exists"], "data": cfg["data"],
            "parse_error": cfg["error"],
            "catalog": [{"provider": p, "models": m}
                        for p, m in sorted(catalog.items())],
            "probe_supported": supported_providers(),
            "probe_hints": UNSUPPORTED_HINTS,
        }

    @router.post("/api/resources/config")
    async def save_resources_config(payload: dict):
        """保存资源配置并热重载 (UI 配置向导)。

        校验不通过 → 400 携带具体错误; 保存前自动备份至 ~/.lan_mesh/backups/。
        F1: 任意节点均可保存 (不再限 Secretary) — 保存后与全网
        对端自动对齐, 主从无关。
        """
        from pathlib import Path
        from .config import load_model_pool
        from .model_resources import (init_resource_manager, save_config,
                                      validate_config)
        data = payload.get("config")
        if not isinstance(data, dict):
            raise HTTPException(status_code=400, detail="缺少 config 字段")
        # 全局数值字段规范化
        if data.get("report_interval") not in (None, ""):
            try:
                data["report_interval"] = float(data["report_interval"])
            except (TypeError, ValueError):
                data.pop("report_interval", None)
        if not (data.get("secretary_url") or "").strip():
            data.pop("secretary_url", None)
        # 规范化: models 逗号分隔字符串 → 列表; 数值字段强转 (UI 输入兼容)
        for item in data.get("resources") or []:
            if not isinstance(item, dict):
                continue
            if isinstance(item.get("models"), str):
                item["models"] = [m.strip()
                                  for m in item["models"].split(",")
                                  if m.strip()]
            for num_key in ("quota", "alert_threshold", "expire_at",
                            "renew_at", "period_days"):
                if item.get(num_key) in (None, ""):
                    item.pop(num_key, None)
                    continue
                try:
                    item[num_key] = float(item[num_key]) \
                        if num_key in ("quota", "alert_threshold",
                                       "expire_at", "renew_at") \
                        else int(float(item[num_key]))
                except (TypeError, ValueError):
                    pass  # 留给 validate_config 报错
            if not (item.get("api_key") or "").strip():
                item.pop("api_key", None)  # 空 key 不落盘
        errors = validate_config(data)
        if errors:
            raise HTTPException(status_code=400, detail="; ".join(errors))
        target = Path(__file__).parent / "resources.yaml"
        saved = save_config(target, data)
        if not saved.get("ok"):
            raise HTTPException(status_code=500,
                                detail=f"保存失败: {saved.get('error')}")
        pool = load_model_pool()
        mgr = init_resource_manager(
            target, pool.models if pool.models else None, controller.db)
        # R7: 热重载后重新注入预警推送回调 (load 会重置管理器状态)
        from .model_resources import set_bot_notify_global
        set_bot_notify_global(controller.bot_gateway.notify)
        publish_event("resource_config",
                      {"ok": True, "pools": len(mgr.list_resources())})
        # F1: 保存后后台与全网对齐 (本机 config_ts 最新 → 自动推送)
        threading.Thread(
            target=controller._align_config_with_peers,
            daemon=True, name="secret-align-save",
        ).start()
        return {"ok": True, "enabled": mgr.enabled,
                "pools": len(mgr.list_resources()),
                "backup": saved.get("backup", "")}

    @router.get("/api/secrets/fetch")
    async def fetch_secrets():
        """S3: 提供本机资源配置加密密文, 供新节点启动时拉取 (API Key 自动同步)。

        与 bootstrap-token 同一信任假设 (局域网成员); 密文受
        AES-256-GCM 保护, 无 mesh_token 无法解密。本机无可用
        密钥配置时返回空 blob (调用方幂等跳过)。
        """
        from pathlib import Path
        from .auth import get_mesh_token
        from .model_resources import read_config_data
        from .secret_sync import config_hash, encrypt_config
        target = Path(__file__).parent / "resources.yaml"
        if not target.is_file():
            return {"blob": "", "detail": "本机无 resources.yaml"}
        cfg = read_config_data(target)
        data = cfg.get("data") or {}
        pools = data.get("resources") or []
        key_count = sum(1 for p in pools if (p.get("api_key") or "").strip())
        if not key_count:
            return {"blob": "", "detail": "本机无 api_key 直填配置"}
        token = get_mesh_auth_token() or get_mesh_token()
        if not token:
            return {"blob": "", "detail": "无 mesh_token, 无法加密"}
        payload = encrypt_config(data, token)
        payload["config_hash"] = config_hash(data)
        payload["pools"] = len(pools)
        # F1: 附配置时间戳 (角色无关对齐的仲裁依据)
        try:
            payload["config_ts"] = float(data.get("config_ts") or 0)
        except (TypeError, ValueError):
            payload["config_ts"] = 0.0
        return payload

    @router.post("/api/secrets/receive")
    async def receive_secrets(payload: dict, request: Request):
        """S1: 接收 Secretary 加密推送的资源配置 (含 API Key 直填)。

        解密用本机 mesh_token (与推送方同信任根, 与认证开关解耦);
        校验通过后落盘 resources.yaml 并热重载 (直填 key 由 load()
        注入环境变量); 配置指纹一致时幂等跳过。
        """
        from pathlib import Path
        from .auth import get_mesh_token
        from .config import load_model_pool
        from .model_resources import (init_resource_manager, read_config_data,
                                      save_config, set_bot_notify_global,
                                      validate_config)
        from .secret_sync import config_hash, decrypt_config
        local_token = get_mesh_auth_token() or get_mesh_token()
        try:
            data = decrypt_config(payload, local_token)
        except (ValueError, RuntimeError) as e:
            # S1 自愈: 加密信任根分歧时从推送方 (Secretary) 收敛
            # mesh_token 后重试一次 (推送方即本轮信任源)
            healed = ""
            if "mesh_token 不匹配" in str(e):
                src_ip = request.client.host if request.client else ""
                try:
                    src_port = int(payload.get("src_port") or 0)
                except (TypeError, ValueError):
                    src_port = 0
                if src_ip and src_port:
                    try:
                        healed = _heal_mesh_token_from(controller, src_ip, src_port)
                    except Exception as heal_err:
                        logger.warning("[S1] 密钥接收自愈收敛失败: %s", heal_err)
            if not healed or healed == local_token:
                raise HTTPException(status_code=400, detail=str(e))
            try:
                data = decrypt_config(payload, healed)
            except (ValueError, RuntimeError) as retry_err:
                raise HTTPException(status_code=400, detail=str(retry_err))
        expected = (payload.get("config_hash") or "").strip()
        if expected and config_hash(data) != expected:
            raise HTTPException(status_code=400, detail="配置指纹不匹配, 拒绝落盘")
        if not isinstance(data, dict):
            raise HTTPException(status_code=400, detail="解密内容非配置对象")
        # 幂等: 与现有配置指纹一致 → 跳过落盘与重载
        target = Path(__file__).parent / "resources.yaml"
        current = read_config_data(target) if target.is_file() else {}
        cur_data = current.get("data") or {}
        if cur_data and config_hash(cur_data) == config_hash(data):
            return {"ok": True, "applied": False, "detail": "配置一致",
                    "pools": len(cur_data.get("resources") or [])}
        errors = validate_config(data)
        if errors:
            raise HTTPException(status_code=400,
                                detail="接收配置校验失败: " + "; ".join(errors))
        saved = save_config(target, data)
        if not saved.get("ok"):
            raise HTTPException(status_code=500,
                                detail=f"保存失败: {saved.get('error')}")
        pool = load_model_pool()
        mgr = init_resource_manager(
            target, pool.models if pool.models else None, controller.db)
        set_bot_notify_global(controller.bot_gateway.notify)
        publish_event("resource_config",
                      {"ok": True, "pools": len(mgr.list_resources()),
                       "source": "secret-sync"})
        logger.info("[S1] 已接收加密密钥分发, 应用 %d 个资源池",
                    len(mgr.list_resources()))
        return {"ok": True, "applied": True, "detail": "已应用",
                "pools": len(mgr.list_resources())}

    @router.post("/api/secrets/sync-all")
    async def sync_secrets_all():
        """F1: 手动触发全网密钥对齐 (角色无关, config_ts 仲裁)。"""
        summary = controller._align_config_with_peers()
        return {"ok": bool(summary["pushed"] or summary["pulled"]),
                "pushed": len(summary["pushed"]),
                "pulled": len(summary["pulled"]),
                "skipped": summary["skipped"],
                "failed": summary["failed"]}

    @router.post("/api/resources/test-key")
    async def test_resource_key(payload: dict):
        """单个 API Key 有效性测试 + 余额查询 (UI 一键测试)。"""
        check_secretary(controller)
        from .balance_probe import probe_balance
        return probe_balance(payload.get("provider", ""),
                             payload.get("api_key", ""))

    @router.post("/api/resources/alerts/check")
    async def check_resource_alerts():
        """R7: 手动触发一轮到期/额度预警检查 (返回新推送的预警)。"""
        check_secretary(controller)
        from .model_resources import check_alerts_global, resource_summary
        pushed = check_alerts_global()
        return {"pushed": pushed,
                "active": resource_summary().get("alerts", [])}

    @router.get("/api/resources/rotation")
    async def get_rotation_plan():
        """R5: 多池轮换调度方案 — 逐模型候选池优先级与选中池。"""
        check_secretary(controller)
        from .model_resources import rotation_plan_global
        return {"plan": rotation_plan_global()}

    @router.get("/api/events/recent")
    async def get_recent_events(n: int = 20):
        """M5: 最近事件查询 (事件总线历史, 供 UI 补拉)。"""
        check_secretary(controller)
        return {"events": recent_events(max(1, min(int(n or 20), 100)))}

    @router.get("/api/roles")
    async def get_role_cards():
        """M6: 角色卡摘要 (Secretary/PM/Worker 人格单一事实源, 供 UI/quest 页面展示)。"""
        check_secretary(controller)
        from .role_cards import list_role_cards
        return {"roles": list_role_cards()}

    return router
