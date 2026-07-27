#!/usr/bin/env python3
"""
Pod 历史记录服务 — GET /api/v1/envs/history
符合 resource-deploy-core 3.8.1 规范，支持多集群架构：

  MODE=collector  连目标集群 watch pod，写入管理集群 arc-history ConfigMap
  MODE=api        聚合 arc-history 全部集群数据，提供 HTTP 接口
  MODE=standalone 单集群模式（默认，兼容旧部署）
"""

import json
import logging
import os
import re
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
HISTORY_NS      = "arc-history"
RETENTION_DAYS  = int(os.environ.get("RETENTION_DAYS", "30"))
PORT            = int(os.environ.get("PORT", "8080"))

# 多集群模式环境变量
CLUSTER_ID      = os.environ.get("CLUSTER_ID", "")         # collector 标识，用于 ConfigMap 前缀
KUBECONFIG_PATH = os.environ.get("KUBECONFIG_PATH", "")    # 目标集群 kubeconfig 路径（collector 用）
MODE            = os.environ.get("MODE", "standalone")      # collector | api | standalone

SKIP_NS = {
    "kube-system", "kube-public", "kube-node-lease",
    "arc-history", "arc-systems",
}

PHASE_TO_STATUS = {
    "Pending":   "provisioning",
    "Running":   "active",
    "Succeeded": "released",
    "Failed":    "rejected",
    "Unknown":   "expired",
}
TERMINAL_PHASES = {"Succeeded", "Failed", "Unknown"}
RUNNING_PHASES  = {"Pending", "Running"}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────
# K8s 客户端（双客户端架构）
# _storage_core: 管理集群 in-cluster，用于读写 arc-history ConfigMap
# _watch_core:   目标集群（collector 模式通过 KUBECONFIG_PATH），用于 watch Pod
# ──────────────────────────────────────────────────────────
_storage_core: k8s_client.CoreV1Api = None
_watch_core:   k8s_client.CoreV1Api = None


def _init_k8s():
    global _storage_core, _watch_core

    # 存储客户端：in-cluster 优先（管理集群），否则 KUBECONFIG 文件
    try:
        k8s_config.load_incluster_config()
        log.info("存储客户端: in-cluster ServiceAccount")
    except k8s_config.ConfigException:
        k8s_config.load_kube_config()
        log.info("存储客户端: kubeconfig 文件")
    _storage_core = k8s_client.CoreV1Api()

    # 监听客户端：KUBECONFIG_PATH 指定时独立加载，否则与存储共用
    if KUBECONFIG_PATH:
        watch_cfg = k8s_client.Configuration()
        k8s_config.load_kube_config(
            config_file=KUBECONFIG_PATH,
            client_configuration=watch_cfg,
        )
        _watch_core = k8s_client.CoreV1Api(k8s_client.ApiClient(watch_cfg))
        log.info(f"监听客户端: {KUBECONFIG_PATH}")
    else:
        _watch_core = _storage_core
        log.info("监听客户端: 与存储客户端共用")


_init_k8s()


# ──────────────────────────────────────────────────────────
# 工具函数
# ──────────────────────────────────────────────────────────

def format_duration(seconds: int) -> str:
    if seconds is None or seconds < 0:
        return ""
    d, r = divmod(seconds, 86400)
    h, r = divmod(r, 3600)
    m, s = divmod(r, 60)
    parts = []
    if d: parts.append(f"{d}d")
    if h: parts.append(f"{h}h")
    if m: parts.append(f"{m}m")
    if s or not parts: parts.append(f"{s}s")
    return "".join(parts)


def parse_iso(s: str) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


# ──────────────────────────────────────────────────────────
# ConfigMap 命名（带集群前缀）
# 格式：history-{cluster_id}-YYYY-MM-DD 或 history-YYYY-MM-DD（无前缀兼容旧数据）
# ──────────────────────────────────────────────────────────
_CM_LEGACY_RE  = re.compile(r'^history-(\d{4}-\d{2}-\d{2})$')
_CM_CLUSTER_RE = re.compile(r'^history-(.+)-(\d{4}-\d{2}-\d{2})$')


def _cm_name(date_str: str) -> str:
    if CLUSTER_ID:
        return f"history-{CLUSTER_ID}-{date_str}"
    return f"history-{date_str}"


def _parse_cm_name(name: str):
    """返回 (cluster_id, date_str)，解析失败返回 (None, None)"""
    m = _CM_LEGACY_RE.match(name)
    if m:
        return "", m.group(1)
    m = _CM_CLUSTER_RE.match(name)
    if m:
        return m.group(1), m.group(2)
    return None, None


# ──────────────────────────────────────────────────────────
# UID 去重（只对终态 Pod 标记"已写入"）
# ──────────────────────────────────────────────────────────
_recorded_uids: set = set()
_uid_lock = threading.Lock()


def _load_today_uids():
    date_str = now_utc().strftime("%Y-%m-%d")
    try:
        cm = _storage_core.read_namespaced_config_map(_cm_name(date_str), HISTORY_NS)
        records = json.loads(cm.data.get("records", "[]"))
        terminal = {"released", "rejected", "expired"}
        uids = {r.get("env_id") for r in records
                if r.get("env_id") and r.get("status") in terminal}
        with _uid_lock:
            _recorded_uids.update(uids)
        log.info(f"从今日历史加载 {len(uids)} 条已终态 UID")
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
# ConfigMap 存储（写入管理集群 arc-history，按天+集群分片）
# ──────────────────────────────────────────────────────────
_cm_lock = threading.Lock()


def _ensure_history_ns():
    try:
        _storage_core.read_namespace(HISTORY_NS)
    except ApiException as e:
        if e.status == 404:
            _storage_core.create_namespace(k8s_client.V1Namespace(
                metadata=k8s_client.V1ObjectMeta(name=HISTORY_NS)
            ))
            log.info(f"已创建 namespace: {HISTORY_NS}")


def _upsert_record(record: dict):
    date_str = now_utc().strftime("%Y-%m-%d")
    cm_name  = _cm_name(date_str)
    uid      = record.get("env_id", "")

    with _cm_lock:
        try:
            cm = _storage_core.read_namespaced_config_map(cm_name, HISTORY_NS)
            records = json.loads(cm.data.get("records", "[]"))
            found = False
            for i, r in enumerate(records):
                if r.get("env_id") == uid:
                    records[i] = record
                    found = True
                    break
            if not found:
                records.append(record)
            cm.data["records"] = json.dumps(records, ensure_ascii=False)
            _storage_core.replace_namespaced_config_map(cm_name, HISTORY_NS, cm)
        except ApiException as e:
            if e.status == 404:
                labels = {"arc.local/type": "pod-history", "date": date_str}
                if CLUSTER_ID:
                    labels["cluster"] = CLUSTER_ID
                new_cm = k8s_client.V1ConfigMap(
                    metadata=k8s_client.V1ObjectMeta(
                        name=cm_name,
                        namespace=HISTORY_NS,
                        labels=labels,
                    ),
                    data={"records": json.dumps([record], ensure_ascii=False)},
                )
                _storage_core.create_namespaced_config_map(HISTORY_NS, new_cm)
                log.info(f"创建历史 ConfigMap: {cm_name}")
            else:
                log.error(f"写入历史失败: {e}")


def _list_records_for_cluster(cluster_id: str, days: int) -> list:
    """读取指定集群最近 N 天的记录，同 UID 保留最新版本。"""
    result    = []
    seen_uids = set()
    today     = now_utc()

    for i in range(days):
        date_str = datetime.fromtimestamp(
            today.timestamp() - i * 86400, tz=timezone.utc
        ).strftime("%Y-%m-%d")

        if cluster_id:
            cm_name = f"history-{cluster_id}-{date_str}"
        else:
            cm_name = f"history-{date_str}"

        try:
            cm = _storage_core.read_namespaced_config_map(cm_name, HISTORY_NS)
            records = json.loads(cm.data.get("records", "[]"))
            for r in records:
                uid = r.get("env_id", "")
                if uid and uid in seen_uids:
                    continue
                if uid:
                    seen_uids.add(uid)
                # 补全 cluster 字段
                if "cluster" not in r:
                    r = dict(r)
                    r["cluster"] = cluster_id or "gy006"
                result.append(r)
        except ApiException as e:
            if e.status != 404:
                log.warning(f"读取 {cm_name} 失败: {e}")

    return result


def _list_all_records_api(days: int, cluster_filter: str = None) -> list:
    """
    API 模式：列举 arc-history 下所有 history-* ConfigMap，
    跨集群聚合，可按 cluster 过滤。
    """
    try:
        cms = _storage_core.list_namespaced_config_map(
            HISTORY_NS, label_selector="arc.local/type=pod-history"
        )
    except ApiException as e:
        log.warning(f"列举 ConfigMap 失败: {e}")
        return []

    # 发现所有集群
    clusters_found = set()
    for cm in cms.items:
        c, _ = _parse_cm_name(cm.metadata.name)
        if c is not None:
            clusters_found.add(c)

    if cluster_filter is not None:
        clusters_to_read = {cluster_filter} if cluster_filter in clusters_found else set()
    else:
        clusters_to_read = clusters_found

    result = []
    for c in clusters_to_read:
        result.extend(_list_records_for_cluster(c, days))
    return result


def _list_all_records(days: int = RETENTION_DAYS, cluster_filter: str = None) -> list:
    if MODE == "api":
        return _list_all_records_api(days, cluster_filter)
    return _list_records_for_cluster(CLUSTER_ID, days)


def _cleanup_old_history():
    cutoff = datetime.fromtimestamp(
        now_utc().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        - RETENTION_DAYS * 86400,
        tz=timezone.utc,
    )
    try:
        cms = _storage_core.list_namespaced_config_map(
            HISTORY_NS, label_selector="arc.local/type=pod-history"
        )
        for cm in cms.items:
            _, date_str = _parse_cm_name(cm.metadata.name)
            if not date_str:
                continue
            try:
                if datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc) < cutoff:
                    _storage_core.delete_namespaced_config_map(cm.metadata.name, HISTORY_NS)
                    log.info(f"清理过期历史: {cm.metadata.name}")
            except Exception:
                pass
    except Exception as e:
        log.error(f"清理历史时出错: {e}")


# ──────────────────────────────────────────────────────────
# Pod → 3.8.1 格式记录提取
# ──────────────────────────────────────────────────────────

def _get_exit_code(status) -> int | None:
    for cs in (status.container_statuses or []):
        if cs.state and cs.state.terminated:
            return cs.state.terminated.exit_code
    return None


def _extract_record(pod) -> dict | None:
    meta   = pod.metadata
    spec   = pod.spec
    status = pod.status

    phase = (status.phase or "Unknown") if status else "Unknown"
    if phase not in PHASE_TO_STATUS:
        return None

    env_status  = PHASE_TO_STATUS[phase]
    is_terminal = phase in TERMINAL_PHASES

    creation_ts = meta.creation_timestamp
    start_time  = status.start_time if status else None
    end_time    = None

    if is_terminal:
        for cs in (status.container_statuses or []):
            if cs.state and cs.state.terminated and cs.state.terminated.finished_at:
                end_time = cs.state.terminated.finished_at
                break

    created_at = start_time or creation_ts
    expires_at = (end_time or now_utc()) if is_terminal else None

    ttl_seconds = 0
    if is_terminal and start_time and end_time:
        ttl_seconds = max(0, int((end_time - start_time).total_seconds()))

    wait_duration = ""
    if creation_ts and start_time:
        wait_sec = int((start_time - creation_ts).total_seconds())
        wait_duration = format_duration(wait_sec) if wait_sec > 0 else "0s"

    if is_terminal:
        if ttl_seconds:
            duration = format_duration(ttl_seconds)
        elif start_time and end_time:
            duration = format_duration(max(0, int((end_time - start_time).total_seconds())))
        elif creation_ts and end_time:
            duration = format_duration(max(0, int((end_time - creation_ts).total_seconds())))
        else:
            duration = ""
    else:
        duration = ""

    _NPU_RE = re.compile(r'ascend|npu|gpu', re.IGNORECASE)

    def _parse_npu(reqs):
        for key, val in (reqs or {}).items():
            if _NPU_RE.search(key):
                try:
                    count = int(str(val))
                except (ValueError, TypeError):
                    count = 1 if val else 0
                model = key.split("/")[-1]
                res_type = "gpu" if "gpu" in key.lower() and "ascend" not in key.lower() else "npu"
                return count, model, res_type
        return 0, "", "container"

    devices = []
    for c in (spec.containers or []):
        res = c.resources
        cpu = mem = ""
        reqs = (res.requests if res else None) or (res.limits if res else None)
        if reqs:
            cpu = reqs.get("cpu", "")
            mem = reqs.get("memory", "")
        npu, device_model, res_type = _parse_npu(reqs)
        pool = (spec.node_selector or {}).get("pool", "")
        devices.append({
            "cpu": cpu, "memory": mem, "npu": npu,
            "pool": pool, "res_type": res_type,
            "device_model": device_model, "group": meta.namespace,
        })

    resource_summary = {"total_devices": len(devices), "devices": devices} if devices else {}
    groups = {meta.namespace: {"device_count": len(devices)}}

    return {
        "env_id":              meta.uid,
        "name":                meta.name,
        "cluster":             CLUSTER_ID,
        "status":              env_status,
        "created_at":          created_at.isoformat() if created_at else "",
        "expires_at":          expires_at.isoformat() if expires_at else "",
        "ttl_seconds":         ttl_seconds,
        "duration":            duration,
        "wait_duration":       wait_duration,
        "groups":              groups,
        "extend_env_comments": {},
        "resource_summary":    resource_summary,
        "_namespace":          meta.namespace,
        "_node":               (spec.node_name or ""),
        "_image":              (spec.containers[0].image if spec.containers else ""),
        "_exit_code":          _get_exit_code(status) if is_terminal else None,
    }


# ──────────────────────────────────────────────────────────
# 后台线程 1：Watch Pod 事件（collector / standalone 模式）
# ──────────────────────────────────────────────────────────

def _watch_loop():
    log.info(f"Pod watcher 启动，监听集群 [{CLUSTER_ID or 'local'}] 所有 namespace …")
    while True:
        try:
            w = k8s_watch.Watch()
            for event in w.stream(
                _watch_core.list_pod_for_all_namespaces,
                timeout_seconds=300,
            ):
                etype = event["type"]
                pod   = event["object"]
                ns    = pod.metadata.namespace
                uid   = pod.metadata.uid
                phase = (pod.status.phase or "Unknown") if pod.status else "Unknown"

                if ns in SKIP_NS:
                    continue

                if phase in TERMINAL_PHASES:
                    if etype not in ("MODIFIED", "DELETED"):
                        continue
                    if _is_recorded(uid):
                        continue
                    record = _extract_record(pod)
                    if record:
                        log.info(
                            f"[{record['status']}] {ns}/{record['name']} "
                            f"时长={record['duration']} 节点={record['_node']}"
                        )
                        _upsert_record(record)
                        _mark_recorded(uid)

                elif phase in RUNNING_PHASES:
                    if _is_recorded(uid):
                        continue
                    if etype == "DELETED":
                        record = _extract_record(pod)
                        if record:
                            record["status"]     = "expired"
                            record["expires_at"] = now_utc().isoformat()
                            _upsert_record(record)
                            _mark_recorded(uid)
                    else:
                        record = _extract_record(pod)
                        if record:
                            _upsert_record(record)

        except Exception as e:
            log.error(f"Pod watcher 异常，5s 后重启: {e}")
            time.sleep(5)


# ──────────────────────────────────────────────────────────
# 启动时全量扫描运行中 Pod（collector / standalone 模式）
# ──────────────────────────────────────────────────────────

def _initial_scan():
    log.info(f"初始扫描集群 [{CLUSTER_ID or 'local'}] 运行中 Pod …")
    try:
        pods = _watch_core.list_pod_for_all_namespaces()
        count = 0
        for pod in pods.items:
            if pod.metadata.namespace in SKIP_NS:
                continue
            phase = (pod.status.phase or "Unknown") if pod.status else "Unknown"
            if phase not in RUNNING_PHASES:
                continue
            if _is_recorded(pod.metadata.uid):
                continue
            record = _extract_record(pod)
            if record:
                _upsert_record(record)
                count += 1
        log.info(f"初始扫描完成，写入 {count} 条运行中记录")
    except Exception as e:
        log.error(f"初始扫描失败: {e}")


# ──────────────────────────────────────────────────────────
# 后台线程 2：每天清理过期历史（api / standalone 模式运行）
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
                  status=None,
                  match_mode: str = "created",
                  name_prefix: str = None,
                  cluster_filter: str = None) -> list:
    delta_days = int((now_utc() - start_time).days) + 2
    delta_days = min(delta_days, RETENTION_DAYS)
    all_records = _list_all_records(days=delta_days, cluster_filter=cluster_filter)

    now    = now_utc()
    result = []

    for r in all_records:
        ns = next(iter(r.get("groups", {})), None)
        if ns in SKIP_NS:
            continue
        if status and r.get("status") not in status:
            continue
        if name_prefix and not r.get("name", "").startswith(name_prefix):
            continue

        r_status  = r.get("status", "")
        r_created = parse_iso(r.get("created_at", ""))
        r_expires = parse_iso(r.get("expires_at", "")) if r.get("expires_at") else None

        if r_status in ("active", "provisioning"):
            r = dict(r)
            if r_created:
                elapsed = max(0, int((now - r_created).total_seconds()))
                r["duration"] = format_duration(elapsed)
            r_end = now
        else:
            r_end = r_expires or now

        if match_mode == "created":
            if not r_created or not (start_time <= r_created <= end_time):
                continue
        elif match_mode == "released":
            if not r_expires:
                continue
            if not (start_time <= r_expires <= end_time):
                continue
        elif match_mode == "overlap":
            if not r_created:
                continue
            if r_created > end_time or r_end < start_time:
                continue

        result.append(r)

    result.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return result


# ──────────────────────────────────────────────────────────
# HTTP Handler（api / standalone 模式）
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
        self._ok({"error": str(msg), "count": 0, "envs": []}, status)

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

        if path == "/api/v1/health":
            return self._ok({"status": "ok", "service": "pod-history-api", "mode": MODE})

        if path == "/api/v1/envs/history":
            st_str = params.get("start_time", [None])[0]
            et_str = params.get("end_time",   [None])[0]
            if not st_str or not et_str:
                return self._err("start_time 和 end_time 为必填参数，格式: 2026-06-10T00:00:00Z")

            start_time = parse_iso(st_str)
            end_time   = parse_iso(et_str)
            if not start_time or not end_time:
                return self._err("时间格式不合法，请使用 ISO 8601 格式")
            if start_time > end_time:
                return self._err("start_time 不能晚于 end_time")

            status_filter  = params.get("status",      [])   # 支持多值
            match_mode     = params.get("match_mode",  ["created"])[0]
            name_prefix    = params.get("name_prefix", [None])[0]
            cluster_filter = params.get("cluster",     [None])[0]

            if match_mode not in ("created", "released", "overlap"):
                return self._err("match_mode 取值: created / released / overlap")

            envs = query_history(
                start_time=start_time,
                end_time=end_time,
                status=status_filter,
                match_mode=match_mode,
                name_prefix=name_prefix,
                cluster_filter=cluster_filter,
            )
            clean_envs = [
                {k: v for k, v in e.items() if not k.startswith("_")}
                for e in envs
            ]
            return self._ok({"count": len(clean_envs), "envs": clean_envs})

        self._err("not found", 404)


# ──────────────────────────────────────────────────────────
# 入口
# ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    log.info(f"启动模式: {MODE}, 集群: [{CLUSTER_ID or 'local'}]")
    _ensure_history_ns()

    if MODE in ("collector", "standalone"):
        _load_today_uids()
        _initial_scan()
        threading.Thread(target=_watch_loop, daemon=True, name="watcher").start()

    if MODE in ("api", "standalone"):
        threading.Thread(target=_cleanup_loop, daemon=True, name="cleanup").start()
        log.info(f"HTTP 服务启动，端口 {PORT}")
        server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
        server.serve_forever()
    else:
        # collector 模式：无 HTTP 服务，主线程阻塞等待
        log.info("collector 模式运行中，等待 Pod 事件 …")
        threading.Event().wait()
