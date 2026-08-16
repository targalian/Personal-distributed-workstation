"""
FastAPI 路由层 - Worker API (装配入口)

Worker API:
  GET  /info          - 返回本机完整配置
  GET  /shared        - 列出共享文件
  GET  /shared/{path} - 下载共享文件
  POST /shared        - 上传文件到共享目录

拆分结构 (iter-31, 原 275 行工厂函数按端点域拆分, 路由集合不变):
| 模块                  | 职责                          |
|-----------------------|-------------------------------|
| worker_routes_basic.py | 本机信息/共享文件/任务执行    |
| worker_routes_pm.py    | 角色管理/PM 生命周期/子 Agent |
| worker_routes_p2p.py   | 主机间 P2P 消息接收           |

历史说明 (P3 清理): 旧 Secretary API (create_secretary_router /
broadcast_ws 及其专用 payload 模型) 已随 secretary.py 历史入口删除,
Secretary 端路由现由 station_routes_* 三件套承担。
"""
from typing import Any, Callable, Optional

from fastapi import APIRouter

from .shared_folder import SharedFolderManager
from .worker_routes_basic import build_basic_routes
from .worker_routes_pm import build_pm_routes
from .worker_routes_p2p import build_p2p_routes


def create_worker_router(
    collect_info_fn: Callable[[], Any],
    shared_folder: SharedFolderManager,
    agent_runtime: Optional[Any] = None,   # AgentRuntime instance (optional, for task execution)
    role_manager: Optional[Any] = None,    # WorkerAgent instance (optional, for remote role management)
) -> APIRouter:
    """创建 Worker 节点的 API 路由 (装配三个端点域子路由)。

    注册顺序与拆分前一致: basic (info/任务执行/shared) →
    pm (角色管理/PM/子 Agent) → p2p (消息接收)。
    """
    router = APIRouter()
    router.include_router(build_basic_routes(collect_info_fn, shared_folder, agent_runtime))
    router.include_router(build_pm_routes(role_manager))
    router.include_router(build_p2p_routes())
    return router
