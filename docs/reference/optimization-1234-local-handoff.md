# 四项优化交付记录（iter125）

2026-11-17源码交接：用户授权将本地更新上库。本次将iter125与GitHub CN截至`07145b5`的修复合并为0.41.14，使用`sync_push.ps1 -SkipGitee`仅同步GitHub CN；实际提交与远端状态以Git核验。下面的“未提交/未发布”和验证数字为各日期的历史记录，实机验收边界保持独立。

## 历史执行板（2026-11-14）

| 字段 | 内容 |
|---|---|
| 唯一主交付 | 真实双机持久投递闭环验收报告：origin `192.168.100.193`，peer `192.168.100.30` |
| 完成判据 | runner 报告 `ok=true`，7 项检查全 true，`classification=physical_protocol_test`，两机 machine-id 身份不同；保存 report.json 与进程日志 |
| 基线 | 发布版 0.41.12 / HEAD 1768a37 + 未提交 iter125 增量；本机双peer已 27 项专项、7/7 检查通过（simulated）；两机 Python 均 3.12.14 |
| 工作线 1（负责人：工作站） | 第二节点依赖与代码离线传输，隔离 peer 启动 |
| 工作线 2（负责人：工作站） | runner 跑真实双机检查，证据归档，回写台账 |
| 关键阻塞 | 无。原 Python 3.10 阻塞已于 11-13 解除（用户授权安装 3.12.14） |
| 下一动作 | 传 site-packages 与工作树 → 起两端 peer → 跑 runner → 归档报告 |
| 授权与边界 | 用户已授权该 SSH 与优化1-4；只起隔离测试进程，不动生产 Station、不装系统包、不提交推送。升级回滚、断电、在线模型对照不在本次判据内 |

日期：2026-11-13。基于发布版 0.41.12 / HEAD 1768a37 的未提交增量。
本轮完成四项优化的本地工具与交互实现；物理双机闭环、升级回滚和真实模型对照仍未完成。

## 交付范围

| 优化 | 已实现并验证 | 尚未验收 |
|---|---|---|
| 1 双机闭环 | 隔离双peer工具；生产DurableDeliveryService派发、执行、outbox/inbox、PM应用；任务与结果去重；长Unicode SHA；取消先到栅栏；暂停发送与持久重试 | 第二台实机运行、物理断网、进程升级回滚、角色切换、地址变化、断电 |
| 2 通知与待核对 | 执行、最终结果入站、人工验收、共享已读状态独立；等待输入、待人工核对、未送达明确显示；持久去重、POST已读、GET无业务写；通知失败不破坏交付 | 真模型/远端节点联调；当前最终交付协议无独立任务attempt标识，入站证据不代表远端ACK |
| 3 环境预检 | Python门槛修正为>=3.11；只读doctor检查声明依赖、Node、CLI路径/停用、隔离目录和版本关系 | CLI登录与真实可运行性；第二节点项目依赖与联调（独立Python已于后续维护安装） |
| 4 同任务基准 | 模板/严格校验/分组统计；输入与验收规则hash；同模型参数比较；fixture隔离；完成率、耗时、人工介入运行数、费用覆盖率、恢复率 | LAN Mesh与DS Bot的真实同模型同任务运行及费用账单；不报告胜负 |

## 实测证据

主环境 `/tmp/lan-mesh-test-py312`，Python 3.12.14，Node v24.21.0。

- 全量：**965 passed / 1 skipped / 7 warnings / 176.20s**，966项收集；日志 `/tmp/lanmesh-opt1234-final/pytest-full.log`。
- 新增104项：双peer27、doctor24、通知18、基准35。通知/既有协作/服务/基准联合专项 **86 passed / 1 warning / 14.00s**；日志 `/tmp/lanmesh-integration-special.log`。
- 真实Chrome：1280桌面、390移动各14项，共28项行为断言通过；API使用明确fixture。后端真实HTTP最终交付与viewer/operator权限另由pytest验证。浏览器报告 `/tmp/lanmesh-opt1234-final/browser-report.json`；截图同目录 `collaboration-1280.png`、`collaboration-390.png`。自动断言验证布局与行为，截图未做模型目视评审。
- 独立双peer报告：`/tmp/lanmesh-opt1234-final/protocol-217917e5d82b4ca396c0d6a6ad9fbce2/report.json`，7项全部true，classification=simulated。成功执行一次、应用一次；取消执行一次且未调用fixture runtime；回传发送3次（含1次重试），两条结果均acked，inbox两条processed。
- 长结果SHA256：`8bf75ab6ccc7c6ee04eddf419d0c3f79a7090bdf560ec6d0137490fabbb116be`。
- 编译/StationController导入、Node语法、未绑定名称扫描（212文件）、设计清单（275条/11域）、dev_status --verify及git diff --check通过。
- doctor实际运行：基础环境可继续检查，版本关系expected_sync；CLI登录始终unverified。结果 `/tmp/lanmesh-opt1234-final/doctor.json`。
- 7项warning为既有Starlette/FastAPI弃用提示；1项skip为未提供的私有资源配置。

双peer的发送层使用实例专属认证transport，运行固定fixture，不调用模型；它测试生产持久投递组件，不冒充完整Station/选举/上线验收。暂停发送属于应用层故障模拟。既有36项mesh_delivery回归另覆盖真实回环断连和子进程突然退出，均纳入上述全量。

## 第二台机器

用户提供 `zero-mass-foam@192.168.100.30`，只读SSH已连通。已发现Linux x86_64、Python3.10.12、PATH无Node；已检查的常见位置未发现3.11/3.12。纯后端不强制Node，Python>=3.11是前提。

当前Bot开发约定禁止操作外部服务，因此本轮未远端安装、部署或启动/停止服务。真实双机验收仍阻塞。后续需在独立目录准备合规Python及声明依赖，再按 `docs/design/08-infrastructure/mesh-acceptance.md` 启动隔离peer；升级/回滚另按现有yomk-rollout流程验证并保存版本与数据库证据。没有把SSH可达当成双机通过。

## 可复跑

```bash
export PATH=/home/zlz/.local/opt/ds-bot-0.3.5/resources/primary-runtime/dependencies/node/bin:$PATH
/tmp/lan-mesh-test-py312/bin/python -m pytest -q
/tmp/lan-mesh-test-py312/bin/python scripts/offline_doctor.py --profile frontend --json
/tmp/lan-mesh-test-py312/bin/python scripts/sync_docs.py
/tmp/lan-mesh-test-py312/bin/python scripts/dev_status.py --verify
```

保留既有工作，未改系统Python或旧.venv、未提交推送、未恢复Gitee同步。VERSION.json仍描述已发布0.41.12，本轮未发布增量记入loop_status。日志位于临时目录，长期归档时应一并保存；本记录保留已核对统计。

## 后续更新：第二节点Python已就绪

用户随后明确授权独立运行时安装，已在第二节点安装并验证Python3.12.14和pip25.0.1，创建专用venv，保留系统3.10与现有服务。上文“未远端安装”是本地交付时的历史状态；当前解释器阻塞已解除，项目依赖与双机验收仍未执行。路径和实际检查见 [安装记录](remote-python312-install.md)。

## 2026-11-17 合并上库验证

本地增量先通过965 passed / 1 skipped / 7 warnings（176.34s），保存为`413d4a8`。与GitHub CN `07145b5`合并后，全量实际**1011 passed / 5 skipped / 27 warnings（182.85s）**；新增跳过项为4项Windows cmd专用测试，其余1项缺少私有resources.yaml。历史0.41.13记录中的29项基线失败不代表本次结果，本次合并回归无失败。

30个合并Python文件编译和StationController导入通过；前端TypeScript、SPA资源引用、依赖清单、doctor、文档280条/11域通过。门禁0 Blocker、39 Warning，仍保留函数长度、类型标注和日志前缀等提示。本次没有重做历史Chrome fixture验收，也未部署或进行物理多机/在线模型验收。日志与JUnit位于`/tmp/lan-mesh-submit1117-merged-pytest.log`和`/tmp/lan-mesh-submit1117-merged-results.xml`。
