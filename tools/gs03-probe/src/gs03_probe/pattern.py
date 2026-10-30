"""探针写进帧环的帧图案。与 Rust 一侧 `gs03-probe` 的 `pattern` 以及 `frame-ring` 示例
`ring_interop` 相同：尺寸、采集时刻与变换版本都由帧号算出，每个像素是同一个由帧号算出的
4 字节值，非提权一侧只凭元数据即可核对整帧、发现撕裂。
"""

from game_runtime.frame_ring import RingFrame


def pattern(number: int, max_width: int, max_height: int) -> tuple[int, int, int, int, bytes]:
    """第 `number` 帧（`number >= 1`）的宽、高、采集时刻、变换版本与每个像素的 4 字节值。"""
    word = ((number * 0x9E3779B1) & 0xFFFFFFFF).to_bytes(4, "little")
    return (
        max_width - number % 7,
        max_height - number % 5,
        number * 1000 + 7,
        number // 5 + 1,
        word,
    )


def check_frame(frame: RingFrame, max_width: int, max_height: int) -> str | None:
    """帧环里的帧与其帧号对应的图案是否一致。一致为 `None`，否则为说明（供报告记录）。

    探针每次用全新的帧环，续写基数为 0，所以帧号即采集源的帧号。
    """
    width, height, capture_ns, transform_version, word = pattern(
        frame.frame_number, max_width, max_height
    )
    got = (frame.width, frame.height, frame.row_stride, frame.capture_ns, frame.transform_version)
    want = (width, height, width * 4, capture_ns, transform_version)
    if got != want:
        return f"帧 {frame.frame_number} 的元数据为 {got}，应为 {want}"
    if frame.pixels != word * (width * height):
        return f"帧 {frame.frame_number} 的像素与图案不符"
    return None
