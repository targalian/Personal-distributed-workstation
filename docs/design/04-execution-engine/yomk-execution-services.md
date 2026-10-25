# Yomk 执行服务（iter-107）

AgentRuntime 保留取消令牌、线程本地执行信息和旧方法门面，执行、普通技能、ReAct、
CLI、提示词和 LLM 通道分别由六个实例级 YomkService 执行业务。模块级策略与配置仍是
原 agent_runtime 单一来源，保留现有 CLI 检测、环境与 HTTP 测试接缝；纯计算函数不注册。

ToolRegistry 分为目录和执行服务；SkillRegistry 分为目录、授权和分发服务。
ReAct 每次执行持有独立 ToolRegistry，并在成功、失败与取消后关闭临时服务容器。
目录遍历使用快照，避免插件动态注册或子 Agent 变化时中断查询。

独立 Worker 的 Secretary 子进程、PM 和子 Agent 管理分为三个服务。PM slot 在网络查询、
构造和启动前先占用；控制回调在锁外执行；旧 PM 线程退出前不复用 slot。启动失败清理
候选实例；可选 pm_id/X-PM-ID 防止旧任务控制新实例，无身份参数的旧请求仍可用。

Worker API 的执行和生命周期调用通过线程池运行。与 Station 相同，HTTP 仅作外部适配层，
跨机仍使用现有路径、报文和鉴权。执行结果、取消、超时与 CLI 非零退出传播继续沿用原约定。
本轮没有把线程停止等同于强杀外部 CLI 进程，现有 subprocess.run 超时行为保持。

验证增加了并发构造窗口和执行期间并发查询两项行为测试。旧 ReAct 源码字符串断言改为
验证真实工具参数中的 cwd 和文件路径，允许内部结构变化，同时继续检验约束。
