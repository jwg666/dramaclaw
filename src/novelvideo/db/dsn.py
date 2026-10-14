"""``NOVELVIDEO_DB_DSN`` 解析与连接参数。

配置形如 ``mysql://user:password@host:3306/dbname?charset=utf8mb4``。
密码中的特殊字符(如 ``@``)须按 URL 规则百分号编码(例如 ``p@ss`` 写作
``p%40ss``),本模块用标准 ``urlsplit`` + ``unquote`` 还原。
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from functools import lru_cache
from urllib.parse import unquote, urlsplit

_DSN_SCHEMES = ("mysql", "mariadb")

#: 连接池上限(每进程)。服务器 max_connections=151,多进程(API + worker)
#: 各自持池,默认保守值;可用 NOVELVIDEO_DB_POOL_MAX 调整。
DEFAULT_POOL_MAX = 10


class DatabaseConfigError(RuntimeError):
    """DSN 非法或缺失。"""


@dataclass(frozen=True)
class DatabaseConfig:
    host: str
    port: int
    user: str
    password: str
    database: str
    charset: str = "utf8mb4"
    pool_max: int = DEFAULT_POOL_MAX

    def redacted(self) -> str:
        """打码后的 DSN 展示形态(日志/状态接口用,绝不能出现明文密码)。"""

        return f"mysql://{self.user}:***@{self.host}:{self.port}/{self.database}"


def database_config() -> DatabaseConfig:
    """读取并解析 NOVELVIDEO_DB_DSN;未配置时抛错(调用方应先用 active_backend 判定)。"""

    dsn = os.environ.get("NOVELVIDEO_DB_DSN", "").strip()
    if not dsn:
        raise DatabaseConfigError("NOVELVIDEO_DB_DSN 未配置")
    return parse_dsn(dsn)


@lru_cache(maxsize=8)
def parse_dsn(dsn: str) -> DatabaseConfig:
    parsed = urlsplit(dsn)
    if parsed.scheme.lower() not in _DSN_SCHEMES:
        raise DatabaseConfigError(
            f"不支持的 DSN scheme: {parsed.scheme!r}(仅支持 mysql://)"
        )
    if not parsed.hostname:
        raise DatabaseConfigError("DSN 缺少主机名")
    database = parsed.path.lstrip("/")
    if not database:
        raise DatabaseConfigError("DSN 缺少库名(路径部分)")
    try:
        port = parsed.port or 3306
    except ValueError as exc:
        raise DatabaseConfigError(f"DSN 端口非法: {exc}") from exc

    query = dict(_parse_query(parsed.query))
    pool_max = DEFAULT_POOL_MAX
    raw_pool = query.get("pool_max", "").strip()
    if raw_pool:
        try:
            pool_max = max(1, int(raw_pool))
        except ValueError as exc:
            raise DatabaseConfigError(f"pool_max 非法: {raw_pool!r}") from exc

    return DatabaseConfig(
        host=parsed.hostname,
        port=port,
        user=unquote(parsed.username or ""),
        password=unquote(parsed.password or ""),
        database=database,
        charset=query.get("charset", "utf8mb4"),
        pool_max=pool_max,
    )


def _parse_query(query: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for part in query.split("&"):
        if not part:
            continue
        key, _, value = part.partition("=")
        pairs.append((unquote(key), unquote(value)))
    return pairs


# ---------------------------------------------------------------------------
# 作用域(逻辑库)→ 表名前缀。单库部署下,每个原 SQLite 文件映射为一个
# 逻辑库,其下所有表名加统一前缀;全局库(注册表/设置/网关等)用固定前缀。
# ---------------------------------------------------------------------------

_GLOBAL_SCOPE_PREFIXES: dict[str, str] = {
    "local/settings.db": "g_settings__",
    "local/projects.db": "g_projects__",
    "verification.db": "g_verification__",
    "director_training.db": "g_training__",
    # Phase 2 现行位置:state/_shared/ 下;与上层留档的裸文件名同属一个逻辑库
    "_shared/verification.db": "g_verification__",
    "_shared/director_training.db": "g_training__",
    "director_auto.db": "g_director_auto__",
    "newapi/one-api.db": "g_newapi__",
}


def scope_identity(db_path) -> tuple[str, str]:
    """返回 (作用域键, 相对路径)。相对路径登记进 mv_scopes 供跨作用域枚举。"""

    from pathlib import Path

    try:
        rel = str(Path(db_path).resolve().relative_to(_state_root()))
    except Exception:
        rel = str(db_path)
    rel_norm = rel.replace("\\", "/")
    fixed = _GLOBAL_SCOPE_PREFIXES.get(rel_norm)
    if fixed:
        return fixed, rel_norm
    digest = hashlib.blake2b(rel_norm.encode("utf-8"), digest_size=8).digest()
    # Crockford 风格 base32,小写、无易混字符
    alphabet = "0123456789abcdefghjkmnpqrstvwxyz"
    value = int.from_bytes(digest, "big")
    token = ""
    for _ in range(10):
        token += alphabet[value & 31]
        value >>= 5
    return f"t_{token}__", rel_norm


def scope_key_for_path(db_path) -> str:
    """把原 SQLite 文件路径映射为稳定的作用域键(表名前缀)。

    全局库用固定前缀;项目/会话库用相对路径哈希(``t_`` + 10 位 base32 +
    ``__``,总长 14,保证 "前缀 + 最长表名" 不超 MySQL 64 字符上限)。
    """

    return scope_identity(db_path)[0]


def _state_root():
    from novelvideo.config import STATE_DIR

    return STATE_DIR


__all__ = [
    "DEFAULT_POOL_MAX",
    "DatabaseConfig",
    "DatabaseConfigError",
    "database_config",
    "parse_dsn",
    "scope_identity",
    "scope_key_for_path",
]
