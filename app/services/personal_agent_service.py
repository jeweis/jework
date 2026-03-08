from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path

from app.services.workspace_service import WorkspaceService, workspace_service


@dataclass(frozen=True)
class PersonalAgentBootstrapResult:
    user_root: Path
    main_agent_root: Path
    created_files: list[Path]


class PersonalAgentService:
    """
    个人 Agent 根目录管理与首次模板引导。

    目录规范：
    - workspaces/personal/<user_id>/workspace
    - workspaces/personal/<user_id>/workspace/project
    - workspaces/personal/<user_id>/workspace/memory
    """

    def __init__(self, workspace_service_dep: WorkspaceService) -> None:
        self._workspace_service = workspace_service_dep
        self._agent_template_dir = (
            Path(__file__).resolve().parents[1] / "templates" / "agent"
        ).resolve()
        self._skill_template_dir = (
            Path(__file__).resolve().parents[1] / "templates" / "skills"
        ).resolve()
        self._backend_root = Path(__file__).resolve().parents[2]

    def ensure_main_agent_workspace(
        self,
        *,
        user_id: int,
        username: str,
    ) -> PersonalAgentBootstrapResult:
        user_root = self._workspace_service.get_personal_user_root(user_id)
        main_root = self._workspace_service.get_personal_main_agent_workspace_root(user_id)
        project_root = (main_root / "project").resolve()
        memory_root = (main_root / "memory").resolve()

        user_root.mkdir(parents=True, exist_ok=True)
        main_root.mkdir(parents=True, exist_ok=True)
        project_root.mkdir(parents=True, exist_ok=True)
        memory_root.mkdir(parents=True, exist_ok=True)

        created_files: list[Path] = []
        for name, content in self._bootstrap_templates(
            username=username,
            user_id=user_id,
        ).items():
            path = (main_root / name).resolve()
            if name.endswith(".md"):
                # 用户可编辑的文档文件：仅首次创建，后续不覆盖。
                if not path.exists():
                    path.write_text(content, encoding="utf-8")
                    created_files.append(path)
                continue
            if name == ".mcp.json":
                if self._merge_mcp_config(path=path, template_content=content):
                    created_files.append(path)
                continue
            path.write_text(content, encoding="utf-8")
            created_files.append(path)

        today = datetime.now().date().isoformat()
        today_path = (memory_root / f"{today}.md").resolve()
        if not today_path.exists():
            daily_template = self._render_template(
                "memory_daily.md",
                username=username,
                user_id=user_id,
                date=today,
            )
            today_path.write_text(daily_template, encoding="utf-8")
            created_files.append(today_path)

        # 默认 skills：黑盒更新模式，每次同步模板到用户目录，确保及时生效。
        created_files.extend(
            self._ensure_default_skills(
                workspace_root=main_root,
                username=username,
                date=today,
            )
        )

        return PersonalAgentBootstrapResult(
            user_root=user_root,
            main_agent_root=main_root,
            created_files=created_files,
        )

    def _bootstrap_templates(self, *, username: str, user_id: int) -> dict[str, str]:
        return {
            "AGENTS.md": self._render_template("AGENTS.md", username=username, user_id=user_id),
            "SOUL.md": self._render_template("SOUL.md", username=username, user_id=user_id),
            "USER.md": self._render_template("USER.md", username=username, user_id=user_id),
            "IDENTITY.md": self._render_template("IDENTITY.md", username=username, user_id=user_id),
            "TOOLS.md": self._render_template("TOOLS.md", username=username, user_id=user_id),
            "MEMORY.md": self._render_template("MEMORY.md", username=username, user_id=user_id),
            ".mcp.json": self._render_template(
                ".mcp.json",
                username=username,
                user_id=user_id,
            ),
        }

    def _render_template(
        self,
        filename: str,
        *,
        username: str,
        user_id: int,
        date: str | None = None,
    ) -> str:
        template = self._load_template_text(filename)
        return (
            template.replace("{{username}}", username)
            .replace("{{date}}", date or datetime.now().date().isoformat())
            .replace("{{user_id}}", str(user_id))
            .replace("{{jework_root}}", str(self._backend_root))
        )

    def _load_template_text(self, filename: str) -> str:
        target = (self._agent_template_dir / filename).resolve()
        if not target.exists() or not target.is_file():
            raise RuntimeError(f"missing agent template: {target}")
        return target.read_text(encoding="utf-8")

    def _merge_mcp_config(self, *, path: Path, template_content: str) -> bool:
        """
        合并并更新 .mcp.json。

        规则：
        1. 若文件不存在，直接写入模板。
        2. 若文件存在，保留用户已有配置，仅更新/注入内置 `jework` 服务。
        """
        template_payload = json.loads(template_content)
        template_servers = template_payload.get("mcpServers")
        if not isinstance(template_servers, dict):
            raise RuntimeError("invalid .mcp.json template: missing mcpServers")

        if not path.exists():
            path.write_text(
                json.dumps(template_payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            return True

        try:
            existing_raw = path.read_text(encoding="utf-8")
            existing_payload = json.loads(existing_raw)
        except Exception:
            # 已存在但不可解析时，回退为模板结构。
            existing_payload = {}

        if not isinstance(existing_payload, dict):
            existing_payload = {}
        existing_servers = existing_payload.get("mcpServers")
        if not isinstance(existing_servers, dict):
            existing_servers = {}

        changed = False
        for server_name, server_value in template_servers.items():
            if existing_servers.get(server_name) != server_value:
                existing_servers[server_name] = server_value
                changed = True

        if not changed and "mcpServers" in existing_payload:
            return False

        existing_payload["mcpServers"] = existing_servers
        path.write_text(
            json.dumps(existing_payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return True

    def _ensure_default_skills(
        self,
        *,
        workspace_root: Path,
        username: str,
        date: str,
    ) -> list[Path]:
        if not self._skill_template_dir.exists() or not self._skill_template_dir.is_dir():
            raise RuntimeError(f"missing skill template dir: {self._skill_template_dir}")

        skills_root = (workspace_root / ".claude" / "skills").resolve()
        skills_root.mkdir(parents=True, exist_ok=True)
        created: list[Path] = []
        managed_skill_names: set[str] = set()

        for template_path in sorted(self._skill_template_dir.glob("*.SKILL.md")):
            if not template_path.is_file():
                continue
            skill_name = self._skill_name_from_template(template_path.name)
            managed_skill_names.add(skill_name)
            target_dir = (skills_root / skill_name).resolve()
            target_dir.mkdir(parents=True, exist_ok=True)
            target_skill = (target_dir / "SKILL.md").resolve()
            rendered = template_path.read_text(encoding="utf-8")
            rendered = (
                rendered.replace("{{username}}", username).replace("{{date}}", date)
            )
            target_skill.write_text(rendered, encoding="utf-8")
            created.append(target_skill)

        # 不做历史兼容：移除已废弃的内置技能目录。
        deprecated_skill_names = {"create_project"}
        for name in deprecated_skill_names:
            if name in managed_skill_names:
                continue
            deprecated_dir = (skills_root / name).resolve()
            if deprecated_dir.exists() and deprecated_dir.is_dir():
                shutil.rmtree(deprecated_dir, ignore_errors=True)

        return created

    def _skill_name_from_template(self, filename: str) -> str:
        suffix = ".SKILL.md"
        if not filename.endswith(suffix):
            raise RuntimeError(f"invalid skill template filename: {filename}")
        skill_name = filename[: -len(suffix)].strip()
        if not skill_name:
            raise RuntimeError(f"empty skill template name: {filename}")
        return skill_name


personal_agent_service = PersonalAgentService(workspace_service)
