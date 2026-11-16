"""离线核对 Bellis 源提交的锁定副本；不生成、修改契约或判断合入状态。"""

import hashlib
import json
import re
import sys
from pathlib import Path
from typing import cast

REPO_ROOT = Path(__file__).resolve().parents[4]
VENDOR_DIR = REPO_ROOT / "vendor" / "bellis-protocol"
REPOSITORY = "https://github.com/leafliber/bellis"
# 本地路径到唯一上游源路径。清单不能扩大本工具读取的文件范围。
SOURCES = {
    "schemas/game-bridge.schema.json": "packages/protocol/schemas/game-bridge.schema.json",
    "models/game_bridge.py": "packages/protocol/models/game_bridge.py",
    "fixtures/game-control.json": "packages/protocol/test/fixtures/game-control.json",
    "schemas/game-bridge-v2.schema.json": "packages/protocol/schemas/game-bridge-v2.schema.json",
    "models/game_bridge_v2.py": "packages/protocol/models/game_bridge_v2.py",
    "fixtures/game-main-v2.json": "packages/protocol/test/fixtures/game-main-v2.json",
}


def differences(root: Path) -> list[str]:
    """缺失、多余、损坏的文件或清单均失败；只按固定的相对路径读取。"""
    try:
        raw: object = json.loads((root / "manifest.json").read_bytes())
    except (OSError, ValueError) as error:
        return [f"cannot read manifest: {error}"]
    if not isinstance(raw, dict):
        return ["invalid manifest fields"]
    manifest = cast(dict[str, object], raw)
    if set(manifest) != {
        "format",
        "repository",
        "commit",
        "files",
    }:
        return ["invalid manifest fields"]
    if type(manifest["format"]) is not int or manifest["format"] != 1:
        return ["unsupported manifest format"]
    if manifest["repository"] != REPOSITORY:
        return ["unexpected source repository"]
    if not isinstance(manifest["commit"], str) or not re.fullmatch(
        r"[0-9a-f]{40}", manifest["commit"]
    ):
        return ["source commit must be a full lowercase Git SHA"]
    raw_files = manifest["files"]
    if not isinstance(raw_files, dict):
        return ["manifest must contain exactly the game bridge schema, models and fixtures"]
    files = cast(dict[str, object], raw_files)
    if set(files) != set(SOURCES):
        return ["manifest must contain exactly the game bridge schema, models and fixtures"]
    errors: list[str] = []
    actual = {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}
    for extra in sorted(actual - {*SOURCES, "manifest.json"}):
        errors.append(f"unexpected file: {extra}")
    for local, source in SOURCES.items():
        raw_entry = files[local]
        if not isinstance(raw_entry, dict):
            errors.append(f"invalid source or digest: {local}")
            continue
        entry = cast(dict[str, object], raw_entry)
        if (
            set(entry) != {"source", "sha256"}
            or entry["source"] != source
            or not isinstance(entry["sha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])
        ):
            errors.append(f"invalid source or digest: {local}")
            continue
        try:
            data = (root / local).read_bytes()
        except OSError as error:
            errors.append(f"cannot read {local}: {error}")
            continue
        if hashlib.sha256(data).hexdigest() != entry["sha256"]:
            errors.append(f"digest mismatch: {local}")
    return errors


def main() -> int:
    errors = differences(VENDOR_DIR)
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    print(f"Bellis game bridge: all {len(SOURCES)} pinned artifacts match their SHA-256 digests")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
