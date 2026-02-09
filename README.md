# Jework

Python + FastAPI backend service.

## 默认端口

- 默认监听端口：`9508`
- 可通过环境变量 `PORT` 覆盖端口

## 一条命令 Docker 部署

请先确保镜像已发布到 Docker Hub（例如：`jeweis/jework:latest`）。

```bash
docker run -d \
  --name jework \
  --restart always \
  -p 9508:9508 \
  -e PORT=9508 \
  -e DATA_DIR=/app/data \
  -v $(pwd)/data:/app/data \
  jeweis/jework:latest
```

启动后访问：`http://<服务器IP>:9508`

如果你使用自定义模型网关，可额外传入：

- `ANTHROPIC_BASE_URL`
- `ANTHROPIC_AUTH_TOKEN`
- `ANTHROPIC_MODEL`
- `ANTHROPIC_DEFAULT_SONNET_MODEL`
- `ANTHROPIC_DEFAULT_HAIKU_MODEL`
- `ANTHROPIC_DEFAULT_OPUS_MODEL`

Claude Agent 相关可选环境变量：

- `CLAUDE_AGENT_MAX_TURNS`：单次请求最大循环次数（默认 `20`）
- `CLAUDE_AGENT_ALLOWED_TOOLS`：允许工具列表，逗号分隔  
  默认工具集：`Skill,Read,Glob,Grep,WebSearch,WebFetch`

工作空间 PAT 加密密钥默认会在首次使用时自动生成并写入 SQLite，普通部署无需额外配置。  
仅在多实例共享同一数据库等高级场景下，才建议显式设置 `APP_SECRET_KEY` 做统一覆盖。

## Docker 镜像自动发布（Docker Hub）

已内置 GitHub Actions 工作流：`/.github/workflows/docker-publish.yml`

- 触发条件：推送 `main` 分支
- 发布目标：`docker.io/<DOCKERHUB_USERNAME>/jework`
- 默认标签：
  - `latest`（main 分支）
  - `main`
  - `sha-<commit>`

### 需要配置的 GitHub Secrets

- `DOCKERHUB_USERNAME`：Docker Hub 用户名
- `DOCKERHUB_TOKEN`：Docker Hub Access Token（建议不要用账号密码）
