# 隔离 Mesh 协议验收

工具只运行固定 deterministic fixture，不调用模型；input/output token 均为 0。
它验证生产 DurableDeliveryService 的执行、持久 outbox/inbox 和 PM 绑定，
owner 实例覆盖 `_send` 为专用认证 transport，不修改全局 requests 或生产密钥。

默认 `python scripts/mesh_acceptance.py` 仅输出 plan 和随机 run_id，无网络、无目录写入。
在 runner 和两端通过安全渠道设置同一个环境变量（至少 32 位 ASCII 非空白随机 secret），
以 `--token-env MESH_ACCEPTANCE_TOKEN` 指定变量名；secret 不进入命令行或报告。
每轮生成新 secret，使用 plan 给出的同一 `--run-id`。

两端分别运行 `scripts/mesh_acceptance_peer.py --execute --run-id RUN --token-env MESH_ACCEPTANCE_TOKEN --origin http://HOST_A:19082 --peer http://HOST_B:19081 --role origin --data-dir /absolute/new-origin`，
第二端改为 `--role remote --data-dir /absolute/new-remote`。
runner 使用相同 run-id、token-env、origin、peer，另指定 `--execute --data-dir /absolute/new-report`。
所有目录必须不存在，exclusive mkdir 与 owner.lock 拒绝复用数据库。
URL 必须带明确端口，无路径、凭据、query、fragment；常见生产端口被拒绝，任何非 200（包含重定向）失败。

经明确允许的本地测试可运行：

```bash
python scripts/mesh_acceptance.py --execute --self-test --run-id RUN --token-env MESH_ACCEPTANCE_TOKEN --data-dir /absolute/new-run
```

RUN 须为 32 位小写十六进制。self-test 用 sys.executable 启动两个隔离子进程，
通过环境传 secret，超时后 finally 仅终止本次创建的进程；保留数据库及 report.json。
默认 loopback 端口 19081/19082 需空闲，可显式更换；pytest 自动选择临时端口。FixtureDatabase 禁用生产全局备份，所有数据库只落专用目录。

派发端通过生产 queue_task/queue_cancel 入 outbox 并由泵发出，核对 ACK 与发送次数。
检查包括精确 task ACK ID/SHA、重复任务实际执行一次、长 Unicode 结果 SHA、
重复结果 ACK 与 origin 仅应用一次。独立 cancel attempt 在 task 到达前建立取消 fence，
随后核对 cancelled、总执行次数未增加及业务应用数未增加。
暂停路由只暂停当前 owner 的结果发送，执行后检查 pending、已发生发送失败和 origin 未应用，恢复后检查 acked 与 retries>=1。
此项明确命名 application_outage_simulation，不模拟断电或系统网络故障。

/status 必须认证且 run_id 匹配；身份由各 peer 本机 machine-id 哈希（缺失回退 hostname）产生，
报告记录 identity_source。仅不同身份且两个目标均为非 loopback IP 字面量时标记 physical_protocol_test；
loopback 为 simulated，DNS 名或证据不足为 unknown。身份是认证 peer 自述，不是硬件证明。

每项检查为实际布尔值；失败返回非零，异常类型及已取得证据写 report.json。
不覆盖选举、在线模型、升级回滚、地址迁移或断电恢复。

本次已实际运行 27 项专项（含真实双进程 loopback），通过。联合 doctor 专项 51 passed。
报告中的 application_outage_simulation 仅为发送端应用层暂停；不等于物理断网、进程重启或断电恢复。既有 tests/test_mesh_delivery.py 另含实际 loopback 断连与子进程崩溃恢复回归。

```bash
/tmp/lan-mesh-test-py312/bin/python -m pytest -q tests/test_mesh_acceptance.py tests/test_mesh_delivery.py
```

2026-11-13 第二节点只读 SSH 已连通：zero-mass-foam@192.168.100.30，Python 3.10.12，无 PATH Node；未部署或启动远端服务。需独立 Python>=3.11 后再按上述双端模式运行；本轮不宣称物理双机、升级回滚或在线模型验收通过。

后续维护更新（2026-11-13）：用户已明确授权安装用户目录独立Python3.12；第二节点现已安装并验证Python3.12.14，venv为 `/home/zero-mass-foam/.local/share/lan-mesh/venvs/py312`。系统3.10保留；解释器版本阻塞已解除，项目依赖/双机验收未执行，未启停既有服务。详见 `docs/reference/remote-python312-install.md`。
