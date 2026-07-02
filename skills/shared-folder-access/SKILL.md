---
name: shared-folder-access
description: LAN Mesh 跨主机共享文件夹访问技能。通过 HTTP API 列出、下载、上传远程主机的共享文件，使用 SharedFolderManager 管理本地共享目录。当用户请求查看远程文件、传输文件到远程主机、浏览共享文件夹内容，或 Agent 需要读写其他主机的共享目录时触发。
category: infrastructure
tags: [shared-folder, file-transfer, cross-host, HTTP-API, SharedFolderManager]
default_access: ["station", "secretary", "worker"]
version: "1.0"
---

# 跨主机共享文件夹访问

## 架构概览

LAN Mesh 的共享文件夹体系基于 **SharedFolderManager**（`lan_mesh/shared_folder.py`），每台主机维护一个本地共享目录，通过 HTTP API 暴露给局域网内其他主机访问。

```
主机A ──HTTP GET /shared──▶ 主机B SharedFolderManager ──▶ 本地磁盘 ~/lan_mesh_shared/
```

## 配置

共享目录路径在 `config.yaml` 中按角色配置：

```yaml
secretary:
  shared_folder: "~/lan_mesh_shared"
worker:
  shared_folder: "~/lan_mesh_shared"
```

`config.py` 中 `get_shared_folder(cfg, role)` 根据角色返回对应路径。

## HTTP API 端点

### 列出共享文件

| 角色 | 端点 |
|------|------|
| Worker | `GET /shared` |
| Secretary | `GET /api/shared` |
| Station Director | `GET /api/shared` |

**响应格式**：
```json
{
  "folder": "/root/lan_mesh_shared",
  "files": [
    {
      "name": "report.txt",
      "path": "report.txt",
      "size": 1024,
      "is_dir": false,
      "modified": 1700000000.0
    },
    {
      "name": "data",
      "path": "data",
      "size": 8192,
      "is_dir": true,
      "file_count": 5,
      "modified": 1700000000.0
    }
  ],
  "file_count": 6
}
```

### 下载共享文件

`GET /shared/{file_path}` — 仅 Worker 提供此端点。

返回 `FileResponse`，`media_type` 为 `application/octet-stream`。

错误码：
- `404` — 文件不存在
- `403` — 路径穿越攻击被拦截

### 上传文件到共享目录

`POST /shared` — 仅 Worker 提供此端点。

使用 `multipart/form-data`，字段名 `file`：

```python
import httpx

with open("local_file.txt", "rb") as f:
    resp = httpx.post(
        f"http://{target_ip}:{target_port}/shared",
        files={"file": ("filename.txt", f)},
        timeout=120,
    )
```

**响应格式**：
```json
{
  "ok": true,
  "filename": "filename.txt",
  "path": "/root/lan_mesh_shared/filename.txt",
  "size": 1024
}
```

同名文件自动加序号：`file.txt` → `file (1).txt`。

## 跨主机访问模式

### 模式一：直接 API 调用

已知目标主机的 IP 和端口时，直接调用其 HTTP API：

```python
import httpx

# 列出远程共享文件
resp = httpx.get(f"http://{ip}:{port}/shared", timeout=10)
files = resp.json()["files"]

# 下载远程文件
resp = httpx.get(f"http://{ip}:{port}/shared/{filename}", timeout=60)
with open(local_path, "wb") as f:
    f.write(resp.content)

# 上传文件到远程
with open(local_path, "rb") as f:
    resp = httpx.post(
        f"http://{ip}:{port}/shared",
        files={"file": (filename, f)},
        timeout=120,
    )
```

### 模式二：通过 P2P 传输

通过 Station Director 的 `/api/p2p/transfer` 端点，自动路由到目标主机：

```python
# Station Director 代理转发文件到目标主机
resp = httpx.post(
    f"http://{station_ip}:{station_port}/api/p2p/transfer",
    data={"target_device_id": device_id},
    files={"file": (filename, data)},
    timeout=120,
)
```

Station Director 内部调用 `_resolve_p2p_target(device_id)` 获取目标 IP/端口，再转发到目标的 `/shared` 端点。

### 模式三：获取主机 IP/端口

从 `/api/hosts` 或 `/api/station/fleet` 获取在线主机的 IP 和端口：

```python
resp = httpx.get(f"http://{secretary_url}/api/hosts")
hosts = resp.json()["hosts"]
for h in hosts:
    if h["online"] and h["ip"] and h["api_port"]:
        # 可访问 http://{h['ip']}:{h['api_port']}/shared
        pass
```

## SharedFolderManager 本地操作

```python
from lan_mesh.shared_folder import SharedFolderManager

mgr = SharedFolderManager("~/lan_mesh_shared")

# 列出文件
files = mgr.list_files()  # 返回 list[dict]

# 读取文件
path = mgr.get_file("report.txt")  # 返回 Path，不存在抛 FileNotFoundError

# 保存上传
dest = mgr.save_upload("data.csv", csv_bytes)  # 返回保存后的 Path

# 写主机配置报告
mgr.write_host_config(host_info)  # 生成 host_config.json + host_config.txt

# 统计文件数
count = mgr.file_count()
```

## 安全机制

1. **路径穿越防护**：`resolve_path()` 对相对路径做 `resolve()` 后校验是否仍在共享目录下，越界则抛 `ValueError`
2. **文件名清理**：`_safe_filename()` 替换 `<>:"/\|?*` 及控制字符为 `_`，去除首尾空格和点号
3. **隐藏文件过滤**：`list_files()` 跳过以 `.` 开头的条目
4. **权限错误容忍**：遍历时捕获 `PermissionError` / `OSError` 并跳过

## 前端集成

Dashboard 中通过 `browseFiles(ip, port)` 函数浏览远程文件：

```javascript
async function browseFiles(ip, port) {
  const r = await fetch(`http://${ip}:${port}/shared`);
  const d = await r.json();
  // 渲染文件列表到 modal
}
```

触发入口在主机详情弹窗的「📂 浏览共享文件」按钮（仅当主机在线且有 IP/端口时显示）。
