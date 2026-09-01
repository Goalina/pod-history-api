# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/zh-CN/1.0.0/).

## [Unreleased]

### Added
- 新增 cn12 侧跨集群补写（`_sync_remote_workflow_pods`，Part B）：Liqo 多集群场景下 runner pod 经虚拟节点 offload 到远端集群后，`-workflow` job pod 落在远端但其 `EphemeralRunner` CR 留在 ARC 源集群（cn12）。cn12 通过 `liqo.io/type=virtual-node` 标签识别虚拟节点、`spec.nodeName` 定位其上的 Running runner pod，本地读 ER 后按 `name=<runner>-workflow` 把工作流信息 UPDATE 到共享 DB 的远端记录；远端 collector 无需任何跨集群查询

### Changed
- `_sync_ephemeral_runners` 合并为单函数两段（Part A 本集群 active/provisioning 记录刷新 + Part B 跨集群推送），共用一次本地 ER list，消除重复列举
- 虚拟节点探测改用 `list_node(label_selector="liqo.io/type=virtual-node")`，只取 vnode 而非全部节点，减小传输
- 抽出 `_er_to_info()` 统一 ER→工作流信息提取逻辑

### Fixed
- upsert 的 `extend_env_comments` 加 `CASE WHEN 新值空 THEN 保留旧值`：远端 collector 重跑 `_extract_record`（跨集群 pod 本地查不到 ER、产出 `{}`）时不再冲掉 cn12 已推送的工作流信息；同时修了既存问题——本地 `-workflow` pod 进终态时若 ER 已删，原逻辑会把已 enrich 的值冲回 `{}`
- `_sync_remote_workflow_pods` 按 `created_at >= runner 创建时间-1h`（或 Pending 空值）限定记录范围，避免 runner 名复用撞旧终态记录（实测 0 误排除）
- `_watch_loop` 断线后改为指数退避重连（5→10→20→40→60s），stream 正常超时后立即重连
- 新增 `_watcher_watchdog` 线程：心跳超过 400s 无活动则 `os._exit(1)`，由 K8s 自动重启 pod，防止 watcher 静默卡死导致长时间漏采
- 新增 `_reconcile()`：仅在 `_last_resource_version` 为空时执行对账（进程重启、410 Gone 后），有断点时 K8s 通过 resourceVersion 回放所有漏采事件，无需额外对账，避免每 300s 多一次全量 LIST 压力
- 修复心跳刷新时机：`_watcher_heartbeat` 仅在成功建立 stream 连接后更新，确保连接持续失败时 watchdog 能在 400s 内触发重启（原逻辑在每次循环开始即刷新，导致 watchdog 永远不触发）
- 实现 resourceVersion 断点续传：watcher 记录最后处理的 `resourceVersion`，重连时从断点回放错过的事件（含宕机期间的 DELETED 事件），宕机 < 1h 时可获取 pod 精确终止时间；410 Gone 时自动回退到全量重连
- 优化 `_reconcile()` 性能：复用 `list_pod_for_all_namespaces()` 结果读取 finishedAt（消除每个僵尸记录一次额外 `read_namespaced_pod()` API 调用），并将所有 DB 更新合并为单次 `executemany` + 一次 `commit`（消除每条记录单独提交的开销）；同时修正 `live_uids` 仅含 Running/Pending 状态的 Pod，使 Succeeded/Failed 状态的 Pod 也能被正确对账
