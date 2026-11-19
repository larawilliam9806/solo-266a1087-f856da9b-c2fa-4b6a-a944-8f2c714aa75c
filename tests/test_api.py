import os
import threading

import pytest
from fastapi.testclient import TestClient

os.environ["APP_DB_PATH"] = ":memory:"  # 避免模块级 app 落盘

from app.main import create_app  # noqa: E402
from app.store import Store  # noqa: E402
from app.models import SubmissionIn  # noqa: E402


def case(cid, age, zipc, disease):
    return {"case_id": cid, "age": age, "zip": zipc, "disease": disease}


# 一组可稳定发布的数据：单一年龄段/邮区，两种病种
GOOD_CASES = [
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


def submit(client, cases, k=2):
    return client.post("/api/submissions", json={"k": k, "cases": cases})


# ------------------------------------------------------------ 基本发布与下载
def test_publish_and_download(client):
    r = submit(client, GOOD_CASES, k=2)
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "published"
    assert body["params"] == {"age_band_years": 5, "zip_prefix_digits": 3}
    assert body["retained"] == 4 and body["suppressed"] == 0

    d = client.get("/api/publication")
    assert d.status_code == 200
    pub = d.json()
    assert pub["version"] == body["version"]
    assert pub["k"] == 2
    assert len(pub["rows"]) == 4
    # 副本只含泛化字段与病种
    for row in pub["rows"]:
        assert set(row.keys()) == {"age_band", "zip", "disease"}
        assert row["age_band"] == "20-24"
        assert row["zip"] == "111**"
    # 每个可见组合 >= k 且 >= 2 种病种
    groups = {}
    for row in pub["rows"]:
        groups.setdefault((row["age_band"], row["zip"]), []).append(row["disease"])
    for members in groups.values():
        assert len(members) >= pub["k"]
        assert len(set(members)) >= 2


def test_download_empty_before_any_submission(client):
    d = client.get("/api/publication")
    assert d.status_code == 404
    assert d.json()["state"] == "empty"


# ------------------------------------------------------------ 非法提交不影响旧版
INVALID_CASES = [
    [case("A", 1, "12345", "flu"), case("A", 2, "12345", "cold")],  # 病例号重复
    [case("A", 101, "12345", "flu")],                                # 年龄 >100
    [case("A", -1, "12345", "flu")],                                 # 年龄 <0
    [case("A", 10, "1234", "flu")],                                  # 邮编不足5位
    [case("A", 10, "123456", "flu")],                                # 邮编超过5位
    [case("A", 10, "12a45", "flu")],                                 # 邮编含非数字
    [case("A", 10, "12345", "  ")],                                  # 病种空白
    [case("A", 10, "12345", "")],                                    # 病种为空
]


def test_invalid_submissions_do_not_affect_old_version(client):
    submit(client, GOOD_CASES, k=2)
    before = client.get("/api/publication").json()

    for cases in INVALID_CASES:
        r = submit(client, cases, k=2)
        assert r.status_code == 422, cases
        assert client.get("/api/publication").json() == before

    for bad_k in (0, 1, 6, 100):
        r = submit(client, GOOD_CASES, k=bad_k)
        assert r.status_code == 422
        assert client.get("/api/publication").json() == before

    # 空批次与超过 40 条
    assert submit(client, [], k=2).status_code == 422
    too_many = [case(f"X{i}", 20 + i % 5, "11111", "flu" if i % 2 else "cold")
                for i in range(41)]
    assert submit(client, too_many, k=2).status_code == 422
    assert client.get("/api/publication").json() == before


def test_max_batch_40_accepted(client):
    cases = [case(f"X{i}", 20 + i % 5, f"1111{i % 10}", "flu" if i % 2 else "cold")
             for i in range(40)]
    r = submit(client, cases, k=5)
    assert r.status_code == 201
    assert r.json()["status"] == "published"
    assert r.json()["retained"] == 40


def test_boundaries_age_and_zip(client):
    cases = [
        case("A", 0, "00123", "flu"),
        case("B", 100, "00124", "cold"),
        case("C", 0, "00125", "cold"),
        case("D", 100, "00126", "flu"),
    ]
    r = submit(client, cases, k=2)
    assert r.status_code == 201
    assert r.json()["status"] == "published"


# ------------------------------------------------------------ 修订与撤下
def test_new_valid_submission_withdraws_old(client):
    v1 = submit(client, GOOD_CASES, k=2).json()
    assert client.get("/api/publication").json()["version"] == v1["version"]

    new_cases = [
        case("D1", 40, "22221", "asthma"),
        case("D2", 41, "22222", "asthma"),
        case("D3", 42, "22223", "flu"),
        case("D4", 43, "22224", "flu"),
    ]
    v2 = submit(client, new_cases, k=2).json()
    assert v2["status"] == "published"
    assert v2["version"] > v1["version"]

    pub = client.get("/api/publication").json()
    assert pub["version"] == v2["version"]
    assert all(row["zip"] == "222**" for row in pub["rows"])
    assert {row["disease"] for row in pub["rows"]} == {"asthma", "flu"}
    assert client.get("/api/status").json()["state"] == "published"


def test_infeasible_new_version_stays_withdrawn_with_reason(client):
    submit(client, GOOD_CASES, k=2)
    # 3 条互不同组的散点：任何参数下都需抑制 3 条（>2）
    scattered = [
        case("S1", 10, "11111", "flu"),
        case("S2", 50, "22222", "cold"),
        case("S3", 90, "33333", "flu"),
    ]
    r = submit(client, scattered, k=2)
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "withdrawn"
    assert body["reason"]

    d = client.get("/api/publication")
    assert d.status_code == 404
    assert d.json()["state"] == "withdrawn"
    assert d.json()["reason"]
    assert client.get("/api/status").json()["state"] == "withdrawn"

    # 再提交有效可发布清单后恢复发布
    v = submit(client, GOOD_CASES, k=2).json()
    assert v["status"] == "published"
    assert client.get("/api/publication").json()["version"] == v["version"]


def test_disease_diversity_required(client):
    # 同组 2 条同病种 -> 不达标
    same_disease = [case("A", 30, "12345", "flu"), case("B", 31, "12346", "flu")]
    assert submit(client, same_disease, k=2).json()["status"] == "withdrawn"
    # 加入第二种病种后达标
    diverse = same_disease + [case("C", 32, "12347", "cold")]
    assert submit(client, diverse, k=2).json()["status"] == "published"


# ------------------------------------------------------------ 参数选择与抑制
def test_prefer_finer_age_band_on_tie(client):
    # 5/10/20 岁段均可行且 0 抑制 -> 选最细的 5 岁段
    cases = [
        case("A", 20, "45678", "flu"),
        case("B", 21, "45678", "cold"),
        case("C", 22, "45678", "flu"),
        case("D", 23, "45678", "cold"),
    ]
    body = submit(client, cases, k=2).json()
    assert body["status"] == "published"
    assert body["params"]["age_band_years"] == 5


def test_coarser_band_when_finer_infeasible(client):
    # 5 岁段下两条记录分属不同组（各仅 1 条），10 岁段合并后达标
    cases = [case("A", 21, "45678", "flu"), case("B", 26, "45678", "cold")]
    body = submit(client, cases, k=2).json()
    assert body["status"] == "published"
    assert body["params"]["age_band_years"] == 10
    assert body["retained"] == 2


def test_prefer_longer_zip_prefix_on_tie(client):
    # 前 3 位不同、前 2 位相同 -> 前缀 3 不可行，应选前缀 2 而非 0
    cases = [case("A", 30, "12200", "flu"), case("B", 30, "12300", "cold")]
    body = submit(client, cases, k=2).json()
    assert body["status"] == "published"
    assert body["params"] == {"age_band_years": 5, "zip_prefix_digits": 2}
    pub = client.get("/api/publication").json()
    assert all(row["zip"] == "12***" for row in pub["rows"])


def test_suppress_up_to_two_outliers(client):
    base = [
        case("G1", 30, "55551", "flu"),
        case("G2", 31, "55552", "flu"),
        case("G3", 32, "55553", "cold"),
        case("G4", 33, "55554", "cold"),
    ]
    out1 = case("O1", 90, "99999", "flu")
    out2 = case("O2", 10, "88888", "cold")
    out3 = case("O3", 60, "77777", "flu")

    body = submit(client, base + [out1], k=2).json()
    assert body["status"] == "published"
    assert body["suppressed"] == 1 and body["retained"] == 4
    assert body["suppressed_case_ids"] == ["O1"]
    assert len(client.get("/api/publication").json()["rows"]) == 4

    body = submit(client, base + [out1, out2], k=2).json()
    assert body["status"] == "published"
    assert body["suppressed"] == 2
    assert sorted(body["suppressed_case_ids"]) == ["O1", "O2"]

    # 3 条离群 -> 超过抑制上限，撤下
    body = submit(client, base + [out1, out2, out3], k=2).json()
    assert body["status"] == "withdrawn"


def test_prefer_more_retained_over_finer_params(client):
    # 5 岁段需抑制 1 条（保留 3），10 岁段 0 抑制（保留 4）-> 选 10 岁段
    cases = [
        case("A", 20, "45678", "flu"),
        case("B", 21, "45678", "cold"),
        case("C", 26, "45678", "flu"),
        case("D", 27, "45678", "cold"),
        case("E", 90, "99999", "flu"),  # 离群，任何参数下都被抑制 -> 不计入差异
    ]
    # 5 岁段: (20-24,456) 2条达标, (25-29,456) 2条达标, 离群抑制1 -> 保留4
    # 构造让 5 岁段出现额外小组：把 C/D 移到同一段但病种单一
    cases[2] = case("C", 25, "45678", "flu")
    cases[3] = case("D", 26, "45678", "flu")
    # 5 岁段: (25-29,456) 两条同病种 -> 整组抑制2 + 离群1 = 3 > 2 不可行
    # 10 岁段: (20-29,456) 4条两种病 -> 仅抑制离群1 -> 保留4
    body = submit(client, cases, k=2).json()
    assert body["status"] == "published"
    assert body["params"]["age_band_years"] == 10
    assert body["retained"] == 4 and body["suppressed"] == 1


# ------------------------------------------------------------ 重启持久化
def test_restart_keeps_state(tmp_path):
    db = str(tmp_path / "restart.db")
    with TestClient(create_app(db)) as c1:
        assert submit(c1, GOOD_CASES, k=2).json()["status"] == "published"
        v = c1.get("/api/publication").json()["version"]

    # 模拟重启：同一数据库文件新建应用
    with TestClient(create_app(db)) as c2:
        pub = c2.get("/api/publication")
        assert pub.status_code == 200
        assert pub.json()["version"] == v

    # 撤下状态重启后也不恢复
    db2 = str(tmp_path / "restart2.db")
    scattered = [
        case("S1", 10, "11111", "flu"),
        case("S2", 50, "22222", "cold"),
        case("S3", 90, "33333", "flu"),
    ]
    with TestClient(create_app(db2)) as c1:
        submit(c1, GOOD_CASES, k=2)
        assert submit(c1, scattered, k=2).json()["status"] == "withdrawn"
        assert c1.get("/api/publication").status_code == 404

    with TestClient(create_app(db2)) as c2:
        d = c2.get("/api/publication")
        assert d.status_code == 404
        assert d.json()["state"] == "withdrawn"
        assert d.json()["reason"]


# ------------------------------------------------------------ 并发一致性
def _payload(k, cases):
    return SubmissionIn.model_validate({"k": k, "cases": cases})


def test_interleaved_submit_and_download_never_stale(tmp_path):
    store = Store(str(tmp_path / "conc.db"))
    feasible = _payload(2, GOOD_CASES)
    infeasible = _payload(2, [
        case("S1", 10, "11111", "flu"),
        case("S2", 50, "22222", "cold"),
        case("S3", 90, "33333", "flu"),
    ])
    errors = []
    stop = threading.Event()

    def writer():
        i = 0
        while not stop.is_set():
            store.submit(feasible if i % 2 == 0 else infeasible)
            i += 1

    def reader():
        while not stop.is_set():
            body, status = store.publication()
            if status == 200:
                # 副本与参数必须来自同一完整版本
                if len(body["rows"]) != body["retained"]:
                    errors.append(f"row count mismatch: {body}")
                hist = {h["version"]: h for h in store.history()}
                sub = hist.get(body["version"])
                if sub is None or sub["status"] != "published":
                    errors.append(f"stale or unknown version served: {body['version']}")
                if sub and sub["k"] != body["k"]:
                    errors.append("k mismatch")
            else:
                if body["state"] not in ("withdrawn", "empty"):
                    errors.append(f"bad 404 body: {body}")

    threads = [threading.Thread(target=writer) for _ in range(2)] + \
              [threading.Thread(target=reader) for _ in range(3)]
    for t in threads:
        t.start()
    import time
    time.sleep(1.0)
    stop.set()
    for t in threads:
        t.join()
    store.close()
    assert not errors


def test_case_ids_may_reappear_across_versions(client):
    submit(client, GOOD_CASES, k=2)
    # 同一批病例号在新版本中再次出现（修订清单）不应报错
    r = submit(client, GOOD_CASES, k=3)
    assert r.status_code == 201
    assert r.json()["status"] == "published"
