"""SQLite DDL → MySQL DDL 转换(单库 + 表名前缀路线)。

输入是各存储模块里的 SQLite 建表/建索引脚本(语法面已审计:类型只有
TEXT/INTEGER/REAL;约束覆盖 PRIMARY KEY 单列/组合、TEXT PRIMARY KEY、
UNIQUE(...)、FOREIGN KEY、AUTOINCREMENT、字面量默认值与
``datetime('now')`` 默认值)。输出 MySQL 8.0 可执行的等价语句列表。

关键映射规则:
* ``TEXT PRIMARY KEY`` → ``VARCHAR(n) ... COLLATE utf8mb4_bin``(MySQL 不允许
  TEXT 主键;bin 排序保持 SQLite 的区分大小写语义)。单列主键 255、组合
  主键成员 191,可用覆盖表改小(entity_evidence 六列组合主键必须收缩才
  能满足 3072 字节索引上限)。
* 带字面量默认值的 TEXT → ``TEXT DEFAULT ('<lit>')``(8.0.13+ 允许 TEXT
  表达式默认值,既保留无上限存储又不丢默认语义)。
* ``DEFAULT (datetime('now'))`` / ``DEFAULT CURRENT_TIMESTAMP`` → 时间戳列
  收敛为 ``VARCHAR(40) DEFAULT (DATE_FORMAT(UTC_TIMESTAMP(), ...))``:
  应用侧时间串上限 32 字符,且默认值格式与 :func:`novelvideo.db.sql_compat.
  utc_datetime_str` 完全一致(空格分隔、UTC)。
* 纯 TEXT → MEDIUMTEXT(SQLite TEXT 本无上限,prompt/正文可能超 MySQL
  64KB 的 TEXT 上限)。
* INTEGER → BIGINT(SQLite INTEGER 是 64 位);REAL → DOUBLE。
* 部分索引(``CREATE UNIQUE INDEX ... WHERE col IS NOT NULL AND col <> ''``)
  MySQL 无等价物 → 生成存储列 ``<col>__nz``(空串归一为 NULL)+ 普通唯一
  索引(唯一索引允许多个 NULL)。
"""

from __future__ import annotations

import re
from typing import Optional

# 注意 MySQL DATE_FORMAT 的占位符:分钟是 %i(%M 是月份名!);%S/%s 均为秒。
_MYSQL_TS_SPACE_EXPR = "DATE_FORMAT(UTC_TIMESTAMP(), '%Y-%m-%d %H:%i:%S')"

_SINGLE_PK_LEN = 255
_COMPOSITE_PK_LEN = 191
_DEFAULTED_TEXT_LEN = 255  # 未命中覆盖表的"短"文本列;索引安全(255*4=1020B)

# 表名登记表:adapter 的 DML 改写器按这里的集合把裸表名加上作用域前缀。
_KNOWN_TABLES: set[str] = set()

# (table, column) → VARCHAR 长度覆盖。组合主键/FK 配对列用。
COLUMN_LENGTH_OVERRIDES: dict[tuple[str, str], int] = {
    # entity_evidence: 4 个 TEXT 列同为主键成员,4×191×4B 超索引上限
    ("entity_evidence", "run_id"): 64,
    ("entity_evidence", "entity_type"): 64,
    ("entity_evidence", "entity_id"): 191,
    ("entity_evidence", "chunk_id"): 64,
    # FK 两侧类型必须一致(workflow_node_actions.run_id → workflow_runs.run_id)
    ("workflow_runs", "run_id"): 64,
    ("workflow_node_actions", "run_id"): 64,
    ("task_states", "task_key"): 191,  # task_key 是长组合键,191 足够且索引安全
}


def known_tables() -> set[str]:
    """已翻译 DDL 登记的全部裸表名(供 adapter 改写 DML 用)。"""

    return set(_KNOWN_TABLES)


# ---------------------------------------------------------------------------
# 语句切分:按引号外的分号拆,保留字符串字面量。
# ---------------------------------------------------------------------------


def _split_statements(script: str) -> list[str]:
    """剥掉 ``--`` 行注释后,按引号外的分号拆语句('' 转义原样保留)。"""

    statements: list[str] = []
    current: list[str] = []
    in_string = False
    i = 0
    n = len(script)
    while i < n:
        ch = script[i]
        if in_string:
            current.append(ch)
            if ch == "'":
                if i + 1 < n and script[i + 1] == "'":
                    current.append("'")
                    i += 2
                    continue
                in_string = False
            i += 1
            continue
        if ch == "'":
            in_string = True
            current.append(ch)
            i += 1
            continue
        if ch == "-" and i + 1 < n and script[i + 1] == "-":
            # 行注释:跳到行尾
            while i < n and script[i] != "\n":
                i += 1
            continue
        if ch == ";":
            text = "".join(current).strip()
            if text:
                statements.append(text)
            current = []
            i += 1
            continue
        current.append(ch)
        i += 1
    text = "".join(current).strip()
    if text:
        statements.append(text)
    return statements


def _split_top_level(body: str) -> list[str]:
    """按最外层逗号拆列定义(括号/字符串深度感知)。"""

    items: list[str] = []
    current: list[str] = []
    depth = 0
    in_string = False
    i = 0
    n = len(body)
    while i < n:
        ch = body[i]
        if in_string:
            current.append(ch)
            if ch == "'":
                if i + 1 < n and body[i + 1] == "'":
                    current.append("'")
                    i += 2
                    continue
                in_string = False
            i += 1
            continue
        if ch == "'":
            in_string = True
            current.append(ch)
            i += 1
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "," and depth == 0:
            items.append("".join(current).strip())
            current = []
            i += 1
            continue
        current.append(ch)
        i += 1
    tail = "".join(current).strip()
    if tail:
        items.append(tail)
    return items


_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def translate_ddl_script(
    script: str,
    prefix: str,
) -> list[str]:
    """把一段 SQLite DDL 脚本翻译成 MySQL 语句列表。

    ``prefix`` 为该逻辑库的表名前缀(如 ``t_ab12cd34ef__``)。语句顺序与
    输入一致;CREATE INDEX 照搬(不带 IF NOT EXISTS——由 schema runner 的
    迁移台账保证幂等)。

    两遍扫描:第一遍收集每张表被索引/唯一约束覆盖的列——纯 TEXT 列一旦
    进索引必须收敛为 VARCHAR(MySQL 不允许 TEXT/BLOB 进索引,错误 1170);
    第二遍按此翻译。
    """

    parsed: list[tuple[str, str]] = []
    indexed_cols: dict[str, set[str]] = {}
    for raw in _split_statements(script):
        upper = raw.upper().lstrip()
        if upper.startswith("CREATE TABLE"):
            parsed.append(("table", raw))
        elif upper.startswith("CREATE INDEX") or upper.startswith("CREATE UNIQUE INDEX"):
            parsed.append(("index", raw))
            info = _parse_index_stmt(raw)
            if info is not None:
                _table, cols = info
                indexed_cols.setdefault(_table, set()).update(cols)
        else:
            parsed.append(("other", raw))

    statements: list[str] = []
    for kind, raw in parsed:
        if kind == "table":
            statements.extend(_translate_create_table(raw, prefix, indexed_cols))
        elif kind == "index":
            statements.extend(_translate_create_index(raw, prefix))
        else:
            # schema 脚本应只含 CREATE 语句;遇到其他语句原样透传,
            # 由 schema runner 记录并原样执行(便于脚本携带种子数据)。
            statements.append(raw)
    return statements


def _parse_index_stmt(raw: str) -> Optional[tuple[str, list[str]]]:
    """从 CREATE [UNIQUE] INDEX 语句提取 (table, [裸列名])。"""

    match = re.match(
        r"CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?[A-Za-z_][A-Za-z0-9_]*\s+"
        r"ON\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(([^)]*)\)",
        raw,
        re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return None
    table = match.group(1)
    cols = []
    for part in match.group(2).split(","):
        name = part.strip().split()[0] if part.strip() else ""
        if name:
            cols.append(name)
    return table, cols


def _q(name: str) -> str:
    return f"`{name}`"


def _translate_create_table(
    raw: str, prefix: str, indexed_cols: Optional[dict[str, set[str]]] = None
) -> list[str]:
    match = re.match(
        r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*\((.*)\)\s*$",
        raw,
        re.IGNORECASE | re.DOTALL,
    )
    if not match:
        raise ValueError(f"无法解析的 CREATE TABLE: {raw[:120]}...")
    table = match.group(1)
    _KNOWN_TABLES.add(table)
    full_name = f"{prefix}{table}"
    body = match.group(2)

    # 表级 UNIQUE(...) 的列同样不允许 TEXT 进索引,提前并入索引列集合
    effective_indexed = set(indexed_cols.get(table, set())) if indexed_cols else set()
    for uniq_body in re.findall(r"UNIQUE\s*\(([^)]*)\)", body, re.IGNORECASE):
        for part in uniq_body.split(","):
            name = part.strip().split()[0] if part.strip() else ""
            if name:
                effective_indexed.add(name)

    pk_cols: list[str] = []
    pk_inline: dict[str, bool] = {}
    autoincr_cols: set[str] = set()
    column_items: list[tuple[str, str]] = []  # (col_name, normalized_def)
    table_constraints: list[str] = []

    for item in _split_top_level(body):
        head = item.strip()
        upper_head = head.upper()
        if upper_head.startswith("PRIMARY KEY"):
            cols = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", head.split("(", 1)[1])
            pk_cols = [c for c in cols if c.lower() != "key"]
            continue
        if upper_head.startswith("UNIQUE"):
            inner = head.split("(", 1)[1].rsplit(")", 1)[0]
            cols = [c.strip() for c in inner.split(",")]
            table_constraints.append(f"UNIQUE KEY ({', '.join(_q(c) for c in cols)})")
            continue
        if upper_head.startswith("FOREIGN KEY"):
            # REFERENCES 目标表也要加前缀
            fk = re.sub(
                r"REFERENCES\s+([A-Za-z_][A-Za-z0-9_]*)",
                lambda m: f"REFERENCES {prefix}{m.group(1)}",
                head,
                flags=re.IGNORECASE,
            )
            table_constraints.append(fk)
            continue
        if upper_head.startswith("CHECK"):
            table_constraints.append(head)
            continue

        col_match = re.match(r"([A-Za-z_][A-Za-z0-9_]*)\s+(.*)", head, re.DOTALL)
        if not col_match:
            raise ValueError(f"无法解析的列定义: {head[:120]}... (table={table})")
        col, rest = col_match.group(1), col_match.group(2).strip()
        rest_upper = rest.upper()
        if "PRIMARY KEY" in rest_upper:
            pk_inline[col] = True
            if "AUTOINCREMENT" in rest_upper:
                autoincr_cols.add(col)
        column_items.append((col, rest))

    composite = bool(pk_cols)
    pk_set = set(pk_cols) | {c for c, v in pk_inline.items() if v}

    mysql_cols: list[str] = []
    for col, rest in column_items:
        mysql_cols.append(
            _translate_column(
                table, col, rest, pk_set, pk_inline, composite, autoincr_cols,
                indexed=col in effective_indexed,
            )
        )

    if pk_cols:
        table_constraints.insert(0, f"PRIMARY KEY ({', '.join(_q(c) for c in pk_cols)})")

    all_defs = mysql_cols + table_constraints
    sql = (
        f"CREATE TABLE IF NOT EXISTS {_q(full_name)} (\n  "
        + ",\n  ".join(all_defs)
        + "\n) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci"
    )
    return [sql]


def _column_len(table: str, col: str, composite: bool) -> int:
    override = COLUMN_LENGTH_OVERRIDES.get((table, col))
    if override:
        return override
    return _COMPOSITE_PK_LEN if composite else _SINGLE_PK_LEN


def _translate_column(
    table: str,
    col: str,
    rest: str,
    pk_set: set[str],
    pk_inline: dict[str, bool],
    composite: bool,
    autoincr_cols: set[str],
    indexed: bool = False,
) -> str:
    rest_upper = rest.upper()
    is_pk_member = col in pk_set
    is_inline_pk = bool(pk_inline.get(col))

    if rest_upper.startswith("INTEGER"):
        if is_inline_pk:
            if col in autoincr_cols:
                return f"{_q(col)} BIGINT AUTO_INCREMENT PRIMARY KEY"
            return f"{_q(col)} BIGINT NOT NULL PRIMARY KEY"
        if is_pk_member:
            return f"{_q(col)} BIGINT NOT NULL"
        return f"{_q(col)} BIGINT" + _tail_constraints(rest, "INTEGER")
    if rest_upper.startswith("REAL"):
        return f"{_q(col)} DOUBLE" + _tail_constraints(rest, "REAL")

    # TEXT 分支
    if is_pk_member:
        length = _column_len(table, col, composite)
        # SQLite 主键文本是二进制可比;utf8mb4_bin 保持区分大小写与 ULID 字典序。
        # 组合成员由表级 PRIMARY KEY 统一定义,这里只给 NOT NULL,避免重复主键。
        inline = " PRIMARY KEY" if is_inline_pk else ""
        return (
            f"{_q(col)} VARCHAR({length}) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin "
            f"NOT NULL{inline}"
        )

    if indexed:
        # 进索引的纯 TEXT 列必须定长(MySQL 1170);255*4=1020B 索引安全
        not_null = "NOT NULL" if "NOT NULL" in rest_upper else ""
        suffix = f" {not_null}" if not_null else ""
        return f"{_q(col)} VARCHAR({_DEFAULTED_TEXT_LEN}){suffix}"

    now_default = re.search(r"DEFAULT\s*(?:\(\s*)?datetime\('now'\)\s*\)?", rest, re.IGNORECASE)
    current_ts_default = "DEFAULT CURRENT_TIMESTAMP" in rest_upper
    if now_default or current_ts_default:
        not_null = "NOT NULL " if "NOT NULL" in rest_upper else ""
        return f"{_q(col)} VARCHAR(40) {not_null}DEFAULT ({_MYSQL_TS_SPACE_EXPR})"

    literal_default = re.search(r"DEFAULT\s+('(?:[^']|'')*')", rest, re.IGNORECASE)
    if literal_default:
        # TEXT 上保留无上限存储;字面量默认用表达式形式(MySQL 8.0.13+)
        not_null = "NOT NULL " if "NOT NULL" in rest_upper else ""
        return f"{_q(col)} MEDIUMTEXT {not_null}DEFAULT ({literal_default.group(1)})"

    plain = "MEDIUMTEXT"
    not_null = "NOT NULL" if "NOT NULL" in rest_upper else ""
    unique = " UNIQUE" if "UNIQUE" in rest_upper else ""
    suffix = f"{not_null}{unique}".strip()
    if suffix:
        return f"{_q(col)} {plain} {suffix}"
    return f"{_q(col)} {plain}"


def _tail_constraints(rest: str, type_word: str) -> str:
    """INTEGER/REAL 列剩余约束(NOT NULL/DEFAULT n/UNIQUE)照搬。"""

    tail = rest[len(type_word):].strip()
    tail = tail.replace("AUTOINCREMENT", "").replace("PRIMARY KEY", "").strip()
    if not tail:
        return ""
    return " " + " ".join(tail.split())


_PARTIAL_WHERE_RE = re.compile(
    r"WHERE\s+([A-Za-z_][A-Za-z0-9_]*)\s+IS\s+NOT\s+NULL\s+AND\s+\1\s*(?:<>|!=)\s*''",
    re.IGNORECASE,
)


def _translate_create_index(raw: str, prefix: str) -> list[str]:
    match = re.match(
        r"CREATE\s+(UNIQUE\s+)?INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_][A-Za-z0-9_]*)\s+"
        r"ON\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(([^)]*)\)\s*(?:WHERE\s+(.*))?\s*$",
        raw,
        re.IGNORECASE | re.DOTALL,
    )
    if not match:
        raise ValueError(f"无法解析的 CREATE INDEX: {raw[:160]}...")
    unique = bool(match.group(1))
    index_name = match.group(2)
    table = match.group(3)
    indexed_cols: list[tuple[str, str]] = []  # (裸列名, 排序方向 ""|"ASC"|"DESC")
    for part in match.group(4).split(","):
        tokens = part.strip().split()
        if not tokens:
            continue
        name = tokens[0]
        direction = tokens[1].upper() if len(tokens) > 1 and tokens[1].upper() in ("ASC", "DESC") else ""
        indexed_cols.append((name, direction))
    where = (match.group(5) or "").strip()

    full = f"{prefix}{table}"
    unique_kw = "UNIQUE " if unique else ""
    if not where:
        rendered = ", ".join(
            f"{_q(name)}{(' ' + direction) if direction else ''}"
            for name, direction in indexed_cols
        )
        return [
            f"CREATE {unique_kw}INDEX {_q(index_name)} ON {_q(full)} ({rendered})"
        ]

    # 部分索引:WHERE col IS NOT NULL AND col <> ''
    pm = _PARTIAL_WHERE_RE.search(f"WHERE {where}")
    if not pm:
        raise ValueError(f"不支持的部分索引 WHERE 形态: {where[:160]}...")
    col = pm.group(1)
    norm_col = f"{col}__nz"
    gen = (
        f"ALTER TABLE {_q(full)} ADD COLUMN {_q(norm_col)} VARCHAR({_DEFAULTED_TEXT_LEN}) "
        f"GENERATED ALWAYS AS (CASE WHEN {_q(col)} IS NOT NULL AND {_q(col)} <> '' "
        f"THEN {_q(col)} END) STORED"
    )
    new_cols = [
        norm_col if name == col else name for name, _direction in indexed_cols
    ]
    idx = (
        f"CREATE {unique_kw}INDEX {_q(index_name)} ON {_q(full)} "
        f"({', '.join(_q(c) for c in new_cols)})"
    )
    return [gen, idx]


def ensure_registry(scripts) -> None:
    """对一批 schema 脚本跑一遍翻译,只为登记表名(不执行)。"""

    for script in scripts:
        try:
            translate_ddl_script(script, "x__")
        except ValueError:
            continue


def translate_column_definition(table: str, col: str, definition: str) -> str:
    """把单个 SQLite 列定义翻译成 MySQL 列定义(含反引号列名)。"""

    return _translate_column(table, col, definition.strip(), set(), {}, False, set())
