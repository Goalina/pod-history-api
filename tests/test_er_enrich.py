"""测试 _cross_enrich_workflow_pods 和 runner_map 构建（review 建议）。

覆盖：
1. _creation 不外泄到 extend_env_comments（用平行结构，不污染 runner_map）
2. 空 info 不进入 info_list（不会把已补写的数据覆盖成 {}）
3. _namespace 过滤生效（不同 ns 同名 runner 不串写）
"""
import os, json

os.environ["DATABASE_URL"] = "postgresql://pod_history:test@127.0.0.1:55496/pod_history"
os.environ["MODE"] = "api"
os.environ["CLUSTER_ID"] = "karmada-cp"

import server


class FakeCur:
    def __init__(self):
        self.executed = []
        self.rowcount = 0

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


def test_cross_enrich():
    conn = FakeConn()
    server._get_conn = lambda: conn
    server._put_conn = lambda c, error=False: None

    # 模拟 runner_map + lower_bounds
    runner_map = {
        ("ns1", "runner-a"): {"workflow_ref": "ref-a", "job_display_name": "job-a"},
        ("ns2", "runner-b"): {},  # 空 info → 应跳过
        ("ns3", "runner-c"): {"workflow_ref": "ref-c"},
    }
    lower_bounds = {("ns1", "runner-a"): "2026-11-13T10:00:00+00:00"}

    server._cross_enrich_workflow_pods(runner_map, lower_bounds)

    # 验证只执行了一条 executemany
    assert len(conn.cur.executed) == 1, f"expected 1, got {len(conn.cur.executed)}"
    sql, params = conn.cur.executed[0]

    # 验证 SQL 含 _namespace 过滤
    assert "_namespace = %s" in sql, "SQL should filter by _namespace"

    # 验证参数结构：(info_json, wf_name, ns, lower, info_json)
    assert len(params[0]) == 5, f"expected 5 params, got {len(params[0])}"

    # 验证空 info 的 runner-b 没有出现在参数里
    names = [p[1] for p in params]
    assert "runner-b-workflow" not in names, "empty info should be skipped"
    assert "runner-a-workflow" in names
    assert "runner-c-workflow" in names

    # 验证 runner-a 的 info JSON 不含 _creation
    for p in params:
        if p[1] == "runner-a-workflow":
            info = json.loads(p[0])
            assert "_creation" not in info, f"_creation leaked: {info}"
            assert info["workflow_ref"] == "ref-a"

    # 验证 lower_bounds 正确传递
    for p in params:
        if p[1] == "runner-a-workflow":
            assert p[3] == "2026-11-13T10:00:00+00:00"
        if p[1] == "runner-c-workflow":
            assert p[3] == ""  # 没有时间窗口

    # 验证 commit 被调用
    assert conn.committed

    print("PASS: _creation 不外泄、空 info 跳过、_namespace 过滤、lower_bounds 正确")


def test_runner_map_no_creation():
    """验证 _er_to_info 返回的 dict 不含 _creation（非 ER_CROSS_ENRICH_ONLY 模式）"""
    server.ER_CROSS_ENRICH_ONLY = False
    er = {
        "metadata": {"name": "runner-x", "namespace": "ns", "creationTimestamp": "2026-11-13T10:00:00Z"},
        "status": {"jobWorkflowRef": "ref", "jobDisplayName": "job"},
    }
    info = server._er_to_info(er)
    assert "_creation" not in info, f"_creation should not be in _er_to_info output: {info}"
    assert info.get("workflow_ref") == "ref"
    print("PASS: _er_to_info 不含 _creation")


def test_er_to_info_empty_status():
    """ER 无 status 字段时返回空 dict"""
    er = {"metadata": {"name": "runner-y"}, "status": {}}
    info = server._er_to_info(er)
    assert info == {}, f"expected empty dict, got {info}"
    print("PASS: 无 status 的 ER 返回空 dict")


if __name__ == "__main__":
    test_cross_enrich()
    test_runner_map_no_creation()
    test_er_to_info_empty_status()
    print("\n全部测试通过")
