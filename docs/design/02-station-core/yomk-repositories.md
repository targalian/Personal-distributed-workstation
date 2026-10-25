# 持久化聚合拆分（iter-109）

Database 仍是连接策略、schema 版本和迁移函数的唯一所有者。85 个 SQL 操作迁移到
主机、Agent、任务、项目、技能、用户、团队、会话与任务记忆、Checkpoint、资源、
LLM 指标、优化与故障记录十二个普通 repository。Database 原方法名和签名保留为兼容门面。

Repository 仅持有 database 引用；每次操作继续调用 database._get_conn()，不缓存连接、
不引入全局连接，也不为每条 SQL 发送服务总线请求。新业务服务可直接使用明确的聚合接口，
原调用方通过旧门面访问同一实现。Repositories 的首次装配在锁内完成，同一 DB 共享聚合实例，
不同线程依旧取得不同连接。

原始建表脚本移到 database_schema.py，SQL 内容、索引、主键与 SCHEMA_VERSION=12 不变。
ALTER TABLE 的既有迁移流程和异常兼容保持。大段 SQL 保留多行布局和注释，未将 SQL 压成
难以维护的单行字符串。指标 P99 查询拆成独立 helper。

验证保存重构前 sqlite_master 契约，重构后逐项对比表/索引的 SQL；六线程并发通过新旧入口
写入 60 个任务，验证连接隔离、单一 repository 实例以及新旧查询结果一致。
YomkContext 不持久化业务实体，避免出现 DB 与内存两个权威来源。
