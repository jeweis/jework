from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.core.errors import AppError

MARKDOWN_SUFFIXES = {".md", ".markdown"}
IGNORED_DIRS = {
    ".git",
    ".idea",
    ".vscode",
    "node_modules",
    ".venv",
    "build",
    "dist",
}
MAX_MARKDOWN_SIZE_BYTES = 2 * 1024 * 1024


class MarkdownPathError(AppError):
    def __init__(self, reason: str):
        super().__init__(
            code="MARKDOWN_PATH_ERROR",
            message="Invalid markdown path",
            details={"reason": reason},
            status_code=400,
        )


class MarkdownFileTooLargeError(AppError):
    def __init__(self, file_path: str, size: int):
        super().__init__(
            code="MARKDOWN_FILE_TOO_LARGE",
            message="Markdown file is too large",
            details={"path": file_path, "size": size, "max_size": MAX_MARKDOWN_SIZE_BYTES},
            status_code=413,
        )


class MarkdownFileNotFoundError(AppError):
    def __init__(self, file_path: str):
        super().__init__(
            code="MARKDOWN_FILE_NOT_FOUND",
            message="Markdown file not found",
            details={"path": file_path},
            status_code=404,
        )


@dataclass(frozen=True)
class MarkdownNode:
    type: str
    name: str
    path: str
    size: int | None = None
    mtime: str | None = None
    children: list["MarkdownNode"] | None = None


@dataclass(frozen=True)
class MarkdownContent:
    workspace: str
    path: str
    name: str
    size: int
    mtime: str
    content: str


class MarkdownService:
    def build_index(self, workspace: str, workspace_path: Path) -> list[MarkdownNode]:
        return self._walk_dir(workspace_path, workspace_path)

    def read_markdown_content(
        self,
        workspace: str,
        workspace_path: Path,
        relative_path: str,
    ) -> MarkdownContent:
        target = self._resolve_target_path(workspace_path, relative_path)
        if not target.exists() or not target.is_file():
            raise MarkdownFileNotFoundError(relative_path)
        if target.suffix.lower() not in MARKDOWN_SUFFIXES:
            raise MarkdownPathError("path is not a markdown file")

        size = target.stat().st_size
        if size > MAX_MARKDOWN_SIZE_BYTES:
            raise MarkdownFileTooLargeError(relative_path, size)

        content = target.read_text(encoding="utf-8")
        mtime = datetime.fromtimestamp(
            target.stat().st_mtime,
            tz=timezone.utc,
        ).isoformat()
        return MarkdownContent(
            workspace=workspace,
            path=target.relative_to(workspace_path).as_posix(),
            name=target.name,
            size=size,
            mtime=mtime,
            content=content,
        )

    def _walk_dir(self, root: Path, current: Path) -> list[MarkdownNode]:
        nodes: list[MarkdownNode] = []
        entries = sorted(
            current.iterdir(),
            key=lambda p: (not p.is_dir(), p.name.lower()),
        )
        for entry in entries:
            if entry.is_dir():
                if entry.name in IGNORED_DIRS:
                    continue
                children = self._walk_dir(root, entry)
                if not children:
                    continue
                nodes.append(
                    MarkdownNode(
                        type="dir",
                        name=entry.name,
                        path=entry.relative_to(root).as_posix(),
                        children=children,
                    )
                )
                continue

            if entry.suffix.lower() not in MARKDOWN_SUFFIXES:
                continue
            stat = entry.stat()
            nodes.append(
                MarkdownNode(
                    type="file",
                    name=entry.name,
                    path=entry.relative_to(root).as_posix(),
                    size=stat.st_size,
                    mtime=datetime.fromtimestamp(
                        stat.st_mtime,
                        tz=timezone.utc,
                    ).isoformat(),
                )
            )
        return nodes

    def _resolve_target_path(self, workspace_path: Path, relative_path: str) -> Path:
        normalized = relative_path.strip()
        if not normalized:
            raise MarkdownPathError("path is required")
        if normalized.startswith("/") or normalized.startswith("\\"):
            raise MarkdownPathError("absolute path is not allowed")
        if ".." in Path(normalized).parts:
            raise MarkdownPathError("path traversal is not allowed")

        target = (workspace_path / normalized).resolve()
        if workspace_path not in target.parents and target != workspace_path:
            raise MarkdownPathError("path is outside workspace")
        return target


markdown_service = MarkdownService()
