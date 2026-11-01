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
        ("playing_gap_ns", 0),
        ("playing_after_ns", 0),
        ("playing_end_ns", 0),
    ):
        with pytest.raises(ValueError, match=field):
            ChangeParams(**{field: value})  # pyright: ignore[reportArgumentType]


# 播片

type Kind = Literal["motion", "band", "spot", "still"]


class Reel:
    """依次交给检测合成的帧，缺省每隔 `step` 毫秒一帧：`motion` 整屏换色（变化比例 1，动帧），
    `band` 只换最上面 `rows` 行格子（格子图 64×36，变化比例为 rows/36），`spot` 只换一小块（变化中，
    不是动帧），`still` 与上一帧相同。第 0 帧在 0 毫秒，没有可比的上一帧，也是动帧。"""

    def __init__(self, params: ChangeParams | None = None, *, step: int = 33) -> None:
        self.detector = ChangeDetector(params)
        self.step = step
        self.image = canvas()
        self.times: list[int] = []
        """各帧的采集时刻（毫秒）。"""
        self.changes: list[Change] = []

    def at(self, number: int) -> int:
        """第 `number` 帧的采集时刻（纳秒）。"""
        return self.times[number] * MS

    def show(
        self,
        kind: Kind,
        count: int = 1,
        *,
        rows: int = 36,
        step: int | None = None,
        interface: bool = False,
    ) -> Change:
        """`interface` 为这些帧上签名判出了画面。"""
        for _ in range(count):
            now = 0 if not self.times else self.times[-1] + (step or self.step)
            self.times.append(now)
            self.image = self.image.copy()
            if kind == "motion":
                self.image[:, :, :3] = 255 - self.image[:, :, :3]
            elif kind == "band":
                band = self.image[: rows * 5, :, :3]
                band[:] = 255 - band
            elif kind == "spot":
                self.image[0:4, 0:4, :3] = 255 - self.image[0:4, 0:4, :3]
            self.changes.append(self.detector.update(self.image, now * MS, interface=interface))
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
    # 这一段动帧从帧 30（990 ms）起（帧 0 也是动帧，但隔得太远，另算一段）；到帧 91（3003 ms）覆盖
    # 不短于 2 秒，起在播片中，开始时刻是帧 30。
    assert not reel.changes[90].playing
    entered = reel.changes[91]
    assert (entered.playing, entered.playing_since_ns, entered.playing_ended_ns) == (
        True,
        reel.at(30),
        None,
    )
    # 最后一个动帧是帧 150（4950 ms）；采集时刻距它不短于 1.5 秒的第一帧（帧 196）起不再在播片中，
    # 结束时刻是它之后的那一帧（帧 151）。其间停住的帧照样在播片中，已稳定也不打断。
    assert reel.playing() == list(range(91, 196))
    assert {c.playing_since_ns for c in reel.changes[91:196]} == {reel.at(30)}
    assert reel.changes[190].stability is Stability.STABLE
    left = reel.changes[196]
    assert (left.playing, left.playing_since_ns, left.playing_ended_ns) == (
        False,
        None,
        reel.at(151),
    )
    assert [n for n, c in enumerate(reel.changes) if c.playing_ended_ns is not None] == [196]


def test_a_transition_shorter_than_playing_after_is_not_playing() -> None:
    reel = Reel()
    reel.show("still", 30)
    reel.show("motion", 45)  # 帧 30–74：约 1.5 秒的整屏转场
    reel.show("still", 60)
    reel.show("motion", 3)  # 换画面的转场
    reel.show("still", 30)

    assert reel.playing() == []
    assert all(c.playing_ended_ns is None for c in reel.changes)


@pytest.mark.parametrize(("fade_ms", "playing"), [(1_430, False), (2_000, True)])
def test_a_fade_out_to_black_and_in_again_is_not_playing(fade_ms: int, playing: bool) -> None:
    """《魔女审判》录制里被误判过的转场：之前几帧只变 0.02（变化中、不是动帧），淡出 1.43 秒（每帧
    变化比例 0.1–0.2），黑屏 0.85 秒（不变），淡入 1.1 秒（0.1–0.15）。淡出与淡入隔着黑屏，各算
    一段，都不到 2 秒。淡出持续 2 秒时就是播片。每 10 毫秒一帧。"""
    reel = Reel(step=10)
    reel.show("still", 100)  # 第一帧（动帧）离淡出远于 0.5 秒
    reel.show("band", 20, rows=1)  # 0.028
    fade_out = len(reel.times)
    for n in range(fade_ms // 10 + 1):  # 第一帧到最后一帧相隔 fade_ms
        reel.show("band", rows=4 + n % 4)  # 4–7 行：0.11–0.19
    last_out = len(reel.times) - 1
    reel.show("still", 84)  # 黑屏
    for n in range(111):  # 淡入 1.1 秒
        reel.show("band", rows=4 + n % 2)  # 4–5 行：0.11–0.14
    reel.show("still", 200)

    fractions = [c.changed_fraction for c in reel.changes]
    assert all(0.1 <= f <= 0.2 for f in fractions[fade_out : last_out + 1])
    assert reel.times[last_out] - reel.times[fade_out] == fade_ms
    assert reel.times[last_out + 85] - reel.times[last_out] == 850
    if playing:
        assert reel.playing()[0] == last_out
        assert reel.changes[last_out].playing_since_ns == reel.at(fade_out)
    else:
        assert reel.playing() == []


def test_a_short_freeze_keeps_the_run_of_motion() -> None:
    """动帧之间定格 0.4 秒（其间已稳定）：相隔不超过 0.5 秒，仍连成一段，从第一个动帧起计时。"""
    reel = Reel()
    reel.show("still", 30)
    reel.show("motion", 30)  # 帧 30–59：990–1947 ms
    reel.show("still", 12)  # 帧 60–71：定格
    reel.show("motion", 40)  # 帧 72–111：2376 ms 起，与帧 59 相隔 429 ms
    reel.show("still", 60)

    assert reel.changes[70].stability is Stability.STABLE
    playing = reel.playing()
    assert playing[0] == 91  # 3003 ms：距帧 30 不短于 2 秒
    assert reel.changes[91].playing_since_ns == reel.at(30)


def test_a_longer_freeze_starts_a_new_run() -> None:
    """动帧之间定格 0.6 秒：相隔超过 0.5 秒，另起一段，从新的第一个动帧重新计时。"""
    reel = Reel()
    reel.show("still", 30)
    reel.show("motion", 30)  # 帧 30–59：990–1947 ms
    reel.show("still", 18)  # 帧 60–77：定格
    reel.show("motion", 70)  # 帧 78–147：2574 ms 起，与帧 59 相隔 627 ms
    reel.show("still", 60)

    playing = reel.playing()
    assert not reel.changes[91].playing  # 从帧 30 算已覆盖 2 秒，但那一段被打断了
    assert playing[0] == 139  # 4587 ms：距帧 78 不短于 2 秒
    assert reel.changes[139].playing_since_ns == reel.at(78)


def test_a_frozen_shot_does_not_interrupt_the_playing() -> None:
    reel = Reel()
    reel.show("still", 30)
    reel.show("motion", 70)  # 帧 30–99，帧 91 起在播片中
    reel.show("still", 40)  # 帧 100–139：定格 1.3 秒，其间已稳定
    reel.show("motion", 30)  # 帧 140–169
    reel.show("still", 60)

    playing = reel.playing()
    assert playing[0] == 91
    assert reel.changes[130].stability is Stability.STABLE
    # 在播片中时定格不到 1.5 秒不结束，播片一直延续到最后一个动帧（帧 169）之后 1.5 秒。
    assert playing == list(range(91, playing[-1] + 1))
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
    assert second[0] == 211  # 另起的一段从帧 150（4950 ms）起，到 6963 ms 覆盖 2 秒
    assert {reel.changes[n].playing_since_ns for n in second} == {reel.at(150)}


def test_a_recognized_screen_breaks_the_run_of_motion() -> None:
    """《魔女审判》审判里对白显示着时的镜头运动：整屏地动，但对白画面的签名不时命中。命中的帧
    不是动帧并打断这一段，从下一个动帧重新计时。"""
    reel = Reel()
    reel.show("still", 30)
    reel.show("motion", 50)  # 帧 30–79：990–2607 ms，还不到 2 秒
    reel.show("motion", 5, interface=True)  # 帧 80–84：照样整屏地动，签名命中
    reel.show("motion", 50)  # 帧 85–134：2805–4422 ms，又不到 2 秒
    reel.show("still", 30)

    assert all(c.changed_fraction == 1 for c in reel.changes[80:85])
    assert reel.playing() == []
    assert all(c.playing_ended_ns is None for c in reel.changes)


@pytest.mark.parametrize("still", [0, 10])
def test_a_recognized_screen_ends_the_playing(still: int) -> None:
    """在播片中命中签名的帧起不再在播片中，结束时刻是最后一个动帧之后那一帧（帧 100，命中的帧
    或之前停住的第一帧）；之后的动帧另起一段。"""
    reel = Reel()
    reel.show("still", 30)
    reel.show("motion", 70)  # 帧 30–99，帧 91 起在播片中
    reel.show("still", still)
    hit = len(reel.times)
    left = reel.show("motion", interface=True)
    reel.show("motion", 40)  # 1.3 秒：另起的一段还不是播片

    assert reel.playing() == list(range(91, hit))
    assert (left.playing, left.playing_since_ns, left.playing_ended_ns) == (
        False,
        None,
        reel.at(100),
    )


def test_motion_from_the_very_first_frame_counts() -> None:
    """一开始就整屏地动：第一帧没有可比的上一帧，也是动帧，这一段从它起计。"""
    reel = Reel(step=100)
    reel.show("motion", 25)

    assert reel.playing() == list(range(20, 25))  # 帧 20：2000 ms
    assert reel.changes[20].playing_since_ns == reel.at(0)


def test_motion_with_small_changes_in_between_is_one_run() -> None:
    """动帧之间夹着只变一小块的帧（变化中、不是动帧）：相隔不超过 0.5 秒，照样连成一段。"""
    reel = Reel()
    reel.show("still", 30)
    for _ in range(30):  # 帧 30–149
        reel.show("motion", 3)
        reel.show("spot")

    assert all(c.changed_fraction < 0.1 for c in reel.changes[33:150:4])
    assert reel.playing()[0] == 91
