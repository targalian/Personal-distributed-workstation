# 2026-09-05 真项目开发联测报告

## 测试输入

- 项目描述来源：`C:\Users\WindowShopper\Desktop\项目描述.txt`
- 目标项目：`E:\ingobj\stock_player`
- 远端仓库：`https://gitee.com/zhu-longzhuo/shrimp-quantification`
- Station：`http://127.0.0.1:45470`
- 对话：`conv-0800df3a38`
- 任务：`task-775bbc2e4dad`
- PM：`pm-5a3b3b127a68`

## 实测结论

1. Secretary 成功进入需求收集流程；补充技术栈、优先级和验收标准后生成 Brief，确认后创建任务并分配 PM。
2. PM 生成了 3 个子任务，Agent Runtime 实际读取了 `E:\ingobj\stock_player` 的 README、策略、回测、执行器和数据库结构。
3. 测试期间目标仓库没有产生文件写入，符合本轮「只做规划与任务拆解，不直接修改业务代码」的安全边界。
4. 因子任务长时间无阶段事件且运行日志停止更新，任务已手动取消；取消接口返回 `ok:true`。

## 发现的问题

### P1：秘书聊天同步阻塞事件循环

- 现象：两次真实需求消息均超过 10 秒，客户端超时；随后认证端点和其他 API 也超时。
- 服务端最终完成处理并写入历史，但 HTTP 响应丢失。
- 根因方向：`/api/secretary/chat` 在 async 路由内同步调用 `chat_handler.chat()`，LLM/需求提取阻塞事件循环。

### P1：项目路径未结构化传递

- 现象：PM 子任务描述显示「分析项目 .」「在 . 中实现功能代码」。
- 根因方向：需求收集派发只写入 `input_data.requirement`，未提取 `project_path`；`PMPlanner` 模板变量默认 `.`。
- Agent Runtime 通过自然语言自行找回了正确路径，但规划层已丢失关键结构化上下文。

### P1：本机注册地址不可达导致远程分发失败

- 现象：主机表中本机 Secretary 地址为 `169.254.154.255:45470`，实际无法连接。
- PM 尝试远程创建子 Agent 后回退本地执行，浪费调度时间且团队结构为空。
- 根因方向：本机分发未优先使用 `127.0.0.1`，也未过滤链路本地地址。

### P1：子任务执行缺少可观测阶段事件

- 现象：Agent Runtime 已执行 20+ 轮工具调用，但任务面板 3 个子任务始终显示 `pending`，任务流停留在 `pm:executing`。
- 取消后任务状态为 `cancelled`，但子任务仍全部 `pending`。

### P2：需求优先级映射丢失

- 现象：Brief 中优先级为「高」，任务 `input_data._priority` 却为 `normal`。
- 根因方向：`_dispatch_from_draft()` 的高优先级关键词只匹配「优先/重要/high」，未匹配「高」。

### P2：通用开发模板违背本轮范围

- 现象：验收标准明确「只做规划与任务拆解」，但 PM 仍规划「代码实现」和「单元测试」。
- 根因方向：任务模板按开发类任务固定生成三段式，未消费「planning-only / audit-only」约束。

### P2：外部仓库 Git safe.directory 拦截

- 现象：Agent 多次尝试 `git status` / `git branch` 均因 `unsafe repository` 失败，并反复重试。
- 项目约定禁止修改 git config，因此应在运行时明确识别该错误并降级为只读文件扫描，不应继续重试。

### P2：取消后健康状态残留

- 现象：任务取消后 `/health` 仍显示 `local_pm=active`、`active_pms=1`，但 `active_tasks=0`。
- PM 详情已变为 `cancelled`，健康统计与实际状态不一致。

### P2：长工具调用缺少超时

- 现象：`run_code` 单轮调用可超过 40 秒，后续日志约 2 分钟无更新。
- 需要为工具执行增加独立超时、取消传播和进度上报。

## 建议修复顺序

1. `/api/secretary/chat` 改为后台任务或线程池执行，先返回可轮询任务 ID。
2. 需求收集提取并传递 `project_path`、`repo_url` 到 `input_data`。
3. 本机分发强制使用 `127.0.0.1`，主机选择过滤 `169.254.0.0/16`。
4. PM 子任务 start/finish 写入任务面板与 runtime trace。
5. 补齐「高」优先级映射。
6. 工具调用增加超时和取消传播。
7. 任务取消同步清理 local PM 健康状态。
