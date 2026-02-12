from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib import error, request

from app.core.errors import AppError


@dataclass(frozen=True)
class FeishuUserInfo:
    union_id: str
    open_id: str | None
    name: str
    avatar_url: str | None


class FeishuAuthService:
    """
    飞书认证适配层。

    该类只负责第三方 OpenAPI 交互，不感知 Jework 的用户/鉴权模型。
    通过“适配层 + 业务层”分离，避免第三方协议侵入主业务结构。
    """

    def exchange_code_v2(
        self,
        *,
        base_url: str,
        app_id: str,
        app_secret: str,
        code: str,
    ) -> str:
        payload = {
            "grant_type": "authorization_code",
            "client_id": app_id,
            "client_secret": app_secret,
            "code": code,
        }
        url = f"{base_url.rstrip('/')}/open-apis/authen/v2/oauth/token"
        data = self._post_json(url, payload)
        self._assert_success(data, error_code="FEISHU_TOKEN_EXCHANGE_FAILED")

        access_token = data.get("access_token")
        if not access_token or not isinstance(access_token, str):
            raise AppError(
                code="FEISHU_TOKEN_EXCHANGE_FAILED",
                message="Feishu token exchange succeeded but access_token missing",
                details={"response": data},
                status_code=400,
            )
        return access_token

    def get_user_info(
        self,
        *,
        base_url: str,
        user_access_token: str,
    ) -> FeishuUserInfo:
        url = f"{base_url.rstrip('/')}/open-apis/authen/v1/user_info"
        data = self._get_json(
            url,
            headers={
                "Authorization": f"Bearer {user_access_token}",
                "Content-Type": "application/json; charset=utf-8",
            },
        )
        self._assert_success(data, error_code="FEISHU_USERINFO_FAILED")

        payload = data.get("data")
        if not isinstance(payload, dict):
            raise AppError(
                code="FEISHU_USERINFO_FAILED",
                message="Feishu user_info payload is invalid",
                details={"response": data},
                status_code=400,
            )
        union_id = payload.get("union_id")
        name = payload.get("name")
        if not isinstance(union_id, str) or not union_id:
            raise AppError(
                code="FEISHU_UNION_ID_MISSING",
                message="union_id missing in Feishu user info",
                details={"payload": payload},
                status_code=400,
            )
        if not isinstance(name, str) or not name:
            name = "飞书用户"
        open_id = payload.get("open_id")
        avatar_url = payload.get("avatar_url")
        return FeishuUserInfo(
            union_id=union_id,
            open_id=open_id if isinstance(open_id, str) and open_id else None,
            name=name,
            avatar_url=avatar_url if isinstance(avatar_url, str) and avatar_url else None,
        )

    def _post_json(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload).encode("utf-8")
        req = request.Request(
            url=url,
            data=body,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        return self._open_json(req)

    def _get_json(self, url: str, headers: dict[str, str]) -> dict[str, Any]:
        req = request.Request(url=url, headers=headers, method="GET")
        return self._open_json(req)

    def _open_json(self, req: request.Request) -> dict[str, Any]:
        try:
            with request.urlopen(req, timeout=10) as resp:
                raw = resp.read().decode("utf-8")
        except error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="ignore")
            raise AppError(
                code="FEISHU_HTTP_ERROR",
                message="Feishu API HTTP error",
                details={"status": exc.code, "body": body, "url": req.full_url},
                status_code=400,
            ) from exc
        except Exception as exc:
            raise AppError(
                code="FEISHU_HTTP_ERROR",
                message="Failed to call Feishu API",
                details={"reason": str(exc), "url": req.full_url},
                status_code=400,
            ) from exc

        try:
            parsed = json.loads(raw)
        except Exception as exc:
            raise AppError(
                code="FEISHU_HTTP_ERROR",
                message="Invalid Feishu API response JSON",
                details={"raw": raw},
                status_code=400,
            ) from exc
        if not isinstance(parsed, dict):
            raise AppError(
                code="FEISHU_HTTP_ERROR",
                message="Invalid Feishu API response shape",
                details={"response": parsed},
                status_code=400,
            )
        return parsed

    def _assert_success(self, payload: dict[str, Any], error_code: str) -> None:
        code = payload.get("code")
        if code == 0:
            return
        raise AppError(
            code=error_code,
            message="Feishu API returned non-zero code",
            details={"response": payload},
            status_code=400,
        )


feishu_auth_service = FeishuAuthService()

