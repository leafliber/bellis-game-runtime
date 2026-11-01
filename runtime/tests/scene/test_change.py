"""变化与稳定：格子图逐帧比较，稳定时长按采集时刻计，大面积的变化是过场，持续整屏的动态是播片。

规则见 `docs/modules/perception.md` 的“变化与稳定”。帧都是合成的纯色块。
"""

from typing import Literal

import numpy as np
import pytest
from numpy.typing import NDArray

from game_runtime.frame_ring import RingFrame
from game_runtime.scene import Change, ChangeDetector, ChangeParams, Stability, bgra_view

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
        ("motion_fraction", 0.0),
        ("motion_fraction", 1.5),
        ("playing_after_ns", 0),
        ("playing_ratio", 0.0),
        ("playing_ratio", 1.5),
        ("playing_end_ns", 0),
    ):
        with pytest.raises(ValueError, match=field):
            ChangeParams(**{field: value})  # pyright: ignore[reportArgumentType]


# 播片

type Kind = Literal["motion", "spot", "still"]


class Reel:
    """每隔 `step` 毫秒交给检测一帧合成的帧：`motion` 整屏换色（变化比例 1，动帧），`spot` 只换
    一小块（变化中，不是动帧），`still` 与上一帧相同。第 0 帧在 0 毫秒，没有可比的上一帧，也是
    动帧。"""

    def __init__(self, params: ChangeParams | None = None, *, step: int = 33) -> None:
        self.detector = ChangeDetector(params)
        self.step = step
        self.number = -1
        self.image = canvas()
        self.changes: list[Change] = []

    def at(self, number: int) -> int:
        """第 `number` 帧的采集时刻。"""
        return number * self.step * MS

    def show(self, kind: Kind, count: int = 1) -> Change:
        for _ in range(count):
            self.number += 1
            if kind == "motion":
                self.image = self.image.copy()
                self.image[:, :, :3] = 255 - self.image[:, :, :3]
            elif kind == "spot":
                self.image = self.image.copy()
                self.image[0:10, 0:10, :3] = 255 - self.image[0:10, 0:10, :3]
            self.changes.append(self.detector.update(self.image, self.at(self.number)))
        return self.changes[-1]

    def playing(self) -> list[int]:
        """在播片中的各帧的帧号。"""
        return [n for n, change in enumerate(self.changes) if change.playing]


def test_two_seconds_of_full_screen_motion_is_playing_until_it_stops_for_a_while() -> None:
    reel = Reel()
    reel.show("still", 30)  # 帧 0–29：第一帧之后不变，已稳定
    assert reel.changes[29].stability is Stability.STABLE
    reel.show("motion", 121)  # 帧 30–150 每帧整屏换色
    reel.show("still", 50)  # 帧 151–200
    # 帧 29（957 ms）是已稳定的帧；最近 2 秒内的帧不含它时（帧 90，2970 ms）起在播片中，开始时刻是
    # 这一段里第一个动帧（帧 30）。
    assert not reel.changes[89].playing
    entered = reel.changes[90]
    assert (entered.playing, entered.playing_since_ns, entered.playing_ended_ns) == (
        True,
        reel.at(30),
        None,
    )
    # 最后一个动帧是帧 150（4950 ms）；采集时刻距它不短于 1.5 秒的第一帧（帧 196）起不再在播片中，
    # 结束时刻是它之后的那一帧（帧 151）。其间停住的帧照样在播片中，已稳定也不打断。
    assert reel.playing() == list(range(90, 196))
    assert {c.playing_since_ns for c in reel.changes[90:196]} == {reel.at(30)}
    assert reel.changes[190].stability is Stability.STABLE
    left = reel.changes[196]
    assert (left.playing, left.playing_since_ns, left.playing_ended_ns) == (
        False,
        None,
        reel.at(151),
    )
    assert [n for n, c in enumerate(reel.changes) if c.playing_ended_ns is not None] == [196]


def test_a_transition_shorter_than_playing_after_is_not_playing() -> None:
    """约 1.5 秒的整屏转场（《魔女审判》录制里转场最长 1.43 秒）：停下 0.25 秒后已稳定，在它离开
    最近 2 秒之前，那一段里总有已稳定的帧。"""
    reel = Reel()
    reel.show("still", 30)
    reel.show("motion", 45)  # 帧 30–74
    reel.show("still", 60)
    reel.show("motion", 3)  # 换画面的转场
    reel.show("still", 30)

    assert reel.playing() == []
    assert all(c.playing_ended_ns is None for c in reel.changes)


def test_a_frozen_shot_does_not_interrupt_the_playing() -> None:
    reel = Reel()
    reel.show("still", 30)
    reel.show("motion", 70)  # 帧 30–99，帧 90 起在播片中
    reel.show("still", 40)  # 帧 100–139：定格 1.3 秒，其间已稳定
    reel.show("motion", 30)  # 帧 140–169
    reel.show("still", 60)

    playing = reel.playing()
    assert playing[0] == 90
    assert reel.changes[130].stability is Stability.STABLE
    # 定格不到 1.5 秒，播片一直延续到最后一个动帧（帧 169）之后 1.5 秒。
    assert playing == list(range(90, playing[-1] + 1))
    assert (
        reel.at(playing[-1] + 1) - reel.at(169) >= 1_500 * MS > reel.at(playing[-1]) - reel.at(169)
    )
    assert {reel.changes[n].playing_since_ns for n in playing} == {reel.at(30)}
    assert reel.changes[playing[-1] + 1].playing_ended_ns == reel.at(170)


def test_a_freeze_of_playing_end_ends_the_playing_and_a_new_one_starts_later() -> None:
    reel = Reel()
    reel.show("still", 30)
    reel.show("motion", 70)  # 帧 30–99
    reel.show("still", 50)  # 帧 100–149：停 1.65 秒，播片结束
    reel.show("motion", 70)  # 帧 150–219：又一段
    reel.show("still", 50)

    first = reel.changes[145]  # 距最后一个动帧 1.5 秒后的第一帧：4785 ms − 3267 ms
    assert (first.playing, first.playing_ended_ns) == (False, reel.at(100))
    assert reel.changes[144].playing
    second = [n for n in reel.playing() if n > 145]
    assert second[0] == 210  # 帧 149（已稳定，4917 ms）离开最近 2 秒
    assert {reel.changes[n].playing_since_ns for n in second} == {reel.at(150)}


@pytest.mark.parametrize(("motions", "playing"), [(3, False), (9, True)])
def test_motion_must_be_most_of_the_frames(motions: int, playing: bool) -> None:
    """动帧之间夹着只变一小块的帧（变化中、不是动帧）：动帧占 3/4 时不够 0.8，占 9/10 时够。"""
    reel = Reel()
    reel.show("still", 30)
    for _ in range(120 // (motions + 1)):  # 帧 30–149
        reel.show("motion", motions)
        reel.show("spot")
    assert bool(reel.playing()) is playing
    assert all(c.stability is not Stability.STABLE for c in reel.changes[31:])


def test_playing_needs_a_window_that_is_covered() -> None:
    """一开始就整屏地动：这之前没有处理过帧，最近 2 秒要到整整覆盖了才算。"""
    reel = Reel(step=100)
    reel.show("motion", 25)

    # 帧 20（2000 ms）时帧 0 落到最近 2 秒之外：这一段之前处理过帧，覆盖了整段。
    assert reel.playing() == list(range(20, 25))
    assert reel.changes[20].playing_since_ns == reel.at(1)


@pytest.mark.parametrize(("end_ms", "playing"), [(1_500, False), (3_000, True)])
def test_playing_does_not_start_when_the_motion_is_already_over(end_ms: int, playing: bool) -> None:
    """动帧密集地挤在一段的开头、随后是稀疏的只变一小块的帧：按帧数够了（`playing_ratio` 取 0.5），
    但此刻距最后一个动帧已不短于 `playing_end`，不进入；`playing_end` 更长时进入。"""
    detector = ChangeDetector(ChangeParams(playing_ratio=0.5, playing_end_ns=end_ms * MS))
    image = canvas()
    detector.update(image, 0)
    assert detector.update(image, 300 * MS).stability is Stability.STABLE
    changes: list[Change] = []
    for step in range(1, 21):  # 305–400 ms：20 个动帧
        image = image.copy()
        image[:, :, :3] = 255 - image[:, :, :3]
        changes.append(detector.update(image, (300 + 5 * step) * MS))
    for at in (700, 1_000, 1_300, 1_600, 1_900, 2_200, 2_310):
        image = image.copy()
        image[0:10, 0:10, :3] = 255 - image[0:10, 0:10, :3]
        changes.append(detector.update(image, at * MS))

    # 2310 ms 时帧 300 ms（已稳定）落到最近 2 秒之外：19 个动帧、7 个变化中的帧。
    assert not any(c.playing for c in changes[:-1])
    assert changes[-1].playing is playing
