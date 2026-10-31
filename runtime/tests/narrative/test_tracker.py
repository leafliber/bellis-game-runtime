"""一句的边界、修订号、完整度与剧情范围：以手写的观察序列逐条核对 `SentenceTracker`。

规则见 `docs/modules/narrative-planner.md` 的“一句的边界”“完整度”与“剧情范围”。合成游戏上按真值的
核对见 `test_synthetic.py`。
"""

import dataclasses

import pytest

from game_runtime.narrative import (
    BoxFrame,
    Completeness,
    Gap,
    LowConfidenceLine,
    NarrativeRecord,
    OtherScreen,
    Reading,
    SentenceTracker,
    TrackerParams,
    Unjudged,
    compare_key,
    continues,
)

MS = 1_000_000


class Feed:
    """按帧号与采集时刻依次给出观察；每帧间隔 `step` 毫秒。"""

    def __init__(self, params: TrackerParams | None = None, *, step: int = 33) -> None:
        self.tracker = SentenceTracker(7, params or TrackerParams())
        self.number = 0
        self.step = step

    def box(
        self,
        text: str,
        speaker: str | None = "阿遥",
        *,
        marker: bool = False,
        screen: str = "dialogue",
        confidence: float = 0.97,
    ) -> tuple[NarrativeRecord, ...]:
        self.number += 1
        reading = Reading("dialogue", ((text, confidence),) if text else ())
        names = (
            None if speaker is None else Reading("speaker", ((speaker, 0.99),) if speaker else ())
        )
        frame = BoxFrame(self.number, self.number * self.step * MS, screen, reading, names, marker)
        return self.tracker.observe(frame).records

    def typing(
        self, text: str, speaker: str | None = "阿遥", *, start: int = 1, finish: bool = True
    ) -> None:
        for end in range(start, len(text) + 1):
            self.box(text[:end], speaker, marker=finish and end == len(text))

    def other(self, screen: str = "choice", *, starts_scope: bool = False) -> None:
        self.number += 1
        self.tracker.observe(OtherScreen(self.number, screen, starts_scope))

    def unjudged(self) -> None:
        self.number += 1
        self.tracker.observe(Unjudged(self.number))

    @property
    def records(self) -> tuple[NarrativeRecord, ...]:
        return self.tracker.records

    def summary(self) -> list[tuple[str | None, str, int, Completeness]]:
        return [(r.speaker, r.text, r.revision, r.completeness) for r in self.records]


# 比较文本与同一句


def test_compare_key_folds_width_and_drops_whitespace() -> None:
    assert compare_key(" 雨停了，\n钟楼 ") == "雨停了,钟楼"
    assert compare_key("ＡＢ　ｃ？") == "ABc?"


@pytest.mark.parametrize(
    ("current", "observed", "same"),
    [
        ("雨停", "雨停了", True),  # 逐字显示
        ("雨停了", "雨停了", True),
        ("雨停了,钟楼的指", "雨停了,钟搂的指针", True),  # 8 个字容许 2 处读错
        ("雨停了,钟楼的指", "雨挺了,钟搂的指针", True),
        ("雨停了,钟楼的指", "雨挺了,钟搂的拍针", False),
        ("雨停了,钟楼的指针停在七点。", "你", False),  # 重新开始的逐字显示
        ("门后也许有路,也许什么都没有。", "门", False),  # 同一个字开头的下一句
        ("雨停", "雨挺了", False),  # 不到 4 个字不容错
        ("雨停了,钟楼的指针停在七点", "雨停了,钟楼的指针停在七", True),  # 末尾少一个字
    ],
)
def test_continues(current: str, observed: str, same: bool) -> None:
    assert continues(current, observed) is same


# 修订号与完整度


def test_typing_revises_one_sentence_until_the_marker_completes_it() -> None:
    feed = Feed()
    feed.typing("你听见了吗？")
    feed.box("你听见了吗？", marker=True)

    [record] = feed.records
    assert (record.text, record.revision, record.completeness) == (
        "你听见了吗？",
        6,
        Completeness.COMPLETE,
    )
    assert (record.first_frame, record.revision_frame, record.complete_frame) == (1, 6, 6)
    assert (record.last_frame, record.last_ns, record.first_ns) == (7, 7 * 33 * MS, 33 * MS)
    assert record.closed_frame is None
    assert (record.occurrence, record.session, record.scope, record.screen) == (1, 7, 1, "dialogue")


def test_without_a_marker_the_text_must_stay_unchanged_for_frames_and_time() -> None:
    feed = Feed(TrackerParams(complete_frames=3, complete_after_ns=500 * MS), step=100)
    for end in range(1, 4):
        feed.box("门后也许"[:end])
    assert feed.records[0].completeness is Completeness.PARTIAL
    feed.box("门后也")  # 第 2 帧不变
    feed.box("门后也")  # 第 3 帧不变，但只过了 200 毫秒
    assert feed.records[0].completeness is Completeness.PARTIAL
    for _ in range(3):
        feed.box("门后也")
    [record] = feed.records
    assert (record.completeness, record.complete_frame) == (Completeness.COMPLETE, 8)


def test_a_sentence_that_grows_after_completing_is_partial_again() -> None:
    """逐字显示中途的停顿被判为完整时自行纠正。"""
    feed = Feed(TrackerParams(complete_frames=2, complete_after_ns=0))
    feed.box("……")
    feed.box("……")
    assert feed.records[0].completeness is Completeness.COMPLETE
    feed.box("……原来")
    [record] = feed.records
    assert (record.completeness, record.complete_frame, record.revision) == (
        Completeness.PARTIAL,
        None,
        2,
    )


def test_ocr_misreading_mid_typing_stays_the_same_sentence() -> None:
    feed = Feed()
    feed.box("雨停了，钟搂的")
    feed.box("雨停了，钟楼的指")
    feed.box("雨停了，钟楼的指针", marker=True)

    assert feed.summary() == [("阿遥", "雨停了，钟楼的指针", 3, Completeness.COMPLETE)]


# 边界


def test_advancing_before_complete_is_a_gap_and_the_next_line_is_new() -> None:
    feed = Feed()
    feed.typing("你听见了吗？")
    feed.box("门", "旁白")  # 推进了：重新开始逐字显示
    feed.typing("门后也许", "旁白", start=2, finish=False)
    feed.box("你听", "阿遥")  # 还没读完就被推进
    feed.other()

    assert feed.summary() == [
        ("阿遥", "你听见了吗？", 6, Completeness.COMPLETE),
        ("旁白", "门后也许", 4, Completeness.GAP),
        ("阿遥", "你听", 1, Completeness.GAP),
    ]
    first, second, third = feed.records
    assert (first.closed_frame, second.first_frame, second.closed_frame) == (7, 7, 11)
    assert third.closed_frame == 12
    assert [r.occurrence for r in feed.records] == [1, 2, 3]


def test_same_text_after_the_box_clears_is_a_new_sentence() -> None:
    feed = Feed()
    feed.box("又敲了三下。", marker=True)
    feed.box("")  # 对白框清空
    feed.box("又敲了三下。", marker=True)
    feed.other()
    feed.box("又敲了三下。", marker=True)  # 换了画面之后

    assert feed.summary() == [("阿遥", "又敲了三下。", 1, Completeness.COMPLETE)] * 3
    assert [r.closed_frame for r in feed.records] == [2, 4, None]


def test_same_text_without_a_clear_is_still_one_sentence() -> None:
    """不按文本哈希去重，也不因为同样的文字一直在就重复记录。"""
    feed = Feed()
    for _ in range(5):
        feed.box("又敲了三下。", marker=True)
    feed.unjudged()  # 弹窗、过场：不改变记录
    feed.box("又敲了三下。", marker=True)

    [record] = feed.records
    assert (record.revision, record.first_frame, record.last_frame) == (1, 1, 7)


def test_unjudged_frames_neither_count_nor_close() -> None:
    feed = Feed(TrackerParams(complete_frames=3, complete_after_ns=0))
    feed.box("门后")
    feed.unjudged()
    feed.unjudged()
    feed.box("门后")
    assert feed.records[0].completeness is Completeness.PARTIAL  # 只算了 2 个对白框帧
    feed.box("门后")
    assert feed.records[0].completeness is Completeness.COMPLETE


def test_speakers_split_only_when_both_are_read_and_differ() -> None:
    feed = Feed()
    feed.box("你听", speaker="")  # 名字栏还在淡入
    feed.box("你听见", speaker="阿遥")
    feed.box("你听见了", speaker="")
    feed.box("你听见了吗", speaker="旁白")  # 换了说话人

    assert feed.summary() == [
        ("阿遥", "你听见了", 3, Completeness.GAP),
        ("旁白", "你听见了吗", 1, Completeness.PARTIAL),
    ]


def test_a_box_without_a_speaker_roi_records_no_speaker() -> None:
    feed = Feed()
    feed.box("雨停了。", speaker=None, marker=True)

    assert feed.summary() == [(None, "雨停了。", 1, Completeness.COMPLETE)]


def test_another_dialogue_screen_is_another_sentence() -> None:
    feed = Feed()
    feed.box("雨停了", screen="dialogue")
    feed.box("雨停了，钟楼", screen="narration")

    assert [(r.screen, r.completeness) for r in feed.records] == [
        ("dialogue", Completeness.GAP),
        ("narration", Completeness.PARTIAL),
    ]


def test_a_gap_in_processing_closes_the_sentence() -> None:
    feed = Feed()
    feed.box("雨停了")
    changes = feed.tracker.observe(Gap(2, 40, 39))
    feed.number = 40
    feed.box("雨停了，钟楼的指针停在七点。", marker=True)

    assert changes.discontinuity is not None
    assert (changes.discontinuity.first_frame, changes.discontinuity.count) == (2, 39)
    assert feed.summary() == [
        ("阿遥", "雨停了", 1, Completeness.GAP),
        ("阿遥", "雨停了，钟楼的指针停在七点。", 1, Completeness.COMPLETE),
    ]
    assert feed.records[0].closed_frame == 2
    assert feed.tracker.discontinuities == (changes.discontinuity,)


# 剧情范围


def test_bad_endings_and_loads_start_new_scopes() -> None:
    feed = Feed()
    feed.box("第一句。", marker=True)
    feed.other("title-menu")  # 菜单不开始新的范围
    feed.box("第二句。", marker=True)
    feed.other("bad-ending", starts_scope=True)
    feed.other("title-menu")
    feed.box("第三句。", marker=True)
    feed.box("还有一句话。", marker=True)
    feed.other("save-load", starts_scope=True)
    feed.box("第五句。", marker=True)

    assert [r.scope for r in feed.records] == [1, 1, 2, 2, 3]
    assert [(s.scope, s.after_screen, s.first_frame) for s in feed.tracker.scopes] == [
        (1, None, 1),
        (2, "bad-ending", 6),
        (3, "save-load", 9),
    ]


# 置信度与通知


def test_low_confidence_lines_are_marked_for_the_current_revision() -> None:
    feed = Feed(TrackerParams(low_confidence=0.8))
    feed.box("雨停", confidence=0.6)
    assert feed.records[0].low_confidence == (LowConfidenceLine("dialogue", "雨停", 0.6),)
    feed.box("雨停了", confidence=0.95)
    assert feed.records[0].low_confidence == ()


def test_changes_are_reported_only_when_the_content_changes() -> None:
    feed = Feed()
    assert len(feed.box("雨停")) == 1
    assert feed.box("雨停") == ()  # 只有最后出现的帧号变了
    [completed] = feed.box("雨停", marker=True)
    assert completed.completeness is Completeness.COMPLETE
    closed, opened = feed.box("门")
    assert (closed.occurrence, closed.closed_frame, opened.occurrence) == (1, 4, 2)


def test_the_same_observations_give_the_same_records() -> None:
    def run() -> list[NarrativeRecord]:
        feed = Feed()
        feed.typing("你听见了吗？")
        feed.box("", None)
        feed.typing("门后也许有路", "旁白")
        feed.other("bad-ending", starts_scope=True)
        feed.typing("雨停了。")
        return [dataclasses.replace(r, session=0) for r in feed.records]

    assert run() == run()


def test_params_are_checked() -> None:
    with pytest.raises(ValueError, match="complete_frames"):
        TrackerParams(complete_frames=0)
    with pytest.raises(ValueError, match="complete_after_ns"):
        TrackerParams(complete_after_ns=-1)
