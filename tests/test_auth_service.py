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


def test_superadmin_can_create_user(tmp_path):
    db_path = tmp_path / "app.db"
    service = AuthService(str(db_path))
    service.init_db()

    admin = service.bootstrap_superadmin("admin", "password123")
    created = service.create_user(admin, "user01", "password123")

    assert isinstance(created, AuthUser)
    assert created.role == "user"


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
