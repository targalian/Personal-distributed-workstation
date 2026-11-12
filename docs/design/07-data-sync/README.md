# 07 数据与同步

主机间文件与数据的流转：共享文件夹、云存储双向同步、主机画像采集。

## 模块清单

<!-- AUTO:module-list -->
| 文件/目录 | 职责一句话 |
|---|---|
| artifact_backup.py | Immutable execution replication preserving scope and checksums without replaying tools. |
| cloud_sync.py | 云存储同步管理器 — S3 兼容服务 (阿里云 OSS / MinIO / AWS S3) |
| cloud_transfer_service.py | CloudTransferService: domain behavior separated from the public compatibility facade. |
| host_info.py | 主机配置自动采集 - 使用 psutil 收集 CPU/内存/磁盘/OS/网络信息 |
| memory_backup.py | Consistent project-scoped memory snapshots preserving raw histories and revision journals. |
| memory_restore.py | Explicit failover restore with a durable prepare record and an atomic visibility index. |
| private_backup_routes.py | Administrator-only private backup writes; receivers can remain standby Stations. |
| private_backup_service.py | Owned private backup operations with explicit peer selection and manual failover. |
| private_bundle.py | Immutable private bundles with bounded validation and resumable authenticated transport. |
| shared_folder.py | 共享文件夹自动管理 - 自动创建、列举、读写共享目录 |
| station_sync_service.py | StationSyncService: domain behavior separated from the public compatibility facade. |
<!-- /AUTO:module-list -->
---

## shared_folder.py — 共享文件夹

**职责**: 自动创建、列举、读写共享目录（默认 `~/lan_mesh_shared`）；
生成主机配置报告（host_config.json）。参考 QuickLAN Shared Store，
简化为基于目录的文件共享。

**使用方**: worker / station（暴露文件）、cloud_sync（同步源）、
api / station_api（上传下载端点）。

## cloud_sync.py — 云存储同步

**职责**: 共享文件夹与 S3 兼容云存储的双向增量同步。

**设计要点**:
- boto3 客户端；每台主机以 device_id 为前缀存储，避免冲突
- 上传本地新增/修改 + 下载云端新增/修改（双向增量）
- 启动时自动同步一次，可配置定时同步（sync_interval）
- 密钥优先从环境变量读取

## host_info.py — 主机画像采集

**职责**: psutil 采集 CPU/内存/磁盘/OS/网络信息，构造 protocol.HostInfo。
参考 QuickLAN device identity，扩展完整硬件画像。是注册/心跳/评级/
资源调度的数据来源。

## 变更记录

| 日期 | 迭代 | 摘要 |
|---|---|---|
| 2026-08-16 | iter-27 后 | 初建 |

### iter122 私有记忆复制
新增项目/会话私有快照、校验分片续传与手工恢复，原文和修订闭环、拒绝覆盖现存项目，见 [协议与故障语义](private-memory-backups.md)。

### iter123 执行产物副本
复用私有快照传输，对每条记录核对内容地址与完整执行范围；Station与独立Worker均支持，Worker只向已配置Secretary推送。目标副本只读且默认保留，不自动续跑工具。
