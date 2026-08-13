#!/usr/bin/env python3
"""
LAN Mesh - 统一入口

用法:
  python main.py station             # 启动 Station Director (推荐, 含 Web UI)
  python main.py secretary           # 启动 Secretary 节点 (向后兼容)
  python main.py worker              # 启动 Worker 节点
  python main.py resources           # 查看模型资源用量报告
  python main.py resources --init    # 生成 resources.yaml 配置模板
  python main.py station --port 8080 # 指定端口
  python main.py --config config.yaml worker

参数:
  role              station | secretary | worker | resources
  --port, -p        指定 API 端口
  --name, -n        指定设备名称
  --shared          指定共享文件夹路径
  --config, -c      指定配置文件路径
  --version, -v     显示版本
"""
import argparse
import sys

# 启动时加载 .env 文件 (API Key 等敏感配置)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from lan_mesh import __version__


def main():
    parser = argparse.ArgumentParser(
        prog="lan-mesh",
        description="LAN Mesh - 跨主机网络连接层框架",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 在主控主机上启动 Station Director (推荐入口)
  python main.py station

  # 在各工作主机上启动 Worker
  python main.py worker

  # 自定义配置
  python main.py station --port 8080 --name "控制中心"
  python main.py worker --shared /data/shared --name "计算节点-01"

  # 向后兼容: 直接启动 Secretary
  python main.py secretary
""",
    )
    parser.add_argument(
        "role",
        choices=["station", "secretary", "worker", "resources"],
        help="节点角色: station (推荐) | secretary | worker | resources (资源管理)",
    )
    parser.add_argument("--port", "-p", type=int, default=None, help="HTTP API 端口")
    parser.add_argument("--name", "-n", type=str, default=None, help="设备名称")
    parser.add_argument("--shared", type=str, default=None, help="共享文件夹路径")
    parser.add_argument("--config", "-c", type=str, default=None, help="配置文件路径")
    parser.add_argument("--dev", action="store_true", help="开发模式: 文件变动自动重载")
    parser.add_argument("--init", action="store_true", help="resources: 生成配置模板")
    parser.add_argument("--probe", action="store_true",
                        help="resources: 触发服务商余额自动探测")
    parser.add_argument("--report", action="store_true",
                        help="resources: 立即向 Secretary 上报一轮用量 (需配置 secretary_url)")
    parser.add_argument("--version", "-v", action="version", version=f"LAN Mesh v{__version__}")

    args = parser.parse_args()

    # 加载配置
    from lan_mesh.config import load_config
    cfg = load_config(args.config)

    # 命令行参数覆盖配置
    if args.name:
        if args.role in ("station", "secretary"):
            cfg.secretary.device_name = args.name
        else:
            cfg.worker.device_name = args.name
    if args.port:
        if args.role in ("station", "secretary"):
            cfg.secretary.api_port = args.port
        else:
            cfg.worker.api_port = args.port
    if args.shared:
        if args.role in ("station", "secretary"):
            cfg.secretary.shared_folder = args.shared
        else:
            cfg.worker.shared_folder = args.shared

    # 启动对应角色
    if args.role == "station":
        from lan_mesh.station_controller import StationController
        controller = StationController(cfg)
        controller.start(dev_reload=args.dev)
    elif args.role == "secretary":
        from lan_mesh.secretary import SecretaryController
        controller = SecretaryController(cfg)
        controller.start()
    elif args.role == "resources":
        _run_resources_cli(cfg, args)
    else:
        from lan_mesh.worker import WorkerAgent
        agent = WorkerAgent(cfg)
        agent.start()


def _run_resources_cli(cfg, args):
    """模型资源管理 CLI: 生成模板 / 查看用量报告。"""
    from pathlib import Path

    from lan_mesh.config import get_db_path, load_model_pool
    from lan_mesh.database import Database
    from lan_mesh.model_resources import ModelResourceManager

    target = Path(__file__).parent / "lan_mesh" / "resources.yaml"
    if args.init:
        example = Path(__file__).parent / "lan_mesh" / "resources.example.yaml"
        if target.exists():
            print("[resources] resources.yaml 已存在, 跳过生成: "
                  f"{target.resolve()}")
        else:
            target.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
            print(f"[resources] 已生成配置模板: {target.resolve()}")
            print("[resources] 请编辑填写资源池 (按量预算 / token 包 / 编程订阅)"
                  "后重新运行:")
            print("[resources]   python main.py resources")
        return

    model_pool = load_model_pool()
    db = Database(str(get_db_path(cfg)))
    mgr = ModelResourceManager()
    enabled = mgr.load(target, model_pool.models if model_pool.models else None, db)
    if not enabled:
        print("[resources] 模型资源管理未启用: 未找到 lan_mesh/resources.yaml")
        print("[resources] 提示: 运行 `python main.py resources --init` "
              "生成配置模板")
        return

    if args.probe:
        print("[resources] 触发服务商余额自动探测...")
        probe = mgr.probe_balances()
        print(f"[resources] 探测完成: {probe.get('probed', 0)} 个池, "
              f"{probe.get('supported', 0)} 个探测成功")
        for rid, res in (probe.get("results") or {}).items():
            if res.get("supported"):
                print(f"[resources]   [{rid}] {res.get('provider')} 余额: "
                      f"{res.get('balance')} {res.get('currency')} "
                      f"(来源 {res.get('source')})")
            else:
                print(f"[resources]   [{rid}] {res.get('provider')} "
                      f"未获取: {res.get('error')} — {res.get('hint', '')}")

    if args.report:
        rep = mgr.report_once()
        if rep.get("error") == "no_report_target":
            print("[resources] 未配置上报目标: 请在 resources.yaml 中"
                  "填写 secretary_url (Worker 主机自动发现时免配置)")
        elif rep.get("error"):
            print(f"[resources] 上报失败: {rep.get('error')} "
                  f"(待上报 {rep.get('pending', '?')} 条, 下轮自动重试)")
        else:
            print(f"[resources] 上报完成: {rep.get('reported', 0)} 条 "
                  f"(Secretary 重复忽略 {rep.get('duplicate', 0)} 条)")

    summary = mgr.summarize()
    print(f"[resources] 模型资源管理已启用 (strict={summary.get('strict', False)})")
    for res in summary.get("resources", []):
        rate = round((res.get("rate") or 0) * 100)
        bal = (res.get("balance") or {})
        bal_txt = ""
        if bal.get("supported"):
            bal_txt = f" | 余额: {bal.get('balance')} {bal.get('currency')}"
        elif bal.get("error"):
            bal_txt = f" | 余额: 未获取 ({bal.get('error')[:40]})"
        print(f"[resources]   [{res.get('resource_id')}] {res.get('provider')} "
              f"{res.get('plan_type')} | 已用 {res.get('used')} / "
              f"{res.get('quota')} {res.get('unit')} ({rate}%) | "
              f"状态: {res.get('status')}{bal_txt} "
              f"{('- ' + res.get('note')) if res.get('note') else ''}")


if __name__ == "__main__":
    main()
