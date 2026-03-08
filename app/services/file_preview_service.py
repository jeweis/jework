from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.core.errors import AppError

IGNORED_DIRS = {
    ".git",
    ".idea",
    ".vscode",
    "node_modules",
    ".venv",
    "build",
    "dist",
}

_CODE_SUFFIXES = {
    ".c",
    ".cc",
    ".cpp",
    ".cs",
    ".css",
    ".dart",
    ".go",
    ".h",
    ".hpp",
    ".html",
    ".java",
    ".js",
    ".json",
    ".jsx",
    ".kt",
    ".mjs",
    ".php",
    ".py",
    ".rb",
    ".rs",
    ".sh",
    ".sql",
    ".swift",
    ".ts",
    ".tsx",
    ".vue",
    ".xml",
    ".yaml",
    ".yml",
}

_TEXT_SUFFIXES = {
    ".txt",
    ".log",
    ".conf",
    ".ini",
    ".env",
    ".toml",
    ".csv",
    ".md",
    ".markdown",
}


class FilePreviewPathError(AppError):
    def __init__(self, reason: str):
        super().__init__(
            code="FILE_PREVIEW_PATH_ERROR",
            message="Invalid file path",
            details={"reason": reason},
            status_code=400,
        )


class FilePreviewNotFoundError(AppError):
    def __init__(self, file_path: str):
        super().__init__(
            code="FILE_PREVIEW_FILE_NOT_FOUND",
            message="File not found",
            details={"path": file_path},
            status_code=404,
        )


@dataclass(frozen=True)
class FileNode:
    type: str
    name: str
    path: str
    size: int | None = None
    mtime: str | None = None
    children: list["FileNode"] | None = None


@dataclass(frozen=True)
class FileContent:
    workspace: str
    path: str
    name: str
    size: int
    mtime: str
    content_type: str
    content: str
    is_binary: bool
    truncated: bool


class FilePreviewService:
    """
    个人工作空间文件预览服务。

    设计目标：
    1) 文件树支持递归浏览，便于后续扩展更多渲染器。
    2) 内容读取默认安全限流，防止单文件过大导致接口阻塞。
    3) 二进制文件只返回元信息，避免不可读字符污染前端渲染。
    """

    def build_index(self, workspace_path: Path) -> list[FileNode]:
        return self._walk_dir(workspace_path, workspace_path)

    def read_file_content(
        self,
        *,
        workspace: str,
        workspace_path: Path,
        relative_path: str,
        max_bytes: int,
        max_lines: int,
    ) -> FileContent:
        target = self._resolve_target_path(workspace_path, relative_path)
        if not target.exists() or not target.is_file():
            raise FilePreviewNotFoundError(relative_path)

        stat = target.stat()
        mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()
        declared_type = self._detect_content_type(target.name)

        # 限流读取：只读取 max_bytes + 1 字节，保留“是否截断”的判断信息。
        safe_max_bytes = max(1024, max_bytes)
        with target.open("rb") as f:
            content_bytes = f.read(safe_max_bytes + 1)
        is_truncated_by_size = len(content_bytes) > safe_max_bytes
        if is_truncated_by_size:
            content_bytes = content_bytes[:safe_max_bytes]

        if b"\x00" in content_bytes:
            return FileContent(
                workspace=workspace,
                path=target.relative_to(workspace_path).as_posix(),
                name=target.name,
                size=stat.st_size,
                mtime=mtime,
                content_type="binary",
                content="",
                is_binary=True,
                truncated=is_truncated_by_size,
            )

        # 优先按 UTF-8 解码：可正常解码的中文/多语言文本应视为文本文件，
        # 避免旧版 ASCII 比例算法把 .md 误判成 binary。
        try:
            decoded = content_bytes.decode("utf-8")
        except UnicodeDecodeError:
            return FileContent(
                workspace=workspace,
                path=target.relative_to(workspace_path).as_posix(),
                name=target.name,
                size=stat.st_size,
                mtime=mtime,
                content_type="binary",
                content="",
                is_binary=True,
                truncated=is_truncated_by_size,
            )

        if self._contains_too_many_control_chars(decoded):
            return FileContent(
                workspace=workspace,
                path=target.relative_to(workspace_path).as_posix(),
                name=target.name,
                size=stat.st_size,
                mtime=mtime,
                content_type="binary",
                content="",
                is_binary=True,
                truncated=is_truncated_by_size,
            )

        # 行级限流：最多返回 max_lines 行，避免超长日志占满前端内存。
        safe_max_lines = max(10, max_lines)
        lines = decoded.splitlines(keepends=True)
        is_truncated_by_lines = len(lines) > safe_max_lines
        if is_truncated_by_lines:
            decoded = "".join(lines[:safe_max_lines])

        return FileContent(
            workspace=workspace,
            path=target.relative_to(workspace_path).as_posix(),
            name=target.name,
            size=stat.st_size,
            mtime=mtime,
            content_type=declared_type,
            content=decoded,
            is_binary=False,
            truncated=is_truncated_by_size or is_truncated_by_lines,
        )

    def resolve_existing_file(
        self,
        *,
        workspace_path: Path,
        relative_path: str,
    ) -> Path:
        """
        解析并校验目标文件路径，供下载等“原始字节读取”场景复用。
        """
        target = self._resolve_target_path(workspace_path, relative_path)
        if not target.exists() or not target.is_file():
            raise FilePreviewNotFoundError(relative_path)
        return target

    def _walk_dir(self, root: Path, current: Path) -> list[FileNode]:
        nodes: list[FileNode] = []
        entries = sorted(current.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        for entry in entries:
            if entry.is_dir():
                if entry.name in IGNORED_DIRS:
                    continue
                children = self._walk_dir(root, entry)
                # 空目录对文件预览价值较低，先不展示，保持目录树简洁。
                if not children:
                    continue
                nodes.append(
                    FileNode(
                        type="dir",
                        name=entry.name,
                        path=entry.relative_to(root).as_posix(),
                        children=children,
                    )
                )
                continue

            stat = entry.stat()
            nodes.append(
                FileNode(
                    type="file",
                    name=entry.name,
                    path=entry.relative_to(root).as_posix(),
                    size=stat.st_size,
                    mtime=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
                )
            )
        return nodes

    def _resolve_target_path(self, workspace_path: Path, relative_path: str) -> Path:
        normalized = relative_path.strip()
        if not normalized:
            raise FilePreviewPathError("path is required")
        if normalized.startswith("/") or normalized.startswith("\\"):
            raise FilePreviewPathError("absolute path is not allowed")
        if ".." in Path(normalized).parts:
            raise FilePreviewPathError("path traversal is not allowed")

        target = (workspace_path / normalized).resolve()
        if workspace_path not in target.parents and target != workspace_path:
            raise FilePreviewPathError("path is outside workspace")
        return target

    def _detect_content_type(self, filename: str) -> str:
        lower_name = filename.lower()
        if lower_name.endswith(".md") or lower_name.endswith(".markdown"):
            return "markdown"
        suffix = Path(lower_name).suffix
        if suffix in _CODE_SUFFIXES:
            return "code"
        if suffix in _TEXT_SUFFIXES:
            return "text"
        return "unknown"

    def _contains_too_many_control_chars(self, text: str) -> bool:
        if not text:
            return False
        total = len(text)
        controls = 0
        for ch in text:
            code = ord(ch)
            if ch in ("\n", "\r", "\t"):
                continue
            if 0 <= code < 32:
                controls += 1
        return controls / max(1, total) > 0.1


file_preview_service = FilePreviewService()
