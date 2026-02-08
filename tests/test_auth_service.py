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
