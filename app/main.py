"""FastAPI 入口：哨点病例脱敏副本发布服务。

接口：
- POST /api/submissions            整批提交新清单（<=40 条）；有效提交立即撤下旧副本，
                                   能达标则发布新版本，否则保持撤下并说明原因；
                                   非法提交返回 422，不影响旧版本。
- POST /api/rounds                 开启分轮汇总：指定应报哨点与 k；同时只收集一轮，
                                   已有开放轮次时返回 409。
- GET  /api/rounds/current         当前轮次状态：缺报哨点、各已报哨点例数（0 为零例）、
                                   总数与是否可结案。
- POST /api/rounds/current/reports 哨点整份上报/更正（空清单表示零例）；非法清单
                                   返回 422 并保留上次上报；结案后返回 409。
- POST /api/rounds/current/close   结案：报齐且总数 <=40 才受理；登记一个提交版本并
                                   按现有匿名规则更新供数（全部零例也登记一版并撤下
                                   旧副本）；重复结案返回已登记的版本。
- GET  /api/publication            公开下载当前发布的脱敏副本与参数（同一完整版本）；
                                   撤下状态下返回 404 及原因。
- GET  /api/status                 当前发布状态。
- GET  /api/submissions            提交历史（仅元数据）。
- GET  /health                     健康检查。
"""
from __future__ import annotations

import os

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from .models import ReportIn, RoundOpenIn, SubmissionIn
from .store import Store

DEFAULT_DB_PATH = os.environ.get("APP_DB_PATH", "./app.db")


def create_app(db_path: str | None = None) -> FastAPI:
    store = Store(db_path or os.environ.get("APP_DB_PATH", "./app.db"))
    app = FastAPI(title="哨点病例脱敏副本发布服务", version="1.1.0")
    app.state.store = store

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.post("/api/submissions", status_code=201)
    def submit(payload: SubmissionIn):
        body, status = store.submit(payload)
        return JSONResponse(body, status_code=status)

    @app.post("/api/rounds", status_code=201)
    def open_round(payload: RoundOpenIn):
        body, status = store.open_round(payload)
        return JSONResponse(body, status_code=status)

    @app.get("/api/rounds/current")
    def current_round():
        body, status = store.round_status()
        return JSONResponse(body, status_code=status)

    @app.post("/api/rounds/current/reports")
    def report(payload: ReportIn):
        body, status = store.submit_report(payload)
        return JSONResponse(body, status_code=status)

    @app.post("/api/rounds/current/close")
    def close_round():
        body, status = store.close_round()
        return JSONResponse(body, status_code=status)

    @app.get("/api/publication")
    def publication():
        body, status = store.publication()
        return JSONResponse(body, status_code=status)

    @app.get("/api/status")
    def status():
        return store.status()

    @app.get("/api/submissions")
    def submissions():
        return {"submissions": store.history()}

    return app


app = create_app(DEFAULT_DB_PATH)
