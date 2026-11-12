import argparse
import signal
import threading

from .demo import demo_snapshot
from .server import GraphServer, file_provider


def main():
    parser = argparse.ArgumentParser(description="YomkGraph · YomkServerInfo 服务与数据流框图")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--demo", action="store_true", help="查看明确标注的示例数据")
    source.add_argument("--snapshot", help="读取项目导出的 yomk-graph/v1 JSON；自动重读")
    parser.add_argument("--port", type=int, default=18765)
    args = parser.parse_args()
    provider = demo_snapshot if args.demo else file_provider(args.snapshot)
    provider()  # Fail before advertising if the input is invalid.
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    with GraphServer(provider, port=args.port) as server:
        print(f"YomkGraph: {server.url}", flush=True)
        stop.wait()


if __name__ == "__main__":
    main()
