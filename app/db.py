"""SQLite 存储：批次、录制、步骤任务、事件、设置。
大块结果（时间戳、关键点、分段）放在工作区文件里，数据库只存状态和小 JSON。"""
import json
import sqlite3
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)
DB_PATH = DATA / "app.db"
_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches(
  id TEXT PRIMARY KEY, name TEXT, src TEXT, mode TEXT, created REAL,
  status TEXT, progress REAL DEFAULT 0, msg TEXT DEFAULT '', info TEXT DEFAULT '{}', opts TEXT DEFAULT '{}');
CREATE TABLE IF NOT EXISTS recordings(
  id TEXT PRIMARY KEY, batch_id TEXT, name TEXT, rel TEXT, data TEXT, status TEXT DEFAULT 'scanned',
  grade INTEGER DEFAULT 0, edits TEXT DEFAULT '{}');
CREATE TABLE IF NOT EXISTS steps(
  rec_id TEXT, key TEXT, ord INTEGER, label TEXT, mode TEXT, status TEXT, progress REAL DEFAULT 0,
  msg TEXT DEFAULT '', result TEXT DEFAULT '{}', started REAL, finished REAL, PRIMARY KEY(rec_id, key));
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, batch_id TEXT, rec_id TEXT, level TEXT, msg TEXT);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS batch_reqs(
  key TEXT PRIMARY KEY, batch_id TEXT, rec_id TEXT, step_key TEXT, model TEXT, status TEXT, job_id TEXT,
  attempts INTEGER DEFAULT 0, result TEXT, err TEXT, created REAL, size INTEGER);
CREATE INDEX IF NOT EXISTS ix_breq_status ON batch_reqs(status, model);
CREATE INDEX IF NOT EXISTS ix_breq_step ON batch_reqs(rec_id, step_key);
CREATE TABLE IF NOT EXISTS batch_jobs(
  id TEXT PRIMARY KEY, model TEXT, n INTEGER, status TEXT, created REAL, updated REAL, input_file TEXT,
  output_file TEXT, error_file TEXT, done INTEGER, failed INTEGER, msg TEXT, ingested INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS projects(
  id TEXT PRIMARY KEY, name TEXT, note TEXT DEFAULT '', created REAL);
"""


def conn():
    c = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
    c.row_factory = sqlite3.Row
    return c


_C = None


def db():
    global _C
    if _C is None:
        _C = conn()
        _C.executescript(SCHEMA)
        _C.execute("PRAGMA journal_mode=WAL")
    return _C


def q(sql, args=(), one=False):
    with _lock:
        cur = db().execute(sql, args)
        rows = [dict(r) for r in cur.fetchall()]
        db().commit()
    return (rows[0] if rows else None) if one else rows


def x(sql, args=()):
    with _lock:
        db().execute(sql, args)
        db().commit()


def jl(s, default=None):
    try:
        return json.loads(s) if s else (default if default is not None else {})
    except Exception:
        return default if default is not None else {}


def jd(o):
    return json.dumps(o, ensure_ascii=False, default=_default)


def _default(o):
    try:
        import numpy as np
        if isinstance(o, np.integer):
            return int(o)
        if isinstance(o, np.floating):
            return float(o)
        if isinstance(o, np.bool_):
            return bool(o)
        if isinstance(o, np.ndarray):
            return o.tolist()
    except Exception:
        pass
    if isinstance(o, Path):
        return str(o)
    raise TypeError(type(o))


def event(batch_id, rec_id, level, msg):
    x("INSERT INTO events(ts,batch_id,rec_id,level,msg) VALUES(?,?,?,?,?)", (time.time(), batch_id, rec_id, level, msg))


DEFAULT_SETTINGS = {
    "vlm_backend": "ollama",  # ollama = 本机模型；api = 外部 OpenAI 兼容接口（如阿里云百炼）
    "api_base": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "api_key": "",
    "api_model": "qwen3.7-plus",
    "ollama_url": "http://127.0.0.1:11434",
    "vlm_model": "",          # 空 = 自动选第一个视觉模型
    "vlm_fps": 1,             # 每段按每秒几帧取图
    "vlm_max_frames": 6,      # 每段最多送几帧
    "vlm_ctx": 8192,          # 模型上下文长度：越大越占显存，8 GB 显卡建议 8192
    "vlm_timeout": 180,       # 每次调用最多等几秒
    "contact_gap": 0.8,
    "contact_dist_cm": 2.0,
    "contact_min": 0.4,
    "tool_min_s": 4.0,
    "batch_window_h": 72,     # 百炼批量任务最长等待（小时，24–336）
    "batch_max_reqs": 5000,   # 每个批量文件最多多少个请求
    "batch_flush_min": 10,    # 请求不够一包时，最多等这么多分钟就先提交        # 一只手稳定握住同一物体超过这么多秒，当作工具；0 = 不识别工具  # 接触短于这个时长（秒）不算：遮挡、手从物体上方掠过造成的误判
    "contact_mode": "3d",     # 3d = 指尖到物体表面的三维距离（需要深度）；2d = 旧的二维轮廓规则   # 指尖到物体表面的三维距离小于这个值算接触       # 接触中断短于这个秒数，算同一次接触
    "min_seg": 1.0,           # 分段最短秒数
    "vocab": None,
    "vlm_model_light": "",    # 简单段用的小模型（如 qwen3-vl:4b）；空 = 都用主模型
    "inventory_mode": "vlm",  # detector = 大模型列名词 + 检测模型画框；vlm = 全部由大模型完成
    "inventory_zoom": False,
    "inventory_max": 20,      # 物体清单最多几个物体
    "inventory_reach_m": 1.5, # 物体清单只列这个距离以内的物体（米）；0 = 不按距离过滤   # 纯大模型方案时，是否分块放大找小物体
    "track_fps": 5,
    "mask_width": 640,        # 物体掩码保存的宽度（像素）           # SAM 2 每秒跟踪几帧
    "fine_scope": "all",      # all = 都做精细标注；good = 画面质量差的跳过
    "fine_sample_pct": 100,   # 精细标注抽样比例（%）            # 动作词表；空 = 用内置默认词表
    "vocab_version": 1,
    "max_segments": 8,
    "fine_annotation": True,  # 物体跟踪 + 接触事件的精细标注
    "edge_tol_cm": 1.0,       # 交界贴合容差：手和物体在交界处深度差小于这个值的像素算“贴合”（原来固定 2.5 cm）
    "max_jobs": 0,            # 总并发上限：0 = 按本机资源自动；1 = 一次只跑一个步骤（旧行为）
    "gpu_jobs": 0,            # 每张显卡同时跑几个物体跟踪：0 = 按显存自动
    "api_parallel": 4,        # 外部接口实时调用时同时发几个请求
    "offlist_refind": True,   # 清单外补找：语义标注发现清单外物体时，自动在那一段框出、加进清单，重跑跟踪→语义（每条录制一轮）
}


_MIGRATIONS = {
    # 第 2 版默认值：8 GB 显卡上 12 帧 + 16k 上下文会让模型溢出到 CPU，改成更省的配置
    "defaults_v2": {"vlm_fps": 1, "vlm_max_frames": 6},
    # 检测模型（YOLOE）方案在实际数据上不准，物体清单改回大模型
    "defaults_v3": {"inventory_mode": "vlm"},
    # 大批量处理时小物体放大识别收益小（实测约 8% 的物体靠它找到，多为背景），默认关闭，需要时在设置页打开
    "defaults_v4": {"inventory_zoom": False},
}


def migrate():
    # 层级：项目 → 批次 → 录制。老数据库补列，没有归属的批次放进“默认项目”
    cols = {r["name"] for r in q("PRAGMA table_info(batches)")}
    for col, ddl in (("project_id", "TEXT"), ("prio", "INTEGER DEFAULT 0"), ("note", "TEXT DEFAULT ''")):
        if col not in cols:
            x(f"ALTER TABLE batches ADD COLUMN {col} {ddl}")
    if not q("SELECT 1 FROM projects", one=True):
        x("INSERT INTO projects(id,name,note,created) VALUES('default','默认项目','',?)", (time.time(),))
    first = q("SELECT id FROM projects ORDER BY created LIMIT 1", one=True)["id"]
    x("UPDATE batches SET project_id=? WHERE project_id IS NULL OR project_id='' OR project_id NOT IN (SELECT id FROM projects)", (first,))
    for flag, vals in _MIGRATIONS.items():
        if not q("SELECT 1 FROM settings WHERE key=?", (flag,), one=True):
            for k, v in vals.items():
                x("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (k, jd(v)))
            x("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (flag, jd(True)))
    # 部署预设：程序根目录下的 preset.json（例如本机 Mage-VL 语义服务的地址），每个文件内容只套用一次
    pf = ROOT / "preset.json"
    if pf.exists():
        try:
            raw = pf.read_text(encoding="utf-8")
            flag = "preset_" + __import__("hashlib").sha1(raw.encode()).hexdigest()[:10]
            if not q("SELECT 1 FROM settings WHERE key=?", (flag,), one=True):
                for k, v in json.loads(raw).items():
                    if not k.startswith("_"):
                        x("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (k, jd(v)))
                x("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (flag, jd(True)))
        except Exception as e:
            print("preset.json 读取失败：", e)


def get_setting(k):
    r = q("SELECT value FROM settings WHERE key=?", (k,), one=True)
    return jl(r["value"], None) if r else DEFAULT_SETTINGS.get(k)


def set_setting(k, v):
    x("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (k, jd(v)))


def all_settings():
    out = dict(DEFAULT_SETTINGS)
    for r in q("SELECT key,value FROM settings"):
        out[r["key"]] = jl(r["value"], None)
    return out


def work_dir(batch_id, rec_id=None):
    p = DATA / "batches" / batch_id
    if rec_id:
        p = p / rec_id
    p.mkdir(parents=True, exist_ok=True)
    return p
