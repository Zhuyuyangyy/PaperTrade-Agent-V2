"""SQLite 一致性快照（Sprint 0）。

为什么不能用 `shutil.copy`：账本是 WAL 模式，最近已提交的事务还在 `-wal`
文件里，只拷主文件的副本读不到它们。审计窗口的**末端**恰好是最新结论依赖的
部分，复制法会静默丢数据（不是报错，是少几行）。

`sqlite3.Connection.backup()` 走 SQLite 的在线备份协议，拿到的是一致的快照。
所有"复制账本再审计"的调用点都必须走这里。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path


def snapshot(src: Path | str, dst: Path | str) -> Path:
    """把 src 的一致性快照写到 dst（含未 checkpoint 的 WAL 内容）。"""
    src_p, dst_p = Path(src), Path(dst)
    if not src_p.is_file():
        raise FileNotFoundError(f"账本不存在: {src_p}")
    dst_p.parent.mkdir(parents=True, exist_ok=True)
    # 只读 URI：绝不让快照过程写活账本
    uri = f"file:{src_p.as_posix()}?mode=ro"
    src_conn = sqlite3.connect(uri, uri=True)
    try:
        dst_conn = sqlite3.connect(str(dst_p))
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
    finally:
        src_conn.close()
    return dst_p