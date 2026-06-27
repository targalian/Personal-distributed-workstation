#!/usr/bin/env python3
"""
LAN Mesh - 统一入口

用法:
  python main.py station             # 启动 Station Director (推荐, 含 Web UI)
  python main.py secretary           # 启动 Secretary 节点 (向后兼容)
  python main.py worker              # 启动 Worker 节点
  python main.py station --port 8080 # 指定端口
  python main.py --config config.yaml worker

参数:
  role              station | secretary | worker
  --port, -p        指定 API 端口
  --name, -n        指定设备名称
  --shared          指定共享文件夹路径
  --config, -c      指定配置文件路径
  --version, -v     显示版本
"""
import argparse
import sys

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
        choices=["station", "secretary", "worker"],
        help="节点角色: station (推荐) | secretary | worker",
    )
    parser.add_argument("--port", "-p", type=int, default=None, help="HTTP API 端口")
    parser.add_argument("--name", "-n", type=str, default=None, help="设备名称")
    parser.add_argument("--shared", type=str, default=None, help="共享文件夹路径")
    parser.add_argument("--config", "-c", type=str, default=None, help="配置文件路径")
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
        controller.start()
    elif args.role == "secretary":
        from lan_mesh.secretary import SecretaryController
        controller = SecretaryController(cfg)
        controller.start()
    else:
        from lan_mesh.worker import WorkerAgent
        agent = WorkerAgent(cfg)
        agent.start()


if __name__ == "__main__":
    main()
