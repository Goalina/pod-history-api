# pod-history-api 架构文档

> 最后更新：2026-11-13 | 对应代码版本：main `20261113-1505`

## 1. 系统概述

pod-history-api 是一个多集群 K8s Pod 历史记录服务，对外提供符合 resource-deploy-core 3.8.1 规范的查询接口。核心能力：

- **Pod 生命周期历史**：watch 各集群 Pod 事件（ADDED/MODIFIED/DELETED），记录从容器启动到终止的完整生命周期
- **集群卡数快照**：每 5 分钟采集各集群加速卡（NPU/GPU）的总数、已用数、可用数
- **CI 工作流富化**：从 EphemeralRunner（GitHub Actions）/ AtomGit 注解 / Buildkite 注解中提取工作流信息，写入 Pod 记录

## 2. 整体架构

```
┌───────────────────────── 006 管理集群（arc-history namespace）────────────────────────┐
│                                                                                       │
│  ┌─────────────┐   ┌─────────────┐   ┌─────────────┐       ┌──────────────────────┐ │
│  │ collector   │   │ collector   │   │ collector   │  ...  │ api-server           │ │
│  │ gy-006      │   │ gy-003~005  │   │ hk-001      │       │ (ThreadingHTTPServer)│ │
│  │ (in-cluster)│   │ (Vault kube)│   │ (Vault kube)│       │ GET /api/v1/envs/... │ │
│  └──────┬──────┘   └──────┬──────┘   └──────┬──────┘       └──────────┬───────────┘ │
│         │                 │                 │                          │             │
│  ┌──────┴─────────────────┴─────────────────┴──────┐  ┌────────────┐  │             │
│  │           PostgreSQL（共享 DB）                  │←─│ 读写数据   │──┘             │
│  │  pod_history / cluster_card_snapshot            │  └────────────┘                │
│  └─────────────────────────────────────────────────┘                                │
│                                                                                       │
│  ┌──────────────────────────┐  ┌─────────────────────────────────────────────────┐ │
│  │ collector-karmada-cp     │  │ collector-sz-lab                                │ │
│  │ (ER 跨写，不采集 Pod)     │  │ ┌──────────────┐  ┌─────────────────────────┐   │ │
│  │                          │  │ │ vpn sidecar  │  │ collector              │   │ │
│  │ 连 Karmada CP API        │  │ │ (OpenVPN)    │→ │ (连内网 10.0.254.x)   │   │ │
│  └──────────────────────────┘  │ └──────────────┘  └─────────────────────────┘   │ │
│                                └─────────────────────────────────────────────────┘ │
└───────────────────────────────────────────────────────────────────────────────────────┘
```

### 2.1 三种运行模式

| 模式 | 启动线程 | 用途 |
|------|---------|------|
| `collector` | watcher / flush / runner-sync / capacity | 只采集，不提供 HTTP |
| `api` | HTTP / cleanup | 只查询，不采集 |
| `standalone` | 全部 | 单机（开发/测试） |

### 2.2 特殊模式

| 环境变量 | 效果 | 用于 |
|----------|------|------|
| `ER_CROSS_ENRICH_ONLY=true` | 只跑 runner-sync（ER 跨写），不 watch Pod / 不采集容量 / 不启动 cleanup | Karmada CP 面 |
| `KUBECONFIG_PATH` | 指定远程集群 kubeconfig（Vault 注入） | 除 gy-006 外所有 collector |

## 3. 当前集群清单

| 集群 ID | 网络接入方式 | kubeconfig 来源 | 说明 |
|---------|-------------|----------------|------|
| gy006 | in-cluster | ServiceAccount | 部署在 006 集群本身 |
| gy003 | 公网 EIP | Vault `gy003` | |
| gy-001 | 公网 EIP | Vault `gy001` | |
| gy-004 | 公网 EIP | Vault `gy004` | |
| gy-005 | 公网 EIP | Vault `gy005` | |
| hk-001 | 公网 EIP | Vault `hk001` | |
| cn12-001 | 公网 EIP | Vault `cn12` | ARC 源集群，有 Liqo 虚拟节点 |
| aiframework-hb3 | 公网 EIP | Vault `aiframework-hb3` | |
| verl-hb3 | 公网 EIP | Vault `verl-hb3` | |
| wlcb-001 | 公网 EIP | Vault `wlcb001` | |
| sh-001 | 公网 EIP | Vault `sh001` | |
| sh-002 | 公网 EIP | Vault `sh002` | |
| **sz-lab** | **VPN sidecar** | Vault `szlab`（内网地址 `10.0.254.194:6443`） | 华为云内网生产集群 |
| **karmada-cp** | 公网 EIP | Vault `karmadacp`（`1.95.146.120:32443`） | Karmada 控制面，ER 补写模式 |

## 4. 数据流与场景

### 4.1 场景 A：标准采集（12 个集群）

```
collector Pod ──watch──→ 目标集群 K8s API
  │                        ├─ Pod 事件 → _extract_record() → 缓冲 → flush → PostgreSQL
  │                        ├─ 每 30s：list EphemeralRunner → Part A（本集群 -workflow 记录富化）
  │                        └─ 每 5min：list nodes + list pods → 容量快照
  └─ 所有数据写入共享 PostgreSQL（cluster 列区分集群）
```

### 4.2 场景 B：Liqo 虚拟节点跨写（cn12-001）

**问题**：ARC 控制器在 cn12 上创建 EphemeralRunner 和 runner Pod，但 job Pod（`-workflow`）经 Liqo 虚拟节点 offload 到远端集群。远端集群的 collector 查不到 ER。

**方案**：cn12 的 collector 在 `_sync_ephemeral_runners` 的 Part B 中，找到 Liqo 虚拟节点上的 runner Pod，把本集群 ER 信息按 `name = <runner>-workflow` 跨写到远端集群的记录：

```sql
UPDATE pod_history SET extend_env_comments = %s
WHERE name = %s
  AND (created_at = '' OR created_at >= %s)  -- runner 创建时间-1h
  AND COALESCE(extend_env_comments, '{}') IS DISTINCT FROM %s
```

### 4.3 场景 C：Karmada 分发 + ER 跨写（sz-lab + karmada-cp）

**问题**：ARC 控制器在 Karmada CP 面上创建 EphemeralRunner，job Pod 被分发到成员集群 sz-lab。sz-lab 的 collector 采集 Pod 正常，但查不到 ER（ER 只在 CP 面）。

**关键发现**：Karmada 分发 Pod 到成员集群时，成员集群创建**新的 Pod 对象**（UID 不同）。如果在 CP 面上也跑普通 collector，同一个物理 Pod 会产生两条记录（数据重复）。

**方案**：在 Karmada CP 面部署 `pod-history-collector-karmada-cp`，设 `ER_CROSS_ENRICH_ONLY=true`：
- 只查 CP 面的 EphemeralRunner（每 30s）
- 按 `(namespace, runner_name)` 跨写所有集群的 `-workflow` 记录（SQL 同上，额外加 `_namespace = %s` 过滤）
- **不** watch Pod、**不**采集容量（避免数据重复）

### 4.4 场景 D：VPN 接入内网集群（sz-lab）

**问题**：sz-lab（华为云内网生产集群）的 API（`10.0.254.194:6443`）从 006 Pod 不可达（无直连路由），公网 EIP 也不可达（安全组未放行 006 出口 IP）。

**方案**：Pod 内加 OpenVPN sidecar：

```
Pod（共享网络命名空间，NET_ADMIN）
├── vpn sidecar: openvpn-sidecar:1.0
│   ├── VPN 隧道到华为云内网（121.36.218.253:1194）
│   ├── 过滤 10.0.0.0/22 推送路由（与 006 Pod CIDR 冲突，否则连 PG 都断）
│   └── 仅走 10.0.254.0/24（生产集群网段）
└── collector: 等 tun0 就绪后启动，kubeconfig 指向 https://10.0.254.194:6443
```

**VPN Secret**：`szlab-vpn-config`（含 `client.conf` + `auth.txt`）

### 4.5 场景 E：upsert 空值保护

`extend_env_comments` 的 upsert 使用 CASE WHEN：

```sql
extend_env_comments = CASE WHEN EXCLUDED.extend_env_comments IN ('', '{}')
                           THEN pod_history.extend_env_comments  -- 保留旧值
                           ELSE EXCLUDED.extend_env_comments     -- 用新值
                      END
```

确保成员集群 collector 写入空 `{}`（因为查不到 ER）时，不会覆盖 karmada-cp 已跨写的工作流信息。

## 5. 数据模型

### 5.1 pod_history（Pod 生命周期，主表）

| 列 | 说明 |
|----|------|
| env_id | Pod UID（主键），Karmada 分发后 CP/成员集群 UID 不同 |
| name / cluster / status | Pod 名称 / 集群 ID / 生命周期状态 |
| created_at / expires_at | 容器启动/终止时间（ISO 8601 UTC） |
| ttl_seconds / duration / wait_duration | 运行时长 / 调度等待 |
| groups / resource_summary / npu_list | 分组 / 资源规格 / NPU 物理卡号 |
| extend_env_comments | CI 工作流信息（GitHub Actions / AtomGit / Buildkite） |
| source | 来源（github-action / atomgit-action / buildkite / unknown） |
| node_ip / _namespace / _node / _image / _exit_code | 内部字段（API 输出时过滤） |

### 5.2 cluster_card_snapshot（集群卡数快照）

| 列 | 说明 |
|----|------|
| cluster + snapshot_time | 主键，每集群每 5 分钟一行 |
| total_cards / used_cards / node_count | 总卡 / 已用 / 有加速卡的节点数 |
| nodes | JSON 数组：`[{node_ip, resource_name, service_type, cards, used}]` |
| status / error | ok / failed（采集失败如实记录） |

## 6. K8s API 最小权限

### 6.1 标准 collector（sz-lab 模板，其他集群同理）

```yaml
# ClusterRole: pod-history-collector-role
rules:
- apiGroups: [""]
  resources: [pods]
  verbs: [get, list, watch]       # watch Pod 事件 + list Pod（对账/容量）
- apiGroups: [""]
  resources: [nodes]
  verbs: [get, list]              # 容量采集 list_node
- apiGroups: [""]
  resources: [namespaces]
  verbs: [get, list]              # 保留（与 006 模板一致，代码未直接调用）
- apiGroups: [actions.github.com]
  resources: [ephemeralrunners]
  verbs: [get, list]              # EphemeralRunner 富化
```

### 6.2 Karmada CP ER 补写（karmada-cp 专用）

```yaml
# ClusterRole: pod-history-er-enrich-role（最小权限，只有 ER 读）
rules:
- apiGroups: [actions.github.com]
  resources: [ephemeralrunners]
  verbs: [get, list]
```

不需要 pods/nodes 权限（不 watch Pod、不采集容量）。

### 6.3 权限创建命令（新集群接入时）

```bash
# 在目标集群上执行（需要 admin kubeconfig）
cat <<'EOF' | kubectl apply -f -
apiVersion: v1
kind: Namespace
metadata:
  name: arc-history
---
apiVersion: v1
kind: ServiceAccount
metadata:
  name: pod-history-collector
  namespace: arc-history
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: pod-history-collector-role
rules:
- apiGroups: [""]
  resources: [pods]
  verbs: [get, list, watch]
- apiGroups: [""]
  resources: [namespaces]
  verbs: [get, list]
- apiGroups: [""]
  resources: [nodes]
  verbs: [get, list]
- apiGroups: [actions.github.com]
  resources: [ephemeralrunners]
  verbs: [get, list]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: pod-history-collector-binding
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: pod-history-collector-role
subjects:
- kind: ServiceAccount
  name: pod-history-collector
  namespace: arc-history
---
apiVersion: v1
kind: Secret
metadata:
  name: pod-history-collector-token
  namespace: arc-history
  annotations:
    kubernetes.io/service-account.name: pod-history-collector
type: kubernetes.io/service-account-token
EOF

# 生成最小权限 kubeconfig
TOKEN=$(kubectl -n arc-history get secret pod-history-collector-token -o jsonpath='{.data.token}' | base64 -d)
CA=$(kubectl -n arc-history get secret pod-history-collector-token -o jsonpath='{.data.ca\.crt}')
cat > kubeconfig.yaml <<EOF
apiVersion: v1
kind: Config
clusters:
- name: <cluster-id>
  cluster:
    server: https://<API地址>:<端口>
    certificate-authority-data: ${CA}
users:
- name: pod-history-collector
  user:
    token: ${TOKEN}
contexts:
- name: default
  context:
    cluster: <cluster-id>
    user: pod-history-collector
current-context: default
EOF
```

### 6.4 Vault 存储

kubeconfig 存入 Vault 后由 Deployment 的 Vault Agent 注入：

```
Vault 路径: internal/data/ascend/npu-exporter
Key 命名:   <集群ID去掉连线和前缀>（如 gy001 / hk001 / szlab / karmadacp）
```

## 7. 对外接口

| 路由 | 模式 | 分页 | 用途 |
|------|------|------|------|
| `GET /api/v1/envs/history` | api | ❌（全量返回） | 全部来源 Pod 历史 |
| `GET /api/v1/envs/history/atomgit` | api | ✅ limit/offset | 仅 AtomGit CI |
| `GET /api/v1/envs/history/<cluster>` | api | 透传 | 远程集群 history 代理 |
| `GET /api/v1/clusters/capacity` | api | ❌ | 各集群卡数（不带时间=最新，带时间=区间） |
| `GET /api/v1/health` | any | - | 健康检查 |

## 8. 已知限制与待办

| Issue | 说明 |
|-------|------|
| #10 | 主路由大时间窗口无分页（35 万行 23s） |
| #9 | AtomGit collector 每 30s 刷 ER 404 日志 |
| - | TEXT 时间戳跨时区比较（parse_iso 未统一 UTC） |
| - | k8s 客户端无请求超时（集群不可达时重试 ~9 分钟） |
| - | cluster 命名不一致（gy003/gy006 vs gy-001/gy-004） |

## 9. 新集群接入流程

1. **准备**：在目标集群创建 Namespace + ServiceAccount + ClusterRole + Binding + Token Secret（见 6.3）
2. **生成 kubeconfig**：用 ServiceAccount token + 集群 CA + API 地址生成（见 6.3）
3. **存入 Vault**：`internal/data/ascend/npu-exporter` 下对应 key
4. **写 Deployment**：在 deploy.yaml 追加（参照现有模板）
5. **提 PR**：走 squash merge 流程
6. **构建镜像**：从 main 构建新镜像（`docker build + push`，tag `YYYYMMDD-HHMM`）
7. **bump tag + apply**：更新 deploy.yaml 全部 tag → `kubectl apply`
8. **验证**：collector pod Running → 日志正常 → DB 有数据 → 容量快照桶齐

### 9.1 VPN 集群额外步骤

- 在 006 集群创建 Secret `szlab-vpn-config`（VPN 配置 + 认证）
- VPN 配置需过滤冲突路由（`pull-filter ignore "route 10.0.0.0"` + `route 10.0.254.0 255.255.255.0`）
- Deployment 需加 vpn sidecar + `NET_ADMIN` + `/dev/net/tun` hostPath
- kubeconfig 的 server 用**内网地址**（VPN 可达），需确认服务端证书 SAN 包含该地址

### 9.2 Karmada CP 集群额外步骤

- Deployment 设 `ER_CROSS_ENRICH_ONLY=true`
- ClusterRole 只需要 `ephemeralrunners: get, list`
- **不要**加 watch Pod 权限（CP 面和成员集群 Pod UID 不同，会产生重复记录）
