# 离线收尾与环境验收清单

本清单区分离线开发证据与真实环境验收。四类环境验收均未完成；源码交付不代表部署或上线验收。

## 1. 物理多机

- 前置条件：至少两台真实、相互隔离的测试主机；具备网络故障注入、地址调整、角色切换及升级回滚条件，备份配置、数据库和产物。
- 步骤：验证节点注册与心跳；改变地址后检查重新发现和路由；断网后恢复，验证角色切换、持久投递及去重；中断复制后验证恢复与产物一致性；执行升级与回滚并检查任务及数据兼容性。
- 通过证据：保留主机标识、拓扑、版本、故障时间线、注册/心跳/角色日志、投递记录、复制恢复记录及产物校验值；无任务静默丢失或重复执行，回滚后可继续运行。单机多实例不能替代真实多机证据。
- 当前状态：**未验收**。

## 2. 在线模型

- 前置条件：可用的目标在线模型、授权凭据和账单访问权限；事先确定上下文窗口、任务样本、质量评分、完成率及成本阈值。
- 步骤：验证 10 秒 HTTP 超时及超时后的状态；测试上下文窗口边界；运行代表性长任务，统计完成率与失败原因，评估输出质量并核对 token 用量和账单成本。
- 通过证据：记录模型及配置、样本数、超时日志、上下文长度、完成率、质量评分、token 明细和账单核对结果；各项达到预先约定阈值。
- 当前状态：**未验收**。

## 3. 断电与磁盘故障

- 前置条件：可恢复的隔离硬件环境、完整备份，以及真实断电或等价硬件故障注入条件。
- 步骤：在 SQLite 写入、journal 持久化、产物落盘及复制过程中分别注入断电或磁盘故障；恢复硬件并重启，检查 SQLite 完整性、journal 重放、任务状态和产物恢复。
- 通过证据：保存硬件故障时间线、数据库完整性检查、journal 恢复日志、任务对账及产物校验结果；已确认持久化的数据可恢复，未完成写入可明确识别并处理。SIGKILL 仅是离线证据，不能替代真实断电或等价硬件故障验收。
- 当前状态：**未验收**。

## 4. 真实 IM 与浏览器配置

- 前置条件：真实 IM 测试账号、认证桥接配置、授权范围及可审计事件；需要浏览器功能时准备可选 Playwright/Chromium 环境。
- 步骤：验证认证桥接与授权检查；重复投递事件并模拟失败重试，检查去重和状态一致性；验证越权请求被拒绝；启用浏览器时验证安装、启动、页面访问和任务结果。
- 通过证据：脱敏认证日志、事件 ID 与去重/重试记录、授权成功与拒绝记录；启用浏览器时补充 Playwright/Chromium 版本及执行证据，不启用时明确记录不适用范围。
- 当前状态：**未验收**。

## 先前离线环境恢复证据（优化前基线）

以下为本轮收尾的实际验证结果；四类真实环境验收仍未执行：

- 本轮测试环境：独立 Python 3.12.14 环境 `/tmp/lan-mesh-test-py312`，运行依赖和 `dev` 可选依赖均按声明安装；前端测试使用已存在的工作站 Node v24.21.0。最终完整回归实际结果：**861 passed / 1 skipped / 7 warnings，217.60s**。未连接真实模型、物理主机、IM 或硬件。
- 交叉验证：独立 Python 3.11.18 环境 `/tmp/lan-mesh-test-py311` + Node v24 也得到 **861 passed / 1 skipped / 7 warnings，162.67s**。
- 对照结果：系统 `/usr/bin/node` 为 v12.22.9，使用它会导致 2 个前端测试失败（不支持 `--test-reporter=tap` 与可选链 `?.`）；这是测试运行时不兼容。使用 Node v24 后 6 个前端专项测试 **6 passed / 3 warnings**，全量通过。
- `dev_status.py --verify`：PASS；`sync_docs.py`：PASS，且无工作区副作用。
- 历史 Python 3.12 全量结果：**861 passed / 1 skipped / 7 warnings / 0 blockers**。本轮已用 Python 3.12.14 重现同样的 861/1/7 实际结果。
- `VERSION.commit` 与 HEAD 差一提交按设计保留；本轮不修改 `VERSION.json`。

## 可复跑命令

本轮最终使用的环境位于 `/tmp/lan-mesh-test-py312`，Node 24 为工作站已有运行时；以下命令不会修改系统 Python 或项目旧 `.venv`：

```bash
export NODE_DIR=/home/zlz/.local/opt/ds-bot-0.3.5/resources/primary-runtime/dependencies/node/bin
export PATH="$NODE_DIR:$PATH"
/tmp/lan-mesh-test-py312/bin/python -m pytest -q
/tmp/lan-mesh-test-py312/bin/python scripts/dev_status.py --verify
/tmp/lan-mesh-test-py312/bin/python scripts/sync_docs.py
```

交叉验证环境为 `/tmp/lan-mesh-test-py311`，同样使用上述 Node 24。若需重建同类环境，应使用 Python 3.11 或更高版本（项目声明 `requires-python = ">=3.11"`），创建新的临时目录后按声明安装，不复用旧 site-packages：

```bash
/home/zlz/.local/opt/ds-bot-0.3.5/resources/primary-runtime/dependencies/python/bin/python3 -m venv /tmp/lan-mesh-test-py312-new
/tmp/lan-mesh-test-py312-new/bin/python -m pip install -r requirements.txt '.[dev]'
```

环境恢复、离线测试通过均不能替代上述四类真实环境验收。
## 2026-11-13 四项优化增量实测

在上述独立Python3.12.14 + Node24环境，新增104项后全量实际 **965 passed / 1 skipped / 7 warnings / 176.20s**。本机双peer七项断言通过（simulated）；Chrome桌面/移动28项API替身行为断言通过；HTTP权限及交付另有真实后端专项。完整交接与日志位置见 [四项优化本地交付记录](optimization-1234-local-handoff.md)。此前861项为增量前基线，不冒充本轮统计。

第二节点zero-mass-foam@192.168.100.30已只读连通，Python3.10.12低于项目要求；未远端部署/启停服务。工具与本机验证完成不等于物理多机、升级回滚、在线同模型对照或硬件断电已验收。

后续维护更新（2026-11-13）：用户已明确授权安装用户目录独立Python3.12；第二节点现已安装并验证Python3.12.14，venv为 `/home/zero-mass-foam/.local/share/lan-mesh/venvs/py312`。系统3.10保留；解释器版本阻塞已解除，项目依赖/双机验收未执行，未启停既有服务。详见 `docs/reference/remote-python312-install.md`。
