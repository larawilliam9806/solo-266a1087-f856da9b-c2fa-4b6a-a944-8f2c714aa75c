# 哨点病例脱敏副本发布服务

研究方按年龄段和邮区分析哨点病例。病例清单以整批方式提交，平台按 k-匿名
规则生成**逐行脱敏副本**对外供数；清单一旦修订，旧副本立即撤下，不再供数。

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

### GET /api/publication

- 已发布：HTTP 200，返回同一完整版本的 `version`、`k`、`params`
  （`age_band_years`、`zip_prefix_digits`）、计数与逐行副本；
  副本每行只含泛化字段与病种，如 `{"age_band": "30-39", "zip": "100**", "disease": "流感"}`；
- 已撤下/无数据：HTTP 404，附撤下原因。

## 一致性设计

- **不过期发布**：发布在提交请求内同步完成，写路径持全局可重入锁并以
  `BEGIN IMMEDIATE` 单事务提交，发布与再次提交交错时不会出现过期清单；
- **不撕裂下载**：读路径持同一把锁，副本行与参数始终来自同一完整版本；
- **重启不恢复**：发布指针持久化于 SQLite（`service_state.published_version`），
  旧副本行在撤下时即物理删除，重启后已撤下副本不可恢复；
- **非法不影响旧版**：所有合法性校验在写库之前完成（Pydantic，HTTP 422）。

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
  store.py       SQLite 持久化与发布状态机
tests/test_api.py
Dockerfile / docker-compose.yml / requirements.txt
```
