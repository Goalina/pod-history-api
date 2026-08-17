#!/usr/bin/env python3
"""
Pod 历史记录服务 — GET /api/v1/envs/history
符合 resource-deploy-core 3.8.1 规范，支持多集群架构：

  MODE=collector  连目标集群 watch pod，写入 PostgreSQL
  MODE=api        聚合所有集群数据，提供 HTTP 接口
  MODE=standalone 单集群模式（默认，兼容旧部署）

存储：PostgreSQL（单库多集群，cluster 列区分）
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

import psycopg2
import psycopg2.extras
import psycopg2.pool
from kubernetes import client as k8s_client, config as k8s_config, watch as k8s_watch

# ──────────────────────────────────────────────────────────
# 配置
# ──────────────────────────────────────────────────────────
RETENTION_DAYS  = int(os.environ.get("RETENTION_DAYS", "30"))
PORT            = int(os.environ.get("PORT", "8080"))
FLUSH_INTERVAL  = int(os.environ.get("FLUSH_INTERVAL", "5"))
DATABASE_URL    = os.environ.get("DATABASE_URL", "")

CLUSTER_ID      = os.environ.get("CLUSTER_ID", "")
KUBECONFIG_PATH = os.environ.get("KUBECONFIG_PATH", "")
MODE            = os.environ.get("MODE", "standalone")

RUNNER_SYNC_INTERVAL = int(os.environ.get("RUNNER_SYNC_INTERVAL", "30"))

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
# K8s 客户端
# ──────────────────────────────────────────────────────────
_k8s_core: k8s_client.CoreV1Api = None
_k8s_custom: k8s_client.CustomObjectsApi = None


def _init_k8s():
    global _k8s_core, _k8s_custom
    if KUBECONFIG_PATH:
        cfg = k8s_client.Configuration()
        k8s_config.load_kube_config(
            config_file=KUBECONFIG_PATH,
            client_configuration=cfg,
        )
        api_client = k8s_client.ApiClient(cfg)
        _k8s_core = k8s_client.CoreV1Api(api_client)
        _k8s_custom = k8s_client.CustomObjectsApi(api_client)
        log.info(f"K8s 客户端: {KUBECONFIG_PATH}")
    else:
        try:
            k8s_config.load_incluster_config()
            log.info("K8s 客户端: in-cluster ServiceAccount")
        except k8s_client.ConfigException:
            k8s_config.load_kube_config()
            log.info("K8s 客户端: kubeconfig 文件")
        _k8s_core = k8s_client.CoreV1Api()
        _k8s_custom = k8s_client.CustomObjectsApi()


if MODE in ("collector", "standalone"):
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
# PostgreSQL 连接池
# ──────────────────────────────────────────────────────────

_pool: psycopg2.pool.ThreadedConnectionPool = None

_CREATE_TABLE_SQL = """
    CREATE TABLE IF NOT EXISTS pod_history (
        env_id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        cluster TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL,
        created_at TEXT,
        expires_at TEXT,
        ttl_seconds INTEGER DEFAULT 0,
        duration TEXT DEFAULT '',
        wait_duration TEXT DEFAULT '',
        groups TEXT DEFAULT '{}',
        extend_env_comments TEXT DEFAULT '{}',
        resource_summary TEXT DEFAULT '{}',
        node_ip TEXT DEFAULT '',
        npu_list TEXT DEFAULT '[]',
        _namespace TEXT DEFAULT '',
        _node TEXT DEFAULT '',
        _image TEXT DEFAULT '',
        _exit_code INTEGER,
        _updated_at TEXT NOT NULL
    )
"""

_CREATE_INDEXES_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_status ON pod_history(status)",
    "CREATE INDEX IF NOT EXISTS idx_created_at ON pod_history(created_at)",
    "CREATE INDEX IF NOT EXISTS idx_cluster ON pod_history(cluster)",
    "CREATE INDEX IF NOT EXISTS idx_name ON pod_history(name)",
]

_UPSERT_SQL = """
    INSERT INTO pod_history
        (env_id, name, cluster, status, created_at, expires_at, ttl_seconds, duration,
         wait_duration, groups, extend_env_comments, resource_summary,
         node_ip, npu_list, _namespace, _node, _image, _exit_code, _updated_at)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (env_id) DO UPDATE SET
        name = EXCLUDED.name,
        cluster = EXCLUDED.cluster,
        status = EXCLUDED.status,
        created_at = EXCLUDED.created_at,
        expires_at = EXCLUDED.expires_at,
        ttl_seconds = EXCLUDED.ttl_seconds,
        duration = EXCLUDED.duration,
        wait_duration = EXCLUDED.wait_duration,
        groups = EXCLUDED.groups,
        extend_env_comments = EXCLUDED.extend_env_comments,
        resource_summary = EXCLUDED.resource_summary,
        node_ip = EXCLUDED.node_ip,
        npu_list = EXCLUDED.npu_list,
        _namespace = EXCLUDED._namespace,
        _node = EXCLUDED._node,
        _image = EXCLUDED._image,
        _exit_code = EXCLUDED._exit_code,
        _updated_at = EXCLUDED._updated_at
"""


def _get_conn():
    return _pool.getconn()


def _put_conn(conn, error=False):
    if error:
        try:
            conn.rollback()
        except Exception:
            pass
    _pool.putconn(conn)


def _init_db():
    global _pool
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL 环境变量未设置")
    _pool = psycopg2.pool.ThreadedConnectionPool(2, 10, dsn=DATABASE_URL)
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(_CREATE_TABLE_SQL)
            for idx_sql in _CREATE_INDEXES_SQL:
                cur.execute(idx_sql)
        conn.commit()
    finally:
        _put_conn(conn)
    log.info(f"PostgreSQL 已连接: {DATABASE_URL.split('@')[-1]}")


_init_db()


# ──────────────────────────────────────────────────────────
# UID 去重（运行中 / 终态 分离）
# ──────────────────────────────────────────────────────────
_running_uids: set = set()
_terminal_uids: set = set()
_uid_lock = threading.Lock()


def _load_uids_from_db():
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT env_id, status FROM pod_history WHERE cluster = %s", (CLUSTER_ID,))
            rows = cur.fetchall()
        conn.commit()
    except Exception:
        _put_conn(conn, error=True)
        raise
    else:
        _put_conn(conn)
    with _uid_lock:
        for uid, status in rows:
            if uid:
                if status in ("released", "rejected", "expired"):
                    _terminal_uids.add(uid)
                else:
                    _running_uids.add(uid)
    log.info(f"从数据库加载 {len(_terminal_uids)} 条终态 UID, {len(_running_uids)} 条运行中 UID")


def _is_running(uid: str) -> bool:
    with _uid_lock:
        return uid in _running_uids


def _is_terminal(uid: str) -> bool:
    with _uid_lock:
        return uid in _terminal_uids


def _mark_running(uid: str):
    with _uid_lock:
        _running_uids.add(uid)


def _mark_terminal(uid: str):
    with _uid_lock:
        _running_uids.discard(uid)
        _terminal_uids.add(uid)


# ──────────────────────────────────────────────────────────
# 写入缓冲 + 定时刷盘
# ──────────────────────────────────────────────────────────
_pending_records: dict[str, dict] = {}
_pending_lock = threading.Lock()


def _buffer_record(record: dict):
    uid = record.get("env_id", "")
    if not uid:
        return
    with _pending_lock:
        _pending_records[uid] = record


def _flush_buffer():
    with _pending_lock:
        if not _pending_records:
            return
        records = list(_pending_records.values())
        _pending_records.clear()

    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.executemany(_UPSERT_SQL, [
                (
                    r.get("env_id", ""),
                    r.get("name", ""),
                    r.get("cluster", ""),
                    r.get("status", ""),
                    r.get("created_at", ""),
                    r.get("expires_at", ""),
                    r.get("ttl_seconds", 0),
                    r.get("duration", ""),
                    r.get("wait_duration", ""),
                    json.dumps(r.get("groups", {}), ensure_ascii=False),
                    json.dumps(r.get("extend_env_comments", {}), ensure_ascii=False),
                    json.dumps(r.get("resource_summary", {}), ensure_ascii=False),
                    r.get("node_ip", ""),
                    json.dumps(r.get("npu_list", []), ensure_ascii=False),
                    r.get("_namespace", ""),
                    r.get("_node", ""),
                    r.get("_image", ""),
                    r.get("_exit_code"),
                    now_utc().isoformat(),
                ) for r in records
            ])
        conn.commit()
    except Exception:
        _put_conn(conn, error=True)
        raise
    else:
        _put_conn(conn)
    log.info(f"刷盘 {len(records)} 条记录")


def _flush_loop():
    while True:
        time.sleep(FLUSH_INTERVAL)
        try:
            _flush_buffer()
        except Exception as e:
            log.error(f"刷盘失败: {e}")


# ──────────────────────────────────────────────────────────
# Pod → 3.8.1 格式记录提取
# ──────────────────────────────────────────────────────────

def _get_exit_code(status) -> int | None:
    for cs in (status.container_statuses or []):
        if cs.state and cs.state.terminated:
            return cs.state.terminated.exit_code
    return None


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


def _parse_npu_list(annotations: dict) -> list:
    """从 huawei.com/AscendReal 注解提取 NPU 卡号列表，如 'Ascend910-0 Ascend910-1' → ['0', '1']"""
    val = (annotations or {}).get("huawei.com/AscendReal", "")
    if not val:
        return []
    ids = []
    for part in re.split(r'[,\s]+', val.strip()):
        m = re.search(r'(\d+)$', part)
        if m:
            ids.append(m.group(1))
    return ids


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

    devices = []
    for c in (spec.containers or []):
        res = c.resources
        cpu = mem = ""
        reqs = (res.requests if res else None) or (res.limits if res else None)
        if reqs:
            cpu = reqs.get("cpu", "")
            mem = reqs.get("memory", "")
        npu, device_model, res_type = _parse_npu(reqs)
        pool_label = (spec.node_selector or {}).get("pool", "")
        devices.append({
            "cpu": cpu, "memory": mem, "npu": npu,
            "pool": pool_label, "res_type": res_type,
            "device_model": device_model, "group": meta.namespace,
        })

    resource_summary = {"total_devices": len(devices), "devices": devices} if devices else {}
    groups = {meta.namespace: {"device_count": len(devices)}}

    extend_env_comments = {}
    if meta.name.endswith("-workflow") and _k8s_custom:
        runner_name = meta.name[: -len("-workflow")]
        try:
            runner = _k8s_custom.get_namespaced_custom_object(
                group="actions.github.com",
                version="v1alpha1",
                namespace=meta.namespace,
                plural="ephemeralrunners",
                name=runner_name,
            )
            rs = runner.get("status", {})
            rl = runner.get("metadata", {}).get("labels", {})
            extend_env_comments = {
                "workflow_ref": rs.get("jobWorkflowRef", ""),
                "workflow_run_id": str(rs.get("workflowRunId", "")) if rs.get("workflowRunId") else "",
                "job_display_name": rs.get("jobDisplayName", ""),
                "job_id": rs.get("jobId", ""),
                "job_repository": rs.get("jobRepositoryName", ""),
                "runner_id": str(rs.get("runnerId", "")) if rs.get("runnerId") else "",
                "organization": rl.get("actions.github.com/organization", ""),
                "repository": rl.get("actions.github.com/repository", ""),
            }
            extend_env_comments = {k: v for k, v in extend_env_comments.items() if v}
            if extend_env_comments:
                log.info(f"  EphemeralRunner {runner_name}: job={extend_env_comments.get('job_display_name', '')}")
        except Exception as e:
            log.debug(f"获取 EphemeralRunner {runner_name} 失败: {e}")

    annotations = meta.annotations or {}

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
        "extend_env_comments": extend_env_comments,
        "resource_summary":    resource_summary,
        "node_ip":             (status.host_ip or "") if status else "",
        "npu_list":            _parse_npu_list(annotations),
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
                _k8s_core.list_pod_for_all_namespaces,
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
                    if _is_terminal(uid):
                        continue
                    record = _extract_record(pod)
                    if record:
                        log.info(
                            f"[{record['status']}] {ns}/{record['name']} "
                            f"时长={record['duration']} 节点={record['_node']}"
                        )
                        _buffer_record(record)
                        _mark_terminal(uid)

                elif phase in RUNNING_PHASES:
                    if etype == "DELETED":
                        record = _extract_record(pod)
                        if record:
                            record["status"]     = "expired"
                            record["expires_at"] = now_utc().isoformat()
                            _buffer_record(record)
                            _mark_terminal(uid)
                    elif _is_running(uid):
                        new_status = PHASE_TO_STATUS.get(phase)
                        if new_status:
                            conn = _get_conn()
                            try:
                                with conn.cursor() as cur:
                                    cur.execute(
                                        "SELECT status FROM pod_history WHERE env_id = %s",
                                        (uid,),
                                    )
                                    row = cur.fetchone()
                                if row and row[0] != new_status:
                                    with conn.cursor() as cur:
                                        cur.execute(
                                            "UPDATE pod_history SET status = %s WHERE env_id = %s",
                                            (new_status, uid),
                                        )
                                    conn.commit()
                                    log.info(f"[{new_status}] {ns}/{pod.metadata.name} 状态更新（原 {row[0]}）")
                                else:
                                    conn.commit()
                            except Exception:
                                _put_conn(conn, error=True)
                                raise
                            else:
                                _put_conn(conn)
                    else:
                        record = _extract_record(pod)
                        if record:
                            _buffer_record(record)
                            _mark_running(uid)

        except Exception as e:
            log.error(f"Pod watcher 异常，5s 后重启: {e}")
            time.sleep(5)


# ──────────────────────────────────────────────────────────
# 启动时全量扫描运行中 Pod（collector / standalone 模式）
# ──────────────────────────────────────────────────────────

def _initial_scan():
    log.info(f"初始扫描集群 [{CLUSTER_ID or 'local'}] 运行中 Pod …")
    try:
        pods = _k8s_core.list_pod_for_all_namespaces()
        count = 0
        for pod in pods.items:
            if pod.metadata.namespace in SKIP_NS:
                continue
            phase = (pod.status.phase or "Unknown") if pod.status else "Unknown"
            if phase not in RUNNING_PHASES:
                continue
            if _is_running(pod.metadata.uid) or _is_terminal(pod.metadata.uid):
                continue
            record = _extract_record(pod)
            if record:
                _buffer_record(record)
                _mark_running(pod.metadata.uid)
                count += 1
        log.info(f"初始扫描完成，缓冲 {count} 条运行中记录")
    except Exception as e:
        log.error(f"初始扫描失败: {e}")


# ──────────────────────────────────────────────────────────
# 后台线程 2：每天清理过期历史
# ──────────────────────────────────────────────────────────

def _cleanup_old_history():
    cutoff = now_utc().replace(hour=0, minute=0, second=0, microsecond=0)
    cutoff = datetime.fromtimestamp(
        cutoff.timestamp() - RETENTION_DAYS * 86400,
        tz=timezone.utc,
    )
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM pod_history WHERE created_at < %s",
                (cutoff.isoformat(),),
            )
            deleted = cur.rowcount
        conn.commit()
        if deleted:
            log.info(f"清理历史: 删除 {deleted} 条 {cutoff.isoformat()} 之前记录")
    except Exception as e:
        log.error(f"清理历史失败: {e}")
        _put_conn(conn, error=True)
        return
    _put_conn(conn)


def _cleanup_loop():
    while True:
        time.sleep(86400)
        log.info("开始清理过期历史 …")
        try:
            _cleanup_old_history()
        except Exception as e:
            log.error(f"清理历史时出错: {e}")


def _sync_ephemeral_runners():
    if not _k8s_custom:
        return
    try:
        runners = _k8s_custom.list_cluster_custom_object(
            group="actions.github.com",
            version="v1alpha1",
            plural="ephemeralrunners",
        )
    except Exception as e:
        log.error(f"列举 EphemeralRunner 失败: {e}")
        return

    runner_map = {}
    for item in runners.get("items", []):
        name = item.get("metadata", {}).get("name", "")
        ns = item.get("metadata", {}).get("namespace", "")
        if not name:
            continue
        rs = item.get("status", {})
        rl = item.get("metadata", {}).get("labels", {})
        info = {
            "workflow_ref": rs.get("jobWorkflowRef", ""),
            "workflow_run_id": str(rs.get("workflowRunId", "")) if rs.get("workflowRunId") else "",
            "job_display_name": rs.get("jobDisplayName", ""),
            "job_id": rs.get("jobId", ""),
            "job_repository": rs.get("jobRepositoryName", ""),
            "runner_id": str(rs.get("runnerId", "")) if rs.get("runnerId") else "",
            "organization": rl.get("actions.github.com/organization", ""),
            "repository": rl.get("actions.github.com/repository", ""),
        }
        info = {k: v for k, v in info.items() if v}
        runner_map[(ns, name)] = info

    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT env_id, name, _namespace, extend_env_comments FROM pod_history "
                "WHERE status IN ('active', 'provisioning') AND name LIKE '%%-workflow' AND cluster = %s",
                (CLUSTER_ID,),
            )
            rows = cur.fetchall()

        updated = 0
        for uid, pod_name, ns, comments_json in rows:
            runner_name = pod_name[: -len("-workflow")]
            info = runner_map.get((ns, runner_name))
            if not info:
                continue
            old_comments = {}
            try:
                old_comments = json.loads(comments_json or "{}")
            except Exception:
                pass
            if info == old_comments:
                continue
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE pod_history SET extend_env_comments = %s WHERE env_id = %s",
                    (json.dumps(info, ensure_ascii=False), uid),
                )
            updated += 1

        conn.commit()
        if updated:
            log.info(f"EphemeralRunner 同步: 更新 {updated} 条 workflow Pod 信息")
    except Exception:
        _put_conn(conn, error=True)
        raise
    else:
        _put_conn(conn)


def _runner_sync_loop():
    while True:
        time.sleep(RUNNER_SYNC_INTERVAL)
        try:
            _sync_ephemeral_runners()
        except Exception as e:
            log.error(f"EphemeralRunner 同步失败: {e}")


# ──────────────────────────────────────────────────────────
# 3.8.1 查询逻辑（SQL）
# ──────────────────────────────────────────────────────────

def _build_query(start_time: datetime, end_time: datetime,
                 status=None, match_mode: str = "created",
                 name_prefix: str = None, cluster_filter: str = None) -> tuple[str, list]:
    conditions = []
    params = []

    ns_placeholders = ",".join("%s" for _ in SKIP_NS)
    conditions.append(f"_namespace NOT IN ({ns_placeholders})")
    params.extend(SKIP_NS)

    if match_mode == "created":
        conditions.append("created_at >= %s")
        params.append(start_time.isoformat())
        conditions.append("created_at <= %s")
        params.append(end_time.isoformat())
    elif match_mode == "released":
        conditions.append("expires_at >= %s")
        params.append(start_time.isoformat())
        conditions.append("expires_at <= %s")
        params.append(end_time.isoformat())
        conditions.append("expires_at IS NOT NULL")
        conditions.append("expires_at != ''")
    elif match_mode == "overlap":
        conditions.append("created_at <= %s")
        params.append(end_time.isoformat())
        conditions.append("(expires_at IS NULL OR expires_at = '' OR expires_at >= %s)")
        params.append(start_time.isoformat())

    if status:
        placeholders = ",".join("%s" for _ in status)
        conditions.append(f"status IN ({placeholders})")
        params.extend(status)

    if name_prefix:
        conditions.append("name LIKE %s")
        params.append(f"{name_prefix}%")

    if cluster_filter:
        conditions.append("cluster = %s")
        params.append(cluster_filter)

    where = " AND ".join(conditions)
    sql = f"SELECT * FROM pod_history WHERE {where} ORDER BY created_at DESC"
    return sql, params


_JSON_FIELDS = ("groups", "extend_env_comments", "resource_summary", "npu_list")


def _rows_to_records(rows, col_names) -> list:
    now = now_utc()
    result = []
    for row in rows:
        r = dict(zip(col_names, row))
        for json_field in _JSON_FIELDS:
            if r.get(json_field):
                try:
                    r[json_field] = json.loads(r[json_field])
                except (json.JSONDecodeError, TypeError):
                    r[json_field] = [] if json_field == "npu_list" else {}
        if r.get("status") in ("active", "provisioning") and r.get("created_at"):
            r_created = parse_iso(r["created_at"])
            if r_created:
                elapsed = max(0, int((now - r_created).total_seconds()))
                r["duration"] = format_duration(elapsed)
        result.append(r)
    return result


def query_history(start_time: datetime, end_time: datetime,
                  status=None,
                  match_mode: str = "created",
                  name_prefix: str = None,
                  cluster_filter: str = None) -> list:
    if MODE in ("collector", "standalone"):
        _flush_buffer()

    sql, params = _build_query(start_time, end_time, status, match_mode, name_prefix, cluster_filter)

    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
            col_names = [d[0] for d in cur.description]
        conn.commit()
    except Exception:
        _put_conn(conn, error=True)
        raise
    else:
        _put_conn(conn)

    return _rows_to_records(rows, col_names)


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

            status_filter  = params.get("status",      [])
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

    if MODE in ("collector", "standalone"):
        _load_uids_from_db()
        _initial_scan()
        threading.Thread(target=_watch_loop, daemon=True, name="watcher").start()
        threading.Thread(target=_flush_loop, daemon=True, name="flush").start()
        threading.Thread(target=_runner_sync_loop, daemon=True, name="runner-sync").start()

    if MODE in ("api", "standalone"):
        threading.Thread(target=_cleanup_loop, daemon=True, name="cleanup").start()
        log.info(f"HTTP 服务启动，端口 {PORT}")
        server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
        server.serve_forever()
    else:
        threading.Thread(target=_cleanup_loop, daemon=True, name="cleanup").start()
        log.info("collector 模式运行中，等待 Pod 事件 …")
        threading.Event().wait()
