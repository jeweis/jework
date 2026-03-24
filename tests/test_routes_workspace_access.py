from pathlib import Path

from app.api import routes
from app.services.auth_service import AuthUser, AuthService
from app.services.workspace_service import WorkspaceService


def test_admin_resolve_workspace_with_access_accepts_workspace_id(tmp_path, monkeypatch):
    db_path = tmp_path / "app.db"
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    auth = AuthService(str(db_path))
    auth.init_db()
    superadmin = auth.bootstrap_superadmin("admin", "password123")
    manager = auth.create_user(superadmin, "manager01", "password123", role="admin")

    workspace_service = WorkspaceService(data_dir)
    workspace_service._db_path = str(db_path)
    item = workspace_service.create_workspace(
        "demo",
        mode="team",
        creator_user_id=superadmin.id,
    )

    monkeypatch.setattr(routes, "auth_service", auth)
    monkeypatch.setattr(routes, "workspace_service", workspace_service)

    meta, resolved_path = routes._resolve_workspace_with_access(
        item.workspace_id,
        manager,
    )

    assert meta.workspace_id == item.workspace_id
    assert Path(resolved_path) == data_dir / "demo"
