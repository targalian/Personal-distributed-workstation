# 2026-09-05 真项目 Secretary 联测摘要

## 结论

使用股票自动交易系统项目描述完成一次端到端真实开发链路测试：需求收集 → Brief → 确认 → 任务入库 → PM 派发 → Agent Runtime 审计目标仓库。链路能跑通，目标仓库未被写入，但暴露多个真实开发场景问题。

## 关键对象

- 对话：`conv-0800df3a38`
- 任务：`task-775bbc2e4dad`
- PM：`pm-5a3b3b127a68`
- 目标项目：`E:\ingobj\stock_player`
- 详细报告：`test_bug/reports/2026-09-05-real-project-secretary-test.md`

## 下一轮修复优先级

1. `/api/secretary/chat` 后台化，避免同步 LLM 调用阻塞事件循环。
2. 需求收集提取并传递 `project_path` / `repo_url`。
3. 本机分发使用 `127.0.0.1`，过滤 `169.254.0.0/16`。
4. 子任务 start/finish 写入任务面板与 runtime trace。
5. 补齐「高」优先级映射。
6. 工具调用增加超时、取消传播和进度上报。
7. 任务取消后清理 `local_pm` / `active_pms` 健康统计。
