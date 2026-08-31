# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/zh-CN/1.0.0/).

## [Unreleased]

### Fixed
- `_watch_loop` 断线后改为指数退避重连（5→10→20→40→60s），stream 正常超时后立即重连
- 新增 `_watcher_watchdog` 线程：心跳超过 400s 无活动则 `os._exit(1)`，由 K8s 自动重启 pod，防止 watcher 静默卡死导致长时间漏采
- 新增 `_reconcile()`：每次 watcher 重连前执行对账，覆盖宕机/重连/watchdog 重启所有场景，将 K8s 中已消失的 active/provisioning 记录标为 expired（优先取 `containerStatuses[].state.terminated.finishedAt`，无法获取则用 `max(_updated_at, now - watchdog窗口)` 兜底）
- 修复心跳刷新时机：`_watcher_heartbeat` 仅在成功建立 stream 连接后更新，确保连接持续失败时 watchdog 能在 400s 内触发重启（原逻辑在每次循环开始即刷新，导致 watchdog 永远不触发）
- 实现 resourceVersion 断点续传：watcher 记录最后处理的 `resourceVersion`，重连时从断点回放错过的事件（含宕机期间的 DELETED 事件），宕机 < 1h 时可获取 pod 精确终止时间；410 Gone 时自动回退到全量重连
