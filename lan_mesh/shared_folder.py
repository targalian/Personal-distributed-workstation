"""
共享文件夹自动管理 - 自动创建、列举、读写共享目录

参考 QuickLAN 的 Shared Store 设计，简化为基于目录的文件共享。
包含主机配置报告自动生成功能。
"""
import json
import os
import shutil
import textwrap
from datetime import datetime
from pathlib import Path
from typing import List


class SharedFolderManager:
    """共享文件夹管理器。

    在启动时自动创建共享目录，提供文件列表和下载能力。
    参考QuickLAN的 shared_store，但简化为直接暴露目录。
    """

    def __init__(self, folder_path: str):
        self.path = Path(folder_path).expanduser().resolve()
        self.ensure()

    def ensure(self) -> Path:
        """确保共享目录存在，不存在则创建。"""
        self.path.mkdir(parents=True, exist_ok=True)
        # 创建一个 README 说明文件
        readme = self.path / ".lan_mesh_shared"
        if not readme.exists():
            readme.write_text(
                "此文件夹由 LAN Mesh 自动创建，用于跨主机文件共享。\n",
                encoding="utf-8",
            )
        return self.path

    def list_files(self) -> List[dict]:
        """列出共享目录中的所有文件（递归一层）。"""
        result = []
        if not self.path.is_dir():
            return result

        for entry in sorted(self.path.iterdir()):
            if entry.name.startswith("."):
                continue
            try:
                if entry.is_file():
                    stat = entry.stat()
                    result.append({
                        "name": entry.name,
                        "path": str(entry.relative_to(self.path)),
                        "size": stat.st_size,
                        "is_dir": False,
                        "modified": stat.st_mtime,
                    })
                elif entry.is_dir():
                    # 统计子目录文件数与总大小
                    file_count = 0
                    total_size = 0
                    for f in entry.rglob("*"):
                        if f.is_file():
                            file_count += 1
                            total_size += f.stat().st_size
                    result.append({
                        "name": entry.name,
                        "path": str(entry.relative_to(self.path)),
                        "size": total_size,
                        "is_dir": True,
                        "file_count": file_count,
                        "modified": entry.stat().st_mtime,
                    })
            except (PermissionError, OSError):
                continue
        return result

    def file_count(self) -> int:
        """返回共享目录中的文件总数。"""
        count = 0
        if not self.path.is_dir():
            return 0
        for entry in self.path.rglob("*"):
            if entry.is_file() and not entry.name.startswith("."):
                count += 1
        return count

    def resolve_path(self, relative_path: str) -> Path:
        """安全解析相对路径，防止路径穿越攻击。"""
        # 标准化路径，防止 ../ 穿越
        full = (self.path / relative_path).resolve()
        if not str(full).startswith(str(self.path)):
            raise ValueError(f"路径越界: {relative_path}")
        return full

    def get_file(self, relative_path: str) -> Path:
        """获取共享文件的完整路径，不存在则抛出异常。"""
        full = self.resolve_path(relative_path)
        if not full.is_file():
            raise FileNotFoundError(f"文件不存在: {relative_path}")
        return full

    def save_upload(self, filename: str, data: bytes) -> Path:
        """保存上传文件到共享目录。"""
        # 安全文件名
        safe_name = self._safe_filename(filename)
        dest = self.path / safe_name
        # 如果已存在则加序号
        if dest.exists():
            stem = dest.stem
            suffix = dest.suffix
            for i in range(1, 10000):
                candidate = self.path / f"{stem} ({i}){suffix}"
                if not candidate.exists():
                    dest = candidate
                    break
        dest.write_bytes(data)
        return dest

    # ── 主机配置报告自动生成 ───────────────────────────────

    def write_host_config(self, host_info):
        """将主机配置写入共享文件夹。

        生成两个文件:
        - host_config.json  — 机器可读的完整配置 (供程序解析)
        - host_config.txt   — 人类可读的格式化报告 (供人查看)

        每次调用覆盖旧文件,确保数据始终最新。
        """
        data = host_info.to_dict()
        # JSON 格式
        json_path = self.path / "host_config.json"
        json_path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        # TXT 格式 (人类可读)
        txt_path = self.path / "host_config.txt"
        txt_path.write_text(
            self._format_config_text(data),
            encoding="utf-8",
        )
        return json_path

    def _format_config_text(self, d: dict) -> str:
        """生成人类可读的配置报告文本。"""
        ts = datetime.fromtimestamp(d.get("timestamp", 0)).strftime("%Y-%m-%d %H:%M:%S")
        uptime = d.get("uptime_seconds", 0)
        uptime_str = f"{int(uptime // 3600)}h {int((uptime % 3600) // 60)}m {int(uptime % 60)}s"

        lines = [
            "=" * 60,
            "  LAN Mesh 主机配置报告",
            "=" * 60,
            f"  生成时间:   {ts}",
            "",
            "-" * 60,
            "  基本信息",
            "-" * 60,
            f"  设备名称:   {d.get('device_name', '-')}",
            f"  设备 ID:    {d.get('device_id', '-')}",
            f"  角色:       {d.get('role', '-')}",
            f"  主机名:     {d.get('hostname', '-')}",
            f"  操作系统:   {d.get('platform', '-')} {d.get('platform_release', '')}",
            f"  架构:       {d.get('architecture', '-')}",
            f"  Python:    {d.get('python_version', '-')}",
            "",
            "-" * 60,
            "  CPU",
            "-" * 60,
            f"  逻辑核心数: {d.get('cpu_count', 0)}",
            f"  主频:       {d.get('cpu_freq_mhz', 0):.0f} MHz",
            f"  使用率:     {d.get('cpu_percent', 0):.1f}%",
            "",
            "-" * 60,
            "  内存",
            "-" * 60,
            f"  总量:       {d.get('memory_total_mb', 0)} MB ({d.get('memory_total_mb', 0) / 1024:.1f} GB)",
            f"  可用:       {d.get('memory_available_mb', 0)} MB",
            f"  使用率:     {d.get('memory_percent', 0):.1f}%",
            "",
            "-" * 60,
            "  磁盘",
            "-" * 60,
            f"  总量:       {d.get('disk_total_gb', 0)} GB",
            f"  已用:       {d.get('disk_used_gb', 0)} GB",
            f"  可用:       {d.get('disk_free_gb', 0)} GB",
            f"  使用率:     {d.get('disk_percent', 0):.1f}%",
            "",
            "-" * 60,
            "  网络",
            "-" * 60,
            f"  MAC 地址:   {d.get('mac_address', '-')}",
            f"  IP 地址:    {', '.join(d.get('ip_addresses', [])) or '-'}",
            f"  API 端口:   {d.get('api_port', '-')}",
            "",
            "-" * 60,
            "  共享",
            "-" * 60,
            f"  共享目录:   {d.get('shared_folder', '-')}",
            f"  文件数:     {d.get('shared_file_count', 0)}",
            "",
            "-" * 60,
            "  运行时",
            "-" * 60,
            f"  运行时长:   {uptime_str}",
            "",
            "=" * 60,
        ]
        return "\n".join(lines) + "\n"

    def _safe_filename(self, name: str) -> str:
        """清理文件名中的非法字符。"""
        illegal = '<>:"/\\|?*'
        cleaned = "".join("_" if c in illegal or ord(c) < 32 else c for c in name)
        cleaned = cleaned.strip().strip(".")
        return cleaned if cleaned else "unnamed_file"
