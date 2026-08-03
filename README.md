# pod-history-api

K8s 集群 Pod 历史记录服务，对外提供符合 [resource-deploy-core](https://gitcode.com/openlibing/resource-deploy-core) **3.8.1 规范**的 `GET /api/v1/envs/history` 接口。

适用于集群内 Pod **不由 resource-deploy-core 管理**的场景，通过 Watch K8s Pod 事件自动记录历史，无需部署 resource-deploy-core 全套系统。

---

## 工作原理

```
┌─────────────────────────────────────────────────────┐
│                  管理集群 (gy-006)                    │
│                                                      │
│  ┌──────────────────┐   ┌─────────────────────────┐ │
│  │ collector-gy006  │   │ collector-gy003         │ │
│  │ (watch 本集群)    │   │ (KUBECONFIG → gy003)    │ │
│  └────────┬─────────┘   └──────────┬──────────────┘ │
│           │ write /data/gy006.db    │ write /data/gy003.db
│           ▼                         ▼                │
│  ┌──────────────────────────────────────────────┐   │
│  │         共享 PVC (csi-sfsturbo, RWX)          │   │
│  │  /data/gy006.db  /data/gy003.db  /data/...   │   │
│  └──────────────────────┬───────────────────────┘   │
│                         │ read all *.db              │
│                         ▼                            │
│  ┌──────────────────────────────────────────────┐   │
│  │              API Server (HTTP)                │   │
│  │     GET /api/v1/envs/history                  │   │
│  └──────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────┘
```

**三种运行模式：**

| 模式 | 说明 |
|---|---|
| `collector` | Watch 目标集群 Pod 事件，写入 SQLite（无 HTTP 服务） |
| `api` | 聚合所有 SQLite 数据，提供 HTTP 接口（无 Watch） |
| `standalone` | 单集群模式，同时提供 Watch + HTTP（默认） |

---

## 接口规范

### `GET /api/v1/envs/history`

符合 resource-deploy-core 3.8.1 规范。

**必填参数**

| 参数 | 类型 | 说明 |
|---|---|---|
| `start_time` | string | ISO 8601，如 `2026-07-01T00:00:00Z` |
| `end_time` | string | ISO 8601，如 `2026-07-13T23:59:59Z` |

**可选参数**

| 参数 | 默认值 | 说明 |
|---|---|---|
| `match_mode` | `created` | `created`：按创建时间过滤；`released`：按结束时间过滤；`overlap`：生命周期有交集 |
| `status` | — | 支持多值，如 `status=released&status=rejected` |
| `name_prefix` | — | Pod 名称前缀过滤 |
| `cluster` | — | 集群 ID 过滤（仅 API 模式有效） |

**响应示例**

```json
{
  "count": 3,
  "envs": [
    {
      "env_id": "08610abf-4ec8-4631-87c-731cfe6b0669",
      "name": "my-job-abc123",
      "cluster": "gy006",
      "status": "released",
      "created_at": "2026-07-13T10:00:00+00:00",
      "expires_at": "2026-07-13T12:30:00+00:00",
      "ttl_seconds": 9000,
      "duration": "2h30m",
      "wait_duration": "5s",
      "groups": {
        "my-namespace": { "device_count": 1 }
      },
      "extend_env_comments": {},
      "resource_summary": {
        "total_devices": 1,
        "devices": [
          {
            "cpu": "4",
            "memory": "8Gi",
            "npu": 2,
            "pool": "",
            "res_type": "npu",
            "device_model": "910b",
            "group": "my-namespace"
          }
        ]
      }
    }
  ]
}
```

**字段说明**

| 字段 | 说明 |
|---|---|
| `env_id` | Pod UID |
| `name` | Pod 名称 |
| `cluster` | 集群 ID（collector 模式由 `CLUSTER_ID` 环境变量指定） |
| `status` | `released` Succeeded / `rejected` Failed / `expired` Unknown / `active` Running / `provisioning` Pending |
| `created_at` | 容器开始运行时间（过 pending 后） |
| `expires_at` | 容器终止时间 |
| `ttl_seconds` | 容器实际运行秒数 |
| `duration` | 格式化运行时长，如 `"2h30m"`、`"1d5h4m3s"` |
| `wait_duration` | 从 Pod 创建到容器开始运行的等待时长（pending 时长） |
| `groups` | 以 namespace 为 group 分组 |
| `resource_summary.devices` | 容器 resource requests 中的 CPU/memory/NPU/GPU |

### `GET /api/v1/health`

```json
{ "status": "ok", "service": "pod-history-api", "mode": "api" }
```

---

## 部署

### 前置条件

- K8s 集群版本 ≥ 1.20
- 节点有 x86_64（amd64）节点（默认调度到 amd64；如需 aarch64 请自行修改 nodeSelector）
- 支持ReadWriteMany 的存储类（如华为云 csi-sfsturbo），用于多 Pod 共享 SQLite 文件

### 1. 构建镜像

```bash
docker build -t <your-registry>/pod-history-api:latest .
docker push <your-registry>/pod-history-api:latest
```

### 2. 修改 deploy.yaml

- 修改镜像地址
- 修改 PVC `storageClassName` 为集群支持的 ReadWriteMany 存储类
- 按需增减 collector Deployment（每个远程集群一个）

### 3. 部署

```bash
kubectl apply -f deploy.yaml
```

### 4. 验证

```bash
# 查看 Pod 状态
kubectl -n arc-history get pods

# 查看日志
kubectl -n arc-history logs -l app=pod-history-api-server -f
kubectl -n arc-history logs -l app=pod-history-collector-gy-006 -f

# 调用接口
curl "http://<node-ip>:30880/api/v1/envs/history?\
start_time=2026-07-01T00:00:00Z&end_time=2026-07-13T23:59:59Z"
```

---

## 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `MODE` | `standalone` | 运行模式：`collector` / `api` / `standalone` |
| `CLUSTER_ID` | — | 集群标识，collector 模式必填，决定 SQLite 文件名（`/data/{CLUSTER_ID}.db`） |
| `KUBECONFIG_PATH` | — | 目标集群 kubeconfig 路径（collector 模式远程集群时使用） |
| `PORT` | `8080` | HTTP 监听端口（api / standalone 模式） |
| `DB_DIR` | `/data` | SQLite 数据库目录 |
| `RETENTION_DAYS` | `30` | 历史保留天数，超期自动清理 |
| `FLUSH_INTERVAL` | `5` | 写入缓冲刷盘间隔（秒） |

---

## 本地开发调试

```bash
pip install kubernetes==31.0.0

# standalone 模式（使用本地 kubeconfig）
KUBECONFIG=/path/to/kubeconfig PORT=18080 python3 server.py

# collector 模式
MODE=collector CLUSTER_ID=gy006 KUBECONFIG=/path/to/kubeconfig python3 server.py

# api 模式（指定 DB_DIR 读取已有数据）
MODE=api DB_DIR=/data PORT=18080 python3 server.py

# 查询
curl "http://localhost:18080/api/v1/envs/history?\
start_time=2026-07-13T00:00:00Z&end_time=2026-07-13T23:59:59Z"
```

---

## 数据存储结构

历史记录存储在共享 PVC 的 SQLite 数据库中，每个集群一个文件：

```
/data/
  gy006.db           ← collector-gy006 写入
  gy003.db           ← collector-gy003 写入
  hk-001.db          ← collector-hk-001 写入
  pod-history.db     ← standalone 模式使用
  ...
```

**写入优化：**

- 内存缓冲 + 定时刷盘（默认 5 秒），减少磁盘 I/O
- 运行中 Pod 只首次写入，终态时更新，避免频繁覆盖
- SQLite WAL 模式，支持并发读取

**查询优化：**

- SQL WHERE 过滤下推，不再全量加载到内存
- 索引：status / created_at / cluster / name

超过 RETENTION_DAYS 的记录自动删除。

---

## 跳过的 namespace

默认不记录以下 namespace 内的 Pod：

- `kube-system`
- `kube-public`
- `kube-node-lease`
- `arc-history`（自身）
- `arc-systems`
