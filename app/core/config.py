import os
from dataclasses import dataclass
from pathlib import Path


def _load_local_dotenv() -> None:
    """
    在应用启动时加载项目根目录 `.env` 到进程环境变量。

    规则：
    1) 仅在当前环境变量不存在时写入，避免覆盖容器/系统显式注入值。
    2) 支持常见 `KEY=VALUE` 与 `export KEY=VALUE` 写法。
    3) 忽略空行与注释行（以 `#` 开头）。
    """
    env_file = Path(__file__).resolve().parents[2] / ".env"
    if not env_file.exists():
        return

    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if not key:
            continue
        os.environ.setdefault(key, value)


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    workspace_root_dir: Path
    frontend_static_dir: Path
    sqlite_db_path: Path


def _resolve_data_dir() -> Path:
    raw_path = os.getenv("DATA_DIR", "./data")
    data_dir = Path(raw_path).expanduser().resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


def _resolve_frontend_static_dir() -> Path:
    raw_path = os.getenv("FRONTEND_STATIC_DIR", "./app/static")
    return Path(raw_path).expanduser().resolve()


def _resolve_workspace_root(data_dir: Path) -> Path:
    root_path = (data_dir / "workspaces").resolve()
    root_path.mkdir(parents=True, exist_ok=True)
    return root_path


def _resolve_sqlite_db_path(data_dir: Path) -> Path:
    db_dir = (data_dir / "db").resolve()
    db_dir.mkdir(parents=True, exist_ok=True)
    return (db_dir / "app.db").resolve()


_load_local_dotenv()
_data_dir = _resolve_data_dir()
settings = Settings(
    data_dir=_data_dir,
    workspace_root_dir=_resolve_workspace_root(_data_dir),
    frontend_static_dir=_resolve_frontend_static_dir(),
    sqlite_db_path=_resolve_sqlite_db_path(_data_dir),
)
