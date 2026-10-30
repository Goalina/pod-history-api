"""
_extract_record 的来源识别 / extend_env_comments 提取测试（不依赖 DB / K8s）。

运行：
    python3 tests/test_source_extract.py      # 直接跑，全绿打印 OK
    pytest tests/test_source_extract.py       # 或用 pytest

server.py 在模块级会执行 _init_db()（连 Postgres）和 _init_k8s()，直接 import 需要真实环境。
这里把源码读进来、注释掉这两处模块级副作用后 exec 到独立命名空间，只取出纯函数 _extract_record 做单测。
"""
import os
import types
import json

# MODE=api 使模块级的 _init_k8s() 被 MODE 守卫跳过；_init_db() 单独注释掉
os.environ.setdefault("MODE", "api")
os.environ.setdefault("DATABASE_URL", "postgresql://unused@localhost/unused")

_SERVER_PY = os.path.join(os.path.dirname(__file__), "..", "server.py")


def _load_extract_record():
    src = open(_SERVER_PY, encoding="utf-8").read()
    src = src.replace("\n_init_db()\n", "\n# _init_db()  # neutralized for test\n")
    ns = {"__name__": "server_under_test"}
    exec(compile(src, "server.py", "exec"), ns)  # noqa: S102
    return ns["_extract_record"]


_extract_record = _load_extract_record()


def _ns(**kw):
    return types.SimpleNamespace(**kw)


def _make_pod(annotations, labels, *, name="p", phase="Running"):
    reqs = {"cpu": "39", "memory": "225Gi", "huawei.com/ascend-1980": "2"}
    container = _ns(resources=_ns(requests=reqs, limits=None), image="img")
    meta = _ns(annotations=annotations, labels=labels, name=name, namespace="vllm-project",
               uid="uid-" + name, creation_timestamp=None, owner_references=[])
    spec = _ns(containers=[container], node_selector={}, node_name="n1")
    status = _ns(phase=phase, start_time=None, container_statuses=[], host_ip="10.0.0.1")
    return _ns(metadata=meta, spec=spec, status=status)


# 真实 Buildkite pod（vllm-project / vllm-omni-npu-ci）的完整声明字段
_BK_ANNOTATIONS = {
    "buildkite.com/pipeline-slug": "vllm-omni-npu-ci",
    "buildkite.com/build-url": "https://buildkite.com/vllm/vllm-omni-npu-ci/builds/10236",
    "buildkite.com/build-branch": "ischencheng:perf/qwen3-omni-mixed-handoff",
    "buildkite.com/build-repo": "https://github.com/vllm-project/vllm-omni.git",
    "buildkite.com/job-url": "https://buildkite.com/vllm/vllm-omni-npu-ci/builds/10236#01a18a84-9166-47fd-9fe0-f13e70062bde",
    "buildkite.com/job-priority": "0",
    "huawei.com/AscendReal": "Ascend910-2,Ascend910-3",
}
_BK_LABELS = {
    "buildkite.com/job-uuid": "01a18a84-9166-47fd-9fe0-f13e70062bde",
    "buildkite.com/controller-id": "linux-aarch64-a3-agent-stack-k8s",
    "tag.buildkite.com/queue": "ascend-a3",
    "tag.buildkite.com/resource_class": "npu-2",
    "batch.kubernetes.io/job-name": "buildkite-01a18a84",  # 噪声 label，不应进 extend
}


def test_buildkite_source_and_fields():
    r = _extract_record(_make_pod(_BK_ANNOTATIONS, _BK_LABELS, name="buildkite-x"))
    assert r["source"] == "buildkite", r["source"]
    e = r["extend_env_comments"]
    assert e["pipeline_slug"] == "vllm-omni-npu-ci"
    assert e["build_url"].endswith("/builds/10236")
    assert e["build_number"] == "10236", e.get("build_number")
    assert e["build_branch"] == "ischencheng:perf/qwen3-omni-mixed-handoff"
    assert e["repository_url"] == "https://github.com/vllm-project/vllm-omni.git"
    assert e["job_url"].startswith("https://buildkite.com")
    assert e["job_priority"] == "0"
    assert e["job_uuid"] == "01a18a84-9166-47fd-9fe0-f13e70062bde"
    assert e["controller_id"] == "linux-aarch64-a3-agent-stack-k8s"
    assert e["queue"] == "ascend-a3"
    assert e["resource_class"] == "npu-2"


def test_buildkite_completeness():
    """所有 buildkite.com/* 与 tag.buildkite.com/* 声明字段的值都必须出现在 extend 里。"""
    r = _extract_record(_make_pod(_BK_ANNOTATIONS, _BK_LABELS, name="buildkite-x"))
    vals = set(r["extend_env_comments"].values())
    decl = {**_BK_ANNOTATIONS, **_BK_LABELS}
    bk_keys = [k for k in decl if k.startswith("buildkite.com/") or k.startswith("tag.buildkite.com/")]
    missing = [(k, decl[k]) for k in bk_keys if decl[k] not in vals]
    assert not missing, f"遗漏声明字段: {missing}"


def test_build_number_parse_variants():
    for url, expect in [
        ("https://buildkite.com/o/p/builds/42", "42"),
        ("https://buildkite.com/o/p/builds/42/", "42"),
        ("https://buildkite.com/o/p/builds/42?x=1", "42"),
    ]:
        ann = dict(_BK_ANNOTATIONS, **{"buildkite.com/build-url": url})
        r = _extract_record(_make_pod(ann, _BK_LABELS))
        assert r["extend_env_comments"]["build_number"] == expect, (url, r["extend_env_comments"].get("build_number"))


def test_regression_other_sources_unaffected():
    # github -workflow（_k8s_custom 未初始化 → extend {}，但 source 判定正确）
    r_gh = _extract_record(_make_pod({}, {"runner-pod": "true"}, name="foo-workflow"))
    assert r_gh["source"] == "github-action", r_gh["source"]

    # atomgit
    at_ann = {"octopus.io/job-run-id": "123", "octopus.io/job-name": "build", "octopus.io/pc-repository": "o/r"}
    r_at = _extract_record(_make_pod(at_ann, {}, name="atom-x"))
    assert r_at["source"] == "atomgit-action"
    assert r_at["extend_env_comments"]["job_display_name"] == "build"

    # unknown
    r_un = _extract_record(_make_pod({}, {}, name="plain"))
    assert r_un["source"] == "unknown" and r_un["extend_env_comments"] == {}


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"  PASS  {t.__name__}")
    print(f"\n{len(tests)} tests passed ✅")
