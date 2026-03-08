---
name: project_management
description: 个人工作台 project 管理工作流。凡涉及文件写入/修改/删除/重命名，必须先执行本技能：先确认目标 project，不存在则先通过 MCP create_project 创建，再进行写操作。
---

# project_management

## 目标
- 统一管理 `project/<name>` 下的项目生命周期，避免目录与 workspace 注册状态不一致。

## 适用场景
- 用户询问“有哪些项目”“项目在哪”。
- 用户要求在某个项目里新增/修改文件。
- 用户要求创建新项目。

## 强制触发条件（必须执行）
- 只要任务包含以下任一动作，必须先执行本技能流程：
  - 新增文件
  - 修改文件
  - 删除文件
  - 重命名/移动文件
  - 创建项目目录相关内容
- 在未完成本技能的“项目确认/创建”步骤前，不得执行任何写操作工具（Write/Edit/MultiEdit/NotebookEdit）。

## 核心规则
1. 写入/修改文件前，必须先确认目标 project。
2. 若目标 project 不存在，必须先通过 MCP 创建，不可直接建目录。
3. 仅在 `project/<name>` 下进行业务文件改动。
4. 若用户未提供 project 名，先追问或基于上下文确认，不能跳过。
5. 当已有 project 时，优先从现有项目中选择最合适的一个，并说明选择依据。
6. 若已有 project 但无法判断哪个合适，或现有项目都不适配当前任务，应先询问用户确认。
7. 当一个 project 都没有时，直接创建一个最贴合任务语义的新 project（不询问用户）。

## 标准流程
1. **查看项目**：
- 首选 `mcp__jework__list_projects`
- 备选 `mcp__personal_project__list_projects`
2. **定位项目**：
- 若用户未指定 project，先基于上下文在现有项目中选择最匹配的项目。
- 若存在项目但都不匹配或无法判断，询问用户确认目标 project。
3. **按需创建**：
- 仅当目标 project 不存在时调用 `create_project`。
  - 若当前没有任何 project，可直接新建语义最贴近的 project（例如 `billing`、`docs`、`ops`），不需询问用户。
4. **执行文件修改**：
- 在 project 创建/确认后，再执行 Write/Edit/MultiEdit。
5. **结果回报**：
- 返回 project 名、是否新建、涉及文件、关键变更。

## MCP 调用规范
- 列表：
  - `mcp__jework__list_projects`
  - `mcp__personal_project__list_projects`（备选）
- 创建：
  - `mcp__jework__create_project`
  - `mcp__personal_project__create_project`（备选）
- 创建参数：
  - `name`: 项目名
  - `initialize_readme`: 默认 `true`

## 禁止事项
- 不经 MCP 直接 `mkdir project/<name>` 创建正式项目。
- 在未确认 project 时直接写文件。
- MCP 创建失败后未经确认直接降级 shell 创建。
- 以“先写后补”方式绕过本技能流程。

## 示例
- 用户：“在 billing 项目新增 docs/api.md”
1. 先 `list_projects`
2. 若无 `billing`，调用 `create_project(name="billing", initialize_readme=true)`
3. 创建成功后再写 `project/billing/docs/api.md`
