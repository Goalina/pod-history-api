# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/zh-CN/1.0.0/).

## [Unreleased]

### Added
- 新增 `source` 字段（`github-action` / `atomgit-action` / `unknown`），在写入时由 `_extract_record` 按 label/annotation 判断填充：
  - `github-action`：Pod 带 `actions-ephemeral-runner: "True"` label（runner pod）或 `runner-pod` label（workflow pod）
  - `atomgit-action`：Pod 带 `octopus.io/job-run-id` annotation（AtomGit/GitCode CI 拉起）
  - `unknown`：来源无法识别的 Pod
- 新增 AtomGit CI pod 的 `extend_env_comments` 提取：直接从 `octopus.io/` 前缀 annotation 读取，包含 `repository`、`organization`、`repository_url`、`ref_name`、`workflow_ref`、`pipeline_id`、`pipeline_run_id`、`job_display_name`、`job_run_id`、`job_external_id`、`runner_set_name`、`project_id`，无需额外 K8s API 调用
- 新增路由 `GET /api/v1/envs/history/atomgit`：固定只返回 `source=atomgit-action` 的记录，参数与原路由完全一致，不影响原有接口行为

### Changed
- `source` 列已加入 DB schema（`ALTER TABLE ... ADD COLUMN IF NOT EXISTS`），存量记录默认值为 `unknown`，服务启动时自动迁移，无需手动执行 SQL
- ARC source 判断从依赖 Pod 名字后缀改为依赖可靠的 label（`actions-ephemeral-runner`、`runner-pod`）

### TODO
- 接口分页（`limit` / `offset`）：当前 7 天查询返回 35 万行、耗时 23s，无分页保护在大时间窗口下会打爆 API server 内存和连接池，需作为下一个 PR 优先处理

---

## [2026-09-16]

### Added
- 新增 wlcb-001 集群 collector（Vault key ascendwlcb001）

---

## [2026-09-09]

### Added
- 新增 gy-001 集群 collector（Vault key ascendGY001）

### Fixed
- collector 命名统一 gy001 → gy-001

---

## [2026-09-01]

### Added
- cn12 侧跨集群补写（`_sync_remote_workflow_pods`，Part B）：Liqo 多集群场景下 runner pod 经虚拟节点 offload 到远端集群后，`-workflow` job pod 落在远端但其 `EphemeralRunner` CR 留在 ARC 源集群（cn12）。cn12 通过 `liqo.io/type=virtual-node` 标签识别虚拟节点、`spec.nodeName` 定位其上的 Running runner pod，本地读 ER 后按 `name=<runner>-workflow` 把工作流信息 UPDATE 到共享 DB 的远端记录；远端 collector 无需任何跨集群查询

### Changed
- `_sync_ephemeral_runners` 合并为单函数两段（Part A 本集群 active/provisioning 记录刷新 + Part B 跨集群推送），共用一次本地 ER list，消除重复列举
- 虚拟节点探测改用 `list_node(label_selector="liqo.io/type=virtual-node")`，只取 vnode 而非全部节点，减小传输
- 抽出 `_er_to_info()` 统一 ER→工作流信息提取逻辑

### Fixed
- upsert 的 `extend_env_comments` 加 `CASE WHEN 新值空 THEN 保留旧值`：远端 collector 重跑 `_extract_record` 时不再冲掉 cn12 已推送的工作流信息；同时修了本地 `-workflow` pod 进终态时若 ER 已删会把已 enrich 的值冲回 `{}` 的问题
- `_sync_remote_workflow_pods` 按 `created_at >= runner 创建时间-1h` 限定记录范围，避免 runner 名复用撞旧终态记录

---

## [2026-08-31]

### Added
- 启动对账 `_reconcile()`：collector 启动或 watcher 410 Gone 后，对账 DB 中 active/provisioning 记录与 K8s 实际状态，把已消失的 pod 标为 expired
- `_watcher_watchdog` 线程：心跳超过 400s 无活动则 `os._exit(1)`，由 K8s 自动重启，防止 watcher 静默卡死

### Changed
- `_reconcile()` 仅在 `_last_resource_version` 为空时执行（进程重启、410 Gone 后），有断点时 K8s 回放保证事件完整性，无需额外对账
- `_reconcile()` 复用 `list_pod_for_all_namespaces()` 结果读取 finishedAt，消除 N 次额外 API 调用，所有 DB 更新合并为单次 `executemany`

### Fixed
- resourceVersion 断点续传：watcher 重连时携带断点，K8s 回放宕机期间漏采事件；410 Gone 自动回退全量重连
- 心跳刷新时机：`_watcher_heartbeat` 仅在 stream 成功建立后更新，确保连接持续失败时 watchdog 能触发
- watcher 断线后指数退避重连（5→10→20→40→60s），stream 正常超时后立即重连

---

## [2026-08-29]

### Fixed
- Bug 1：`_flush_buffer` 数据丢失——`clear()` 移到 commit 成功后执行
- Bug 2：watch loop DB 异常导致整个 watcher 重启——内层 except 改为 log.warning，不再 raise
- Bug 3：watcher 重连后 terminal pod 被重写为 active——else 分支加 `_is_terminal` 守卫
- Bug 4：`_sync_ephemeral_runners` 长时间持有连接——改为先收集再 executemany 一次性提交
- Bug 5：`query_history` flush 异常穿透为 TCP reset——`_flush_buffer()` 加 try/except
- Bug 6：`_rows_to_records` falsy 判断跳过空字符串——改为 `is not None`
- Pending pod 被错误计入 NPU 占用统计：`created_at` 不再 fallback 到 `creation_ts`，overlap 查询加 `created_at != ''` 条件
- 服务重启后 provisioning pod 无法更新 `created_at`：`_load_uids_from_db` 只把 active 加入 `_running_uids`
- expired pod 的 `ttl_seconds`/`duration` 始终为 0

---

## [2026-08-17]

### Added
- 迁移存储至 PostgreSQL，解决多 collector 并发写导致的 SQLite 索引损坏问题
- 新增 `node_ip` 字段（`status.hostIP`）
- 新增 `npu_list` 字段（解析 `huawei.com/AscendReal` 注解）

### Fixed
- PostgreSQL 连接池连接状态管理：写操作异常时 rollback 再归还，SELECT 完成后显式 commit
- psycopg2 LIKE 子句中 `%` 未转义导致 EphemeralRunner 同步持续报错

---

## [2026-08-03]

### Added
- 从 EphemeralRunner CRD 获取 workflow 信息写入 `extend_env_comments`
- `runner-sync` 线程：每 30s 批量同步 EphemeralRunner 信息到运行中 `-workflow` pod 记录
- 多集群 collector/api 架构：collector 写独立存储，api 模式聚合查询

### Changed
- 存储从 ConfigMap 迁移到 SQLite（WAL 模式 + 索引）
- 内存缓冲 + 定时刷盘（5s），查询下推 SQL WHERE，不再全量加载内存过滤

### Fixed
- 运行中 Pod 状态变更时更新数据库（Pending→Active）
- SQLite journal_mode WAL → DELETE，兼容 NFS/SFS Turbo 文件锁

---

## [2026-07-13]

### Added
- 初始版本：Watch K8s Pod 生命周期事件，记录历史到 ConfigMap
- `GET /api/v1/envs/history`，符合 resource-deploy-core 3.8.1 规范
- 支持 `match_mode`（created/released/overlap）、`status`、`name_prefix` 过滤
- 30 天 retention，每日自动清理
