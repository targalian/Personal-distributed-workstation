#!/usr/bin/env python3
"""
LAN Mesh 主机配置独立采集脚本

此脚本为自包含工具,不依赖 lan_mesh 框架包,
可被放置在共享文件夹中分发给任意主机使用。

用法:
  python3 collect_config.py                 # 采集到当前目录
  python3 collect_config.py /path/to/dir    # 采集到指定目录
  python3 collect_config.py --loop 10       # 每 10 秒采集一次 (持续)
  python3 collect_config.py --json          # 仅输出 JSON 到 stdout
"""
import json
import os
import platform
import socket
import sys
import time
from datetime import datetime

try:
    import psutil
except ImportError:
    print("错误: 需要 psutil 库")
    print("安装: pip install psutil")
    sys.exit(1)


# ── 配置采集函数 ─────────────────────────────────────────────────

def collect_all_config():
    """采集主机全部配置信息,返回字典。"""
    config = {}

    # ── 基本信息 ──
    config["hostname"] = socket.gethostname()
    config["platform"] = platform.system()
    config["platform_release"] = platform.release()
    config["platform_version"] = platform.version()
    config["architecture"] = platform.machine()
    config["processor"] = platform.processor()
    config["python_version"] = platform.python_version()

    # ── CPU ──
    cpu_freq = psutil.cpu_freq()
    config["cpu"] = {
        "logical_count": psutil.cpu_count(logical=True),
        "physical_count": psutil.cpu_count(logical=False),
        "frequency_mhz": round(cpu_freq.current, 0) if cpu_freq else 0,
        "frequency_max_mhz": round(cpu_freq.max, 0) if cpu_freq else 0,
        "usage_percent": round(psutil.cpu_percent(interval=0.5), 1),
        "load_avg_1m": None,
        "load_avg_5m": None,
        "load_avg_15m": None,
    }
    # 负载均值 (仅 Unix)
    try:
        load1, load5, load15 = os.getloadavg()
        config["cpu"]["load_avg_1m"] = round(load1, 2)
        config["cpu"]["load_avg_5m"] = round(load5, 2)
        config["cpu"]["load_avg_15m"] = round(load15, 2)
    except (OSError, AttributeError):
        pass

    # ── 内存 ──
    mem = psutil.virtual_memory()
    swap = psutil.swap_memory()
    config["memory"] = {
        "total_mb": int(mem.total / (1024 ** 2)),
        "available_mb": int(mem.available / (1024 ** 2)),
        "used_mb": int(mem.used / (1024 ** 2)),
        "usage_percent": round(mem.percent, 1),
        "swap_total_mb": int(swap.total / (1024 ** 2)),
        "swap_used_mb": int(swap.used / (1024 ** 2)),
        "swap_percent": round(swap.percent, 1),
    }

    # ── 磁盘 ──
    disks = []
    for part in psutil.disk_partitions(all=False):
        try:
            usage = psutil.disk_usage(part.mountpoint)
            disks.append({
                "device": part.device,
                "mountpoint": part.mountpoint,
                "fstype": part.fstype,
                "total_gb": int(usage.total / (1024 ** 3)),
                "used_gb": int(usage.used / (1024 ** 3)),
                "free_gb": int(usage.free / (1024 ** 3)),
                "usage_percent": round(usage.percent, 1),
            })
        except (PermissionError, OSError):
            continue
    config["disks"] = disks

    # ── 网络 ──
    interfaces = []
    for name, addrs in psutil.net_if_addrs().items():
        if name.startswith(("lo", "docker", "virbr", "veth", "br-")):
            continue
        iface = {"name": name, "ipv4": [], "ipv6": [], "mac": ""}
        for addr in addrs:
            if addr.family == socket.AF_INET:
                iface["ipv4"].append(addr.address)
            elif addr.family == psutil.AF_LINK:
                iface["mac"] = addr.address.replace("-", ":")
        if iface["ipv4"] or iface["mac"]:
            interfaces.append(iface)

    io = psutil.net_io_counters()
    config["network"] = {
        "interfaces": interfaces,
        "bytes_sent": io.bytes_sent,
        "bytes_recv": io.bytes_recv,
        "packets_sent": io.packets_sent,
        "packets_recv": io.packets_recv,
    }

    # ── GPU (如果可用) ──
    config["gpu"] = _detect_gpu()

    # ── 已安装的包数量 ──
    config["python_packages"] = _count_packages()

    # ── 运行进程数 ──
    config["process_count"] = len(psutil.pids())

    # ── 启动时间 ──
    boot_ts = psutil.boot_time()
    config["boot_time"] = datetime.fromtimestamp(boot_ts).strftime("%Y-%m-%d %H:%M:%S")
    config["uptime_hours"] = round((time.time() - boot_ts) / 3600, 1)

    # ── 采集时间 ──
    config["collected_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    config["timestamp"] = time.time()

    return config


def _detect_gpu():
    """尝试检测 GPU 信息。"""
    gpus = []
    try:
        import subprocess
        result = subprocess.run(
            ["lspci"], capture_output=True, text=True, timeout=5
        )
        for line in result.stdout.splitlines():
            if "VGA" in line or "3D" in line or "Display" in line:
                gpus.append(line.split(":")[-1].strip())
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return gpus


def _count_packages():
    """统计已安装的 Python 包数量。"""
    try:
        import pkg_resources
        return len(list(pkg_resources.working_set))
    except Exception:
        return None


# ── 格式化输出 ──────────────────────────────────────────────────

def format_text_report(config: dict) -> str:
    """生成人类可读的配置报告文本。"""
    lines = [
        "=" * 60,
        "  主机配置采集报告 (collect_config.py)",
        "=" * 60,
        f"  采集时间:  {config.get('collected_at', '-')}",
        "",
        "-" * 60,
        "  基本信息",
        "-" * 60,
        f"  主机名:    {config.get('hostname', '-')}",
        f"  系统:      {config.get('platform', '-')} {config.get('platform_release', '')}",
        f"  版本:      {config.get('platform_version', '-')[:60]}",
        f"  架构:      {config.get('architecture', '-')}",
        f"  处理器:    {config.get('processor', '-')[:50]}",
        f"  Python:   {config.get('python_version', '-')}",
        f"  启动时间:  {config.get('boot_time', '-')}",
        f"  运行时长:  {config.get('uptime_hours', 0)} 小时",
        "",
        "-" * 60,
        "  CPU",
        "-" * 60,
    ]
    cpu = config.get("cpu", {})
    lines += [
        f"  物理核心:  {cpu.get('physical_count', '?')}",
        f"  逻辑核心:  {cpu.get('logical_count', '?')}",
        f"  主频:      {cpu.get('frequency_mhz', 0):.0f} MHz (最大 {cpu.get('frequency_max_mhz', 0):.0f} MHz)",
        f"  使用率:    {cpu.get('usage_percent', 0):.1f}%",
    ]
    if cpu.get("load_avg_1m") is not None:
        lines.append(
            f"  负载均值:  {cpu['load_avg_1m']} / {cpu['load_avg_5m']} / {cpu['load_avg_15m']} (1/5/15分钟)"
        )
    lines += [
        "",
        "-" * 60,
        "  内存",
        "-" * 60,
    ]
    mem = config.get("memory", {})
    lines += [
        f"  总量:      {mem.get('total_mb', 0)} MB ({mem.get('total_mb', 0) / 1024:.1f} GB)",
        f"  可用:      {mem.get('available_mb', 0)} MB",
        f"  已用:      {mem.get('used_mb', 0)} MB",
        f"  使用率:    {mem.get('usage_percent', 0):.1f}%",
    ]
    if mem.get("swap_total_mb", 0) > 0:
        lines += [
            f"  Swap:      {mem.get('swap_total_mb', 0)} MB (使用 {mem.get('swap_percent', 0):.1f}%)",
        ]
    lines += [
        "",
        "-" * 60,
        "  磁盘",
        "-" * 60,
    ]
    for disk in config.get("disks", []):
        lines += [
            f"  [{disk['mountpoint']}] {disk['fstype']}",
            f"    设备:    {disk['device']}",
            f"    总量:    {disk['total_gb']} GB (已用 {disk['used_gb']} GB / 可用 {disk['free_gb']} GB)",
            f"    使用率:  {disk['usage_percent']:.1f}%",
            "",
        ]
    lines += [
        "-" * 60,
        "  网络",
        "-" * 60,
    ]
    net = config.get("network", {})
    for iface in net.get("interfaces", []):
        lines.append(f"  [{iface['name']}]")
        if iface["mac"]:
            lines.append(f"    MAC:     {iface['mac']}")
        for ip in iface["ipv4"]:
            lines.append(f"    IPv4:    {ip}")
    lines += [
        f"  流量统计:  发送 {net.get('bytes_sent', 0) / (1024**3):.2f} GB / 接收 {net.get('bytes_recv', 0) / (1024**3):.2f} GB",
    ]
    if config.get("gpu"):
        lines += [
            "",
            "-" * 60,
            "  GPU",
            "-" * 60,
        ]
        for gpu in config["gpu"]:
            lines.append(f"  {gpu}")
    lines += [
        "",
        "-" * 60,
        "  其他",
        "-" * 60,
        f"  进程数:    {config.get('process_count', '?')}",
        f"  Python包:  {config.get('python_packages', '?')}",
        "",
        "=" * 60,
    ]
    return "\n".join(lines) + "\n"


# ── 主函数 ──────────────────────────────────────────────────────

def save_config(output_dir: str = "."):
    """采集配置并保存到指定目录。"""
    config = collect_all_config()
    out = os.path.abspath(output_dir)
    os.makedirs(out, exist_ok=True)

    json_path = os.path.join(out, "host_config.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)

    txt_path = os.path.join(out, "host_config.txt")
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(format_text_report(config))

    return json_path, txt_path, config


def main():
    args = sys.argv[1:]
    output_dir = "."
    loop_interval = 0
    json_only = False

    i = 0
    while i < len(args):
        if args[i] == "--loop" and i + 1 < len(args):
            loop_interval = int(args[i + 1])
            i += 2
        elif args[i] == "--json":
            json_only = True
            i += 1
        else:
            output_dir = args[i]
            i += 1

    if json_only:
        config = collect_all_config()
        print(json.dumps(config, indent=2, ensure_ascii=False))
        return

    if loop_interval > 0:
        print(f"持续采集模式: 每 {loop_interval} 秒更新一次,输出到 {output_dir}/")
        print("按 Ctrl+C 停止\n")
        try:
            while True:
                jp, tp, cfg = save_config(output_dir)
                print(f"[{cfg['collected_at']}] 已更新: {jp}")
                time.sleep(loop_interval)
        except KeyboardInterrupt:
            print("\n已停止")
    else:
        jp, tp, cfg = save_config(output_dir)
        print(f"配置已生成:")
        print(f"  JSON: {jp}")
        print(f"  TXT:  {tp}")
        print(f"\n{format_text_report(cfg)}")


if __name__ == "__main__":
    main()
