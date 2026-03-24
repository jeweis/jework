import pytest

from app.core.errors import (
    AppError,
    AuthForbiddenError,
    AuthInvalidCredentialsError,
    AuthRequiredError,
)
from app.services.auth_service import AuthUser, AuthService


def test_bootstrap_and_login(tmp_path):
    db_path = tmp_path / "app.db"
    service = AuthService(str(db_path))
    service.init_db()

    assert service.requires_bootstrap() is True

    admin = service.bootstrap_superadmin("admin", "password123")
    assert admin.role == "superadmin"
    assert service.requires_bootstrap() is False

    token, user = service.login("admin", "password123")
    assert token
    assert user.username == "admin"

    from_token = service.get_user_by_token(token)
    assert from_token.username == "admin"
    assert from_token.has_local_password is True


def test_superadmin_can_create_user(tmp_path):
    db_path = tmp_path / "app.db"
    service = AuthService(str(db_path))
    service.init_db()

    admin = service.bootstrap_superadmin("admin", "password123")
    created = service.create_user(admin, "user01", "password123")

    assert isinstance(created, AuthUser)
    assert created.role == "user"
    assert created.has_local_password is True


def test_superadmin_can_create_admin_user(tmp_path):
    db_path = tmp_path / "app.db"
    service = AuthService(str(db_path))
    service.init_db()

    admin = service.bootstrap_superadmin("admin", "password123")
    created = service.create_user(
        admin,
        "manager01",
        "password123",
        role="admin",
    )

    assert isinstance(created, AuthUser)
    assert created.role == "admin"
    assert created.has_local_password is True


def test_admin_can_create_normal_user(tmp_path):
    db_path = tmp_path / "app.db"
    service = AuthService(str(db_path))
    service.init_db()

    superadmin = service.bootstrap_superadmin("admin", "password123")
    manager = service.create_user(
        superadmin,
        "manager08",
        "password123",
        role="admin",
    )

    created = service.create_user(manager, "user07", "password123")

    assert created.role == "user"
    assert created.created_by == manager.id


def test_admin_cannot_create_admin_user(tmp_path):
    db_path = tmp_path / "app.db"
    service = AuthService(str(db_path))
    service.init_db()

    superadmin = service.bootstrap_superadmin("admin", "password123")
    manager = service.create_user(
        superadmin,
        "manager09",
        "password123",
        role="admin",
    )

    with pytest.raises(AppError) as exc_info:
        service.create_user(
            manager,
            "manager10",
            "password123",
            role="admin",
        )

    assert exc_info.value.code == "USER_ROLE_ASSIGN_FORBIDDEN"


def test_superadmin_can_update_user_role_to_admin(tmp_path):
    db_path = tmp_path / "app.db"
    service = AuthService(str(db_path))
    service.init_db()

    superadmin = service.bootstrap_superadmin("admin", "password123")
    created = service.create_user(superadmin, "user03", "password123")

    updated = service.set_user_role(
        current_user=superadmin,
        user_id=created.id,
        role="admin",
    )

    assert updated.role == "admin"
    users = service.list_users(superadmin)
    target = next(item for item in users if item.id == created.id)
    assert target.role == "admin"


def test_admin_can_list_users(tmp_path):
    db_path = tmp_path / "app.db"
    service = AuthService(str(db_path))
    service.init_db()

    superadmin = service.bootstrap_superadmin("admin", "password123")
    manager = service.create_user(
        superadmin,
        "manager03",
        "password123",
        role="admin",
    )

    users = service.list_users(manager)

    assert {item.username for item in users} >= {"admin", "manager03"}


def test_admin_can_update_normal_user_workspace_access(tmp_path):
    db_path = tmp_path / "app.db"
    service = AuthService(str(db_path))
    service.init_db()

    superadmin = service.bootstrap_superadmin("admin", "password123")
    manager = service.create_user(
        superadmin,
        "manager04",
        "password123",
        role="admin",
    )
    user = service.create_user(superadmin, "user06", "password123")

    updated = service.set_user_workspace_access(
        current_user=manager,
        user_id=user.id,
        workspace_names=["alpha", "beta"],
    )

    assert updated == ["alpha", "beta"]


def test_admin_cannot_update_admin_workspace_access(tmp_path):
    db_path = tmp_path / "app.db"
    service = AuthService(str(db_path))
    service.init_db()

    superadmin = service.bootstrap_superadmin("admin", "password123")
    manager = service.create_user(
        superadmin,
        "manager05",
        "password123",
        role="admin",
    )
    other_admin = service.create_user(
        superadmin,
        "manager06",
        "password123",
        role="admin",
    )

    with pytest.raises(AppError) as exc_info:
        service.set_user_workspace_access(
            current_user=manager,
            user_id=other_admin.id,
            workspace_names=["alpha"],
        )

    assert exc_info.value.code == "WORKSPACE_ASSIGNMENT_TARGET_FORBIDDEN"


def test_admin_has_global_workspace_access(tmp_path):
    db_path = tmp_path / "app.db"
    service = AuthService(str(db_path))
    service.init_db()

    superadmin = service.bootstrap_superadmin("admin", "password123")
    manager = service.create_user(
        superadmin,
        "manager07",
        "password123",
        role="admin",
    )

    assert service.get_accessible_workspaces(manager) == []
    assert service.can_access_workspace(manager, "any-workspace") is True


def test_admin_can_delete_self_created_normal_user(tmp_path):
    db_path = tmp_path / "app.db"
    service = AuthService(str(db_path))
    service.init_db()

    superadmin = service.bootstrap_superadmin("admin", "password123")
    manager = service.create_user(
        superadmin,
        "manager11",
        "password123",
        role="admin",
    )
    created = service.create_user(manager, "user08", "password123")

    service.delete_user(current_user=manager, user_id=created.id)

    users = service.list_users(superadmin)
    assert "user08" not in {item.username for item in users}


def test_admin_cannot_delete_user_created_by_others(tmp_path):
    db_path = tmp_path / "app.db"
    service = AuthService(str(db_path))
    service.init_db()

    superadmin = service.bootstrap_superadmin("admin", "password123")
    manager = service.create_user(
        superadmin,
        "manager12",
        "password123",
        role="admin",
    )
    other_user = service.create_user(superadmin, "user09", "password123")

    with pytest.raises(AppError) as exc_info:
        service.delete_user(current_user=manager, user_id=other_user.id)

    assert exc_info.value.code == "USER_DELETE_FORBIDDEN"


def test_superadmin_can_delete_admin_user(tmp_path):
    db_path = tmp_path / "app.db"
    service = AuthService(str(db_path))
    service.init_db()

    superadmin = service.bootstrap_superadmin("admin", "password123")
    manager = service.create_user(
        superadmin,
        "manager13",
        "password123",
        role="admin",
    )

    service.delete_user(current_user=superadmin, user_id=manager.id)

    users = service.list_users(superadmin)
    assert "manager13" not in {item.username for item in users}


def test_non_superadmin_cannot_update_user_role(tmp_path):
    db_path = tmp_path / "app.db"
    service = AuthService(str(db_path))
    service.init_db()

    superadmin = service.bootstrap_superadmin("admin", "password123")
    manager = service.create_user(
        superadmin,
        "manager02",
        "password123",
        role="admin",
    )
    target = service.create_user(superadmin, "user04", "password123")

    with pytest.raises(AuthForbiddenError):
        service.set_user_role(
            current_user=manager,
            user_id=target.id,
            role="admin",
        )


def test_set_user_role_rejects_invalid_role(tmp_path):
    db_path = tmp_path / "app.db"
    service = AuthService(str(db_path))
    service.init_db()

    superadmin = service.bootstrap_superadmin("admin", "password123")
    target = service.create_user(superadmin, "user05", "password123")

    with pytest.raises(AppError) as exc_info:
        service.set_user_role(
            current_user=superadmin,
            user_id=target.id,
            role="owner",
        )

    assert exc_info.value.code == "USER_ROLE_INVALID"


def test_feishu_first_login_and_relogin_updates_profile(tmp_path):
    db_path = tmp_path / "app.db"
    service = AuthService(str(db_path))
    service.init_db()

    token1, user1, first1 = service.login_by_feishu(
        union_id="on_test_union_001",
        open_id="ou_test_open_001",
        name="飞书张三",
        avatar_url="https://example.com/a.png",
    )
    assert token1
    assert first1 is True
    assert user1.role == "user"
    assert user1.has_local_password is False
    assert user1.display_name == "飞书张三"
    assert user1.feishu_union_id == "on_test_union_001"

    token2, user2, first2 = service.login_by_feishu(
        union_id="on_test_union_001",
        open_id="ou_test_open_002",
        name="飞书李四",
        avatar_url="https://example.com/b.png",
    )
    assert token2
    assert first2 is False
    assert user2.id == user1.id
    assert user2.has_local_password is False
    assert user2.display_name == "飞书李四"
    assert user2.feishu_open_id == "ou_test_open_002"


def test_feishu_first_login_assigns_default_workspaces(tmp_path):
    db_path = tmp_path / "app.db"
    service = AuthService(str(db_path))
    service.init_db()

    _, user, first_login = service.login_by_feishu(
        union_id="on_test_union_with_workspace_001",
        open_id="ou_test_open_with_workspace_001",
        name="飞书王五",
        avatar_url=None,
        default_workspace_names=["alpha", "beta", "alpha"],
    )

    assert first_login is True
    assert service.get_accessible_workspaces(user) == ["alpha", "beta"]


def test_set_local_password_enables_password_login_and_revokes_tokens(tmp_path):
    db_path = tmp_path / "app.db"
    service = AuthService(str(db_path))
    service.init_db()

    old_token, feishu_user, _ = service.login_by_feishu(
        union_id="on_set_password_001",
        open_id="ou_set_password_001",
        name="飞书用户A",
        avatar_url=None,
    )
    assert feishu_user.has_local_password is False

    service.set_local_password(
        current_user=feishu_user,
        new_password="newpass123",
    )

    with pytest.raises(AuthRequiredError):
        service.get_user_by_token(old_token)

    new_token, user_after_set = service.login(feishu_user.username, "newpass123")
    assert new_token
    assert user_after_set.has_local_password is True


def test_superadmin_can_reset_user_password_and_revoke_user_tokens(tmp_path):
    db_path = tmp_path / "app.db"
    service = AuthService(str(db_path))
    service.init_db()

    admin = service.bootstrap_superadmin("admin", "password123")
    created = service.create_user(admin, "user02", "oldpass123")
    old_token, _ = service.login("user02", "oldpass123")

    service.admin_reset_user_password(
        current_user=admin,
        user_id=created.id,
        new_password="newpass123",
    )

    with pytest.raises(AuthRequiredError):
        service.get_user_by_token(old_token)

    with pytest.raises(AuthInvalidCredentialsError):
        service.login("user02", "oldpass123")

    _, user_after_reset = service.login("user02", "newpass123")
    assert user_after_reset.has_local_password is True
