# 07 数据与同步

主机间文件与数据的流转：共享文件夹、云存储双向同步、主机画像采集。

## 模块清单

<!-- AUTO:module-list -->
| 文件/目录 | 职责一句话 |
|---|---|
| cloud_sync.py | 云存储同步管理器 — S3 兼容服务 (阿里云 OSS / MinIO / AWS S3) |
| host_info.py | 主机配置自动采集 - 使用 psutil 收集 CPU/内存/磁盘/OS/网络信息 |
| shared_folder.py | 共享文件夹自动管理 - 自动创建、列举、读写共享目录 |
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
