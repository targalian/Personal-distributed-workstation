# 只读环境诊断（开发增量，未发布）

`lan_mesh/preflight.py` 的 Python 门槛与 pyproject 对齐为 >=3.11。
`scripts/offline_doctor.py` 仅依赖标准库，可在 Python 3.10 缺依赖时直接诊断。

```bash
/tmp/lan-mesh-test-py312/bin/python scripts/offline_doctor.py --json
PATH=/home/zlz/.local/opt/ds-bot-0.3.5/resources/primary-runtime/dependencies/node/bin:$PATH /tmp/lan-mesh-test-py312/bin/python scripts/offline_doctor.py --profile frontend --isolated-dir /tmp/lanmesh-opt1234 --json
```

退出码 0 表示具备进一步检查的基础条件，1 表示声明读取失败、解释器/依赖/所选前端能力不满足或已声明隔离目录不可用，参数错误为 2。0 不等于部署验收通过。依赖使用 distribution metadata，比较仓库当前数值版本范围；预发布版本标 unknown，保守不通过。不会导入应用依赖或创建配置。Node 仅执行 --version，纯后端不要求 Node。CLI 只报告 PATH 路径与进程环境显式停用列表，不读取 .env，不执行任务或登录，登录状态始终未验证。

VERSION 的 commit 与 HEAD 完全相同标 exact；仅当它是 HEAD 的唯一直接父提交，且该次提交只改 VERSION.json 时标 expected_sync（自动同步形态，不验证提交者身份）。跨多次提交即使净差异只有 VERSION.json 也标 different；不修改发布元数据。隔离目录仅检查是否声明、存在和当前进程写权限，不读取内容或创建目录；这不证明完整运行隔离。工具不绑定端口、不启动服务、不查询网络。

边界：本轮已补双peer协议工具、协作状态增强和离线对照记录工具，详见同域 mesh-acceptance.md、06-interaction/collaboration-status.md 和 10-test-loop/task-benchmark.md；工具完成不代表四项真实环境目标全部验收。真实双机（包括 192.168.100.30）、升级回滚、地址迁移、断电、Station选举、真实模型与IM均未验收。


Python 要求直接读取 pyproject.toml 的 requires-python。为兼容 Python 3.10，无需 tomllib 或额外依赖，仅提取 project 中已知字段并用 AST 字面量解析；不支持的声明格式或读取失败输出 JSON errors 并退出 1。约束必须完整符合数值比较规则；标记、未知运算符、通配符和其他语法均为 unknown。前端要求 Node >=20，未知输出保守失败；backend 可不安装 Node。

environment_runnable 单独表达解释器、依赖及所选 profile 的基础条件。isolation.available 表达声明路径存在、是目录且当前进程具有写权限；不存在、文件、不可写均为 failed，退出 1。未声明为 not_declared，不阻止基础检查。这不是写入实测，也不证明配置、数据和子进程均隔离；任何结果都不是部署 ready。

生产 runtime 支持 codex、claude、aider。CLI_AGENT_BACKEND 经 strip/lower 后优先选择可用且未停用的后端，否则按 codex > claude > aider 自动选择；CLI_AGENT_DISABLED_BACKENDS 按逗号拆分并 strip/lower。doctor 仅报告 PATH 检测与停用值，不执行后端，不等价于生产的路径回退检测或成功运行。生产任务 cwd 优先来自任务指定路径，否则回退 shared_folder，并受允许根目录及自举保护约束；doctor 的 isolated-dir 不会替代 runtime 的 cwd 或配置。CLI 路径存在、版本可读都不能证明登录成功；认证及真实调用始终 unverified。
