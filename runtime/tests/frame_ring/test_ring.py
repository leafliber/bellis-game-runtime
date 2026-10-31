"""主进程一侧的帧环：创建与命名、读最新一帧的 seqlock 规则、帧龄、销毁与安全设置。

写帧用只在测试中使用的假显示面（它就是一个帧环，写法与实时核相同）；与 Rust 写入方的互通见
`test_interop.py`。
"""

import sys
import time

import pytest

from core_protocol.frame_ring import (
    LAYOUT_VERSION,
    SLOT_ALIGN,
    SLOT_PIXELS_OFFSET,
    SLOTS_OFFSET,
)
from game_runtime._shm import create_shared_memory, shared_memory_name
from game_runtime.frame_ring import (
    DEFAULT_SLOT_COUNT,
    FrameRing,
    FrameRingCorruptError,
    FrameRingError,
    FrameRingTornError,
    RingFrame,
    frame_age_ns,
    slot_stride_for,
)
from game_runtime.frame_ring.fake import FakeDisplay


def solid(width: int, height: int, value: int, row_stride: int | None = None) -> bytes:
    """每个像素都是 (value, value+1, value+2, 255) 的帧，行尾填充 0xEE。"""
    stride = width * 4 if row_stride is None else row_stride
    row = bytes([value, value + 1, value + 2, 255]) * width + b"\xee" * (stride - width * 4)
    return row * height


class Probe(FakeDisplay):
    """测试用：直接改槽的 seqlock 序号与头部，模拟写方的各种中间状态。"""

    copies: int = 0
    on_copy: tuple[int, ...] = ()
    """第几次拷贝之后让 seq 变化，像是写方在拷贝期间写了这一槽。"""

    def bump_seq(self, slot: int, by: int) -> None:
        self._open_views().slots[slot].seq += by

    def set_width(self, slot: int, width: int) -> None:
        self._open_views().slots[slot].width = width

    def set_write_seq(self, number: int) -> None:
        self._open_views().header.write_seq = number

    def _copy_pixels(self, slot: int, length: int) -> bytes:
        pixels = super()._copy_pixels(slot, length)
        self.copies += 1
        if self.copies in self.on_copy:
            self.bump_seq(slot, 2)
        return pixels


def test_create_lays_out_the_ring_and_describes_it_for_attach() -> None:
    with FrameRing.create(320, 180) as ring:
        params = ring.attach_params()
        stride = slot_stride_for(320, 180)
        assert stride % SLOT_ALIGN == 0
        assert (
            SLOT_PIXELS_OFFSET + 320 * 180 * 4 <= stride < SLOT_PIXELS_OFFSET + 320 * 180 * 4 + 64
        )
        assert params.layout_version == LAYOUT_VERSION
        assert (params.slot_count, params.slot_stride) == (DEFAULT_SLOT_COUNT, stride)
        assert (params.max_width, params.max_height) == (320, 180)
        assert params.size_bytes == SLOTS_OFFSET + DEFAULT_SLOT_COUNT * stride
        assert params.name == ring.name
        prefix = "Local\\bellis-frames-" if sys.platform == "win32" else "/bellis-frames-"
        assert params.name.startswith(prefix)
        assert ring.latest_frame_number() == 0
        assert ring.read_latest() is None


def test_names_are_random() -> None:
    names = {shared_memory_name("frames") for _ in range(100)}
    assert len(names) == 100
    with FrameRing.create(4, 4) as first, FrameRing.create(4, 4) as second:
        assert first.name != second.name


@pytest.mark.parametrize(
    ("width", "height", "slots"),
    [(0, 10, 3), (10, 0, 3), (2**32, 10, 3), (10, 10, 1)],
)
def test_create_rejects_impossible_sizes(width: int, height: int, slots: int) -> None:
    with pytest.raises(ValueError, match=r"尺寸|槽数"):
        FrameRing.create(width, height, slot_count=slots)


def test_presented_frames_read_back_with_their_metadata() -> None:
    with FakeDisplay.create(8, 6) as display:
        assert display.present(solid(8, 6, 10), 8, 6, capture_ns=1_000, transform_version=2) == 1
        pixels = solid(5, 3, 20, row_stride=24)
        assert display.present(pixels, 5, 3, row_stride=24, capture_ns=2_000) == 2
        assert display.latest_frame_number() == 2
        assert display.read_latest() == RingFrame(
            frame_number=2,
            capture_ns=2_000,
            transform_version=1,
            width=5,
            height=3,
            row_stride=24,
            pixels=pixels,
        )


def test_present_rejects_frames_that_do_not_fit() -> None:
    with FakeDisplay.create(8, 6) as display:
        with pytest.raises(ValueError, match="超出"):
            display.present(solid(9, 6, 0), 9, 6)
        with pytest.raises(ValueError, match="行跨度"):
            display.present(solid(8, 6, 0), 8, 6, row_stride=31)
        with pytest.raises(ValueError, match="放不进"):
            display.present(solid(8, 6, 0, row_stride=64), 8, 6, row_stride=64)
        with pytest.raises(ValueError, match="少于"):
            display.present(solid(8, 6, 0)[:-1], 8, 6)
        assert display.latest_frame_number() == 0


def test_reader_retries_while_the_slot_is_being_written_and_then_gives_up() -> None:
    with Probe.create(4, 4, slot_count=2) as display:
        display.present(solid(4, 4, 1), 4, 4)
        display.bump_seq(0, 1)  # 写方正在写这一槽：seq 为奇数
        with pytest.raises(FrameRingTornError):
            display.read_latest(max_attempts=3)
        assert display.copies == 0, "an odd seq is never copied"
        display.bump_seq(0, 1)
        frame = display.read_latest(max_attempts=1)
        assert frame is not None
        assert frame.frame_number == 1


def test_reader_discards_a_copy_when_the_seq_moves_during_it() -> None:
    with Probe.create(4, 4, slot_count=2) as display:
        display.on_copy = (1, 2)
        display.present(solid(4, 4, 1), 4, 4)
        with pytest.raises(FrameRingTornError):
            display.read_latest(max_attempts=2)
        frame = display.read_latest(max_attempts=1)
        assert frame is not None
        assert display.copies == 3


def test_reader_skips_a_slot_that_holds_another_frame() -> None:
    with Probe.create(4, 4, slot_count=2) as display:
        display.present(solid(4, 4, 1), 4, 4)
        display.present(solid(4, 4, 2), 4, 4)
        # write_seq 指向帧 3，它的槽 0 里还是帧 1：写方已经超过这一槽或还没写完它。
        display.set_write_seq(3)
        with pytest.raises(FrameRingTornError):
            display.read_latest(max_attempts=2)


def test_reader_rejects_a_stable_slot_that_breaks_the_layout() -> None:
    with Probe.create(4, 4, slot_count=2) as display:
        display.present(solid(4, 4, 1), 4, 4)
        display.set_width(0, 5)
        with pytest.raises(FrameRingCorruptError, match="超过"):
            display.read_latest()


def test_frame_age_is_never_negative() -> None:
    assert frame_age_ns(1_000, now_ns=1_500) == 500
    assert frame_age_ns(2_000, now_ns=1_500) == 0
    before = time.perf_counter_ns()
    assert 0 <= frame_age_ns(before) < 10_000_000_000
    assert frame_age_ns(time.perf_counter_ns() + 5_000_000) == 0


def test_close_destroys_the_ring_and_is_idempotent() -> None:
    ring = FrameRing.create(4, 4)
    name = ring.name
    assert shared_memory_exists(name)
    ring.close()
    ring.close()
    assert ring.closed
    assert not shared_memory_exists(name)
    with pytest.raises(FrameRingError, match="已关闭"):
        ring.read_latest()


def test_an_existing_name_is_not_reused() -> None:
    name = shared_memory_name("frames")
    first = create_shared_memory(name, 4096)
    try:
        with pytest.raises(FileExistsError):
            create_shared_memory(name, 4096)
        assert not first.closed
    finally:
        first.close()


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    from game_runtime._winsecurity import current_user_sid

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _adv = ctypes.WinDLL("advapi32", use_last_error=True)
    _k32.OpenFileMappingW.restype = wintypes.HANDLE
    _k32.OpenFileMappingW.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR)
    _k32.CloseHandle.argtypes = (wintypes.HANDLE,)
    _k32.LocalFree.argtypes = (wintypes.HLOCAL,)
    _adv.GetSecurityInfo.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.LPVOID),
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.LPVOID),
    )
    _adv.GetSecurityInfo.restype = wintypes.DWORD
    _adv.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = (
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPWSTR),
        wintypes.LPVOID,
    )
    _READ_CONTROL = 0x00020000
    _FILE_MAP_READ = 0x0004
    _ERROR_FILE_NOT_FOUND = 2

    def _open_mapping(name: str, access: int) -> tuple[int | None, int]:
        handle = _k32.OpenFileMappingW(access, False, name)
        return handle, ctypes.get_last_error()

    def shared_memory_exists(name: str) -> bool:
        handle, error = _open_mapping(name, _FILE_MAP_READ)
        if handle is None:
            assert error == _ERROR_FILE_NOT_FOUND, error
            return False
        _k32.CloseHandle(handle)
        return True

    def _dacl(handle: int) -> str:
        dacl = wintypes.LPVOID()
        descriptor = wintypes.LPVOID()
        # SE_KERNEL_OBJECT = 6，DACL_SECURITY_INFORMATION = 4
        status = _adv.GetSecurityInfo(
            handle, 6, 4, None, None, ctypes.byref(dacl), None, ctypes.byref(descriptor)
        )
        assert status == 0, status
        text = wintypes.LPWSTR()
        ok = _adv.ConvertSecurityDescriptorToStringSecurityDescriptorW(
            descriptor, 1, 4, ctypes.byref(text), None
        )
        assert ok, ctypes.get_last_error()
        value = text.value
        _k32.LocalFree(ctypes.cast(text, wintypes.HLOCAL))
        _k32.LocalFree(descriptor)
        assert value is not None
        return value

    def test_mapping_dacl_allows_only_the_current_user() -> None:
        with FrameRing.create(4, 4) as ring:
            handle, error = _open_mapping(ring.name, _READ_CONTROL)
            assert handle is not None, error
            try:
                # GA 在文件映射对象上映射为 SECTION_ALL_ACCESS（0xf001f），
                # SDDL 按位写成 CC DC LC SW RP SD RC WD WO。
                rights = "CCDCLCSWRPSDRCWDWO"
                assert _dacl(handle) == f"D:P(A;;{rights};;;{current_user_sid()})"
            finally:
                _k32.CloseHandle(handle)

else:
    import os

    from game_runtime._shm import posix_shm_open

    def shared_memory_exists(name: str) -> bool:
        try:
            fd = posix_shm_open(name, os.O_RDONLY)
        except FileNotFoundError:
            return False
        os.close(fd)
        return True
