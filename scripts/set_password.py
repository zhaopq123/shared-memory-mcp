#!/usr/bin/env python3
"""给指定用户设置/重置密码（服务器本机执行，密码由管理员当场输入）。

    cd /opt/shared-memory-mcp
    ./venv/bin/python scripts/set_password.py <用户名>

━━━ 为什么需要这个脚本 ━━━

首次启动时 bootstrap 会生成一个随机密码并**只打印一次**。
那个密码没人记下来 → 账号实际不可用（面板登录框填不了）。

必须有一条**不经过对话**的路径来设密码：
  · 在这里输入密码，直接进 scrypt 哈希，只写进 tenants.json
  · 明文不落盘、不进日志、不进 git

━━━ 安全设计 ━━━

  · 密码用 ``getpass`` 读（不回显）
  · 校验两次输入一致 + 长度 >= 8
  · 写盘后 chmod 600（tenants.json 含哈希）
"""

import getpass
import sys
from pathlib import Path

sys.path.insert(0, "/opt/shared-memory-mcp/src")

from shared_memory.tenancy import TenantHub  # noqa: E402
from shared_memory.tenant import User  # noqa: E402


def main() -> int:
    home = Path("/opt/shared-memory-mcp/tenant-home")
    hub = TenantHub(home)

    if len(sys.argv) < 2:
        print("用法: set_password.py <用户名>")
        print("\n现有用户:")
        for name in hub.store.users:
            u = hub.store.users[name]
            has_pw = "已设密码" if u.password else "**未设密码**"
            print(f"  · {name:<16} {u.role:<8} {has_pw}")
        return 1

    name = sys.argv[1].strip()
    u = hub.store.user(name)
    if u is None:
        print(f"用户不存在：{name}")
        return 1

    if u.password:
        print(f"⚠️  {name} 已经设过密码。继续会**覆盖**它（旧密码立即失效）。")
        if input("确认覆盖？[y/N] ").strip().lower() != "y":
            print("已取消")
            return 0

    pw1 = getpass.getpass("新密码（至少 8 位，输入不回显）: ")
    pw2 = getpass.getpass("再输一次: ")

    if len(pw1) < 8:
        print("✗ 密码至少 8 位")
        return 1
    if pw1 != pw2:
        print("✗ 两次输入不一致")
        return 1

    u.password = User.hash_password(pw1)
    hub.store.save()
    import os
    os.chmod(home / "tenants.json", 0o600)
    print(f"\n✓ 已为 {name} 设置密码（已 scrypt 哈希，只存哈希不存明文）")
    print("  现在可以用这个账号密码登录面板了。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
