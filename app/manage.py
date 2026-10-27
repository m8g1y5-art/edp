"""运维命令行。

  python -m app.manage version
  python -m app.manage create-admin <用户名>            # 交互输入密码；也可 --password
  python -m app.manage reset-password <用户名>
  python -m app.manage users
  python -m app.manage db status                        # 当前数据库版本、升级记录、备份列表
  python -m app.manage db upgrade                       # 手动升级（平时启动时会自动升级）
  python -m app.manage db backup
  python -m app.manage db rollback [备份文件名]          # 恢复到某个备份（默认最近一个），执行前先停掉服务
  python -m app.manage license show
  python -m app.manage license fingerprint               # 本机指纹，发给供应商签发绑定本机的许可证
  python -m app.manage license install <文件>
"""
import argparse
import getpass
import json
import sys
import time


def _pw(a, confirm=True):
    if getattr(a, "password", None):
        return a.password
    p1 = getpass.getpass("新密码（至少 8 位，含字母和数字）：")
    if confirm and getpass.getpass("再输一遍：") != p1:
        sys.exit("两次输入不一致")
    return p1


def main():
    ap = argparse.ArgumentParser(prog="python -m app.manage")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("version")
    c = sub.add_parser("create-admin"); c.add_argument("username"); c.add_argument("--password"); c.add_argument("--name", default="")
    r = sub.add_parser("reset-password"); r.add_argument("username"); r.add_argument("--password")
    sub.add_parser("users")
    d = sub.add_parser("db"); d.add_argument("action", choices=["status", "upgrade", "backup", "rollback"]); d.add_argument("file", nargs="?")
    lc = sub.add_parser("license"); lc.add_argument("action", choices=["show", "fingerprint", "install"]); lc.add_argument("file", nargs="?")
    a = ap.parse_args()

    from .version import VERSION
    if a.cmd == "version":
        print(VERSION)
        return
    from . import migrations
    if a.cmd == "db" and a.action == "rollback":
        bks = migrations.backups()
        if not bks:
            sys.exit("没有备份")
        target = next((b for b in bks if b.name == a.file), None) if a.file else bks[0]
        if not target:
            sys.exit("找不到这个备份。可用的备份：\n" + "\n".join(b.name for b in bks))
        print(f"将恢复 {target.name}（{time.strftime('%Y-%m-%d %H:%M', time.localtime(target.stat().st_mtime))}）")
        print("请确认网页服务和 worker 都已停止。")
        if input("输入 yes 继续：").strip() != "yes":
            sys.exit("已取消")
        migrations.restore(target)
        print("完成。注意：恢复到旧版本的数据库后，需要用对应的旧版本程序启动，或重新启动新版本程序让它再次升级。")
        return

    from .db import migrate, q
    migrate()
    from . import auth
    if a.cmd == "create-admin":
        try:
            u = auth.create_user(a.username, _pw(a), "admin", a.name)
        except ValueError as e:
            sys.exit(str(e))
        print(f"已创建管理员：{u['username']}")
    elif a.cmd == "reset-password":
        u = q("SELECT id FROM users WHERE username=?", (a.username,), one=True)
        if not u:
            sys.exit("没有这个用户")
        try:
            auth.update_user(u["id"], pw=_pw(a), disabled=False)
        except ValueError as e:
            sys.exit(str(e))
        print("密码已重置，该用户的旧登录已全部失效")
    elif a.cmd == "users":
        for u in auth.users():
            print(f"{u['id']:>3}  {u['username']:<20} {auth.ROLES[u['role']]:<6} {'已停用' if u['disabled'] else ''}")
    elif a.cmd == "db":
        if a.action == "status":
            print(f"数据库版本：{migrations.current()}（程序要求 {migrations.LATEST}）")
            for h in migrations.history():
                print(f"  v{h['version']}  {time.strftime('%Y-%m-%d %H:%M', time.localtime(h['applied']))}  程序 {h['app_version']}  {h['name']}")
            print("备份：")
            for b in migrations.backups()[:20]:
                print("  " + b.name)
        elif a.action == "upgrade":
            print("已是最新" if not migrations.upgrade() else "完成")
        elif a.action == "backup":
            print(migrations.backup("manual"))
    elif a.cmd == "license":
        from . import license as L
        if a.action == "fingerprint":
            print(L.fingerprint() or "取不到本机指纹")
        elif a.action == "install":
            if not a.file:
                sys.exit("请给出许可证文件路径")
            try:
                st = L.install(open(a.file, encoding="utf-8").read())
            except ValueError as e:
                sys.exit(str(e))
            print("已安装：", "有效" if st["valid"] else st["reason"])
        else:
            st = L.status(force=True)
            print(json.dumps({k: st[k] for k in ("valid", "reason", "days_left", "info", "fingerprint", "path")}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
