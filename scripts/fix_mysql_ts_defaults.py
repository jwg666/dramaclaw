"""一次性修复:MySQL DATE_FORMAT 默认值里的 %M(月份名)应为 %i(分钟)。

- 重建所有 column_default 含字面量 '%M' 的时间戳列默认;
- 把已写入的畸形值('...:October:41' 形态)的"月名段"替换回 ':00:'。
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1] / "src"))

import pymysql  # noqa: E402

from novelvideo.db.dsn import database_config  # noqa: E402


def main() -> int:
    cfg = database_config()
    conn = pymysql.connect(
        host=cfg.host,
        port=cfg.port,
        user=cfg.user,
        password=cfg.password,
        database=cfg.database,
        charset=cfg.charset,
        autocommit=True,
    )
    cur = conn.cursor()
    cur.execute(
        "SELECT table_name, column_name, column_type, is_nullable "
        "FROM information_schema.columns "
        "WHERE table_schema = DATABASE() AND column_default IS NOT NULL "
        "AND INSTR(column_default, '%M') > 0"
    )
    cols = cur.fetchall()
    print("待修复列数:", len(cols))
    good_default = "DATE_FORMAT(UTC_TIMESTAMP(), '%Y-%m-%d %H:%i:%S')"
    for t, c, ctype, nullable in cols:
        null_sql = "NOT NULL" if nullable == "NO" else "NULL"
        cur.execute(
            f"ALTER TABLE `{t}` MODIFY `{c}` {ctype} {null_sql} DEFAULT ({good_default})"
        )
        cur.execute(
            f"UPDATE `{t}` SET `{c}` = REGEXP_REPLACE(`{c}`, ':[A-Za-z]+:', ':00:') "
            f"WHERE REGEXP_LIKE(`{c}`, ':[A-Za-z]+:')"
        )
        if cur.rowcount:
            print("数据修复:", t, c, cur.rowcount, "行")
    print("全部修复完成")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
