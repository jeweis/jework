import sqlite3

from app.services.mcp_token_service import McpTokenService


def test_reset_token_can_be_loaded_again(tmp_path):
    db_path = tmp_path / "app.db"
    service = McpTokenService(str(db_path))
    service.init_db()

    result = service.reset_token(user_id=1)

    loaded = service.get_token(user_id=1)
    assert loaded == result.token
    assert service.verify_token(result.token) == 1


def test_legacy_hash_only_token_cannot_be_loaded(tmp_path):
    db_path = tmp_path / "app.db"
    service = McpTokenService(str(db_path))
    service.init_db()

    result = service.reset_token(user_id=2)

    # 模拟历史版本：只保留 hash，不保留可回读密文。
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "UPDATE mcp_tokens SET token_encrypted = NULL WHERE user_id = ?",
        (2,),
    )
    conn.commit()
    conn.close()

    assert service.get_token(user_id=2) is None
    # 旧 token 仍应可鉴权使用，不影响兼容性。
    assert service.verify_token(result.token) == 2
