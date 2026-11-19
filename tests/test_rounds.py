"""分轮汇总 API 测试。

覆盖：开轮（同时只收集一轮）、哨点整份上报/更正（空清单=零例、
非法更正保留上次）、缺报/例数查询、结案门槛（报齐且总数 <=40）、
结案生成提交版本并更新供数、全部零例登记版本并撤下旧副本、
结案后拒绝更正、重复结案返回已登记版本、结案原子性、重启持久化，
以及原整批提交入口照常使用。
"""
import os

import pytest
from fastapi.testclient import TestClient

os.environ["APP_DB_PATH"] = ":memory:"  # 避免模块级 app 落盘

from app.main import create_app  # noqa: E402
from app.models import ReportIn, RoundOpenIn, SubmissionIn  # noqa: E402
from app.store import Store  # noqa: E402


def case(cid, age, zipc, disease):
    return {"case_id": cid, "age": age, "zip": zipc, "disease": disease}


# 两个哨点的可发布病例：同一（年龄段 × 邮区）组、两种病种
S1_CASES = [case("A1", 21, "11111", "flu"), case("A2", 22, "11112", "cold")]
S2_CASES = [case("B1", 23, "11113", "flu"), case("B2", 24, "11114", "cold")]
BATCH_CASES = [
    case("C1", 21, "11111", "flu"),
    case("C2", 22, "11112", "flu"),
    case("C3", 23, "11113", "cold"),
    case("C4", 24, "11114", "cold"),
]


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


def open_round(client, sentinels=("S1", "S2"), k=2):
    return client.post("/api/rounds", json={"k": k, "sentinels": list(sentinels)})


def report(client, sentinel, cases):
    return client.post(
        "/api/rounds/current/reports", json={"sentinel": sentinel, "cases": cases}
    )


def close(client):
    return client.post("/api/rounds/current/close")


def round_status(client):
    return client.get("/api/rounds/current")


# ------------------------------------------------------------ 开轮
def test_open_round(client):
    r = open_round(client, k=3)
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "open"
    assert body["k"] == 3
    assert body["expected_sentinels"] == ["S1", "S2"]
    assert body["reports"] == {}
    assert body["missing_sentinels"] == ["S1", "S2"]
    assert body["total_cases"] == 0
    assert body["all_reported"] is False
    assert body["closeable"] is False
    assert body["submission_version"] is None


def test_only_one_round_at_a_time(client):
    r = open_round(client)
    assert r.status_code == 201
    first = r.json()
    # 已有开放轮次时不得再开新轮
    r = open_round(client)
    assert r.status_code == 409
    assert r.json()["round_id"] == first["round_id"]
    # 结案后可开新轮
    report(client, "S1", [])
    report(client, "S2", [])
    assert close(client).status_code == 201
    r = open_round(client)
    assert r.status_code == 201
    assert r.json()["round_id"] > first["round_id"]


def test_open_round_validation(client):
    assert client.post("/api/rounds", json={"k": 2, "sentinels": []}).status_code == 422
    assert client.post("/api/rounds", json={"k": 2, "sentinels": ["S1", "S1"]}).status_code == 422
    assert client.post("/api/rounds", json={"k": 2, "sentinels": ["  "]}).status_code == 422
    assert client.post("/api/rounds", json={"k": 1, "sentinels": ["S1"]}).status_code == 422
    assert client.post("/api/rounds", json={"k": 6, "sentinels": ["S1"]}).status_code == 422
    # 非法开轮不占用轮次
    assert open_round(client).status_code == 201


def test_round_endpoints_without_round(client):
    assert round_status(client).status_code == 404
    assert close(client).status_code == 404
    assert report(client, "S1", []).status_code == 404


# ------------------------------------------------------------ 上报与更正
def test_report_and_query_counts(client):
    open_round(client)
    r = report(client, "S1", S1_CASES)
    assert r.status_code == 201
    body = r.json()
    assert body["case_count"] == 2
    assert body["zero_cases"] is False
    assert body["replaced"] is False
    assert body["missing_sentinels"] == ["S2"]

    st = round_status(client).json()
    assert st["reports"] == {"S1": 2}
    assert st["missing_sentinels"] == ["S2"]
    assert st["total_cases"] == 2
    assert st["closeable"] is False

    # 空清单 = 零例：已上报，不算缺报
    r = report(client, "S2", [])
    assert r.status_code == 201
    assert r.json()["zero_cases"] is True

    st = round_status(client).json()
    assert st["reports"] == {"S1": 2, "S2": 0}
    assert st["missing_sentinels"] == []
    assert st["all_reported"] is True
    assert st["total_cases"] == 2
    assert st["closeable"] is True


def test_report_unknown_sentinel(client):
    open_round(client)
    r = report(client, "UNKNOWN", S1_CASES)
    assert r.status_code == 400
    assert round_status(client).json()["reports"] == {}


def test_correction_replaces_previous(client):
    open_round(client)
    report(client, "S1", S1_CASES)
    new_list = [case("A9", 30, "55555", "flu"), case("A10", 31, "55556", "cold")]
    r = report(client, "S1", new_list)
    assert r.status_code == 200  # 更正已上报过的哨点
    assert r.json()["replaced"] is True
    assert round_status(client).json()["reports"] == {"S1": 2}


def test_invalid_correction_keeps_last_report(client):
    open_round(client)
    report(client, "S1", S1_CASES)
    bad_lists = [
        [case("A", 1, "12345", "flu"), case("A", 2, "12345", "cold")],  # 清单内病例号重复
        [case("A", 101, "12345", "flu")],                                # 年龄 >100
        [case("A", -1, "12345", "flu")],                                 # 年龄 <0
        [case("A", 10, "1234", "flu")],                                  # 邮编不足5位
        [case("A", 10, "12a45", "flu")],                                 # 邮编含非数字
        [case("A", 10, "12345", "")],                                    # 病种为空
    ]
    for bad in bad_lists:
        r = report(client, "S1", bad)
        assert r.status_code == 422, bad
        assert round_status(client).json()["reports"] == {"S1": 2}


# ------------------------------------------------------------ 结案门槛
def test_close_requires_all_reported(client):
    open_round(client)
    report(client, "S1", S1_CASES)
    r = close(client)
    assert r.status_code == 409
    assert r.json()["missing_sentinels"] == ["S2"]
    # 未结案，轮次仍开放
    assert round_status(client).json()["status"] == "open"


def test_close_requires_total_within_40(client):
    open_round(client)
    many1 = [case(f"A{i}", 20 + i % 5, f"1111{i % 10}", "flu" if i % 2 else "cold")
             for i in range(21)]
    many2 = [case(f"B{i}", 20 + i % 5, f"1111{i % 10}", "flu" if i % 2 else "cold")
             for i in range(21)]
    assert report(client, "S1", many1).status_code == 201
    assert report(client, "S2", many2).status_code == 201
    r = close(client)
    assert r.status_code == 409
    assert r.json()["total_cases"] == 42
    # 更正到 40 条以内后可结案
    assert report(client, "S2", S2_CASES).status_code == 200
    r = close(client)
    assert r.status_code == 201
    assert r.json()["status"] == "published"
    assert r.json()["total_cases"] == 23


# ------------------------------------------------------------ 结案与供数
def test_close_publishes_merged_version(client):
    open_round(client, k=2)
    report(client, "S1", S1_CASES)
    report(client, "S2", S2_CASES)
    r = close(client)
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "published"
    assert body["k"] == 2
    assert body["total_cases"] == 4
    assert body["retained"] == 4
    assert body["round_id"]

    pub = client.get("/api/publication").json()
    assert pub["version"] == body["version"]
    assert len(pub["rows"]) == 4
    # 轮次已关闭并关联到登记版本
    st = round_status(client).json()
    assert st["status"] == "closed"
    assert st["submission_version"] == body["version"]


def test_same_case_id_from_different_sentinels_counted_separately(client):
    open_round(client, k=2)
    # 两个哨点上报完全相同的病例号
    report(client, "S1", [case("X1", 21, "11111", "flu"), case("X2", 22, "11112", "cold")])
    report(client, "S2", [case("X1", 23, "11113", "flu"), case("X2", 24, "11114", "cold")])
    r = close(client)
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "published"
    assert body["total_cases"] == 4
    assert body["retained"] == 4
    assert len(client.get("/api/publication").json()["rows"]) == 4


def test_close_all_zero_registers_version_and_withdraws(client):
    # 先通过整批提交发布一版
    r = client.post("/api/submissions", json={"k": 2, "cases": BATCH_CASES})
    assert r.json()["status"] == "published"
    old_version = r.json()["version"]
    assert client.get("/api/publication").status_code == 200

    open_round(client, k=4)
    report(client, "S1", [])
    report(client, "S2", [])
    r = close(client)
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "withdrawn"
    assert body["total_cases"] == 0
    assert body["k"] == 4
    assert body["version"] > old_version
    assert "零例" in body["reason"]

    # 旧副本被撤下
    d = client.get("/api/publication")
    assert d.status_code == 404
    assert d.json()["state"] == "withdrawn"
    assert client.get("/api/status").json()["state"] == "withdrawn"
    # 零例版本已登记在提交历史
    hist = client.get("/api/submissions").json()["submissions"]
    entry = [h for h in hist if h["version"] == body["version"]]
    assert entry and entry[0]["status"] == "withdrawn" and entry[0]["k"] == 4


def test_close_infeasible_stays_withdrawn(client):
    open_round(client, k=2)
    report(client, "S1", [case("A1", 10, "11111", "flu")])
    report(client, "S2", [case("B1", 50, "22222", "cold"), case("B2", 90, "33333", "flu")])
    r = close(client)
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "withdrawn"
    assert body["reason"]
    assert client.get("/api/publication").status_code == 404


def test_close_uses_round_k(client):
    open_round(client, k=5)
    report(client, "S1", S1_CASES)
    report(client, "S2", S2_CASES)
    # 合并 4 条同组两种病：k=2 可发布，k=5 不可行
    r = close(client)
    assert r.status_code == 201
    body = r.json()
    assert body["k"] == 5
    assert body["status"] == "withdrawn"


def test_repeat_close_returns_registered_version(client):
    open_round(client)
    report(client, "S1", S1_CASES)
    report(client, "S2", S2_CASES)
    first = close(client).json()
    r = close(client)
    assert r.status_code == 200
    again = r.json()
    assert again["version"] == first["version"]
    assert again["status"] == first["status"]
    assert again["round_id"] == first["round_id"]
    # 供数未被重复结案改变
    assert client.get("/api/publication").json()["version"] == first["version"]
    # 提交历史只登记了一个版本
    hist = client.get("/api/submissions").json()["submissions"]
    assert [h["version"] for h in hist] == [first["version"]]


def test_report_rejected_after_close(client):
    open_round(client)
    report(client, "S1", S1_CASES)
    report(client, "S2", S2_CASES)
    close(client)
    r = report(client, "S1", [case("A9", 30, "55555", "flu")])
    assert r.status_code == 409
    # 已发布内容不受结案后更正影响
    assert len(client.get("/api/publication").json()["rows"]) == 4


def test_pre_close_correction_is_included(client):
    open_round(client)
    report(client, "S1", [case("A1", 30, "55551", "flu"), case("A2", 31, "55552", "cold")])
    report(client, "S2", S2_CASES)
    # 结案前整份更正 S1：改报 111** 邮区
    report(client, "S1", S1_CASES)
    r = close(client)
    assert r.status_code == 201
    assert r.json()["status"] == "published"
    pub = client.get("/api/publication").json()
    assert len(pub["rows"]) == 4
    assert all(row["zip"] == "111**" for row in pub["rows"])


# ------------------------------------------------------------ 与整批入口共存
def test_batch_submission_still_works_with_open_round(client):
    open_round(client)
    report(client, "S1", S1_CASES)
    r = client.post("/api/submissions", json={"k": 2, "cases": BATCH_CASES})
    assert r.status_code == 201
    assert r.json()["status"] == "published"
    assert client.get("/api/publication").json()["version"] == r.json()["version"]
    # 轮次不受整批提交影响
    st = round_status(client).json()
    assert st["status"] == "open"
    assert st["reports"] == {"S1": 2}


def test_round_close_replaces_batch_publication(client):
    r = client.post("/api/submissions", json={"k": 2, "cases": BATCH_CASES})
    assert r.json()["status"] == "published"
    open_round(client)
    report(client, "S1", S1_CASES)
    report(client, "S2", S2_CASES)
    body = close(client).json()
    assert body["status"] == "published"
    pub = client.get("/api/publication").json()
    assert pub["version"] == body["version"]
    assert len(pub["rows"]) == 4


# ------------------------------------------------------------ 重启持久化
def test_round_state_survives_restart(tmp_path):
    db = str(tmp_path / "rounds.db")
    with TestClient(create_app(db)) as c1:
        open_round(c1, k=3)
        report(c1, "S1", S1_CASES)

    # 模拟重启：未结案轮次的上报进度保留
    with TestClient(create_app(db)) as c2:
        st = c2.get("/api/rounds/current").json()
        assert st["status"] == "open"
        assert st["k"] == 3
        assert st["reports"] == {"S1": 2}
        assert st["missing_sentinels"] == ["S2"]
        report(c2, "S2", S2_CASES)
        body = close(c2).json()
        assert body["status"] == "published"
        assert body["k"] == 3

    # 结案状态与登记版本重启后仍可查
    with TestClient(create_app(db)) as c3:
        st = c3.get("/api/rounds/current").json()
        assert st["status"] == "closed"
        assert st["submission_version"] == body["version"]
        r = c3.post("/api/rounds/current/close")
        assert r.status_code == 200
        assert r.json()["version"] == body["version"]
        assert c3.get("/api/publication").json()["version"] == body["version"]


# ------------------------------------------------------------ 结案原子性
def test_close_failure_preserves_reports_and_publication(tmp_path, monkeypatch):
    import app.store as store_mod

    store = Store(str(tmp_path / "fail.db"))
    # 先通过整批提交发布一版，作为“原供数”
    sub_body, status = store.submit(
        SubmissionIn.model_validate({"k": 2, "cases": BATCH_CASES})
    )
    assert status == 201 and sub_body["status"] == "published"
    pub_before, pub_status = store.publication()
    assert pub_status == 200

    store.open_round(RoundOpenIn.model_validate({"k": 2, "sentinels": ["S1", "S2"]}))
    store.submit_report(ReportIn.model_validate({"sentinel": "S1", "cases": S1_CASES}))
    store.submit_report(ReportIn.model_validate({"sentinel": "S2", "cases": S2_CASES}))

    def boom(*args, **kwargs):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(store_mod, "evaluate", boom)
    with pytest.raises(RuntimeError):
        store.close_round()

    # 本轮上报保留、轮次仍开放
    st, st_status = store.round_status()
    assert st_status == 200
    assert st["status"] == "open"
    assert st["reports"] == {"S1": 2, "S2": 2}
    # 原供数不变
    pub_after, pub_status = store.publication()
    assert pub_status == 200
    assert pub_after == pub_before
    # 未登记任何新版本
    assert [h["version"] for h in store.history()] == [sub_body["version"]]

    # 故障恢复后结案成功
    monkeypatch.undo()
    closed, status = store.close_round()
    assert status == 201
    assert closed["status"] == "published"
    store.close()
