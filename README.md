# pod-history-api

K8s 集群 Pod 历史记录服务，对外提供符合 [resource-deploy-core](https://gitcode.com/openlibing/resource-deploy-core) **3.8.1 规范**的 `GET /api/v1/envs/history` 接口。

适用于集群内 Pod **不由 resource-deploy-core 管理**的场景，通过 Watch K8s Pod 事件自动记录历史，无需部署 resource-deploy-core 全套系统。

---

## 工作原理

```
K8s API Server
     │  Watch Pod 事件（所有 namespace）
     ▼
pod-history-api (后台线程)
     │  Pod 终态（Succeeded/Failed）→ 转换为 env 格式
     ▼
ConfigMap 存储（arc-history namespace，按天分片）
history-2026-07-13 / history-2026-07-14 / ...
     │
     ▼
GET /api/v1/envs/history  ← 外部调用（NodePort 30880）
```

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
| `status` | — | `released`（Succeeded）/ `rejected`（Failed）/ `expired`（Unknown） |
| `name_prefix` | — | Pod 名称前缀过滤 |

**响应示例**

```json
{
  "count": 3,
  "envs": [
    {
      "env_id": "08610abf-4ec8-4631-87c-731cfe6b0669",
      "name": "my-job-abc123",
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
            "res_type": "container",
            "device_model": "",
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
| `status` | `released` Succeeded / `rejected` Failed / `expired` Unknown |
| `created_at` | 容器开始运行时间（过 pending 后） |
| `expires_at` | 容器终止时间 |
| `ttl_seconds` | 容器实际运行秒数 |
| `duration` | 格式化运行时长，如 `"2h30m"`、`"1d5h4m3s"` |
| `wait_duration` | 从 Pod 创建到容器开始运行的等待时长（pending 时长） |
| `groups` | 以 namespace 为 group 分组 |
| `resource_summary.devices` | 容器 resource requests 中的 CPU/memory/NPU |

### `GET /api/v1/health`

```json
{ "status": "ok", "service": "pod-history-api" }
```

---

## 部署

### 前置条件

- K8s 集群版本 ≥ 1.20
- 节点有 x86_64（amd64）节点（默认调度到 amd64；如需 aarch64 请自行修改 nodeSelector）

### 1. 构建镜像

```bash
docker build -t <your-registry>/pod-history-api:v1 .
docker push <your-registry>/pod-history-api:v1
```

### 2. 修改 deploy.yaml 中的镜像地址

```yaml
# deploy.yaml 第 84 行
image: <your-registry>/pod-history-api:v1
```

### 3. 部署

```bash
kubectl apply -f deploy.yaml
```

### 4. 验证

```bash
# 查看 Pod 状态
kubectl -n arc-history get pods

# 查看日志（观察是否在收录 Pod 事件）
kubectl -n arc-history logs -l app=pod-history-api -f

# 调用接口（NodePort 30880）
curl "http://<node-ip>:30880/api/v1/envs/history?\
start_time=2026-07-01T00:00:00Z&end_time=2026-07-13T23:59:59Z"
```

---

## 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `PORT` | `8080` | HTTP 监听端口 |
| `RETENTION_DAYS` | `30` | 历史保留天数，超期 ConfigMap 自动清理 |

---

## 本地开发调试

```bash
# 使用本地 kubeconfig 连接远端集群
pip install kubernetes==31.0.0

KUBECONFIG=/path/to/kubeconfig PORT=18080 python3 server.py

# 查询
curl "http://localhost:18080/api/v1/envs/history?\
start_time=2026-07-13T00:00:00Z&end_time=2026-07-13T23:59:59Z"
```

---

## 数据存储结构

历史记录存储在 `arc-history` namespace 下的 ConfigMap，按天分片：

```
arc-history/
  history-2026-07-13   ← 每天一个 ConfigMap
  history-2026-07-14
  ...
```

每个 ConfigMap 的 `data.records` 字段为 JSON 数组，超过 30 天（RETENTION_DAYS）自动删除。

---

## 跳过的 namespace

默认不记录以下 namespace 内的 Pod：

- `kube-system`
- `kube-public`
- `kube-node-lease`
- `arc-history`（自身）
