# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/zh-CN/1.0.0/).

## [Unreleased]

### Added
- 新增集群卡数快照与查询接口：
  - collector/standalone 新增 `_capacity_loop` 线程，每 `CAPACITY_INTERVAL`（默认 300s，`snapshot_time` 对齐整 5 分钟）采集本集群各节点加速卡 `allocatable`（总卡）与已用卡，写入新表 `cluster_card_snapshot`（**一集群一行**）：`total_cards`、`used_cards`、`node_count`、`nodes`（JSON 数组，含 `node_ip`/`resource_name`/`service_type`/`cards`/`used`）
  - `used`（已用）= 按 `(node, 资源键)` 汇总未终止（非 Succeeded/Failed）且已绑定节点的 Pod 的 `resources.requests` 加速卡数，含所有 namespace；`available = total - used`
  - `service_type` 取节点 label `servertype`；资源键匹配 `ascend|npu|gpu`，跳过值为 0 的键与无加速卡的节点；`node_ip` 取 `InternalIP`
  - 采集失败写一条 `status='failed'`（`total_cards=0`、`nodes=[]`、`error` 记原因），如实记录不伪造成 0
  - 新增接口 `GET /api/v1/clusters/capacity[?cluster=..]`：**不带时间时返回各集群最近一次记录**（看最新直接省略时间参数）；**带 `start_time`/`end_time` 时返回该区间内全部快照**（两者需同时提供）。每条含 `total_cards`/`used_cards`/`available_cards`、`node_cards`（`node_ip -> 总卡`）与 `node_used`（`node_ip -> 已用`）扁平映射，以及 `nodes` 明细（含 per-node `cards`/`used`）
  - 保留期沿用 30 天（并入现有每日清理）
- 多机 CI worker pod 自动关联到拉起它的 job pod：对 LWS（`leaderworkerset.sigs.k8s.io/name` label）和 Volcano Job（`batch.volcano.sh` ownerRef）等由 K8s 控制器创建的 worker pod，由 `_sync_worker_pods` 事后在 DB 中关联到同 cluster 有工作流信息的 job pod，继承其 `extend_env_comments`（PR / workflow_run_id 等）与 `source`；不修改任何 CI/业务代码
  - **路径 A1 — run-id label 精确匹配**（sglang 多机 Volcano）：worker pod 带 `run-id` label（= `github.run_id`，sglang 模板 `k8s_multi_pd_*.yaml.jinja2` 注入），与 job 的 `extend_env_comments.workflow_run_id` 精确相等即命中，确定性最高
  - **路径 A2 — BENCHMARK_JOB_NAME 精确匹配**（vllm-ascend LWS）：从 pod env 读 `BENCHMARK_JOB_NAME` 原始值，用 job 的 `job_display_name` 括号内 `(branch, matrix_name)` 重建 `"{branch}-{matrix_name}"` 精确比较（vllm-ascend 专用格式）；兼容任意分支名（含 `-`），且避免"某个 matrix 是另一个后缀"的碰撞；实测 gy-005 集群 4/4 命中，零误配
  - **路径 B — token 匹配 fallback**（前两路不适用时）：从 pod name/command/env 提取归一化 token，只计在候选 job 中唯一出现（df==1）的 token 计分，唯一最高分（≥5）才写入
  - 路径 A1/A2 精确命中多个时（同一 config 在窗口内重复跑），取 worker 之前最近创建的 job（latest-preceding），而非直接放弃；创建时间并列才保持 `unknown`
  - 时间窗口：job 先于 worker 创建（5min 余量），差值不超过 24h
  - 启动时 `_initial_scan` 对存量运行中的 worker pod 重新提取一次，回填 `_worker_kind`/`_worker_tokens`/`_worker_run_id`，使部署前已存在的 worker 也能被关联
  - **当前覆盖范围**：github-action 来源的 job pod（vllm-ascend / sglang）；atomgit-action 多机场景生产暂未出现，若出现走路径 B，届时需验证
- 新增内部列 `_worker_kind`（`lws` / `volcano`）、`_worker_tokens`（LWS 存 `BENCHMARK_JOB_NAME` 原始值；Volcano 等存 token 串）与 `_worker_run_id`（worker pod 的 `run-id` label），随建表/迁移自动创建，不对外暴露
- 新增 `source` 字段（`github-action` / `atomgit-action` / `unknown`），在写入时由 `_extract_record` 按 label/annotation 判断填充：
  - `github-action`：Pod 带 `actions-ephemeral-runner: "True"` label（runner pod）或 `runner-pod` label（workflow pod）
  - `atomgit-action`：Pod 带 `octopus.io/job-run-id` annotation（AtomGit/GitCode CI 拉起）
  - `unknown`：来源无法识别的 Pod
- 新增 AtomGit CI pod 的 `extend_env_comments` 提取：直接从 `octopus.io/` 前缀 annotation 读取 9 个字段（`repository`、`organization`、`repository_url`、`workflow_ref`、`pipeline_id`、`pipeline_run_id`、`job_display_name`、`job_external_id`、`project_id`），无需额外 K8s API 调用
- 新增路由 `GET /api/v1/envs/history/atomgit`：固定只返回 `source=atomgit-action` 的记录，参数与原路由完全一致，不影响原有接口行为
- `/atomgit` 路由支持分页：`limit`（默认 1000，范围 1~5000）+ `offset`（默认 0），响应新增 `total`（满足条件的总条数）、`limit`、`offset` 字段。主路由不分页，行为保持不变

### Changed
- 集群卡数采集改用原始 JSON（`_preload_content=False` + `json.loads`），跳过 kubernetes 客户端逐对象模型反序列化：实测 741 Pod / 8.1MB 的 `list_pod_for_all_namespaces` 从 **~30s（默认反序列化）降到 ~10s**，整轮采集（含 `list_node`）从 30s+ 降到 ~8s，显著减少 CPU 占用与对同进程 watcher/flush 的 GIL 挤压
- upsert 的 `source` 更新改为：新值为 `unknown` 且旧值非 `unknown` 时保留旧值。避免多机 worker pod 被关联后，进入终态被 watcher 重新提取（`source=unknown`）时把已继承的 `source` 冲掉
- `source` 列已加入 DB schema（`ALTER TABLE ... ADD COLUMN IF NOT EXISTS`），存量记录默认值为 `unknown`，服务启动时自动迁移，无需手动执行 SQL
- ARC source 判断从依赖 Pod 名字后缀改为依赖可靠的 label（`actions-ephemeral-runner`、`runner-pod`）
- `_build_query` 拆分为 `_build_where`（只出 WHERE 子句），SELECT/ORDER/LIMIT 由 `query_history` 拼接，以支持分页与 COUNT 复用同一套过滤条件
- `_init_db` 迁移流程加固（免人工预热、多副本并发安全、建索引不停写）：
  - DDL 改用独立 `autocommit` 连接：`CREATE INDEX CONCURRENTLY` 不能在事务块内执行，且每条语句独立提交、单条失败不连累其它语句（原来所有 DDL 在单事务内，任一句失败会全部回滚并导致进程退出）
  - 索引统一由 `_ensure_indexes` 以 `CREATE INDEX CONCURRENTLY` 构建（索引定义收敛为 `_INDEXES` 列表），建索引期间不阻塞读写；索引失败仅告警、不影响服务启动（表结构/加列失败仍致命）
  - 多副本并发启动用 `pg_try_advisory_lock` 轮询串行化迁移：阻塞式 `pg_advisory_lock` 的等待事务会持有快照，与持锁进程的 `CREATE INDEX CONCURRENTLY` 互相等待形成死锁（实测可复现）
  - 检测 `indisvalid=false` 的无效索引（并发构建失败遗留、`IF NOT EXISTS` 永不重建）并自动 `DROP INDEX CONCURRENTLY` 后重建
  - 拿不到迁移锁时校验 `pod_history` 必需列是否齐全：缺列则快速失败重启重试，避免带缺列对外服务；迁移在 autocommit 下逐条提交、非原子，要求所有迁移语句幂等

### Fixed
- 修复集群卡数快照桶漂移：`_capacity_loop` 原为「采集后 `sleep(CAPACITY_INTERVAL)`」，而采集本身耗时（`list nodes` + `list pods`，实测约 40s）会叠加到周期上，导致每轮相位漂移、约每 7~8 轮**跳过整个 bucket**（实测 gy-001/hk-001 丢了 09:00 桶）。改为睡到下一个整 bucket 边界（`_next_capacity_sleep`），保证每轮恰好前进一个 bucket
- 修复迁移在繁忙大表上 crashloop：`ALTER TABLE ADD COLUMN IF NOT EXISTS` 即使列已存在也要先拿 `ACCESS EXCLUSIVE` 锁，之前设的 `lock_timeout=15s` 在生产 121 万行表上拿不到锁即超时失败（致命）→ 新 pod 反复重启。改为先 `_schema_ready` 校验，列已齐全则完全跳过 DDL（不再加锁），仅缺列时才执行且不设 `lock_timeout`（等待而非超时崩溃）
- 补充 `idx_expires_at` 索引：`match_mode=released` 按 `expires_at` 范围过滤，但 `_CREATE_INDEXES_SQL` 只有 `status`/`created_at`/`cluster`/`name`/`source`，缺 `expires_at` 索引，导致全表 Seq Scan；默认 `created` 路径命中 `idx_created_at`，故去掉 `match_mode` 反而更快。50 万行实测：`released` 53.6ms（Parallel Seq Scan）→ 0.118ms（`Index Scan using idx_expires_at`），与 `created` 对齐
- `_init_db` 迁移顺序：`ALTER TABLE ADD COLUMN source` 必须在 `CREATE INDEX idx_source` 之前执行。原顺序在存量表（`CREATE TABLE IF NOT EXISTS` 跳过）上会因 `source` 列不存在导致建索引崩溃、服务无法启动
- `_MIGRATE_SQL` 中 `source` 默认值 `'plain'` 更正为 `'unknown'`，与建表 SQL 一致；去掉其中重复的 `idx_source` 建索引（统一由 `_ensure_indexes` 负责）

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
