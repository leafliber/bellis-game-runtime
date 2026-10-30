"""安装目录检查的逻辑（GS-03 第 4 项的非提权侧）。

真实的管理员目录由维护者做；这里用一个 ACL 受限目录模拟“拒绝写入”，验证检查逻辑把每种写操作都判为
被拒。可写目录上则应判为未拒，确保逻辑不是恒为真。
"""

import sys
from pathlib import Path

from gs03_probe.install_check import all_denied, check_install_dir


def _seed(directory: Path) -> str:
    name = "gs03-core-probe.exe"
    (directory / name).write_bytes(b"MZ probe")
    return name


def test_writable_dir_is_not_denied(tmp_path: Path) -> None:
    # 逻辑不是恒为真：可写目录上各操作都成功，判为未拒。
    name = _seed(tmp_path)
    results = check_install_dir(tmp_path, name)
    assert not all_denied(results)
    assert any(result.verdict == "allowed" for result in results)


# ACL 受限目录用 icacls 设置，只在 Windows 上；放进 win32 分支，Linux 容器里不收集。
if sys.platform == "win32":
    import ntpath
    import os
    import subprocess

    from game_runtime._winsecurity import current_user_sid

    def _icacls() -> str:
        root = os.environ.get("SYSTEMROOT", "C:\\Windows")
        return ntpath.join(root, "System32", "icacls.exe")

    def test_acl_denied_dir_blocks_every_write(tmp_path: Path) -> None:
        protected = tmp_path / "install"
        protected.mkdir()
        name = _seed(protected)
        sid = current_user_sid()
        icacls = _icacls()
        # 拒绝当前用户在目录及其（继承到的）文件上创建、追加、删除、删除子项。
        deny = subprocess.run(
            [icacls, str(protected), "/deny", f"*{sid}:(OI)(CI)(WD,AD,DE,DC)"],
            capture_output=True,
            check=False,
        )
        assert deny.returncode == 0, deny.stderr
        try:
            results = check_install_dir(protected, name)
            assert all_denied(results), [(r.op, r.verdict, r.detail) for r in results]
        finally:
            subprocess.run(
                [icacls, str(protected), "/remove:d", f"*{sid}"], capture_output=True, check=False
            )
