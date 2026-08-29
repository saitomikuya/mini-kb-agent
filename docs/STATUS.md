# Development Status

## Project state

**Feature-complete and frozen. Final integration/hardening is complete.**

- [x] 01 Scaffold
- [x] 02 Database and Auth
- [x] 03 Model Providers
- [x] 04 Source File Management
- [x] 05 Async and Incremental Conversion
- [x] 06 Hierarchical JSON Indexing
- [x] 07 Query Routing and Answer Generation
- [x] 08 Citations, Conflicts, and Source Download
- [x] 09 Chat and Admin UI
- [x] 10 Single-Container Production Hardening
- [x] 11 Final A–G Integration Acceptance
- [x] 12 Documentation and Release Handoff

本项目不再增加产品功能。后续只允许 bug fix、安全更新、依赖维护与文档修正。

## Final acceptance checklist

- [x] Chat 密码独立
- [x] Admin 密码独立
- [x] Provider 多实例
- [x] Provider 一对多 Model Profile
- [x] 四模型角色独立配置
- [x] 模型 Profile 简化配置与四角色七任务提示词编辑
- [x] `document_conversion` Vision 能力校验
- [x] Azure OpenAI v1 与 legacy
- [x] OpenAI-compatible
- [x] Sub2API
- [x] Responses API
- [x] Chat Completions fallback
- [x] 文件扫描与 hash 变更检测
- [x] 增量转换
- [x] Huey 异步任务
- [x] 过期心跳重启恢复
- [x] Markdown 分片与 manifest-last 发布
- [x] root/folder 两级 JSON 索引
- [x] incremental index（card / affected folder / root）
- [x] query routing 父级白名单校验
- [x] Profile-aware context budget
- [x] citations 后端证据校验
- [x] conflict 全值保留
- [x] source file download
- [x] SSE 进度与结构化完成事件
- [x] Chat UI
- [x] Chat 历史仅保存在浏览器 IndexedDB
- [x] 无状态 Chat SSE 与 router 模型会话标题
- [x] `/chat/<local-id>` 刷新恢复与单会话删除
- [x] Admin UI
- [x] `python:3.12-slim` Dockerfile
- [x] supervisord 管理 uvicorn + huey_consumer
- [x] SIGTERM/SIGINT 有序停止配置
- [x] `GET /health` Docker HEALTHCHECK
- [x] `/app/sources` 与 `/app/data` 两个 volume
- [x] entrypoint 目录 → secret → Alembic → supervisord 顺序
- [x] 重启不删除 `app.db`
- [x] pytest
- [x] 禁用依赖未安装

## Final integration scenarios

- [x] **A** 空 knowledge 目录 → Admin 登录 → 上传 → 扫描 NEW → Huey 转换 → Markdown READY → 索引 INDEXED → Chat/SSE → router 选文件 → answer → 有效 citation。
- [x] **B** 只修改一个文件后仅它为 CHANGED；只转换它；只生成它的 card 和受影响 folder，未变 folder/card 字节复用，并生成新 root/generation。
- [x] **C** 两个文件的同一指标数值不同，SSE 发出 `conflict_detected`，回答保留两个有来源的值。
- [x] **D** “给我产品A一页图”返回产品A的 ID-based download URL，下载字节与源文件一致。
- [x] **E** 四个 mock model 严格分工：`conversion-model`、`index-model`、`router-model`、`answer-model`。
- [x] **F** 第二个真实转换 item 发生硬崩溃，重启恢复后只重做未完成 item，已完成 artifact 字节未变。
- [x] **G** `../`、绝对路径和 symlink escape 全部拒绝。

## Verification results

- Python: `3.12.13`
- Full suite: **`136 passed`**（仅保留既有上游弃用/可选 ffmpeg 警告）
- `pip check`: **No broken requirements found**
- `compileall app migrations tests`: **passed**
- `alembic upgrade head`: **passed**
- `alembic check`: **No new upgrade operations detected**
- `alembic downgrade base` / re-upgrade: covered by database tests
- JavaScript syntax checks: passed in the Admin/Chat UI stage
- Forbidden top-level installed packages: **none** of `langchain`, `llama-index`, `chromadb`, `faiss`, `qdrant`, `milvus`, `pinecone`, `redis`, `celery`
- Production packaging tests execute the real entrypoint twice, verify `app.db` data and `app.secret` survive restart, and statically verify both supervised processes, port 8080, two volumes, signal settings, and HEALTHCHECK.

## Docker build status

Docker 29.7.2 successfully built `mini-kb-agent:latest` from the required
`python:3.12-slim` image. A temporary container was run against empty host
source/data mounts and alternate host port 18080. Docker reported `healthy`,
`/health` returned `{"status":"ok"}`, and `docker top` showed supervisord with
separate uvicorn and huey_consumer children.

SIGTERM and SIGINT were both exercised. Huey exited cleanly, uvicorn completed
its application shutdown, and the container exited 0 in both cases. A restart
preserved an inserted SQLite marker and the exact `app.secret` hash. Temporary
acceptance data/container were removed; the final image remains locally.

## Runtime data contract

```text
/app/sources
/app/data/
  app.db
  queue.db
  app.secret
  md/
  index/
  tmp/
  logs/
```

The application never deletes the database on startup. Existing migrations are
applied with `alembic upgrade head` before supervisord starts.

## Known limitations

- Single-host/single-container deployment; no horizontal scaling.
- Container processes run as root for broad bind-mount compatibility; mount only dedicated source/data directories and do not treat the deployment as an untrusted multi-tenant boundary.
- Huey Worker concurrency is intentionally 1.
- Audio/video conversion is unsupported.
- Complex scanned/visual document quality depends on the configured conversion vision model.
- Indexing, routing, and final answers require reachable external model APIs.
- `/health` checks the Web HTTP process, not external Providers or full Worker liveness.
- Quiesce the container for a consistent SQLite/artifact backup.
- The current verification environment emits one upstream FastAPI/Starlette `TestClient` deprecation warning and MarkItDown/pydub's optional ffmpeg discovery warning. Supported document conversion is unaffected.

## Architecture invariants

- Provider != Model Profile != Role Binding；角色提示词独立持久化。
- Business model lookup goes only through `ModelRegistry.get_for_role(...)`.
- `/app/sources` is the source of truth and conversion never mutates it.
- JSON is the canonical index; Markdown index files are previews only.
- There is no embedding, vector database, Redis, Celery, Node build, React, or Vue.
- Web and Worker coordinate only through persisted SQLite state, queue storage, and validated artifacts.

## Final handoff

See [`docs/handoff/12-final.md`](handoff/12-final.md). There is no next feature stage.
