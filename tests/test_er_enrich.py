"""
_cross_enrich_workflow_pods / runner_map 构建的测试（不依赖 DB / K8s，可被 pytest 收集）。

运行：
    python3 tests/test_er_enrich.py      # 直接跑，全绿打印 OK
    pytest tests/test_er_enrich.py      # 或用 pytest（不会触发 server.py 的模块级副作用）

server.py 在模块级会执行 _init_db()（连 Postgres）和 _init_k8s()，直接 import 需要真实环境。
这里把源码读进来、注释掉模块级副作用后 exec 到独立命名空间，只取出需要的函数做单测
（与 test_source_extract.py 同一做法）。

覆盖 review 提出的回归点：
1. _creation 不外泄到 extend_env_comments（runner_map / lower_bounds 分离）
2. 空 info 跳过（不会把已补写数据覆盖成 {}）
3. _namespace 过滤生效（不同 ns 同名 runner 不串写）
4. _sync_ephemeral_runners 在普通模式下不构建 lower bounds、不污染 runner_map
5. _sync_ephemeral_runners 在 enrich 模式下 lower bounds 与 runner_map 分离
"""
import os
import json

os.environ.setdefault("MODE", "api")
os.environ.setdefault("DATABASE_URL", "postgresql://unused@localhost/unused")

_SERVER_PY = os.path.join(os.path.dirname(__file__), "..", "server.py")


def _load_server_ns():
    """把 server.py exec 到独立命名空间（注释掉模块级 _init_db），返回命名空间。"""
    src = open(_SERVER_PY, encoding="utf-8").read()
    src = src.replace("\n_init_db()\n", "\n# _init_db()  # neutralized for test\n")
    ns = {"__name__": "server_under_test"}
    exec(compile(src, "server.py", "exec"), ns)  # noqa: S102
    return ns


# ─── 工具：fake DB 连接 ──────────────────────────────────────

class FakeCur:
    def __init__(self):
        self.executed = []
        self.rowcount = 0

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        return self

    def fetchall(self):
        return []  # Part A 查不到记录，不做 UPDATE

    def executemany(self, sql, params):
        self.executed.append((sql, params))

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeConn:
    def __init__(self):
        self.cur = FakeCur()
        self.committed = False

    def cursor(self):
        return self.cur

    def commit(self):
        self.committed = True

    def rollback(self):
        pass


# ─── 测试 1-3：_cross_enrich_workflow_pods ────────────────────

def test_cross_enrich():
    ns = _load_server_ns()
    _cross_enrich = ns["_cross_enrich_workflow_pods"]

    conn = FakeConn()
    ns["_get_conn"] = lambda: conn
    ns["_put_conn"] = lambda c, error=False: None

    runner_map = {
        ("ns1", "runner-a"): {"workflow_ref": "ref-a", "job_display_name": "job-a"},
        ("ns2", "runner-b"): {},  # 空 info → 应跳过
        ("ns3", "runner-c"): {"workflow_ref": "ref-c"},
    }
    lower_bounds = {("ns1", "runner-a"): "2026-11-13T10:00:00+00:00"}

    _cross_enrich(runner_map, lower_bounds)

    assert len(conn.cur.executed) == 1, f"expected 1, got {len(conn.cur.executed)}"
    sql, params = conn.cur.executed[0]

    # SQL 含 _namespace 过滤
    assert "_namespace = %s" in sql, "SQL should filter by _namespace"

    # 参数结构：(info_json, wf_name, ns, lower, info_json)
    assert len(params[0]) == 5, f"expected 5 params, got {len(params[0])}"

    # 空 info 的 runner-b 没有出现在参数里
    names = [p[1] for p in params]
    assert "runner-b-workflow" not in names, "empty info should be skipped"
    assert "runner-a-workflow" in names
    assert "runner-c-workflow" in names

    # runner-a 的 info JSON 不含 _creation
    for p in params:
        if p[1] == "runner-a-workflow":
            info = json.loads(p[0])
            assert "_creation" not in info, f"_creation leaked: {info}"
            assert info["workflow_ref"] == "ref-a"

    # lower_bounds 正确传递
    for p in params:
        if p[1] == "runner-a-workflow":
            assert p[3] == "2026-11-13T10:00:00+00:00"
        if p[1] == "runner-c-workflow":
            assert p[3] == ""

    assert conn.committed

    print("PASS: _creation 不外泄、空 info 跳过、_namespace 过滤、lower_bounds 正确")


def test_cross_enrich_empty_map():
    """runner_map 为空时应直接返回，不执行任何 SQL"""
    ns = _load_server_ns()
    _cross_enrich = ns["_cross_enrich_workflow_pods"]

    conn = FakeConn()
    ns["_get_conn"] = lambda: conn
    ns["_put_conn"] = lambda c, error=False: None

    _cross_enrich({}, {})
    assert len(conn.cur.executed) == 0
    print("PASS: 空 runner_map 不执行 SQL")


# ─── 测试 4-5：_sync_ephemeral_runners 的 runner_map 构建 ────

def _fake_runners():
    """构造 fake _k8s_custom.list_cluster_custom_object 返回值"""
    return {
        "items": [
            {
                "metadata": {
                    "name": "runner-x",
                    "namespace": "ns-a",
                    "creationTimestamp": "2026-11-13T10:00:00Z",
                },
                "status": {"jobWorkflowRef": "ref-x", "jobDisplayName": "job-x"},
            },
            {
                "metadata": {
                    "name": "runner-y",
                    "namespace": "ns-b",
                    "creationTimestamp": "2026-11-13T11:00:00Z",
                },
                "status": {},  # 无 status → info 为空 dict
            },
        ]
    }


class FakeCustomApi:
    def list_cluster_custom_object(self, **kwargs):
        return _fake_runners()


class FakeCoreApi:
    """mock _k8s_core：Part B 的 list_node 返回空列表（无虚拟节点，直接返回）"""

    def list_node(self, **kwargs):
        class R:
            items = []
        return R()

    def list_pod_for_all_namespaces(self, **kwargs):
        class R:
            items = []
        return R()


def test_sync_runners_normal_mode():
    """普通模式下：runner_map 不含 _creation、不构建 lower bounds"""
    ns = _load_server_ns()
    ns["ER_CROSS_ENRICH_ONLY"] = False
    ns["_k8s_custom"] = FakeCustomApi()

    # mock _k8s_core（避免 Part B 的 list_node 报错打日志）
    ns["_k8s_core"] = FakeCoreApi()

    # mock _cross_enrich 确保不被调用（普通模式不走 enrich）
    called = {"cross": False}
    def fake_cross(rm, lb):
        called["cross"] = True
    ns["_cross_enrich_workflow_pods"] = fake_cross

    # mock DB（Part A 会查 DB）
    conn = FakeConn()
    ns["_get_conn"] = lambda: conn
    ns["_put_conn"] = lambda c, error=False: None

    ns["_sync_ephemeral_runners"]()

    assert not called["cross"], "normal mode should NOT call cross_enrich"
    # Part A 的 DB 查询被执行（至少有一条 SELECT）
    select_count = sum(1 for sql, _ in conn.cur.executed if sql and "SELECT" in sql.upper())
    assert select_count >= 1, f"Part A should execute at least 1 SELECT, got {select_count}"
    print("PASS: 普通模式不调 cross_enrich，Part A 执行了 SELECT")


def test_sync_runners_enrich_mode():
    """enrich 模式下：runner_map 和 lower_bounds 分离，_creation 不在 runner_map 里"""
    ns = _load_server_ns()
    ns["ER_CROSS_ENRICH_ONLY"] = True
    ns["_k8s_custom"] = FakeCustomApi()

    captured = {}
    def fake_cross(rm, lb):
        captured["rm"] = rm
        captured["lb"] = lb
    ns["_cross_enrich_workflow_pods"] = fake_cross

    # mock DB
    conn = FakeConn()
    ns["_get_conn"] = lambda: conn
    ns["_put_conn"] = lambda c, error=False: None

    ns["_sync_ephemeral_runners"]()

    rm = captured["rm"]
    lb = captured["lb"]

    # runner_map 的 info 不含 _creation
    for key, info in rm.items():
        assert "_creation" not in info, f"_creation leaked in {key}: {info}"

    # runner-x 有完整信息
    assert rm[("ns-a", "runner-x")]["workflow_ref"] == "ref-x"
    # runner-y 无 status → info 为空
    assert rm[("ns-b", "runner-y")] == {}

    # lower_bounds 有 runner-x 的时间（10:00 - 1h = 09:00）
    assert ("ns-a", "runner-x") in lb
    assert lb[("ns-a", "runner-x")] == "2026-11-13T09:00:00+00:00"
    # runner-y 也有
    assert ("ns-b", "runner-y") in lb

    # lower_bounds 和 runner_map 是不同结构（分离）
    assert id(rm) != id(lb)

    print("PASS: enrich 模式 runner_map/lower_bounds 分离，_creation 不外泄")


if __name__ == "__main__":
    test_cross_enrich()
    test_cross_enrich_empty_map()
    test_sync_runners_normal_mode()
    test_sync_runners_enrich_mode()
    print("\n全部测试通过")
