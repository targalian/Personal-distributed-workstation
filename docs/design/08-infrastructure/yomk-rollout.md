# 候选包验收与实机放行（iter-111）

当前版本为 **0.41.0 候选**，源码按用户授权通过 sync_push.ps1 上库。阶段 1～7 已通过阶段回归，阶段 8 的
前端与运维代码、本机验收已完成；真实多机演练需要可用测试节点，尚未放行上线。

## 本机证据

- 源码版本读取根 VERSION.json，wheel 构建用同一值，安装后从 distribution metadata 回读。
  `lan-mesh --version`、Station/Worker OpenAPI version 与发布版本统一。
- `pyproject.toml` 是运行依赖事实源，`python scripts/sync_dependencies.py` 检查生成的
  requirements.txt；修改后用 `--write` 同步。补齐 wheel 的 cryptography 与开发环境的 httpx。
- 新建验证虚拟环境安装 wheel 和声明依赖，不借用原环境残留包。测试发现 FastAPI 0.162
  将 UploadFile 的 OpenAPI 从 format=binary 改成 contentMediaType；为保持本项目契约，
  固定重构前已验证的 FastAPI 0.128.8，未用放宽快照断言掩盖差异。
- wheel 含 CLI、16 个仪表盘源模块、React 构建产物、Yomk 源码及许可证；明确排除用户
  resources.yaml。安装模式的版本、源模块组装和原生框架调用已检查。
- 剩余兼容路由的 76 处同步领域/磁盘/网络调用移到线程池；新增阻塞自愈时并发查询测试。
  发现服务替换后由路由实时解析当前实例，避免自愈后仍读旧 discovery。
- 真实 loopback HTTP 验证 Worker→Yomk 执行→文件结果、取消、应用关闭；浏览器与前端构建
  证据见[前端说明](../09-frontend/yomk-frontend.md)。

最终全量测试数字由重构评估与 loop_status.json 记录。

重复构建时先核实并清理 setuptools 的生成目录 build/lib，再核对 wheel 成员均来自当前源码，
避免旧 SPA 哈希文件残留。最终候选包为 193 文件、729712 字节，SHA-256：
`b7775b2e21c77dc2213a260fe42c8b30e45d8d60f2936c04bd99b0f69d148781`。
复装后 pip check 通过；不包含用户 resources.yaml。

## 尚需用户提供的条件

2026-10-25 对现有配置中的本机、LAN 与 VPN 节点做了只读 `/health` 检查，本机与两台 LAN
地址连接失败，VPN 节点超时，未找到可用于演练的在线双节点。未修改这些节点，也未部署或派发任务。

需要两台可达的测试节点/虚拟机及地址，并确认允许安装候选包和使用隔离端口联调。
当前 station 的身份和单实例锁是用户级 `~/.lan_mesh`，因此仅更换 checkout/HTTP 端口
不能隔离已有站点；测试应使用独立测试账户/虚拟机与数据目录。测试网格采用专用 discovery.port，
避免加入已有网格。HTTP 端口、shared_folder 与 secretary.db_path 在独立配置中显式指定。

## 演练与回滚顺序

1. 在隔离节点安装同一候选 wheel，确认 `lan-mesh --version` 与 `/api/version` 一致。
2. 用专用发现端口和两份配置启动 Station/Worker，验证注册、心跳、角色切换与断线恢复。
3. 验证本机/远程任务、PM 身份隔离、取消/暂停/恢复与交付；观察 WS、记账去重和蓝图回流。
4. 验证反复启停与资源热重载，确认无旧轮询复活，关闭报告无遗留 pending；长 CLI 任务单独
   等待其退出，不将强制中止当成正常完成。若需要真实模型验收，由测试节点配置受控测试资源。
5. 在测试数据库副本上验证回滚：停候选进程、确认退出，再恢复旧包/配置与快照重启。
   本次 schema 仍为 12，没有主键变更，但不能跳过任务执行态与资源配置的回滚检查。

通过上述演练后再提交/放行部署。仓库推送仍只能使用 scripts/sync_push.ps1；本轮没有推送。
