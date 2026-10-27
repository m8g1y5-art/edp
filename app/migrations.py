"""数据库结构的版本化升级。

每个版本一个函数，按版本号顺序执行，执行过的记在 schema_version 表里，不会重复执行。
升级前自动把数据库备份到 data/backups/，出问题可以用 `python -m app.manage db rollback` 恢复到升级前。

加新版本的规则：
  1. 在 MIGRATIONS 末尾追加 (版本号, 说明, 函数)，版本号连续递增；
  2. 函数里只用 c.execute(...)（同一个事务里执行，失败会整体撤销）；
  3. 已发布的版本函数不要再改，改结构就再加一个新版本。
"""
import shutil
import sqlite3
import time
from pathlib import Path

from .db import DATA, DB_PATH, db, _lock
from .version import VERSION

BACKUPS = DATA / "backups"


def _cols(c, table):
    return {r[1] for r in c.execute(f"PRAGMA table_info({table})").fetchall()}


def m1_baseline(c):
    """本机试用版的结构：项目层级、批次优先级和备注"""
    cols = _cols(c, "batches")
    for col, ddl in (("project_id", "TEXT"), ("prio", "INTEGER DEFAULT 0"), ("note", "TEXT DEFAULT ''")):
        if col not in cols:
            c.execute(f"ALTER TABLE batches ADD COLUMN {col} {ddl}")
    if not c.execute("SELECT 1 FROM projects").fetchone():
        c.execute("INSERT INTO projects(id,name,note,created) VALUES('default','默认项目','',?)", (time.time(),))
    first = c.execute("SELECT id FROM projects ORDER BY created LIMIT 1").fetchone()[0]
    c.execute("UPDATE batches SET project_id=? WHERE project_id IS NULL OR project_id='' "
              "OR project_id NOT IN (SELECT id FROM projects)", (first,))


def m2_server(c):
    """部署版：账号与会话、操作日志、跨进程的调度状态（暂停、作废、正在运行、worker 心跳）"""
    c.execute("""CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL, name TEXT DEFAULT '',
        pw TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'annotator', disabled INTEGER DEFAULT 0,
        created REAL, last_login REAL)""")
    c.execute("""CREATE TABLE IF NOT EXISTS sessions(
        token TEXT PRIMARY KEY, user_id INTEGER, created REAL, expires REAL, ip TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS audit(
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, user TEXT, role TEXT, method TEXT, path TEXT,
        status INTEGER, detail TEXT DEFAULT '', ip TEXT)""")
    c.execute("CREATE INDEX IF NOT EXISTS ix_audit_ts ON audit(ts)")
    c.execute("CREATE TABLE IF NOT EXISTS paused(batch_id TEXT PRIMARY KEY, ts REAL)")
    c.execute("CREATE TABLE IF NOT EXISTS rec_gen(rec_id TEXT PRIMARY KEY, gen INTEGER DEFAULT 0)")
    c.execute("""CREATE TABLE IF NOT EXISTS running(
        rec_id TEXT PRIMARY KEY, bid TEXT, key TEXT, res TEXT, dev TEXT, since REAL, worker TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS workers(
        id TEXT PRIMARY KEY, host TEXT, pid INTEGER, started REAL, seen REAL, info TEXT DEFAULT '{}')""")


MIGRATIONS = [
    (1, "基线：本机试用版的项目层级", m1_baseline),
    (2, "部署版：账号、会话、操作日志、调度状态", m2_server),
]
LATEST = MIGRATIONS[-1][0]


def _ensure_table(c):
    c.execute("""CREATE TABLE IF NOT EXISTS schema_version(
        version INTEGER PRIMARY KEY, name TEXT, applied REAL, app_version TEXT)""")


def current():
    with _lock:
        c = db()
        _ensure_table(c)
        r = c.execute("SELECT MAX(version) FROM schema_version").fetchone()
        c.commit()
    return r[0] or 0


def history():
    with _lock:
        c = db()
        _ensure_table(c)
        rows = c.execute("SELECT version, name, applied, app_version FROM schema_version ORDER BY version").fetchall()
        c.commit()
    return [dict(version=r[0], name=r[1], applied=r[2], app_version=r[3]) for r in rows]


def backup(tag):
    """用 SQLite 自带的在线备份把数据库复制一份（服务在跑也能安全复制）"""
    BACKUPS.mkdir(parents=True, exist_ok=True)
    dst = BACKUPS / f"app-{time.strftime('%Y%m%d-%H%M%S')}-{tag}.db"
    with _lock:
        out = sqlite3.connect(dst)
        db().backup(out)
        out.close()
    return dst


def upgrade(log=print):
    """把数据库升级到当前程序要求的版本；已是最新则什么都不做"""
    with _lock:
        c = db()
        _ensure_table(c)
        c.commit()
        c.execute("BEGIN IMMEDIATE")  # 网页服务和 worker 同时启动时，只有一个进程执行升级
        try:
            cur = c.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] or 0
            if cur > LATEST:
                c.rollback()
                raise SystemExit(f"数据库版本（{cur}）比程序支持的（{LATEST}）新：请升级程序，"
                                 f"或用 `python -m app.manage db rollback` 恢复旧备份")
            todo = [m for m in MIGRATIONS if m[0] > cur]
            if not todo:
                c.commit()
                return []
            has_data = c.execute("SELECT 1 FROM batches LIMIT 1").fetchone() is not None
            c.commit()
        except BaseException:
            try:
                c.rollback()
            except Exception:
                pass
            raise
        bk = backup(f"v{cur}") if (cur or has_data) else None
        if bk:
            log(f"数据库升级前已备份：{bk}")
        done = []
        c.execute("BEGIN IMMEDIATE")
        try:
            cur = c.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] or 0
            for ver, name, fn in MIGRATIONS:
                if ver <= cur:
                    continue
                fn(c)
                c.execute("INSERT INTO schema_version(version,name,applied,app_version) VALUES(?,?,?,?)",
                          (ver, name, time.time(), VERSION))
                done.append(ver)
            c.commit()
        except BaseException:
            c.rollback()
            raise
        for v in done:
            log(f"数据库已升级到第 {v} 版：{dict((m[0], m[1]) for m in MIGRATIONS)[v]}")
        return done


def backups():
    if not BACKUPS.exists():
        return []
    return sorted(BACKUPS.glob("app-*.db"), key=lambda p: p.stat().st_mtime, reverse=True)


def restore(path, log=print):
    """用备份文件替换当前数据库（先把当前数据库也备份一份，所以恢复本身也能撤销）。服务必须先停掉"""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)
    keep = backup("before-restore")
    log(f"当前数据库已另存为：{keep}")
    from . import db as D
    with _lock:
        if D._C is not None:
            D._C.close()
            D._C = None
        for suf in ("-wal", "-shm"):
            p = Path(str(DB_PATH) + suf)
            if p.exists():
                p.unlink()
        shutil.copy2(path, DB_PATH)
    log(f"已恢复：{path.name}")
