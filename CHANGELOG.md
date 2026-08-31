# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/zh-CN/1.0.0/).

## [Unreleased]

### Fixed
- `_watch_loop` 断线后改为指数退避重连（5→10→20→40→60s），stream 正常超时后立即重连
- 新增 `_watcher_watchdog` 线程：心跳超过 400s 无活动则 `os._exit(1)`，由 K8s 自动重启 pod，防止 watcher 静默卡死导致长时间漏采
- 新增 `_startup_reconcile()`：collector 启动时对账 DB 与 K8s 实际状态，将 K8s 中已消失的 active/provisioning 记录标为 expired（优先取 `containerStatuses[].state.terminated.finishedAt`，无法获取则用 `_updated_at + 30min` 兜底），解决采集器宕机期间漏采 DELETED 事件导致的僵尸记录问题
