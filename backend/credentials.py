from __future__ import annotations

import json
from cryptography.fernet import Fernet, InvalidToken
from backend.errors import ConfigurationNeeded


def cipher(settings):
    try:
        return Fernet(settings.secrets_encryption_key.encode())
    except (ValueError, TypeError):
        raise ConfigurationNeeded("请在服务端配置 SECRETS_ENCRYPTION_KEY；使用 python -m backend.cli generate-key 生成") from None


def seal(settings, project_id: str, kind: str, value: str) -> str:
    return cipher(settings).encrypt(json.dumps([project_id, kind, value]).encode()).decode()


def unseal(settings, project_id: str, kind: str, value: str | None) -> str:
    if not value:
        return ""
    try:
        owner, purpose, secret = json.loads(cipher(settings).decrypt(value.encode()))
        if owner != project_id or purpose != kind or not isinstance(secret, str):
            raise ValueError()
        return secret
    except (InvalidToken, ValueError, TypeError):
        raise ConfigurationNeeded("项目凭据无法解密，请检查服务端主密钥或重新配置凭据") from None
