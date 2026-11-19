"""FastAPI 入口：哨点病例脱敏副本发布服务。

接口：
- POST /api/submissions   整批提交新清单（<=40 条）；有效提交立即撤下旧副本，
                          能达标则发布新版本，否则保持撤下并说明原因；
                          非法提交返回 422，不影响旧版本。
- GET  /api/publication   公开下载当前发布的脱敏副本与参数（同一完整版本）；
                          撤下状态下返回 404 及原因。
- GET  /api/status        当前发布状态。
- GET  /api/submissions   提交历史（仅元数据）。
- GET  /health            健康检查。
"""
from __future__ import annotations

import os

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from .models import SubmissionIn
from .store import Store

DEFAULT_DB_PATH = os.environ.get("APP_DB_PATH", "./app.db")


def create_app(db_path: str | None = None) -> FastAPI:
    store = Store(db_path or os.environ.get("APP_DB_PATH", "./app.db"))
    app = FastAPI(title="哨点病例脱敏副本发布服务", version="1.0.0")
    app.state.store = store

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.post("/api/submissions", status_code=201)
    def submit(payload: SubmissionIn):
        body, status = store.submit(payload)
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
