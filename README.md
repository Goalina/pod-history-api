# pod-history-api

K8s 集群 Pod 历史记录服务，对外提供符合 [resource-deploy-core](https://gitcode.com/openlibing/resource-deploy-core) **3.8.1 规范**的 `GET /api/v1/envs/history` 接口。

适用于集群内 Pod **不由 resource-deploy-core 管理**的场景，通过 Watch K8s Pod 事件自动记录历史，无需部署 resource-deploy-core 全套系统。

---

## 数据采集方式

通过 K8s Watch API 实时监听集群内所有 namespace 的 Pod 事件（ADDED / MODIFIED / DELETED），自动提取 Pod 生命周期信息并持久化到 PostgreSQL。

**采集规则：**

- Pod 进入 Running 时记录创建信息；进入终态（Succeeded / Failed / Unknown）时记录结束信息
- 运行中的 Pod 状态变更会实时更新（如 Pending → Running）
- Pod 被删除但未经过正常终止流程时，标记为 `expired`
- 默认跳过 `kube-system`、`kube-public`、`kube-node-lease`、`arc-history`、`arc-systems` 中的 Pod
- 历史记录默认保留 30 天，超期自动清理

**多集群架构：**

```
┌──────────────────────────────────────────────────────┐
│                  管理集群 (gy-006)                     │
│                                                       │
│  ┌──────────────────┐  ┌────────────────────────┐    │
│  │ collector-gy006  │  │ collector-gy003        │    │
│  │ (watch 本集群)    │  │ (kubeconfig → gy003)   │    │
│  └────────┬─────────┘  └──────────┬─────────────┘    │
│           │                       │                   │
│           ▼                       ▼                   │
│  ┌─────────────────────────────────────────────────┐  │
│  │              PostgreSQL                         │  │
│  │     所有集群数据统一存储，cluster 列区分         │  │
│  └──────────────────────┬──────────────────────────┘  │
│                         │                              │
│                         ▼                              │
│  ┌─────────────────────────────────────────────────┐  │
│  │            API Server (HTTP)                     │  │
│  │       GET /api/v1/envs/history                   │  │
│  └─────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────┘
```

每个目标集群部署一个 collector，watch 该集群的 Pod 事件并写入共享 PostgreSQL。API Server 聚合所有集群数据，提供统一查询接口。

---

## 接口文档

### `GET /api/v1/envs/history`

查询 Pod 历史记录，符合 resource-deploy-core 3.8.1 规范。

#### 请求参数

**必填参数**

| 参数 | 类型 | 说明 |
|---|---|---|
| `start_time` | string | 查询起始时间，ISO 8601 格式，如 `2026-07-01T00:00:00Z` |
| `end_time` | string | 查询结束时间，ISO 8601 格式，如 `2026-07-13T23:59:59Z` |

**可选参数**

| 参数 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `match_mode` | string | `created` | 时间匹配模式，详见下方 |
| `status` | string（多值） | — | 按状态过滤，如 `status=released&status=rejected` |
| `name_prefix` | string | — | 按 Pod 名称前缀过滤 |
| `cluster` | string | — | 按集群 ID 过滤 |

**`match_mode` 说明：**

| 值 | 含义 |
|---|---|
| `created` | 返回 `created_at` 在 `[start_time, end_time]` 内的记录 |
| `released` | 返回 `expires_at` 在 `[start_time, end_time]` 内的已终止记录 |
| `overlap` | 返回生命周期与 `[start_time, end_time]` 有交集的记录 |

#### 响应示例

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
      "extend_env_comments": {
        "workflow_ref": "owner/repo/.github/workflows/ci.yml@refs/heads/main",
        "job_display_name": "build",
        "job_repository": "owner/repo"
      },
      "resource_summary": {
        "total_devices": 1,
        "devices": [
          {
            "cpu": "4",
            "memory": "8Gi",
            "npu": 2,
            "pool": "npu-pool-1",
            "res_type": "npu",
            "device_model": "910b",
            "group": "my-namespace"
          }
        ]
      },
      "node_ip": "10.0.1.50",
      "npu_list": ["0", "1"]
    }
  ]
}
```

#### 错误响应

```json
{ "error": "start_time 和 end_time 为必填参数，格式: 2026-06-10T00:00:00Z", "count": 0, "envs": [] }
```

| HTTP 状态码 | 触发条件 |
|---|---|
| `400` | 缺少必填参数 / 时间格式非法 / `start_time > end_time` / `match_mode` 非法 |
| `404` | 请求路径不存在 |

---

#### 响应字段说明

##### 顶层字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `count` | integer | 返回的记录条数 |
| `envs` | array | Pod 历史记录列表，按 `created_at` 降序（最新在前） |

##### `envs` 记录字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `env_id` | string | Pod 的 Kubernetes UID，全局唯一标识，同一 Pod 生命周期内不变 |
| `name` | string | Pod 名称。GitHub Actions Runner Pod 以 `-workflow` 结尾，系统会自动关联 EphemeralRunner CRD 提取工作流信息 |
| `cluster` | string | 集群标识（如 `gy006`、`hk-001`），由 collector 的 `CLUSTER_ID` 环境变量指定 |
| `status` | string | Pod 生命周期状态，详见下方状态映射表 |
| `created_at` | string (ISO 8601) | **容器开始运行的时间**（Pod 从 Pending 进入 Running 的时刻），非 Pod 对象创建时间。`match_mode=created` 依据此字段过滤 |
| `expires_at` | string (ISO 8601) | **容器终止的时间**，仅已终止的 Pod 有值，运行中的 Pod 为空。`match_mode=released` 依据此字段过滤 |
| `ttl_seconds` | integer | 容器实际运行秒数（`expires_at - created_at`），仅已终止 Pod 计算，运行中 Pod 为 `0` |
| `duration` | string | 格式化运行时长（如 `"2h30m"`、`"1d5h4m3s"`、`"30s"`）。已终止 Pod 基于 `ttl_seconds`；运行中 Pod 由 API 实时计算，每次查询可能不同 |
| `wait_duration` | string | 从 Pod 创建到容器开始运行的等待时长（Pending 阶段耗时），反映调度和镜像拉取耗时，如 `"5s"`、`"2m10s"` |
| `groups` | object | 以 namespace 为维度的分组，格式 `{"<namespace>": {"device_count": <容器数量>}}` |
| `extend_env_comments` | object | 扩展信息，当前用于 GitHub Actions 工作流关联数据，详见下方 |
| `resource_summary` | object | Pod 资源请求摘要，取自容器 `resources.requests`（无则回退到 limits），详见下方 |
| `node_ip` | string | Pod 所在节点 IP |
| `npu_list` | array of string | Pod 使用的 NPU 物理卡号列表（如 `["0", "1"]`），从 Pod 注解 `huawei.com/AscendReal` 解析，无 NPU 时为空数组 |

##### `status` 状态映射

| K8s Pod Phase | API status | 含义 |
|---|---|---|
| `Pending` | `provisioning` | 等待调度或资源分配 |
| `Running` | `active` | 容器正在运行 |
| `Succeeded` | `released` | 所有容器成功执行并退出 |
| `Failed` | `rejected` | 至少一个容器以非零退出码退出 |
| `Unknown` | `expired` | 无法获取状态（通常节点 NotReady） |

> 运行中的 Pod 被直接删除（未正常终止）时，状态也会被标记为 `expired`。

##### `extend_env_comments` 字段（GitHub Actions 工作流信息）

仅对名称以 `-workflow` 结尾的 Pod 生效，系统自动查询对应的 `EphemeralRunner` CRD 提取工作流信息。非 GHA Pod 此字段为空对象 `{}`。

| 字段 | 说明 |
|---|---|
| `workflow_ref` | 触发的工作流文件引用，如 `owner/repo/.github/workflows/ci.yml@refs/heads/main` |
| `workflow_run_id` | Workflow Run ID，可用于拼接 GitHub API URL |
| `job_display_name` | Job 显示名称，如 `build`、`test` |
| `job_id` | Job 唯一 ID |
| `job_repository` | Job 所属仓库，如 `owner/repo` |
| `runner_id` | GitHub Actions Runner ID |
| `organization` | 所属 GitHub 组织 |
| `repository` | 所属 GitHub 仓库 |

> 以上字段仅非空时才会出现，不同 Pod 返回的字段可能不同。

##### `resource_summary` 字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `total_devices` | integer | Pod 中的容器数量 |
| `devices` | array | 每个容器的资源详情 |

**`devices` 中每条记录：**

| 字段 | 类型 | 说明 |
|---|---|---|
| `cpu` | string | 请求的 CPU，如 `"4"` (4核)、`"500m"` (0.5核) |
| `memory` | string | 请求的内存，如 `"8Gi"`、`"4096Mi"` |
| `npu` | integer | 加速卡数量，从资源键名（含 `ascend`/`npu`/`gpu`）解析，无加速卡时为 `0` |
| `pool` | string | 节点调度池名称（来自 nodeSelector），无则为空 |
| `res_type` | string | 资源类型：`"npu"` (华为昇腾) / `"gpu"` / `"container"` (仅CPU内存) |
| `device_model` | string | 加速卡型号，如 `"910b"`、`"910a"`，从资源键名解析 |
| `group` | string | 所属 namespace |

---

### `GET /api/v1/health`

健康检查接口。

```json
{ "status": "ok", "service": "pod-history-api", "mode": "api" }
```

| 字段 | 说明 |
|---|---|
| `status` | 服务状态，正常为 `"ok"` |
| `service` | 服务名称，固定 `"pod-history-api"` |
| `mode` | 当前运行模式：`collector` / `api` / `standalone` |

---

## 部署

### 前置条件

- K8s 集群版本 ≥ 1.20
- x86_64 节点（默认调度到 amd64）
- PostgreSQL 16+（deploy.yaml 中包含 PostgreSQL StatefulSet，也可使用外部 PG）

### 快速开始

```bash
# 1. 构建镜像
docker build -t <your-registry>/pod-history-api:latest .
docker push <your-registry>/pod-history-api:latest

# 2. 修改 deploy.yaml 中的镜像地址和 DATABASE_URL

# 3. 部署
kubectl apply -f deploy.yaml

# 4. 验证
kubectl -n arc-history get pods
kubectl -n arc-history port-forward svc/pod-history-api-server 18080:8080 &
curl "http://localhost:18080/api/v1/envs/history?start_time=2026-07-01T00:00:00Z&end_time=2026-07-13T23:59:59Z"
```

---

## 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `MODE` | `standalone` | 运行模式：`collector` (仅采集) / `api` (仅查询) / `standalone` (采集+查询) |
| `CLUSTER_ID` | — | 集群标识，collector 模式必填 |
| `KUBECONFIG_PATH` | — | 远程集群 kubeconfig 路径，留空则使用 in-cluster ServiceAccount |
| `DATABASE_URL` | — | PostgreSQL 连接字符串，如 `postgresql://user@host:5432/dbname`，必填 |
| `PORT` | `8080` | HTTP 监听端口 |
| `RETENTION_DAYS` | `30` | 历史记录保留天数 |
| `FLUSH_INTERVAL` | `5` | 写入缓冲刷盘间隔（秒） |
| `RUNNER_SYNC_INTERVAL` | `30` | EphemeralRunner 工作流信息同步间隔（秒） |

---

## 本地开发调试

```bash
pip install kubernetes==31.0.0 psycopg2-binary

# standalone 模式
DATABASE_URL="postgresql://pod_history@localhost:5432/pod_history" \
KUBECONFIG=/path/to/kubeconfig PORT=18080 python3 server.py

# collector 模式
MODE=collector CLUSTER_ID=gy006 \
DATABASE_URL="postgresql://pod_history@localhost:5432/pod_history" \
KUBECONFIG=/path/to/kubeconfig python3 server.py

# api 模式
MODE=api DATABASE_URL="postgresql://pod_history@localhost:5432/pod_history" \
PORT=18080 python3 server.py

# 查询
curl "http://localhost:18080/api/v1/envs/history?start_time=2026-07-13T00:00:00Z&end_time=2026-07-13T23:59:59Z"
```
