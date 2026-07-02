---
name: cloud-storage-sync
description: 通过云存储 (S3 兼容) 实现跨主机共享文件夹同步，支持阿里云 OSS、MinIO、AWS S3
category: ops
tags: [cloud, sync, s3, oss, shared-folder, cross-host]
default_access: [all]
version: "1.0"
---

# 云存储共享文件夹同步

通过 S3 兼容云存储（阿里云 OSS / MinIO / AWS S3）实现跨主机共享文件夹自动同步。

## 工作原理

```
主机A 共享文件夹 ──上传──→ 云存储 bucket
                              ↑↓
主机B 共享文件夹 ──上传──→ 云存储 bucket
```

每台主机启动时：
1. 将本地共享文件夹中的文件**上传**到云端（以 `device_id` 为前缀隔离）
2. 从云端**下载**其他主机上传的新文件
3. 可选定时自动同步（默认 300 秒）

## 配置

### 1. config.yaml

```yaml
cloud_storage:
  enabled: true                        # 启用云存储同步
  endpoint: "oss-cn-hangzhou.aliyuncs.com"  # S3 端点
  bucket: "lan-mesh"                   # 存储桶名称
  prefix: "shared/"                    # 桶内路径前缀
  access_key: ""                       # 访问密钥（建议用环境变量）
  secret_key: ""                       # 密钥（建议用环境变量）
  region: ""                           # 区域（部分服务可留空）
  secure: true                         # 是否使用 HTTPS
  auto_sync: true                      # 启动时自动同步
  sync_interval: 300                   # 自动同步间隔（秒），0 = 仅启动时同步一次
```

### 2. 环境变量（推荐）

密钥建议通过环境变量配置，避免明文写入配置文件：

```bash
export CLOUD_STORAGE_ACCESS_KEY="your-access-key"
export CLOUD_STORAGE_SECRET_KEY="your-secret-key"
```

### 3. 常见云存储端点

| 服务 | 端点示例 |
|------|----------|
| 阿里云 OSS | `oss-cn-hangzhou.aliyuncs.com` |
| AWS S3 | `s3.amazonaws.com` |
| MinIO | `your-minio-server:9000` |
| 腾讯云 COS | `cos.ap-guangzhou.myqcloud.com` |

## API 端点

### 查询同步状态

```
GET /api/cloud-sync/status
```

响应示例：
```json
{
  "enabled": true,
  "running": true,
  "auto_sync": true,
  "sync_interval": 300,
  "endpoint": "oss-cn-hangzhou.aliyuncs.com",
  "bucket": "lan-mesh",
  "prefix": "shared/abc123/",
  "local_path": "/home/user/lan_mesh_shared",
  "last_sync": {
    "uploaded": 3,
    "downloaded": 5,
    "errors": [],
    "duration": 1.23,
    "timestamp": 1700000000
  }
}
```

### 手动触发同步

```
POST /api/cloud-sync/sync
```

响应示例：
```json
{
  "uploaded": 2,
  "downloaded": 3,
  "errors": [],
  "duration": 0.85
}
```

### 测试云存储连接

```
POST /api/cloud-sync/test
```

响应示例：
```json
{
  "ok": true,
  "bucket": "lan-mesh",
  "endpoint": "oss-cn-hangzhou.aliyuncs.com"
}
```

## 云端存储结构

```
s3://bucket/
└── prefix/
    ├── device_id_A/
    │   ├── host_config.json
    │   ├── collect_config.py
    │   └── report.txt
    └── device_id_B/
        ├── host_config.json
        └── data.csv
```

每台主机的文件以 `device_id` 为前缀隔离存储，避免文件名冲突。

## 使用场景

### 场景 1：跨网段主机共享文件

当主机不在同一局域网时，无法通过 UDP 广播发现对方。通过云存储同步，所有主机启动后自动上传文件到云端并下载其他主机的文件，实现跨网段文件共享。

### 场景 2：离线主机文件同步

主机离线期间的文件变更，在下次启动时自动同步到云端，其他主机可获取最新文件。

### 场景 3：多站点协作

不同办公室/数据中心的主机通过同一云存储桶共享配置报告和数据文件。

## 依赖

```
pip install boto3>=1.28.0
```

## 注意事项

1. **密钥安全**：生产环境务必使用环境变量配置密钥，不要明文写入 config.yaml
2. **网络带宽**：首次同步可能传输大量文件，建议在带宽充足时进行
3. **存储成本**：注意云存储的存储和流量计费
4. **文件冲突**：当前以 `device_id` 前缀隔离，不同主机不会覆盖同一文件
5. **同步延迟**：定时同步间隔内的文件变更不会立即可见
