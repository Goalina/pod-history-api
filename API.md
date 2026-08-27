# API 接口文档

## `GET /api/v1/envs/history`

查询 Pod 历史记录，符合 resource-deploy-core 3.8.1 规范。

---

### 请求参数

**必填**

| 参数 | 类型 | 示例 | 说明 |
|---|---|---|---|
| `start_time` | string | `2026-07-01T00:00:00Z` | 查询起始时间，ISO 8601 格式 |
| `end_time` | string | `2026-07-13T23:59:59Z` | 查询结束时间，ISO 8601 格式 |

**可选**

| 参数 | 类型 | 默认值 | 示例 | 说明 |
|---|---|---|---|---|
| `match_mode` | string | `created` | `overlap` | 时间匹配模式，见下表 |
| `status` | string（可多值） | — | `status=released&status=rejected` | 按状态过滤 |
| `name_prefix` | string | — | `my-job-` | 按 Pod 名称前缀过滤 |
| `cluster` | string | — | `gy006` | 按集群 ID 过滤 |

**`match_mode` 取值**

| 值 | 过滤逻辑 | 适用场景 |
|---|---|---|
| `created` | `created_at` 在 `[start_time, end_time]` 内 | 查某个时间段内**启动**的 Pod |
| `released` | `expires_at` 在 `[start_time, end_time]` 内（排除未终止的） | 查某个时间段内**结束**的 Pod |
| `overlap` | Pod 生命周期与 `[start_time, end_time]` 有交集 | 查某个时间段内**正在运行**的 Pod（包括之前启动的、之后结束的） |

---

### 响应格式

```json
{
  "count": 1,
  "envs": [
    {
      "env_id": "08610abf-4ec8-4631-87c-731cfe6b0669",
      "name": "my-job-abc123-workflow",
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
        "workflow_ref": "my-org/my-repo/.github/workflows/ci.yml@refs/heads/main",
        "workflow_run_id": "12345678901",
        "job_display_name": "build",
        "job_id": "42",
        "job_repository": "my-org/my-repo",
        "runner_id": "99",
        "organization": "my-org",
        "repository": "my-org/my-repo"
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

---

### 错误响应

```json
{ "error": "start_time 和 end_time 为必填参数，格式: 2026-06-10T00:00:00Z", "count": 0, "envs": [] }
```

| 状态码 | 触发条件 |
|---|---|
| `400` | 缺少 `start_time` 或 `end_time` / 时间格式非法 / `start_time > end_time` / `match_mode` 取值非法 |
| `404` | 请求路径不匹配 |

---

### 字段说明

#### 顶层

| 字段 | 类型 | 说明 |
|---|---|---|
| `count` | integer | 本次返回的记录条数 |
| `envs` | array | 记录列表，按 `created_at` 降序（最新在前） |

---

#### `envs[].env_id`

- **类型：** string
- **示例：** `"08610abf-4ec8-4631-87c-731cfe6b0669"`
- **说明：** Pod 的 Kubernetes UID，全局唯一。同一 Pod 从创建到删除，UID 不变。接口用此字段作为记录的唯一标识

---

#### `envs[].name`

- **类型：** string
- **示例：** `"my-job-abc123"`
- **说明：** Pod 名称，对应 K8s 的 `metadata.name`
- **GHA 场景：** GitHub Actions Runner Pod 名称以 `-workflow` 结尾（如 `runner-abc123-workflow`）。系统检测到此后缀时，会自动查找同 namespace 下名为 `runner-abc123` 的 `EphemeralRunner` CRD，将工作流信息填入 `extend_env_comments`

---

#### `envs[].cluster`

- **类型：** string
- **示例：** `"gy006"`、`"hk-001"`、`"aiframework-hb3"`
- **说明：** 该 Pod 所属的集群标识。由 collector 的 `CLUSTER_ID` 环境变量指定，通过 `cluster` 查询参数可按集群过滤

---

#### `envs[].status`

- **类型：** string
- **取值：** `provisioning` | `active` | `released` | `rejected` | `expired`
- **映射关系：**

| 值 | 对应 K8s Phase | 含义 |
|---|---|---|
| `provisioning` | `Pending` | 等待调度、镜像拉取或资源分配 |
| `active` | `Running` | 容器正在运行 |
| `released` | `Succeeded` | 所有容器正常退出 |
| `rejected` | `Failed` | 至少一个容器异常退出 |
| `expired` | `Unknown` | 节点失联导致状态不可达 |

- **补充：** 若 Pod 在 Running/Pending 状态被直接删除（未走正常终止流程），状态也会被设为 `expired`

---

#### `envs[].created_at`

- **类型：** string（ISO 8601）
- **示例：** `"2026-07-13T10:00:00+00:00"`
- **说明：** 容器**开始运行**的时间点，即 Pod 从 Pending 进入 Running 的时刻（`status.startTime`），而非 Pod 对象的创建时间
- **与 `wait_duration` 的关系：** `wait_duration = created_at - Pod创建时间`
- **用途：** `match_mode=created` 依据此字段过滤

---

#### `envs[].expires_at`

- **类型：** string（ISO 8601）或空字符串 `""`
- **示例：** `"2026-07-13T12:30:00+00:00"` 或 `""`
- **说明：** 容器**终止**的时间点，取自容器 `state.terminated.finishedAt`
- **条件：** 仅已终止的 Pod 有值（`released`/`rejected`/`expired`），运行中的 Pod 为空字符串
- **用途：** `match_mode=released` 依据此字段过滤；`match_mode=overlap` 结合 `created_at` 判断生命周期交集

---

#### `envs[].ttl_seconds`

- **类型：** integer
- **示例：** `9000`
- **说明：** 容器实际运行的秒数，计算方式：`expires_at - created_at`
- **条件：** 仅已终止 Pod 且两个字段都存在时才计算，运行中 Pod 为 `0`

---

#### `envs[].duration`

- **类型：** string
- **示例：** `"2h30m"`、`"1d5h4m3s"`、`"30s"`、`"0s"`
- **说明：** 运行时长的可读格式，按天/小时/分钟/秒组合，非零单位才出现
- **已终止 Pod：** 固定值，等于 `ttl_seconds` 的格式化
- **运行中 Pod：** 由 API **实时计算** `当前时间 - created_at`，每次查询结果可能不同

---

#### `envs[].wait_duration`

- **类型：** string
- **示例：** `"5s"`、`"2m10s"`、`"0s"`
- **说明：** Pod 从创建到容器开始运行的等待时长，即 Pending 阶段耗时，包含调度等待、镜像拉取、资源分配等
- **计算：** `status.startTime - metadata.creationTimestamp`

---

#### `envs[].groups`

- **类型：** object
- **示例：** `{"my-namespace": {"device_count": 1}}`
- **说明：** 以 namespace 分组，key 是 namespace 名称，`device_count` 是该 namespace 下的容器数量
- **注意：** 单 Pod 记录通常只有一个 namespace 条目

---

#### `envs[].extend_env_comments`

- **类型：** object
- **说明：** 扩展元信息。当前用于关联 **GitHub Actions** 工作流数据
- **触发条件：** 仅 Pod 名称以 `-workflow` 结尾时自动填充，其余 Pod 为空对象 `{}`
- **子字段：**

| 字段 | 类型 | 示例 | 说明 |
|---|---|---|---|
| `workflow_ref` | string | `my-org/my-repo/.github/workflows/ci.yml@refs/heads/main` | 工作流文件引用，包含仓库、文件路径和触发分支 |
| `workflow_run_id` | string | `12345678901` | Workflow Run ID |
| `job_display_name` | string | `build` | Job 在 GitHub UI 中的显示名 |
| `job_id` | string | `42` | Job 唯一 ID |
| `job_repository` | string | `my-org/my-repo` | Job 所属仓库 |
| `runner_id` | string | `99` | GitHub Runner ID |
| `organization` | string | `my-org` | 所属 GitHub 组织 |
| `repository` | string | `my-org/my-repo` | 所属 GitHub 仓库（含组织前缀） |

> 仅非空字段会出现，不同 Pod 返回的字段子集可能不同。

**拼接 GitHub URL：**

根据 `extend_env_comments` 中的字段可以拼出以下 GitHub 页面链接：

| 页面 | URL 拼接方式 | 示例 |
|---|---|---|
| **Workflow Run 详情** | `https://github.com/{repository}/actions/runs/{workflow_run_id}` | `https://github.com/my-org/my-repo/actions/runs/12345678901` |
| **工作流文件** | `https://github.com/{workflow_ref}` | `https://github.com/my-org/my-repo/.github/workflows/ci.yml@refs/heads/main` |
| **仓库首页** | `https://github.com/{repository}` | `https://github.com/my-org/my-repo` |

代码示例：

```python
def build_github_urls(comments: dict) -> dict:
    repo = comments.get("repository", "")
    run_id = comments.get("workflow_run_id", "")
    workflow_ref = comments.get("workflow_ref", "")

    urls = {}
    if repo and run_id:
        urls["workflow_run_url"] = f"https://github.com/{repo}/actions/runs/{run_id}"
    if workflow_ref:
        urls["workflow_file_url"] = f"https://github.com/{workflow_ref}"
    if repo:
        urls["repo_url"] = f"https://github.com/{repo}"
    return urls
```

```javascript
function buildGithubUrls(comments) {
  const { repository, workflow_run_id, workflow_ref } = comments;
  const urls = {};
  if (repository && workflow_run_id)
    urls.workflow_run_url = `https://github.com/${repository}/actions/runs/${workflow_run_id}`;
  if (workflow_ref)
    urls.workflow_file_url = `https://github.com/${workflow_ref}`;
  if (repository)
    urls.repo_url = `https://github.com/${repository}`;
  return urls;
}
```

---

#### `envs[].resource_summary`

- **类型：** object
- **说明：** Pod 的资源请求摘要，数据取自容器 `resources.requests`（若为空则回退到 `resources.limits`）

| 字段 | 类型 | 说明 |
|---|---|---|
| `total_devices` | integer | Pod 中的容器数量 |
| `devices` | array | 每个容器的资源详情，顺序与 `spec.containers` 一致 |

**`devices[]` 字段：**

| 字段 | 类型 | 示例 | 说明 |
|---|---|---|---|
| `cpu` | string | `"4"`、`"500m"` | 请求的 CPU 核数。整数表示完整核，`m` 后缀表示千分之核（500m = 0.5 核） |
| `memory` | string | `"8Gi"`、`"4096Mi"` | 请求的内存。`Gi` = Gibibyte，`Mi` = Mebibyte |
| `npu` | integer | `2`、`0` | 加速卡数量。从资源键名中含 `ascend`/`npu`/`gpu` 的字段解析。无加速卡为 `0` |
| `pool` | string | `"npu-pool-1"`、`""` | 节点调度池，来自 `spec.nodeSelector.pool`，无则为空 |
| `res_type` | string | `"npu"`、`"gpu"`、`"container"` | 资源类型。键名含 `gpu`（不含 `ascend`）→ `"gpu"`；含 `ascend`/`npu` → `"npu"`；否则 → `"container"`（仅 CPU/内存） |
| `device_model` | string | `"910b"`、`""` | 加速卡型号，从资源键名解析（如 `huawei.com/Ascend910B` → `"910b"`），无加速卡为空 |
| `group` | string | `"my-namespace"` | 所属 namespace |

---

#### `envs[].node_ip`

- **类型：** string
- **示例：** `"10.0.1.50"`
- **说明：** Pod 所在 K8s 节点的 IP 地址（`status.hostIP`）。Pending 状态未调度时可能为空

---

#### `envs[].npu_list`

- **类型：** array of string
- **示例：** `["0", "1"]`、`[]`
- **说明：** Pod 使用的 NPU 物理卡号列表，从 Pod 注解 `huawei.com/AscendReal` 解析
- **解析规则：** 注解值如 `"Ascend910-0 Ascend910-1"` → 提取末尾数字 → `["0", "1"]`
- **条件：** 仅华为云昇腾环境且 Pod 带有该注解时有值，否则为空数组

---

### 常见查询示例

服务域名：`pod-history-api.test.osinfra.cn`

```bash
# 查询今天所有已终止的 Pod
curl "https://pod-history-api.test.osinfra.cn/api/v1/envs/history?\
start_time=2026-08-27T00:00:00Z&end_time=2026-08-27T23:59:59Z&status=released&status=rejected"

# 查询某个时间点正在运行的 Pod（overlap 模式）
curl "https://pod-history-api.test.osinfra.cn/api/v1/envs/history?\
start_time=2026-08-27T10:00:00Z&end_time=2026-08-27T10:00:00Z&match_mode=overlap"

# 按集群过滤
curl "https://pod-history-api.test.osinfra.cn/api/v1/envs/history?\
start_time=2026-08-01T00:00:00Z&end_time=2026-08-27T23:59:59Z&cluster=gy006"

# 按 Pod 名称前缀过滤
curl "https://pod-history-api.test.osinfra.cn/api/v1/envs/history?\
start_time=2026-08-01T00:00:00Z&end_time=2026-08-27T23:59:59Z&name_prefix=my-job-"
```
