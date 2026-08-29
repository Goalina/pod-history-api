# server.py Bug Review — 2026-08-29

## 🔴 高危

### Bug 1 — `_flush_buffer` 数据丢失（line 301）

`_pending_records.clear()` 在锁内、DB 写入之前执行。任何 DB 异常（网络抖动、连接池耗尽）都会导致当前批次记录**永久丢失**，没有重试路径。`_flush_loop` 只记录 error 日志，下次循环时 buffer 已空。

```python
# 当前（有问题）
with _pending_lock:
    records = list(_pending_records.values())
    _pending_records.clear()   # ← commit 之前就清空

conn = _get_conn()
try:
    ...
    conn.commit()
except Exception:
    _put_conn(conn, error=True)
    raise   # ← records 已丢失，无法重试
```

**修复**：commit 成功后再 clear，或在 except 块中将 records 重新写回 `_pending_records`。

---

### Bug 2 — `_watch_loop` 任意 DB 错误导致整个 watcher 重启（line 577）

更新单个 pod 状态时，内层 `except` 执行 `raise`，异常传播到外层 line 586 的 `except Exception`，触发整个 watch stream 销毁 + 5s 休眠 + 重连。**一次短暂的 DB 抖动会导致 5 秒内所有 pod 事件全部丢失**。

```python
except Exception:
    _put_conn(conn, error=True)
    raise   # ← 传播到外层，watcher 重启
```

**修复**：内层 except 改为 `log.warning(...); continue`，单次状态更新失败不应中断 watch stream。

---

## 🟡 中危

### Bug 3 — watcher 重连后 terminal pod 被重新标为 running（line 580）

watcher 重启会 replay 当前所有 pod 事件。若某 pod 已在 `_terminal_uids` 中，但不在 `_running_uids` 中，会进入 `else` 分支被当作新 pod 再次写入 DB，**覆盖原本正确的 `released`/`rejected` 状态**。

```python
else:
    # ← 缺少 if not _is_terminal(uid) 守卫
    record = _extract_record(pod)
    if record:
        _buffer_record(record)   # status = 'active'/'provisioning'
        _mark_running(uid)
```

**修复**：`else` 分支加 `if not _is_terminal(uid):` 守卫。

---

### Bug 4 — `_sync_ephemeral_runners` 长时间持有连接（line 690）

一个连接从 line 690 持续持有到 line 720 的 commit，中间串行执行所有 workflow pod 的 `UPDATE`。workflow pod 数量多时连接被占用数秒，挤压连接池（max=10），影响 flush、HTTP handler 等其他线程。

**修复**：改用 `executemany` 批量更新，减少连接占用时间。

---

### Bug 5 — `query_history` 未捕获 `_flush_buffer` 异常（line 817）

`_flush_buffer()` 抛出异常时，异常穿透 `query_history` → `do_GET`，被 `ThreadingHTTPServer` 捕获后强制关闭 TCP 连接，**HTTP client 收到 connection reset，无任何 HTTP 响应**。同时触发 Bug 1 的数据丢失。

**修复**：在 `query_history` 中对 `_flush_buffer()` 加 `try/except`，flush 失败只打印 warning，不阻断查询。

---

## ⚪ 低危

### Bug 6 — `_rows_to_records` falsy 判断跳过空字符串（line 798）

`if r.get(json_field)` 对 `""` 为 False，若 DB 中 JSON 字段存了空字符串，API 会直接返回字符串 `""` 而非 `{}`/`[]`，导致 client 解析失败。

**修复**：改为 `if r.get(json_field) is not None`。
