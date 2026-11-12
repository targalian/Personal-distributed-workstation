# 私有记忆快照、跨站复制与手工故障恢复

iter122 / DS-Bot P1-B后续。`PrivateBackupService` 为Station拥有的Yomk组件；HTTP操作沿用认证/角色中间件。所有路径位于 `/api/station/`，写操作仅boss或现有网格凭据可调用。接收节点可保持备用角色；导出与恢复要求已激活Secretary和ChatHandler。不启动后台复制、不自动传送用户历史。

## 快照范围与一致性

选一个有项目的会话时，快照包含同项目全部现存会话、PM线程原始JSONL、当前作用域记忆和完整修订。项目记忆跨会话更正仍可回链；已删除会话的私有条目不属于现存范围，项目记忆指向已缺失的来源仍维持不可用。无项目会话只包含自身，空project_id不是通配符。原始损坏行保持逐字节原样，索引可重建。

导出持有ChatHandler变更锁及SQLite事务，流式复制文件；每个文件及整个复制窗口复核inode/大小/mtime/ctime。变化则拒绝发布。ZIP使用未压缩格式，每文件SHA256及整体内容地址校验，最多512MiB/10,000文件，对话元数据最多4MiB/1,000会话。原文件一次复制最多512KiB在途；修订JSONL逐行读取上限256KiB。整个私有备份目录配额2GiB（包含接收的分片），无隐式删除策略。

`conversations.json`、`memories.jsonl`、`revisions.jsonl` 与 `history/{conv}/main.jsonl`、`history/{conv}/threads/{pm}.jsonl` 构成封闭清单。拒绝路径穿越、重复ZIP条目、超限展开内容、校验不一致、范围不一致和非连续修订。`context_*` 索引不复制。

快照存储在配置共享工作目录的 `.private_replicas/`（0700）。该路径及指向它的符号链接被共享下载、文件列表和云上传/下载排除；不是操作系统安全沙箱，不阻止具有本机文件权限的进程读取。HTTP沿用现有网格认证；快照SHA用于完整性，不是数字签名或端到端加密。

## 传输与故障语义

用户选已登记device_id；服务从Host记录解析IP/端口，不接受请求体中的任意URL。HTTP每次10秒、禁跟随重定向。GET接收位置后，从该位置继续POST分片；逐片检查offset、总长度和SHA，相同分片重发只确认，不重复追加。最终完整SHA通过后原子重命名。最终字节fsync后进程退出、未重命名时，下次查询会完成内容地址确认。对端已接收但确认丢失，重试同一快照即可，不能创建第二份不同身份来掩盖原状态。

传输中站点停止信号终止后续分片；重启后显式重试可续传。不会重放任务、工具、IM消息或调度动作。单站后台线程/请求模型下串行保护同一接收目录；不支持多个独立Station进程共享同一个私有备份目录。

## 手工恢复

在备用实例激活Secretary，用户明确确认后恢复。目标不得已有同project的会话、相同conv_id、冲突PM绑定、不同原文文件或同memory_id记录。拒绝覆盖活动数据，也不做多主合并。

新增schema18表 `private_restore_jobs`：记忆和审计记录一起提交后状态为prepared；文件落盘使用禁止覆盖的硬链接安装。全部文件完成后，原子发布全局会话index.json才使会话可见。派生检索同步成功后标记completed。若进程在任一阶段退出，同一bundle_id会继续prepared步骤；索引已发布后不再覆盖文件，期间新增消息会保留。完成后的重复恢复不重复记忆或修订。目标冲突需人工解决，不能自动删除用户数据。

恢复的是会话/记忆；项目配置、执行任务、周期计划、供应商凭据、PM存活状态和集群选举不在此快照。恢复后的PM线程用于历史回看，新任务需重新分配PM。主节点不可达时需先遵守现有Secretary激活/仲裁规则；此功能不提供网络分区共识或自动主备切换。旧主重新出现时，不能把两份分别修改的项目记忆自动合并。

## 接口与页面

| 方法 | 路径 | 行为 |
|---|---|---|
| POST | `/api/station/memory-backups/export?conv_id=…` | 创建私有记忆快照 |
| GET | `/api/station/private-bundles` | 本机完整快照清单 |
| GET | `/api/station/private-bundle-peers` | 已登记目标站点 |
| GET | `/api/station/private-bundles/{id}` | 已接收位置与完整性状态 |
| POST | `/api/station/private-bundles/{id}/chunks` | 带offset/total/data/sha256的分片 |
| POST | `/api/station/private-bundles/{id}/push` | 向peer_id传输/续传 |
| POST | `/api/station/memory-backups/{id}/restore` | confirmed:true后手工恢复 |
| POST | `/api/station/private-bundles/{id}/delete` | confirmed:true清理快照；prepared恢复禁止删除 |

“记忆与历史”页面的备份区不依赖当前存在会话，因此干净备用实例也能恢复。已恢复的原文与记忆独立保留，删除快照不会删会话。清单显示项目、会话数、时间、大小及内容地址；Web与React复用同一页面。

## 验证边界

使用临时目录、独立SQLite与真实loopback HTTP服务进行验证，无真实用户数据外发。涵盖来源回链、损坏JSONL原样保留、项目/无项目隔离、修订一致性、重复恢复、已提交响应丢失、真实子进程退出、目标冲突、路径穿越/校验失败、权限与共享/云隔离。真实物理双机、网络分区和断电由后续隔离环境验收，不宣称已经完成。

实测：14新增专项，全量846 passed/1 skipped（156.92s）；真实子进程退出与双loopback HTTP/确认丢失通过；Chrome四组及390px布局通过；门禁0 Blocker/21 Warning、docs259项通过。
