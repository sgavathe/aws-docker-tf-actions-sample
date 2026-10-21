"""Guard for model-written SQL.

Defense in depth: the database role is already read-only with a statement timeout
(see db/init/01_roles.sh). This check rejects anything that is not a single
SELECT/WITH statement before it reaches the database, and caps the row count.
"""

import re

FORBIDDEN = re.compile(
    r"\b(insert|update|delete|merge|drop|alter|create|truncate|grant|revoke|copy|"
    r"vacuum|analyze|call|do|execute|prepare|listen|notify|set|reset|lock|"
    r"pg_read_file|pg_ls_dir|pg_sleep|lo_import|lo_export|dblink)\b",
    re.IGNORECASE,
)
COMMENTS = re.compile(r"--[^\n]*|/\*.*?\*/", re.DOTALL)
STRINGS = re.compile(r"'(?:[^']|'')*'")


class UnsafeSQL(ValueError):
    pass


def guard_select(sql: str, max_rows: int = 500) -> str:
    body = COMMENTS.sub(" ", sql).strip().rstrip(";").strip()
    if not body:
        raise UnsafeSQL("empty query")
    scan = STRINGS.sub("''", body)  # keywords inside string literals are fine
    if ";" in scan:
        raise UnsafeSQL("only a single statement is allowed")
    if not re.match(r"^(select|with)\b", scan, re.IGNORECASE):
        raise UnsafeSQL("only SELECT or WITH queries are allowed")
    hit = FORBIDDEN.search(scan)
    if hit:
        raise UnsafeSQL(f"keyword not allowed: {hit.group(0)}")
    return f"SELECT * FROM ({body}) AS q LIMIT {int(max_rows)}"
