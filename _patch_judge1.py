"""One-shot patch: JUDGE agent into the pipeline + candidates columns."""
import pathlib

# ---------- 1. candidates 表加 LLM 列（migration） ----------
p = pathlib.Path("research/memehunter.py")
s = p.read_text(encoding="utf-8")
old = '''    eligible INTEGER NOT NULL DEFAULT 0,
    reject_reason TEXT,'''
if old not in s:
    # candidates 的建表在 SCHEMA 里，找实际定义
    old = "    eligible INTEGER,\n    reject_reason TEXT"
assert old in s, "MISS candidates schema"
s = s.replace(old, old + "\n    llm_score INTEGER,\n    llm_flags TEXT,\n    llm_reason TEXT,", 1)

anchor = "def connect(path: Path) -> sqlite3.Connection:"
i = s.index(anchor)
j = s.index("\ndef ", i + 10)
body = s[i:j]
if "llm_score" not in body:
    # 在 connect/ensure 里加 ALTER 迁移
    s = s.replace(
        anchor + "\n",
        anchor + "\n",
        1,
    )
p.write_text(s, encoding="utf-8")
print("candidates schema: llm columns added")

# ---------- 2. memehunter.connect 后确保列存在 ----------
p = pathlib.Path("research/memehunter.py")
s = p.read_text(encoding="utf-8")
old = '''def now_ms() -> int:'''
new = '''def ensure_candidate_columns(conn: sqlite3.Connection) -> None:
    """Ledgers created before the JUDGE agent need the llm_* columns."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(candidates)")}
    for name, ddl in (
        ("llm_score", "ALTER TABLE candidates ADD COLUMN llm_score INTEGER"),
        ("llm_flags", "ALTER TABLE candidates ADD COLUMN llm_flags TEXT"),
        ("llm_reason", "ALTER TABLE candidates ADD COLUMN llm_reason TEXT"),
    ):
        if name not in columns:
            conn.execute(ddl)
    conn.commit()


def now_ms() -> int:'''
assert old in s
s = s.replace(old, new, 1)
p.write_text(s, encoding="utf-8")
print("ensure_candidate_columns added")
