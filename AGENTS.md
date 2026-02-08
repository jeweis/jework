# AGENTS.md

本文件用于指导 AI 在 `jework`（后端子模块）中开展开发与维护工作。

## 1. 项目定位
- 技术栈：Python + FastAPI。
- 当前阶段：v0.1（只读 Agent）。
- 目标：提供工作目录列表、会话创建、消息流式回复接口，并在同端口托管前端静态资源。

## 2. 运行与开发
- 推荐环境：`uv` 虚拟环境（必须）。
- 初始化：
  - `uv venv .venv`
  - `source .venv/bin/activate`
  - `uv pip install -r requirements.txt pytest`
- 启动：
  - `uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload`
  - 或 `python main.py`
- 测试：
  - `pytest -q`

## 3. 关键配置
- `DATA_DIR`：统一数据根目录（环境变量）。
  - 默认：`./data`
  - 工作目录固定：`$DATA_DIR/workspaces`
  - SQLite 固定：`$DATA_DIR/db/app.db`
- `FRONTEND_STATIC_DIR`：前端静态资源目录。
  - 默认：`./app/static`

## 4. API 约束（v0.1）
- `GET /workspaces`：读取工作目录列表（只读）。
- `POST /sessions`：基于已有 workspace 创建会话。
- `POST /sessions/{id}/messages`：流式返回消息。
- 仅允许 Agent 使用 `Read` 工具，禁止写入类工具。

## 5. 安全规则
- 严格限制 workspace 在总根目录内。
- 防止路径穿越（禁止 `..`、非法分隔符、越界路径）。
- 不得硬编码 API Key、Token、密钥类信息。
- 错误输出采用统一结构，不在日志中泄露敏感信息。

## 6. 代码规范
- 遵循 PEP 8。
- 公共函数必须有 Type Hints。
- 命名：类 `PascalCase`，函数/变量 `snake_case`，常量 `UPPER_SNAKE_CASE`。
- 导入顺序：标准库 -> 第三方 -> 本地模块。

## 7. 目录约定
- `app/api`：路由层。
- `app/services`：业务与集成逻辑。
- `app/core`：配置、异常、基础能力。
- `app/models`：请求/响应模型。
- `tests`：测试代码。

## 8. 提交前检查
- `pytest -q` 通过。
- 若改动接口，需同步更新 `docs/` 需求或接口文档。
- 保持改动最小且可回滚，避免顺手重构大范围无关代码。
