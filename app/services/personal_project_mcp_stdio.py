from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from app.core.errors import WorkspaceAlreadyExistsError
from app.services.auth_service import auth_service
from app.services.workspace_service import workspace_service


def _required_user_id() -> int:
    raw = os.getenv("JEWORK_PERSONAL_USER_ID", "").strip()
    if not raw:
        raise RuntimeError("JEWORK_PERSONAL_USER_ID is required")
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError("JEWORK_PERSONAL_USER_ID must be int") from exc
    if value <= 0:
        raise RuntimeError("JEWORK_PERSONAL_USER_ID must be positive")
    return value


def _list_projects(user_id: int) -> dict[str, Any]:
    user = auth_service.get_user_by_id(user_id)
    allowed = set(auth_service.get_accessible_workspaces(user))
    items = workspace_service.list_workspaces(allowed)
    projects = [
        {
            "workspace_id": item.workspace_id,
            "name": item.name,
            "path": item.path,
            "mode": item.mode,
        }
        for item in items
        if item.mode == "personal" and item.owner_user_id == user_id
    ]
    projects.sort(key=lambda row: str(row["name"]))
    return {"items": projects}


def _create_project(user_id: int, *, name: str, initialize_readme: bool) -> dict[str, Any]:
    normalized = name.strip()
    if not normalized:
        raise ValueError("name is required")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", normalized):
        raise ValueError("invalid project name")
    created = True
    try:
        item = workspace_service.create_workspace(
            workspace=normalized,
            mode="personal",
            creator_user_id=user_id,
            owner_user_id=user_id,
        )
    except WorkspaceAlreadyExistsError:
        created = False
        meta = workspace_service.resolve_workspace_reference(
            normalized,
            allowed_workspace_ids=set(auth_service.get_accessible_workspaces(auth_service.get_user_by_id(user_id))),
        )
        item = workspace_service.list_workspaces({meta.workspace_id})[0]

    target = Path(item.path).resolve()
    if initialize_readme:
        readme = (target / "README.md").resolve()
        if not readme.exists():
            readme.write_text(
                f"# {item.name}\n\n- 项目名称：{item.name}\n- 创建方式：personal-agent stdio MCP create_project\n",
                encoding="utf-8",
            )

    return {
        "workspace_id": item.workspace_id,
        "name": item.name,
        "path": item.path,
        "created": created,
        "mode": item.mode,
        "owner_user_id": item.owner_user_id,
    }


def _build_mcp():
    from fastmcp import FastMCP

    mcp = FastMCP(
        name="jework-personal-project-mcp",
        instructions=(
            "This MCP is dedicated to personal-agent project management. "
            "Use create_project/list_projects to manage personal projects."
        ),
    )
    user_id = _required_user_id()

    @mcp.tool(
        name="list_projects",
        description="列出当前个人 Agent 可管理的个人项目。",
    )
    def list_projects() -> dict[str, Any]:
        return _list_projects(user_id)

    @mcp.tool(
        name="create_project",
        description="创建个人项目目录并同步注册 personal workspace。",
    )
    def create_project(
        name: str,
        initialize_readme: bool = True,
    ) -> dict[str, Any]:
        return _create_project(
            user_id,
            name=name,
            initialize_readme=bool(initialize_readme),
        )

    return mcp


def main() -> None:
    mcp = _build_mcp()
    if hasattr(mcp, "run"):
        try:
            mcp.run(transport="stdio")
            return
        except TypeError:
            mcp.run()
            return
    if hasattr(mcp, "run_stdio"):
        mcp.run_stdio()
        return
    raise RuntimeError("fastmcp stdio entry is not available")


if __name__ == "__main__":
    main()
