# 工作站整机备份与恢复

从 v0.41.14 起提供离线整机备份。命令在项目目录运行，Python 用项目虚拟环境（Windows 为 `.venv\Scripts\python.exe`）。

## 1. 核对范围

先列出实际备份路径，不创建归档：

```powershell
.venv\Scripts\python.exe scripts\backup_workstation.py create D:\backup\workstation.zip --plan
```

默认按配置确定主库（`secretary.db_path`）与共享目录（`secretary.shared_folder`），并收集已存在的项目 `.env`、`lan_mesh/model_pool.yaml`、`model_pool.yaml`、`lan_mesh/resources.yaml`（资源配置与密钥）。启动时用了自定义 `--config`、`--db`、`--shared` 的，备份时要给同样的参数；也可以 `--extra-file` 追加额外配置文件。

若 `~/.lan_mesh/` 存在，会以 `state` 根备份，其中包括 `mesh_token`、节点身份文件 `device_id_station` / `device_id_worker`、MCP 配置 `mcp_servers.yaml` 等；缺失时跳过。该根排除顶层 `logs/`、`backups/` 和 `station.lock`。`--plan` 只列出路径，不输出 token 或密钥内容。

代码、虚拟环境、云端对象、外部工具工作目录、只在进程环境里的 API Key 不会被收集，需要另行保存。显式指定的缺失路径会报错，不会生成空备份。

## 2. 创建并校验

先停止所有会写这些数据的 Station、Worker 和外部工具（进程检查只是本机辅助，替代不了停止写入），然后：

```powershell
.venv\Scripts\python.exe scripts\backup_workstation.py create D:\backup\workstation.zip --offline-confirmed
.venv\Scripts\python.exe scripts\backup_workstation.py verify D:\backup\workstation.zip
```

归档不能放在备份源内部，同名文件不会覆盖；上限 10 GiB / 100000 文件。符号链接与非普通文件拒绝打包。每个文件用 SHA-256 清单校验；SQLite 通过 backup API 生成含已提交 WAL 内容的独立快照并做 `integrity_check`；备份前后核对源内容，检测到变化即失败。

归档可能含明文密钥与节点身份，工具不打印内容。Linux 上归档 600、恢复目录 700；Windows 由存放目录的 ACL 控制。归档不加密、不签名，摘要只能发现损坏。

## 3. 恢复到新目录

```powershell
.venv\Scripts\python.exe scripts\backup_workstation.py restore D:\backup\workstation.zip D:\restore\workstation
```

目标目录必须不存在。先完整校验再创建目录，绝不覆盖在用数据。恢复结果含 `manifest.json` 与 `恢复说明.md`，列出每个备份根对应的原路径。恢复写盘失败会留下 `RESTORE_INCOMPLETE`，该目录不可用，换新目录重试。

## 4. 切换与回退

停服务和外部写入，先给当前数据做一份新备份。按 `恢复说明.md` 把各根内容复制回原路径；单文件根只复制该文件。不要把旧的 `*-wal`/`*-shm` 覆盖到恢复快照，也不要混用两个批次。

用原配置启动并检查会话、任务归属与交付。换路径要改配置里的主库与共享目录；换机器要核对节点身份，不能让两台活跃节点共用同一身份。

数据恢复不等于在途任务续跑：备份之后发生的工具副作用与执行编号可能不在归档里，必须核实后再继续。升级前另见 [数据库升级与快照](schema-upgrades.md)，迁移前快照只保护主库，不能代替整机备份。
