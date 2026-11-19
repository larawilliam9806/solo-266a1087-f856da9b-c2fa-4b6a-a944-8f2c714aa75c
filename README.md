# 哨点病例脱敏副本发布服务

研究方按年龄段和邮区分析哨点病例。病例清单可以**整批提交**，也可以按
**分轮汇总**（开轮 → 各哨点上报 → 结案）归集；平台按 k-匿名规则生成
**逐行脱敏副本**对外供数；清单一旦修订，旧副本立即撤下，不再供数。

技术栈：FastAPI + SQLite（WAL），支持 `docker compose` 一键启动。

## 快速开始

```bash
docker compose up --build        # 服务监听 http://localhost:8000
```

本地开发（Python 3.11+）：

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
APP_DB_PATH=./app.db .venv/bin/uvicorn app.main:app --reload
```

运行测试：

```bash
.venv/bin/pip install pytest httpx
.venv/bin/python -m pytest tests/ -q
```

## 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/submissions` | 整批提交新清单（≤40 条） |
| POST | `/api/rounds` | 开启分轮汇总（指定应报哨点与 k；同时只收集一轮） |
| GET  | `/api/rounds/current` | 当前轮次状态：缺报哨点、各已报哨点例数、总数 |
| POST | `/api/rounds/current/reports` | 哨点整份上报/更正（空清单表示零例） |
| POST | `/api/rounds/current/close` | 结案：登记提交版本并更新供数 |
| GET  | `/api/publication` | 公开下载当前发布的脱敏副本与参数 |
| GET  | `/api/status`      | 当前发布状态（published / withdrawn / empty） |
| GET  | `/api/submissions` | 提交历史（仅元数据，不含原始病例） |
| GET  | `/health`          | 健康检查 |

### POST /api/submissions

```json
{
  "k": 3,
  "cases": [
    {"case_id": "A1", "age": 34, "zip": "10001", "disease": "流感"},
    {"case_id": "A2", "age": 36, "zip": "10002", "disease": "哮喘"}
  ]
}
```

**合法性校验**（任一不满足即 HTTP 422，且不影响已发布的旧版本）：

- 整批 1～40 条；`k` 为 2～5 的整数；
- `case_id` 批内唯一、非空；`age` 为 0～100 的整数；
- `zip` 为五位数字；`disease` 非空。

**有效提交的处理**（在单个事务内完成）：

1. 立即撤下旧脱敏副本（删除旧副本行、清空发布指针）；
2. 在 9 种参数组合（年龄段 5/10/20 岁 × 邮区保留前 3/2/0 位）中寻找可行发布：
   - 每个可见（年龄段 × 邮区）组合须 ≥ k 条且含 ≥ 2 种病种；
   - 不达标组合只能整组抑制，抑制总数 ≤ 2 条，且至少保留一个组合；
   - 优先保留更多病例；并列时依次选更细的年龄段、更长的邮区前缀；
3. 可行则发布新版本（HTTP 201，`status=published`，含参数与被抑制病例号）；
   不可行则保持撤下（HTTP 201，`status=withdrawn`，附原因与逐组合诊断）。

### 分轮汇总

同一轮汇总要分清**哪些哨点报了零例、哪些还没报**，流程为：
开轮 → 各哨点上报/更正 → 查询进度 → 结案。

#### POST /api/rounds —— 开轮

```json
{"k": 3, "sentinels": ["东城哨点", "西城哨点"]}
```

- 指定本轮**应报哨点名单**与 `k`（2～5）；名单非空、去重、哨点名非空，
  否则 HTTP 422；
- **同时只收集一轮**：已有开放轮次时返回 HTTP 409；
- 成功返回 HTTP 201 与轮次状态（`round_id`、应报名单等）。

#### POST /api/rounds/current/reports —— 哨点上报/整份更正

```json
{"sentinel": "东城哨点", "cases": [{"case_id": "A1", "age": 34, "zip": "10001", "disease": "流感"}]}
```

- `cases` 为该哨点**当次完整清单**，再次上报即整份更正（先报 HTTP 201，
  更正 HTTP 200）；**空清单表示零例**；
- 病例字段校验与整批提交一致（清单内 `case_id` 唯一）；**无效更正返回
  HTTP 422，该哨点保留上一次上报**；
- 哨点不在本轮应报名单中返回 HTTP 400；**结案后拒绝上报与更正**
  （HTTP 409）。

#### GET /api/rounds/current —— 查询进度

```json
{
  "round_id": 1, "status": "open", "k": 3,
  "expected_sentinels": ["东城哨点", "西城哨点"],
  "reports": {"东城哨点": 4, "西城哨点": 0},
  "missing_sentinels": [],
  "total_cases": 4, "all_reported": true, "closeable": true,
  "submission_version": null
}
```

- `reports` 给出**各已报哨点的例数**（`0` 表示报了零例），`missing_sentinels`
  给出**缺报哨点**——两者严格区分；
- 无轮次时返回 HTTP 404。

#### POST /api/rounds/current/close —— 结案

- **报齐且总数 ≤ 40 条才可结案**：有缺报或总数超限时返回 HTTP 409
  （附缺报名单或当前总数）；
- 结案把各哨点当次清单合并，**登记一个提交版本**并按现有匿名规则更新供数
  （HTTP 201）；**不同哨点的同号病例分别计入**（版本内病例号以
  `哨点:病例号` 形式登记）；
- **全部为零例时也登记一版**（`status=withdrawn`），并撤下旧副本；
- **先于结案生效的更正必须纳入**；结案后更正被拒绝；
- 结案登记与供数更新**同时生效**（同一事务）：失败时整体回滚，本轮上报
  保留、轮次保持开放、原供数不变；
- **重复结案返回已登记的版本**（HTTP 200），不会重复登记。

### GET /api/publication

- 已发布：HTTP 200，返回同一完整版本的 `version`、`k`、`params`
  （`age_band_years`、`zip_prefix_digits`）、计数与逐行副本；
  副本每行只含泛化字段与病种，如 `{"age_band": "30-39", "zip": "100**", "disease": "流感"}`；
- 已撤下/无数据：HTTP 404，附撤下原因。

## 一致性设计

- **不过期发布**：发布在提交/结案请求内同步完成，写路径持全局可重入锁并以
  `BEGIN IMMEDIATE` 单事务提交，发布与再次提交/结案交错时不会出现过期清单；
- **不撕裂下载**：读路径持同一把锁，副本行与参数始终来自同一完整版本；
- **结案原子性**：结案把“登记提交版本 + 更新供数 + 关闭轮次”放在同一事务，
  任一步失败整体回滚——本轮上报保留、轮次保持开放、原供数不变；
- **重启不恢复**：发布指针持久化于 SQLite（`service_state.published_version`），
  旧副本行在撤下时即物理删除，重启后已撤下副本不可恢复；轮次与各哨点
  当次清单同样落盘（`rounds` / `round_sentinels` / `round_reports`），
  未结案轮次的上报进度重启后不丢失；
- **非法不影响旧版**：所有合法性校验在写库之前完成（Pydantic，HTTP 422）；
  分轮汇总中的无效更正同样被拒绝，哨点保留上一次上报。

## 配置

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `APP_DB_PATH` | `./app.db`（容器内 `/data/app.db`） | SQLite 数据库路径 |

## 目录结构

```
app/
  main.py        FastAPI 入口与路由
  models.py      请求模型与合法性校验
  generalize.py  k-匿名泛化与最优参数选择
  store.py       SQLite 持久化与发布状态机（含分轮汇总）
tests/test_api.py     整批提交与发布测试
tests/test_rounds.py  分轮汇总测试
Dockerfile / docker-compose.yml / requirements.txt
```
