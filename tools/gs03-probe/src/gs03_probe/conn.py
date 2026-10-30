"""非提权一侧交给提权一侧的连接信息。JSON 形状与 Rust 一侧 `gs03-probe` 的 `ConnInfo` 完全一致。"""

import json

from game_runtime.channel import ChannelCredentials
from game_runtime.frame_ring import FrameRing

#: 连接信息的版本，Python 与 Rust 两侧同步改动。
CONN_INFO_VERSION = 1
#: 连接角色，默认与实时核相同，走同一条服务端接受路径。
DEFAULT_ROLE = "game-runtime.core"


def build_conn_info(
    credentials: ChannelCredentials, ring: FrameRing, *, role: str, frames: int
) -> dict[str, object]:
    """组装连接信息：通道地址与密钥、角色、帧环参数、要写的帧数。"""
    return {
        "v": CONN_INFO_VERSION,
        "channel_path": credentials.path,
        "channel_key": credentials.encoded_key,
        "role": role,
        "frame_ring": json.loads(ring.attach_params().model_dump_json()),
        "frames": frames,
    }


def conn_info_json(
    credentials: ChannelCredentials, ring: FrameRing, *, role: str, frames: int
) -> str:
    """连接信息的紧凑 JSON 文本。"""
    return json.dumps(build_conn_info(credentials, ring, role=role, frames=frames))
