"""变化与稳定：格子图逐帧比较，稳定时长按采集时刻计，大面积的变化是过场。

规则见 `docs/modules/perception.md` 的“变化与稳定”。帧都是合成的纯色块。
"""

import numpy as np
import pytest
from numpy.typing import NDArray

from game_runtime.frame_ring import RingFrame
from game_runtime.scene import ChangeDetector, ChangeParams, Stability, bgra_view

MS = 1_000_000


def canvas(
    width: int = 320, height: int = 180, color: tuple[int, int, int] = (40, 50, 60)
) -> NDArray[np.uint8]:
    image = np.zeros((height, width, 4), np.uint8)
    image[:, :, :3] = color
    image[:, :, 3] = 255
    return image


def test_unchanged_frames_become_stable_after_stable_after() -> None:
    detector = ChangeDetector(ChangeParams(stable_after_ns=250 * MS))
    image = canvas()

    first = detector.update(image, 1_000 * MS)
    # 没有可比的上一帧：按全部变化处理。
    assert (first.stability, first.stable_ns, first.changed_fraction) == (
        Stability.TRANSITION,
        0,
        1.0,
    )
    almost = detector.update(image, 1_249 * MS)
    assert (almost.stability, almost.stable_ns, almost.changed_fraction) == (
        Stability.CHANGING,
        249 * MS,
        0.0,
    )
    stable = detector.update(image, 1_250 * MS)
    assert (stable.stability, stable.stable_ns) == (Stability.STABLE, 250 * MS)


def test_a_small_change_is_changing_and_resets_the_stable_time() -> None:
    detector = ChangeDetector()
    image = canvas()
    detector.update(image, 0)
    assert detector.update(image, 400 * MS).stability is Stability.STABLE

    typed = image.copy()
    typed[20:30, 20:30, :3] = 245  # 像逐字显示多出一个字
    change = detector.update(typed, 433 * MS)
    assert change.stability is Stability.CHANGING
    assert change.stable_ns == 0
    assert 0 < change.changed_fraction < 0.05

    later = detector.update(typed, 600 * MS)
    assert (later.stability, later.stable_ns) == (Stability.CHANGING, 167 * MS)
    assert detector.update(typed, 683 * MS).stability is Stability.STABLE


def test_a_large_change_is_a_transition() -> None:
    detector = ChangeDetector()
    detector.update(canvas(), 0)
    detector.update(canvas(), 300 * MS)

    cut = detector.update(canvas(color=(200, 10, 10)), 333 * MS)
    assert cut.stability is Stability.TRANSITION
    assert cut.changed_fraction == 1.0
    # 切换之后只剩局部变化：变化中。
    after = canvas(color=(200, 10, 10))
    after[0:10, 0:10, :3] = 0
    assert detector.update(after, 366 * MS).stability is Stability.CHANGING


def test_a_fade_changes_every_cell_a_little() -> None:
    detector = ChangeDetector()
    detector.update(canvas(color=(100, 100, 100)), 0)
    for step in range(1, 6):
        level = 100 - step * 15
        change = detector.update(canvas(color=(level, level, level)), step * 33 * MS)
        assert change.stability is Stability.TRANSITION
        assert change.changed_fraction == 1.0


def test_differences_within_the_threshold_are_not_changes() -> None:
    detector = ChangeDetector(ChangeParams(cell_threshold=8))
    detector.update(canvas(color=(40, 50, 60)), 0)

    noise = detector.update(canvas(color=(48, 42, 60)), 100 * MS)
    assert noise.changed_fraction == 0.0
    # 与上一处理帧比较：这回 R 差 9。
    assert detector.update(canvas(color=(57, 42, 60)), 200 * MS).changed_fraction == 1.0


def test_a_new_size_has_nothing_to_compare_with() -> None:
    detector = ChangeDetector()
    detector.update(canvas(320, 180), 0)
    detector.update(canvas(320, 180), 300 * MS)

    resized = detector.update(canvas(640, 360), 333 * MS)
    assert (resized.stability, resized.changed_fraction) == (Stability.TRANSITION, 1.0)


def test_the_grid_has_the_same_cells_at_any_resolution() -> None:
    small, large = ChangeDetector(), ChangeDetector()
    for detector, (width, height) in ((small, (640, 360)), (large, (2560, 1440))):
        image = canvas(width, height)
        detector.update(image, 0)
        image[: height // 4, : width // 4, :3] = 255  # 左上的十六分之一
        fraction = detector.update(image, 10 * MS).changed_fraction
        assert fraction == pytest.approx(1 / 16, abs=0.01)
    # 比格子图还小的帧按帧的尺寸。
    tiny = ChangeDetector(ChangeParams(grid_width=64))
    tiny.update(canvas(8, 4), 0)
    image = canvas(8, 4)
    image[0, 0, :3] = 255
    assert tiny.update(image, 1).changed_fraction == pytest.approx(1 / 32)


def test_padded_rows_are_read_without_the_padding() -> None:
    width, height, stride = 6, 3, 6 * 4 + 8
    rows = b"".join(bytes([10, 20, 30, 255]) * width + b"\xee" * 8 for _ in range(height))
    frame = RingFrame(1, 0, 1, width, height, stride, rows)

    view = bgra_view(frame)
    assert view.shape == (height, width, 4)
    assert (view[:, :, :3] == (10, 20, 30)).all()


def test_params_are_checked() -> None:
    for field, value in (
        ("grid_width", 0),
        ("cell_threshold", 255),
        ("stable_after_ns", 0),
        ("transition_fraction", 0.0),
        ("transition_fraction", 1.5),
    ):
        with pytest.raises(ValueError, match=field):
            ChangeParams(**{field: value})  # pyright: ignore[reportArgumentType]
