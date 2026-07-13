#!/usr/bin/env python3
"""
Pod 历史记录服务 — GET /api/v1/envs/history
对外提供符合 resource-deploy-core 3.8.1 规范的接口。
数据来源：Watch 集群 Pod 生命周期事件，写入 arc-history ConfigMap。
"""

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from kubernetes import client as k8s_client, config as k8s_config, watch as k8s_watch
from kubernetes.client.rest import ApiException

# ──────────────────────────────────────────────────────────
# 配置
# ──────────────────────────────────────────────────────────
HISTORY_NS     = "arc-history"
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "30"))
PORT           = int(os.environ.get("PORT", "8080"))

# 跳过系统 namespace
SKIP_NS = {
    "kube-system", "kube-public", "kube-node-lease",
    "arc-history",
}

# Pod phase → 3.8.1 env status 映射
PHASE_TO_STATUS = {
    "Succeeded": "released",
    "Failed":    "rejected",
    "Unknown":   "expired",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────
# K8s 客户端
# ──────────────────────────────────────────────────────────
def _init_k8s():
    try:
        k8s_config.load_incluster_config()
        log.info("认证方式: in-cluster ServiceAccount")
    except k8s_config.ConfigException:
        k8s_config.load_kube_config()
        log.info("认证方式: kubeconfig 文件")

_init_k8s()
_core = k8s_client.CoreV1Api()


# ──────────────────────────────────────────────────────────
# 工具函数
# ──────────────────────────────────────────────────────────

def format_duration(seconds: int) -> str:
    """把秒数格式化为 '1d4h15m30s' 风格"""
    if seconds is None or seconds < 0:
        return ""
    d, r  = divmod(seconds, 86400)
    h, r  = divmod(r, 3600)
    m, s  = divmod(r, 60)
    parts = []
    if d: parts.append(f"{d}d")
    if h: parts.append(f"{h}h")
    if m: parts.append(f"{m}m")
    if s or not parts: parts.append(f"{s}s")
    return "".join(parts)


def parse_iso(s: str) -> datetime | None:
    """解析 ISO 8601 时间字符串，返回带时区的 datetime"""
    if not s:
        return None
    try:
        # Python 3.11+ 直接支持；低版本做简单兼容
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


# ──────────────────────────────────────────────────────────
# 去重：记录已写入的 Pod UID
# ──────────────────────────────────────────────────────────
_recorded_uids: set = set()
_uid_lock = threading.Lock()


def _load_today_uids():
    date_str = now_utc().strftime("%Y-%m-%d")
    try:
        cm = _core.read_namespaced_config_map(f"history-{date_str}", HISTORY_NS)
        records = json.loads(cm.data.get("records", "[]"))
        uids = {r.get("env_id") for r in records if r.get("env_id")}
        with _uid_lock:
            _recorded_uids.update(uids)
        log.info(f"从今日历史加载 {len(uids)} 条已记录 UID")
    except ApiException as e:
        if e.status != 404:
            log.warning(f"加载已记录 UID 失败: {e}")


def _is_recorded(uid: str) -> bool:
    with _uid_lock:
        return uid in _recorded_uids


def _mark_recorded(uid: str):
    with _uid_lock:
        _recorded_uids.add(uid)


# ──────────────────────────────────────────────────────────
# ConfigMap 存储（arc-history namespace，按天分片）
# ──────────────────────────────────────────────────────────
_cm_lock = threading.Lock()


def _ensure_history_ns():
    try:
        _core.read_namespace(HISTORY_NS)
    except ApiException as e:
        if e.status == 404:
            _core.create_namespace(k8s_client.V1Namespace(
                metadata=k8s_client.V1ObjectMeta(name=HISTORY_NS)
            ))
            log.info(f"已创建 namespace: {HISTORY_NS}")


def _append_record(record: dict):
    """追加一条记录到今天的 ConfigMap"""
    date_str = now_utc().strftime("%Y-%m-%d")
    cm_name  = f"history-{date_str}"
    with _cm_lock:
        try:
            cm = _core.read_namespaced_config_map(cm_name, HISTORY_NS)
            records = json.loads(cm.data.get("records", "[]"))
            records.append(record)
            cm.data["records"] = json.dumps(records, ensure_ascii=False)
            _core.replace_namespaced_config_map(cm_name, HISTORY_NS, cm)
        except ApiException as e:
            if e.status == 404:
                new_cm = k8s_client.V1ConfigMap(
                    metadata=k8s_client.V1ObjectMeta(
                        name=cm_name,
                        namespace=HISTORY_NS,
                        labels={"arc.local/type": "pod-history", "date": date_str},
                    ),
                    data={"records": json.dumps([record], ensure_ascii=False)},
                )
                _core.create_namespaced_config_map(HISTORY_NS, new_cm)
                log.info(f"创建历史 ConfigMap: {cm_name}")
            else:
                log.error(f"写入历史失败: {e}")


def _list_all_records(days: int = RETENTION_DAYS) -> list:
    """读取最近 N 天的全部记录（原始，不过滤）"""
    result = []
    today = now_utc()
    for i in range(days):
        date_str = (today.replace(hour=0, minute=0, second=0, microsecond=0)
                    .__class__.fromtimestamp(
                        today.timestamp() - i * 86400, tz=timezone.utc
                    ).strftime("%Y-%m-%d"))
        try:
            cm = _core.read_namespaced_config_map(f"history-{date_str}", HISTORY_NS)
            records = json.loads(cm.data.get("records", "[]"))
            result.extend(records)
        except ApiException as e:
            if e.status != 404:
                log.warning(f"读取 {date_str} 历史失败: {e}")
    return result


def _cleanup_old_history():
    from datetime import timedelta
    cutoff = now_utc().replace(hour=0, minute=0, second=0, microsecond=0)
    cutoff = cutoff.__class__.fromtimestamp(
        cutoff.timestamp() - RETENTION_DAYS * 86400, tz=timezone.utc
    )
    try:
        cms = _core.list_namespaced_config_map(
            HISTORY_NS, label_selector="arc.local/type=pod-history"
        )
        for cm in cms.items:
            date_str = (cm.metadata.labels or {}).get("date", "")
            if not date_str:
                continue
            try:
                cm_date = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                if cm_date < cutoff:
                    _core.delete_namespaced_config_map(cm.metadata.name, HISTORY_NS)
                    log.info(f"清理过期历史: {cm.metadata.name}")
            except Exception:
                pass
    except Exception as e:
        log.error(f"清理历史时出错: {e}")


# ──────────────────────────────────────────────────────────
# Pod → 3.8.1 格式记录提取
# ──────────────────────────────────────────────────────────

def _extract_record(pod) -> dict | None:
    """
    将 Pod 对象转换为符合 3.8.1 规范的 env 历史记录。
    只记录 Succeeded / Failed 两种终态。
    """
    meta   = pod.metadata
    spec   = pod.spec
    status = pod.status

    phase = (status.phase or "Unknown") if status else "Unknown"
    if phase not in ("Succeeded", "Failed"):
        return None

    env_status = PHASE_TO_STATUS.get(phase, "expired")

    # ── 时间 ──
    # created_at: Pod 最早出现的时间
    creation_ts = meta.creation_timestamp    # Pod 被 API 接受的时间
    start_time  = status.start_time          # 容器开始运行的时间（过了 pending 之后）
    end_time    = None

    for cs in (status.container_statuses or []):
        if cs.state and cs.state.terminated and cs.state.terminated.finished_at:
            end_time = cs.state.terminated.finished_at
            break

    created_at  = start_time or creation_ts
    expires_at  = end_time or now_utc()

    # ttl_seconds：容器实际运行时长
    ttl_seconds = None
    if start_time and end_time:
        ttl_seconds = max(0, int((end_time - start_time).total_seconds()))

    # wait_duration：pending 等待时长（从 API 接受到容器开始运行）
    wait_duration = ""
    if creation_ts and start_time:
        wait_sec = int((start_time - creation_ts).total_seconds())
        wait_duration = format_duration(wait_sec) if wait_sec > 0 else "0s"

    # duration：格式化存活时长
    duration = format_duration(ttl_seconds) if ttl_seconds is not None else ""

    # ── 资源规格（从 containers requests 提取）──
    devices = []
    for c in (spec.containers or []):
        res = c.resources
        cpu = mem = npu = ""
        if res and res.requests:
            cpu = res.requests.get("cpu", "")
            mem = res.requests.get("memory", "")
            npu = res.requests.get("huawei.com/Ascend910", "0")

        # 从 nodeSelector / labels 推断 pool
        node_sel = spec.node_selector or {}
        pool = node_sel.get("pool", "")

        devices.append({
            "cpu":          cpu,
            "memory":       mem,
            "npu":          int(npu) if npu.isdigit() else 0,
            "pool":         pool,
            "res_type":     "container",
            "device_model": "",
            "group":        meta.namespace,   # 用 namespace 作为 group
        })

    resource_summary = {
        "total_devices": len(devices),
        "devices": devices,
    } if devices else {}

    # ── groups（用 namespace 模拟 nodes_X 分组）──
    groups = {
        meta.namespace: {"device_count": len(devices)}
    }

    return {
        # 3.8.1 规范字段
        "env_id":             meta.uid,
        "name":               meta.name,
        "status":             env_status,
        "created_at":         created_at.isoformat() if created_at else "",
        "expires_at":         expires_at.isoformat() if expires_at else "",
        "ttl_seconds":        ttl_seconds or 0,
        "duration":           duration,
        "wait_duration":      wait_duration,
        "groups":             groups,
        "extend_env_comments": {},
        "resource_summary":   resource_summary,
        # 额外附加（方便排查，不在规范内）
        "_namespace":         meta.namespace,
        "_node":              (spec.node_name or ""),
        "_image":             (spec.containers[0].image if spec.containers else ""),
        "_exit_code":         _get_exit_code(status),
    }


def _get_exit_code(status) -> int | None:
    for cs in (status.container_statuses or []):
        if cs.state and cs.state.terminated:
            return cs.state.terminated.exit_code
    return None


# ──────────────────────────────────────────────────────────
# 后台线程 1：Watch Pod 事件
# ──────────────────────────────────────────────────────────

def _watch_loop():
    log.info("Pod watcher 启动，监听所有 namespace …")
    while True:
        try:
            w = k8s_watch.Watch()
            for event in w.stream(
                _core.list_pod_for_all_namespaces,
                timeout_seconds=300,
            ):
                etype = event["type"]
                pod   = event["object"]
                ns    = pod.metadata.namespace
                uid   = pod.metadata.uid

                if ns in SKIP_NS:
                    continue
                if etype not in ("MODIFIED", "DELETED"):
                    continue
                if _is_recorded(uid):
                    continue

                record = _extract_record(pod)
                if record:
                    log.info(
                        f"[{record['status']}] {ns}/{record['name']} "
                        f"时长={record['duration']} "
                        f"节点={record['_node']}"
                    )
                    _append_record(record)
                    _mark_recorded(uid)

        except Exception as e:
            log.error(f"Pod watcher 异常，5s 后重启: {e}")
            time.sleep(5)


# ──────────────────────────────────────────────────────────
# 后台线程 2：每天清理过期历史
# ──────────────────────────────────────────────────────────

def _cleanup_loop():
    while True:
        time.sleep(86400)
        log.info("开始清理过期历史 …")
        _cleanup_old_history()


# ──────────────────────────────────────────────────────────
# 3.8.1 查询逻辑
# ──────────────────────────────────────────────────────────

def query_history(start_time: datetime, end_time: datetime,
                  status: str = None,
                  match_mode: str = "created",
                  name_prefix: str = None) -> list:
    """
    按 3.8.1 规范查询历史记录。
    match_mode:
      created  — created_at 在 [start_time, end_time] 内（默认）
      released — expires_at 在 [start_time, end_time] 内
      overlap  — 生命周期与 [start_time, end_time] 有交集
    """
    # 计算需要扫描的天数范围
    delta_days = int((now_utc() - start_time).days) + 2
    delta_days = min(delta_days, RETENTION_DAYS)
    all_records = _list_all_records(days=delta_days)

    result = []
    for r in all_records:
        # status 过滤
        if status and r.get("status") != status:
            continue

        # name_prefix 过滤
        if name_prefix and not r.get("name", "").startswith(name_prefix):
            continue

        # 时间过滤
        r_created  = parse_iso(r.get("created_at", ""))
        r_expires  = parse_iso(r.get("expires_at", ""))

        if match_mode == "created":
            if not r_created:
                continue
            if not (start_time <= r_created <= end_time):
                continue

        elif match_mode == "released":
            if not r_expires:
                continue
            if not (start_time <= r_expires <= end_time):
                continue

        elif match_mode == "overlap":
            r_end = r_expires or now_utc()
            if not r_created:
                continue
            # 生命周期 [r_created, r_end] 与 [start_time, end_time] 有交集
            if r_created > end_time or r_end < start_time:
                continue

        result.append(r)

    # 按 created_at 倒序
    result.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return result


# ──────────────────────────────────────────────────────────
# HTTP Handler
# ──────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):

    def _ok(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False, indent=2)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body.encode())

    def _err(self, msg, status=400):
        self._ok({"error": str(msg)}, status)

    def log_message(self, fmt, *args):
        pass

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path   = parsed.path.rstrip("/")
        params = parse_qs(parsed.query)

        # ── 健康检查 ──
        if path == "/api/v1/health":
            return self._ok({"status": "ok", "service": "pod-history-api"})

        # ── 3.8.1 环境历史查询 ──
        if path == "/api/v1/envs/history":
            # start_time / end_time 必填
            st_str = params.get("start_time", [None])[0]
            et_str = params.get("end_time",   [None])[0]
            if not st_str or not et_str:
                return self._err(
                    "start_time 和 end_time 为必填参数，格式: 2026-06-10T00:00:00Z",
                    status=400,
                )

            start_time = parse_iso(st_str)
            end_time   = parse_iso(et_str)
            if not start_time or not end_time:
                return self._err("时间格式不合法，请使用 ISO 8601 格式", status=400)
            if start_time > end_time:
                return self._err("start_time 不能晚于 end_time", status=400)

            status_filter = params.get("status",      [None])[0]
            match_mode    = params.get("match_mode",  ["created"])[0]
            name_prefix   = params.get("name_prefix", [None])[0]

            if match_mode not in ("created", "released", "overlap"):
                return self._err(
                    "match_mode 取值: created / released / overlap", status=400
                )

            envs = query_history(
                start_time=start_time,
                end_time=end_time,
                status=status_filter,
                match_mode=match_mode,
                name_prefix=name_prefix,
            )

            # 返回 3.8.1 规范格式（去掉内部 _ 开头字段）
            clean_envs = [
                {k: v for k, v in e.items() if not k.startswith("_")}
                for e in envs
            ]
            return self._ok({
                "count": len(clean_envs),
                "envs":  clean_envs,
            })

        self._err("not found", 404)


# ──────────────────────────────────────────────────────────
# 入口
# ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    _ensure_history_ns()
    _load_today_uids()
    threading.Thread(target=_watch_loop,   daemon=True, name="watcher").start()
    threading.Thread(target=_cleanup_loop, daemon=True, name="cleanup").start()
    log.info(f"HTTP 服务启动，端口 {PORT}")
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.serve_forever()
