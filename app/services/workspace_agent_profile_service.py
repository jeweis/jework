from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
import zipfile

from app.core.config import settings
from app.core.errors import AppError

_MAX_SKILL_UPLOAD_BYTES = 5 * 1024 * 1024
_ENV_TEMPLATE_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)\}")


@dataclass(frozen=True)
class AgentMcpServer:
    name: str
    type: str
    url: str | None
    command: str | None
    args: list[str]
    headers: dict[str, str]


@dataclass(frozen=True)
class WorkspaceSkillItem:
    name: str
    relative_path: str
    description: str | None


@dataclass(frozen=True)
class WorkspaceAgentProfile:
    workspace_id: str
    mcp_servers: list[AgentMcpServer]
    extra_allowed_tools: list[str]
    updated_by: int | None
    updated_at: str | None


class WorkspaceAgentProfileService:
    """
    个人工作空间 Agent 配置服务。

    目标：
    - 为每个 workspace_id 维护一份独立 MCP/工具白名单配置；
    - 为技能上传提供统一落盘目录与安全校验。
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path

    def init_db(self) -> None:
        with closing(sqlite3.connect(self._db_path)) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS workspace_agent_profiles (
                    workspace_id TEXT PRIMARY KEY,
                    mcp_servers_json TEXT NOT NULL,
                    extra_allowed_tools_json TEXT NOT NULL,
                    updated_by INTEGER,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.commit()

    def get_profile(self, workspace_id: str) -> WorkspaceAgentProfile:
        with closing(sqlite3.connect(self._db_path)) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT workspace_id, mcp_servers_json, extra_allowed_tools_json, updated_by, updated_at
                FROM workspace_agent_profiles
                WHERE workspace_id = ?
                LIMIT 1
                """,
                (workspace_id,),
            ).fetchone()
            if row is None:
                return WorkspaceAgentProfile(
                    workspace_id=workspace_id,
                    mcp_servers=[],
                    extra_allowed_tools=[],
                    updated_by=None,
                    updated_at=None,
                )
            return WorkspaceAgentProfile(
                workspace_id=str(row["workspace_id"]),
                mcp_servers=self._parse_mcp_servers(row["mcp_servers_json"]),
                extra_allowed_tools=self._parse_allowed_tools(
                    row["extra_allowed_tools_json"]
                ),
                updated_by=(
                    int(row["updated_by"]) if row["updated_by"] is not None else None
                ),
                updated_at=str(row["updated_at"]) if row["updated_at"] else None,
            )

    def upsert_profile(
        self,
        *,
        workspace_id: str,
        mcp_servers: list[dict[str, object]],
        extra_allowed_tools: list[str],
        updated_by: int,
    ) -> WorkspaceAgentProfile:
        normalized_servers = self._normalize_mcp_servers(mcp_servers)
        normalized_tools = self._normalize_allowed_tools(extra_allowed_tools)
        now = datetime.now(timezone.utc).isoformat()
        with closing(sqlite3.connect(self._db_path)) as conn:
            conn.execute(
                """
                INSERT INTO workspace_agent_profiles (
                    workspace_id,
                    mcp_servers_json,
                    extra_allowed_tools_json,
                    updated_by,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(workspace_id) DO UPDATE SET
                    mcp_servers_json = excluded.mcp_servers_json,
                    extra_allowed_tools_json = excluded.extra_allowed_tools_json,
                    updated_by = excluded.updated_by,
                    updated_at = excluded.updated_at
                """,
                (
                    workspace_id,
                    json.dumps(
                        [self._mcp_server_to_json(item) for item in normalized_servers],
                        ensure_ascii=False,
                    ),
                    json.dumps(normalized_tools, ensure_ascii=False),
                    updated_by,
                    now,
                ),
            )
            conn.commit()
        return WorkspaceAgentProfile(
            workspace_id=workspace_id,
            mcp_servers=normalized_servers,
            extra_allowed_tools=normalized_tools,
            updated_by=updated_by,
            updated_at=now,
        )

    def list_skills(self, workspace_path: Path) -> list[WorkspaceSkillItem]:
        skills_root = self._skills_root(workspace_path)
        if not skills_root.exists():
            return []
        items: list[WorkspaceSkillItem] = []
        for skill_dir in skills_root.iterdir():
            if not skill_dir.is_dir():
                continue
            skill_file = skill_dir / "SKILL.md"
            if not skill_file.exists():
                continue
            relative = skill_file.relative_to(workspace_path).as_posix()
            items.append(
                WorkspaceSkillItem(
                    name=skill_dir.name,
                    relative_path=relative,
                    description=self._extract_skill_description(skill_file),
                )
            )
        return sorted(items, key=lambda item: item.name)

    def upload_skill(
        self,
        *,
        workspace_path: Path,
        filename: str,
        content: bytes,
    ) -> WorkspaceSkillItem:
        if len(content) <= 0:
            raise AppError(
                code="WORKSPACE_SKILL_UPLOAD_INVALID",
                message="Skill file is empty",
                status_code=400,
            )
        if len(content) > _MAX_SKILL_UPLOAD_BYTES:
            raise AppError(
                code="WORKSPACE_SKILL_UPLOAD_TOO_LARGE",
                message="Skill upload exceeds size limit",
                details={"max_bytes": _MAX_SKILL_UPLOAD_BYTES},
                status_code=400,
            )

        trimmed_name = filename.strip()
        if not trimmed_name:
            raise AppError(
                code="WORKSPACE_SKILL_UPLOAD_INVALID",
                message="Missing skill filename",
                status_code=400,
            )
        suffix = Path(trimmed_name).suffix.lower()
        if suffix == ".md":
            return self._store_single_markdown_skill(
                workspace_path=workspace_path,
                filename=trimmed_name,
                content=content,
            )
        if suffix == ".zip":
            return self._store_zip_skill(
                workspace_path=workspace_path,
                filename=trimmed_name,
                content=content,
            )
        raise AppError(
            code="WORKSPACE_SKILL_UPLOAD_INVALID",
            message="Only .zip or .md skill upload is supported",
            status_code=400,
        )

    def delete_skill(self, *, workspace_path: Path, skill_name: str) -> None:
        normalized_name = self._normalize_skill_name(skill_name)
        skill_dir = (self._skills_root(workspace_path) / normalized_name).resolve()
        if not skill_dir.exists():
            raise AppError(
                code="WORKSPACE_SKILL_NOT_FOUND",
                message=f"Skill not found: {normalized_name}",
                status_code=404,
            )
        self._assert_inside_workspace(skill_dir, workspace_path)
        shutil.rmtree(skill_dir)

    def build_sdk_mcp_servers(
        self,
        mcp_servers: list[AgentMcpServer],
        runtime_env: dict[str, str],
    ) -> dict[str, dict[str, object]]:
        """
        将工作空间 MCP 配置转换为 ClaudeAgentOptions.mcp_servers 结构。
        """
        resolved: dict[str, dict[str, object]] = {}
        for server in mcp_servers:
            key = server.name
            server_type = server.type
            if server_type in {"http", "sse"}:
                if not server.url:
                    continue
                entry: dict[str, object] = {
                    "type": server_type,
                    "url": self._expand_env_templates(server.url, runtime_env),
                }
                if server.headers:
                    entry["headers"] = {
                        header_key: self._expand_env_templates(value, runtime_env)
                        for header_key, value in server.headers.items()
                    }
                resolved[key] = entry
                continue
            if server_type == "stdio":
                if not server.command:
                    continue
                entry = {
                    "type": "stdio",
                    "command": self._expand_env_templates(server.command, runtime_env),
                }
                if server.args:
                    entry["args"] = [
                        self._expand_env_templates(item, runtime_env)
                        for item in server.args
                    ]
                if server.headers:
                    entry["env"] = {
                        header_key: self._expand_env_templates(value, runtime_env)
                        for header_key, value in server.headers.items()
                    }
                resolved[key] = entry
        return resolved

    def _store_single_markdown_skill(
        self,
        *,
        workspace_path: Path,
        filename: str,
        content: bytes,
    ) -> WorkspaceSkillItem:
        stem = Path(filename).stem
        skill_name = self._normalize_skill_name(stem)
        skill_dir = self._prepare_skill_dir(workspace_path, skill_name)
        skill_file = skill_dir / "SKILL.md"
        skill_file.write_bytes(content)
        return WorkspaceSkillItem(
            name=skill_name,
            relative_path=skill_file.relative_to(workspace_path).as_posix(),
            description=self._extract_skill_description(skill_file),
        )

    def _store_zip_skill(
        self,
        *,
        workspace_path: Path,
        filename: str,
        content: bytes,
    ) -> WorkspaceSkillItem:
        with tempfile.TemporaryDirectory(prefix="workspace-skill-") as tmpdir:
            tmp_path = Path(tmpdir)
            self._safe_extract_zip(content=content, output_dir=tmp_path)
            skill_files = [
                item
                for item in tmp_path.rglob("SKILL.md")
                if item.is_file() and "__MACOSX" not in item.parts
            ]
            if not skill_files:
                raise AppError(
                    code="WORKSPACE_SKILL_UPLOAD_INVALID",
                    message="ZIP does not contain SKILL.md",
                    status_code=400,
                )
            if len(skill_files) > 1:
                raise AppError(
                    code="WORKSPACE_SKILL_UPLOAD_INVALID",
                    message="ZIP contains multiple SKILL.md files",
                    status_code=400,
                )
            skill_file = skill_files[0]
            source_dir = skill_file.parent
            skill_name_seed = source_dir.name or Path(filename).stem
            skill_name = self._normalize_skill_name(skill_name_seed)
            target_dir = self._prepare_skill_dir(workspace_path, skill_name)
            shutil.copytree(source_dir, target_dir, dirs_exist_ok=False)
            target_skill_file = target_dir / "SKILL.md"
            if not target_skill_file.exists():
                raise AppError(
                    code="WORKSPACE_SKILL_UPLOAD_INVALID",
                    message="Uploaded skill folder missing SKILL.md",
                    status_code=400,
                )
            return WorkspaceSkillItem(
                name=skill_name,
                relative_path=target_skill_file.relative_to(workspace_path).as_posix(),
                description=self._extract_skill_description(target_skill_file),
            )

    def _prepare_skill_dir(self, workspace_path: Path, skill_name: str) -> Path:
        skills_root = self._skills_root(workspace_path)
        skills_root.mkdir(parents=True, exist_ok=True)
        skill_dir = (skills_root / skill_name).resolve()
        self._assert_inside_workspace(skill_dir, workspace_path)
        if skill_dir.exists():
            raise AppError(
                code="WORKSPACE_SKILL_ALREADY_EXISTS",
                message=f"Skill already exists: {skill_name}",
                status_code=409,
            )
        return skill_dir

    def _skills_root(self, workspace_path: Path) -> Path:
        root = (workspace_path / ".claude" / "skills").resolve()
        self._assert_inside_workspace(root, workspace_path)
        return root

    def _safe_extract_zip(self, *, content: bytes, output_dir: Path) -> None:
        total_uncompressed = 0
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            for info in archive.infolist():
                name = info.filename
                member_path = Path(name)
                if member_path.is_absolute() or ".." in member_path.parts:
                    raise AppError(
                        code="WORKSPACE_SKILL_UPLOAD_INVALID",
                        message="ZIP contains invalid path entry",
                        details={"entry": name},
                        status_code=400,
                    )
                total_uncompressed += int(info.file_size)
                if total_uncompressed > _MAX_SKILL_UPLOAD_BYTES * 4:
                    raise AppError(
                        code="WORKSPACE_SKILL_UPLOAD_INVALID",
                        message="ZIP extracted size is too large",
                        status_code=400,
                    )
            archive.extractall(path=output_dir)

    def _assert_inside_workspace(self, target: Path, workspace_path: Path) -> None:
        workspace_root = workspace_path.resolve()
        resolved = target.resolve()
        if resolved != workspace_root and workspace_root not in resolved.parents:
            raise AppError(
                code="WORKSPACE_SKILL_INVALID_PATH",
                message="Skill path escapes workspace root",
                status_code=400,
            )

    def _normalize_skill_name(self, raw: str) -> str:
        value = re.sub(r"[^a-zA-Z0-9._-]+", "-", raw.strip()).strip("-_.")
        if not value:
            raise AppError(
                code="WORKSPACE_SKILL_UPLOAD_INVALID",
                message="Skill name is empty",
                status_code=400,
            )
        if len(value) > 64:
            raise AppError(
                code="WORKSPACE_SKILL_UPLOAD_INVALID",
                message="Skill name is too long",
                status_code=400,
            )
        return value

    def _extract_skill_description(self, skill_file: Path) -> str | None:
        try:
            lines = skill_file.read_text(encoding="utf-8").splitlines()
        except Exception:
            return None
        for line in lines[:40]:
            stripped = line.strip()
            lowered = stripped.lower()
            if lowered.startswith("description:"):
                value = stripped.split(":", 1)[1].strip()
                return value or None
        for line in lines[:40]:
            stripped = line.strip()
            if stripped.startswith("# "):
                return stripped[2:].strip() or None
        return None

    def _parse_mcp_servers(self, raw: object) -> list[AgentMcpServer]:
        data = self._load_json_array(raw)
        parsed: list[dict[str, object]] = []
        for item in data:
            if isinstance(item, dict):
                parsed.append(item)
        return self._normalize_mcp_servers(parsed)

    def _parse_allowed_tools(self, raw: object) -> list[str]:
        data = self._load_json_array(raw)
        values = [item for item in data if isinstance(item, str)]
        return self._normalize_allowed_tools(values)

    def _load_json_array(self, raw: object) -> list[object]:
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                return []
            if isinstance(parsed, list):
                return parsed
            return []
        if isinstance(raw, list):
            return raw
        return []

    def _normalize_mcp_servers(
        self,
        servers: list[dict[str, object]],
    ) -> list[AgentMcpServer]:
        result: list[AgentMcpServer] = []
        names_seen: set[str] = set()
        for index, item in enumerate(servers):
            name = str(item.get("name", "")).strip()
            server_type = str(item.get("type", "")).strip().lower()
            if not name:
                raise AppError(
                    code="WORKSPACE_AGENT_PROFILE_INVALID",
                    message="MCP server name is required",
                    details={"index": index},
                    status_code=400,
                )
            if server_type not in {"http", "sse", "stdio"}:
                raise AppError(
                    code="WORKSPACE_AGENT_PROFILE_INVALID",
                    message="MCP server type must be http/sse/stdio",
                    details={"index": index, "type": server_type},
                    status_code=400,
                )
            if name in names_seen:
                raise AppError(
                    code="WORKSPACE_AGENT_PROFILE_INVALID",
                    message="Duplicated MCP server name",
                    details={"name": name},
                    status_code=400,
                )
            names_seen.add(name)
            raw_headers = item.get("headers", {})
            headers: dict[str, str] = {}
            if isinstance(raw_headers, dict):
                for key, value in raw_headers.items():
                    header_key = str(key).strip()
                    header_value = str(value).strip()
                    if not header_key:
                        continue
                    headers[header_key] = header_value
            url = str(item.get("url", "")).strip() or None
            command = str(item.get("command", "")).strip() or None
            args_raw = item.get("args", [])
            args: list[str] = []
            if isinstance(args_raw, list):
                args = [str(value).strip() for value in args_raw if str(value).strip()]
            if server_type in {"http", "sse"} and not url:
                raise AppError(
                    code="WORKSPACE_AGENT_PROFILE_INVALID",
                    message="MCP http/sse server url is required",
                    details={"name": name},
                    status_code=400,
                )
            if server_type == "stdio" and not command:
                raise AppError(
                    code="WORKSPACE_AGENT_PROFILE_INVALID",
                    message="MCP stdio server command is required",
                    details={"name": name},
                    status_code=400,
                )
            result.append(
                AgentMcpServer(
                    name=name,
                    type=server_type,
                    url=url,
                    command=command,
                    args=args,
                    headers=headers,
                )
            )
        return result

    def _normalize_allowed_tools(self, tools: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for raw in tools:
            value = raw.strip()
            if not value or value in seen:
                continue
            seen.add(value)
            normalized.append(value)
        return normalized

    def _mcp_server_to_json(self, item: AgentMcpServer) -> dict[str, object]:
        payload: dict[str, object] = {
            "name": item.name,
            "type": item.type,
            "headers": item.headers,
            "args": item.args,
        }
        if item.url is not None:
            payload["url"] = item.url
        if item.command is not None:
            payload["command"] = item.command
        return payload

    def _expand_env_templates(self, value: str, env: dict[str, str]) -> str:
        merged: dict[str, str] = {}
        merged.update(env)

        def _replace(match: re.Match[str]) -> str:
            key = match.group(1)
            replacement = merged.get(key)
            return replacement if replacement is not None else match.group(0)

        return _ENV_TEMPLATE_PATTERN.sub(_replace, value)


workspace_agent_profile_service = WorkspaceAgentProfileService(
    str(settings.sqlite_db_path)
)
