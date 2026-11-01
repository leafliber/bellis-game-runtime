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
    Unread,
    compare_key,
    continues,
    same_name,
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
        screen: str | None = "dialogue",
        confidence: float = 0.97,
        speaker_confidence: float = 0.99,
        scope_screen: str | None = None,
    ) -> tuple[NarrativeRecord, ...]:
        self.number += 1
        reading = Reading("dialogue", ((text, confidence),) if text else ())
        names = (
            None
            if speaker is None
            else Reading("speaker", ((speaker, speaker_confidence),) if speaker else ())
        )
        frame = BoxFrame(
            self.number, self.number * self.step * MS, screen, reading, names, marker, scope_screen
        )
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

    def unread(self, scope_screen: str | None = None) -> None:
        self.number += 1
        self.tracker.observe(Unread(self.number, scope_screen))

    @property
    def records(self) -> tuple[NarrativeRecord, ...]:
        return self.tracker.records

    def summary(self) -> list[tuple[str | None, str, int, Completeness]]:
        return [(r.speaker, r.text, r.revision, r.completeness) for r in self.records]


# 比较文本与同一句


def test_compare_key_keeps_only_letters_and_digits() -> None:
    assert compare_key(" 雨停了，\n钟楼 ") == "雨停了钟楼"
    assert compare_key("ＡＢ　ｃ？１２") == "ABc12"
    assert compare_key("「那扇门——不，那面墙……」♪") == "那扇门不那面墙"
    assert compare_key("等等ー々〇") == "等等ー々〇"  # 长音符、叠字符与〇是字母或数字
    assert compare_key("……！？") == ""


@pytest.mark.parametrize(
    "variants",
    [
        ("……我先走了。", "…我先走了。", "·我先走了.", "我先走了"),  # 省略号读成“…”“·”或漏掉
        ("那扇门——不对", "那扇门-不对", "那扇门—不对", "那扇门不对"),  # 破折号
        ("（等等！）", "(等等!)", "「等等」"),  # 全角与半角、括号与引号
    ],
)
def test_misread_punctuation_gives_the_same_compare_key(variants: tuple[str, ...]) -> None:
    assert len({compare_key(v) for v in variants}) == 1


@pytest.mark.parametrize(
    ("current", "observed", "same"),
    [
        ("雨停", "雨停了", True),  # 逐字显示
        ("雨停了", "雨停了", True),
        ("雨停了钟楼的指针", "雨停了钟搂的指针停", True),  # 8 个字容许 2 处读错
        ("雨停了钟楼的指针", "雨挺了钟搂的指针停", True),
        ("雨停了钟楼的指针", "雨挺了钟搂的拍针停", False),
        ("雨停", "雨挺了", False),  # 不到 4 个字不容错
        ("门怎么开了", "咦门怎么开了风好大", True),  # 早一帧漏读了开头的一个字
        ("门怎么开了", "咦咦门怎么开了", False),  # 5 个字只容 1 个
        ("我先走了明天见", "那么我先走了明天见", False),  # 7 个字只容 1 个
        ("我先走了明天见再", "那么我先走了明天见再会", True),  # 8 个字容 2 个
        ("么我先走了明天见", "那么我先走了明天见再会", True),  # 漏读开头与逐字显示
        ("钟楼的指针停在七点", "钟楼的指针停在七点钟楼的指针停在八点", True),  # b 的尾部不计
        ("雨停了钟楼的指针停在七点", "雨停了钟楼的指针停在七", True),  # 末尾少一个字
        ("雨停了钟楼的指针停在七点", "雨停了钟楼的指针停", True),  # 少 3 个字：还在容差里
        ("雨停了钟楼的指针停在七点", "雨停了钟楼的指针", False),  # 少 4 个字：重新开始的逐字显示
        ("雨停了钟楼的指针停在七点", "你", False),
        ("门后也许有路也许什么都没有", "门", False),  # 同一个字开头的下一句
        ("门后也许有路也许什么都没有", "门后也许有路也许", False),
    ],
)
def test_continues(current: str, observed: str, same: bool) -> None:
    assert continues(current, observed) is same


def test_continues_on_long_text_stays_within_the_band() -> None:
    """长句只算带内：一处读错的长句仍续得上，前面多出超过容差的字即续不上。"""
    sentence = "雨停了钟楼的指针停在七点" * 40
    assert continues(sentence, "咦" + sentence[:200] + "拍" + sentence[201:] + "又一句")
    assert not continues(sentence, "咦" * (len(sentence) // 4 + 1) + sentence)


@pytest.mark.parametrize(
    ("current", "observed", "same"),
    [
        ("林小满", "林小满", True),
        ("林小满", "林小蛮", True),  # 花哨的字形上读错一个字
        ("林小满", "林小", True),
        ("欧阳晓星", "欧阳晚星", True),
        ("欧阳晓星", "欧阳晚月", False),  # 4 个字只容 1 个
        ("阿遥", "旁白", False),
        ("林小满", "周远", False),
        ("阿遥", "阿远", True),  # 代价：只差一字的两个名字当成同一个
        ("", "", True),  # 只有标点的名字（“？？？”与“？”）
        ("", "阿遥", False),
        ("司徒青云长老", "司徒清云长老", True),  # 6 个字容 1 个……
        ("司徒青云长老", "司徒清云长者", False),  # ……不容 2 个
        ("司徒青云长老大人", "司徒清云长者大人", True),  # 8 个字容 2 个
    ],
)
def test_same_name(current: str, observed: str, same: bool) -> None:
    assert same_name(current, observed) is same
    assert same_name(observed, current) is same


# 修订号与完整度


def test_typing_revises_one_sentence_until_the_marker_completes_it() -> None:
    feed = Feed()
    feed.typing("你听见了吗？")
    feed.box("你听见了吗？", marker=True)

    [record] = feed.records
    assert (record.text, record.revision, record.completeness) == (
        "你听见了吗？",
        5,  # 最后的“？”只换文字，不加修订号
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
    feed.box("原来")
    feed.box("原来")
    assert feed.records[0].completeness is Completeness.COMPLETE
    feed.box("原来如此")
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


def test_misread_ellipses_and_dashes_stay_the_same_sentence() -> None:
    feed = Feed()
    feed.box("…那么，我先走了。明天")
    feed.box("……那么，我先走了。明天见·明天再")  # 省略号读法不同，句末多读了一截
    feed.box("那扇门——不，那面墙", "旁白")
    feed.box("那扇门一不，那面墙后面", "旁白")  # “——”读成“一”：多一个字，在容差里
    feed.box("那扇门-不，那面墙后面有风。", "旁白", marker=True)

    assert feed.summary() == [
        ("阿遥", "……那么，我先走了。明天见·明天再", 2, Completeness.GAP),
        ("旁白", "那扇门-不，那面墙后面有风。", 3, Completeness.COMPLETE),
    ]


def test_a_missed_first_character_while_fading_in_stays_the_same_sentence() -> None:
    feed = Feed()
    feed.box("（门怎么开了")
    feed.box("（咦，门怎么开了！？风好大……）", marker=True)

    assert feed.summary() == [("阿遥", "（咦，门怎么开了！？风好大……）", 2, Completeness.COMPLETE)]


def test_punctuation_only_changes_update_the_text_but_not_the_revision() -> None:
    """只差标点：文字与读出它的帧号换成新的，修订号与连续不变的帧数不变，照常通知。"""
    feed = Feed(TrackerParams(complete_frames=3, complete_after_ns=0, low_confidence=0.8))
    feed.box("雨停了")
    [changed] = feed.box("雨停了，", confidence=0.6)
    assert (changed.text, changed.revision, changed.revision_frame) == ("雨停了，", 1, 2)
    assert changed.low_confidence == (LowConfidenceLine("dialogue", "雨停了，", 0.6),)
    assert changed.completeness is Completeness.PARTIAL
    [completed] = feed.box("雨停了。")  # 第 3 个比较文本不变的帧
    assert (completed.text, completed.revision, completed.revision_frame) == ("雨停了。", 1, 3)
    assert (completed.completeness, completed.complete_frame) == (Completeness.COMPLETE, 3)
    assert completed.low_confidence == ()


def test_a_complete_sentence_keeps_its_text_when_only_punctuation_differs() -> None:
    """完整之后只差标点的读数是噪声：不改记录，不通知。"""
    feed = Feed()
    feed.box("雨停了。", marker=True)
    assert feed.box("雨停了·") == ()
    assert feed.box("雨停了。▼") == ()
    [record] = feed.records
    assert (record.text, record.revision, record.revision_frame, record.last_frame) == (
        "雨停了。",
        1,
        1,
        3,
    )
    [revised] = feed.box("雨停了。钟")  # 比较文本变了：回到部分
    assert (revised.revision, revised.completeness) == (2, Completeness.PARTIAL)


def test_punctuation_only_text_ends_the_sentence_and_is_not_recorded() -> None:
    feed = Feed()
    feed.box("你听见了")
    assert [r.completeness for r in feed.box("……")] == [Completeness.GAP]
    assert feed.box("……") == ()
    feed.box("……嗯", marker=True)

    assert feed.summary() == [
        ("阿遥", "你听见了", 1, Completeness.GAP),
        ("阿遥", "……嗯", 1, Completeness.COMPLETE),
    ]
    assert feed.records[0].closed_frame == 2


def test_restarted_typing_is_a_new_sentence() -> None:
    """b 比 a 短超过 ⌊n/4⌋ 个字就是重新开始的逐字显示，哪怕 b 是 a 的开头；少的在容差里时是 OCR
    漏读了句末，还是这一句。"""
    sentence = "门后也许有路，也许什么都没有。"  # 比较文本 13 个字，容差 3
    restarted = Feed()
    restarted.box(sentence, marker=True)
    restarted.box("门后也许有路，也许什")  # 少 4 个字：推进后同样开头的下一句
    assert restarted.summary() == [
        ("阿遥", sentence, 1, Completeness.COMPLETE),
        ("阿遥", "门后也许有路，也许什", 1, Completeness.PARTIAL),
    ]

    misread = Feed()
    misread.box(sentence, marker=True)
    misread.box("门后也许有路，也许什么")  # 少 3 个字
    assert misread.summary() == [("阿遥", "门后也许有路，也许什么", 2, Completeness.PARTIAL)]


# 边界


def test_advancing_before_complete_is_a_gap_and_the_next_line_is_new() -> None:
    feed = Feed()
    feed.typing("你听见了吗？")
    feed.box("门", "旁白")  # 推进了：重新开始逐字显示
    feed.typing("门后也许", "旁白", start=2, finish=False)
    feed.box("你听", "阿遥")  # 还没读完就被推进
    feed.other()

    assert feed.summary() == [
        ("阿遥", "你听见了吗？", 5, Completeness.COMPLETE),
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


def test_unread_frames_neither_count_nor_close() -> None:
    """节流跳过、过场或 OCR 出错而没有读的帧：不改变记录，也不计入连续帧数。"""
    feed = Feed(TrackerParams(complete_frames=3, complete_after_ns=0))
    feed.box("门后")
    for _ in range(5):
        feed.unread()
    feed.box("门后")
    assert feed.records[0].completeness is Completeness.PARTIAL
    feed.box("门后")
    [record] = feed.records
    assert (record.completeness, record.last_frame, record.closed_frame) == (
        Completeness.COMPLETE,
        8,
        None,
    )


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


def test_a_misread_character_in_the_name_is_the_same_speaker() -> None:
    feed = Feed()
    feed.box("你听", speaker="林小满")
    feed.box("你听见了", speaker="林小蛮")  # 花哨的字形上读错一个字
    feed.box("你听见了吗", speaker="林小满")
    feed.box("你听见了吗？", speaker="周远")  # 两个不同的名字：文字续得上也拆开

    assert feed.summary() == [
        ("林小满", "你听见了吗", 3, Completeness.GAP),
        ("周远", "你听见了吗？", 1, Completeness.PARTIAL),
    ]


def test_a_name_of_only_punctuation_is_read() -> None:
    """“？？？”是读到了的名字：与“？”是同一个，与有字的名字不同。"""
    feed = Feed()
    feed.box("你是谁", speaker="？？？")
    feed.box("你是谁？", speaker="？")
    feed.box("你是谁？我", speaker="林小满")

    assert feed.summary() == [
        ("？？？", "你是谁？", 1, Completeness.GAP),
        ("林小满", "你是谁？我", 1, Completeness.PARTIAL),
    ]


def test_a_box_without_a_speaker_roi_records_no_speaker() -> None:
    feed = Feed()
    feed.box("雨停了。", speaker=None, marker=True)

    assert feed.summary() == [(None, "雨停了。", 1, Completeness.COMPLETE)]


def test_another_dialogue_screen_is_another_sentence() -> None:
    feed = Feed()
    feed.box("雨停了", screen="dialogue")
    feed.box("雨停了，钟楼", screen="narration")
    feed.box("雨停了，钟楼的", screen=None)  # 不写画面的对白框是另一个对白框
    feed.box("雨停了，钟楼的指针", screen=None)

    assert [(r.screen, r.completeness, r.revision) for r in feed.records] == [
        ("dialogue", Completeness.GAP, 1),
        ("narration", Completeness.GAP, 1),
        (None, Completeness.PARTIAL, 2),
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


def test_scope_screens_count_whether_or_not_the_box_was_read() -> None:
    """不写画面的对白框在坏结局上也读文字：那一帧上新开的句子仍在原来的范围里，下一句才开始新的
    范围；没读的帧（过场、节流）判出这类画面同样开始新的范围。"""
    feed = Feed()
    feed.box("第一句。", marker=True, screen=None)
    feed.box("坏结局", marker=True, screen=None, scope_screen="bad-ending")
    feed.box("", screen=None)
    feed.box("第三句。", marker=True, screen=None)
    feed.unread("save-load")
    feed.box("钟声又响了一次。", marker=True, screen=None)

    assert [(r.text, r.scope) for r in feed.records] == [
        ("第一句。", 1),
        ("坏结局", 1),
        ("第三句。", 2),
        ("钟声又响了一次。", 3),
    ]
    assert [(s.scope, s.after_screen, s.first_frame) for s in feed.tracker.scopes] == [
        (1, None, 1),
        (2, "bad-ending", 4),
        (3, "save-load", 6),
    ]


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


def test_low_confidence_lines_come_from_the_frames_of_the_speaker_and_the_text() -> None:
    feed = Feed(TrackerParams(low_confidence=0.8))
    feed.box("雨停", speaker="林小满", speaker_confidence=0.6)
    feed.box("雨停了", speaker="林小蛮", speaker_confidence=0.95, confidence=0.7)

    [record] = feed.records
    assert record.speaker == "林小满"
    assert record.low_confidence == (
        LowConfidenceLine("speaker", "林小满", 0.6),
        LowConfidenceLine("dialogue", "雨停了", 0.7),
    )


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
