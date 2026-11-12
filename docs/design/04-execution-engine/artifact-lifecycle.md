# 执行产物保留、跨站副本与崩溃语义

iter123 / DS-Bot P1-C后续。新增ArtifactManagementService，Station与独立Worker均注册服务和管理路由。复用[私有快照传输](../07-data-sync/private-memory-backups.md)的分片、SHA、配额和共享/云隔离，不进入普通共享目录下载链路。

## 保留与清理

RuntimeArtifacts为每次执行增加created_at/updated_at，更新状态保留已有管理标记。执行级进程内RLock覆盖写、检查点、读、快照和清理；多个独立进程不得同时写同一执行目录。读取旧格式时兼容mtime，不要求迁移原始记录。

清理必须先预览再确认；默认至少保留7天，界面默认30天，允许1至3650天。预览按task_id和project_id完整匹配，仅终态且通过保护检查的执行可入候选。以下默认保留：

- 未终结的执行（包括崩溃后仍显示running）、本机DB尚未结束的关联任务。
- 验收源、已绑定的工具证据及旧任务交付acceptance_review中的执行引用。
- 手动pinned记录、最新检查点不可用、工具pending_call/review_required未核对的执行。
- 不能确认关联任务状态的远程记录；管理员填写结论后可明确确认任务/工具已结束，但不能绕过本机活跃任务或验收证据保护。

核对结论与当时updated_at绑定，后续执行更新使旧核对失效。验收证据保护不能通过“取消手动保留”移除。新的验收绑定会在原记录或只读副本的管理元数据中写protected_evidence；模型自评仍不等于独立测试通过。

预览包含具体execution_id、总字节数、保护原因及完整目录签名，保存到`.runtime_artifacts/.retention/`，一小时后过期。执行清理逐项重新检查任务、标记和目录内容，预览后变化的项跳过。先在执行锁内把目录原子移动到`.trash/{execution}-{preview}`，再删除；进程在两步之间退出，使用同一预览再次确认可继续清理。没有后台定时删除；坏身份/坏目录不会被当成可清理项。`.pending-*`/`.incoming-*`等未完成目录不是可清理的完整执行，不自动删除需要核对的故障证据。

## 执行副本

快照包含run.json及所有已完整发布的内容地址记录，逐项校验SHA与execution/agent/task/project/attempt。源执行仍在运行时允许读取一个一致的已发布快照，但不会把它标成已完成，也不包含尚未发布的临时文件。复制沿用已登记站点；独立Worker仅可选择其已配置Secretary。

恢复前验证整个快照、文件清单和最新检查点。目标已有原执行或不同副本时拒绝覆盖。全部记录先写暂存目录、fsync后原子发布执行目录。原始SHA、作用域和execution_id不变；run管理元数据增加read_only、source_status、replica_bundle_id、restored_at，显示为replica并默认pinned。相同副本重复恢复只校验，不覆盖已修改的保留策略。RuntimeArtifacts拒绝对副本save/checkpoint/update，因此不会用旧身份续跑工具。

恢复不导入任务/PM状态，不自动重放副作用。检查点用于回看原始要求、观察和待核对工具；重新执行必须走新的任务/尝试。对端主机需显式执行恢复；仅传输成功并不代表产物已进入可读执行目录。

## 接口与界面

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/runtime/artifact-runs?task_id=…&project_id=…` | 本机完整执行清单、记录标识与保护原因 |
| POST | `/api/runtime/artifact-runs/{id}/retention` | 完整scope、pinned及可选人工核对结论 |
| POST | `/api/runtime/artifact-cleanup/preview` | 按范围和保留天数生成具体清理预览 |
| POST | `/api/runtime/artifact-cleanup/execute` | preview_id与confirmed:true，重新检查后清理 |
| POST | `/api/runtime/artifact-runs/{id}/export` | 完整scope生成私有产物快照 |
| POST | `/api/runtime/artifact-replicas/{bundle}/restore` | confirmed:true恢复只读副本 |

保留旧`/api/runtime/artifacts/{execution_id}`和记录分页读取路径。写操作属于现有管理员路径，仅boss/网格凭据可调用，viewer/operator不可清理或复制。Web与React协作页提供“打开产物管理”入口，`/static/artifacts/index.html`共用界面可分页读取完整记录、标记保留、预览清理、指定站点复制及恢复副本。未登记任务的Worker可用任务/项目身份查看自己的记录；身份不匹配不会回退全局搜索。

## 验证边界

真实子进程在记录发布前、发布后及检查点manifest发布后分别SIGKILL：重启只能看到最后完整发布的检查点，内容SHA可读，不将running伪装completed。清理进程在目录移动后退出，同一确认恢复删除。双loopback HTTP验证Worker→配置Secretary复制，恢复后读取范围与只读边界。该验证不是物理断电或真实局域网多机验收；供应商在线长任务完成率、实际账单/延迟与生产窗口仍需专门环境校准。
