"""声明、计时和语义风险规则的回归。"""

from dataclasses import replace

import pytest
from pydantic import ValidationError

from game_runtime.adapter import GAMES_DIR, PixelRect, ScreenType, load_adapter
from game_runtime.adapter.schema import DecisionTimer, ReturnTarget
from game_runtime.frame_ring import RingFrame
from game_runtime.ocr import OcrLine
from game_runtime.operator import messages
from game_runtime.scene import (
    SceneState,
    SceneUpdate,
    ScreenClassification,
    ScreenObservation,
    ScreenSource,
    ScreenState,
    Stability,
    Verdict,
)
from game_runtime.step_control import (
    Category,
    Click,
    EffectKind,
    ExpectedEffect,
    GoalJudgment,
    Plan,
    StepProposal,
    TextTarget,
)
from game_runtime.step_control.decisions import DecisionEvidence, PendingDecision, timer_deadline
from game_runtime.step_control.risk import ActionRefusedError, Located, assess


def frame(screen: str, kind: ScreenType) -> SceneUpdate:
    sample = RingFrame(2, 10, 1, 1280, 720, 5120, b"")
    state = SceneState(
        sample.frame_number,
        sample.capture_ns,
        sample.transform_version,
        sample.width,
        sample.height,
        Stability.STABLE,
        250_000_000,
        0,
        False,
        None,
        None,
        Verdict.SCREEN,
        screen,
        (screen,),
        (),
        None,
    )
    return SceneUpdate(
        sample, state, ScreenState(ScreenClassification(kind, ScreenSource.SIGNATURE, 1))
    )


def target(text: str, box: PixelRect) -> Located:
    return Located(
        ((box.left + box.right) // 2, (box.top + box.bottom) // 2), OcrLine(text, box, 1)
    )


def proposal(text: str, roi: str, category: Category = Category.NAVIGATE) -> StepProposal:
    return StepProposal(category, Click(TextTarget(text, roi)), ExpectedEffect(EffectKind.CHANGED))


def test_positive_goal_judgment_requires_observation_and_excludes_action_and_decision() -> None:
    judgment = GoalJudgment(True, "当前画面显示所需确认信息。", 7, 2, 1)
    observation = ScreenObservation(screen_type=ScreenType.MENU, confidence=0.9)
    with pytest.raises(ValueError, match="完成判断须附画面观察"):
        Plan(goal_judgment=judgment)
    with pytest.raises(ValueError, match="不能同时提议输入或打开决策点"):
        Plan(
            observation=observation,
            steps=(proposal("开始", "menu-start"),),
            goal_judgment=judgment,
        )
    with pytest.raises(ValueError, match="不能同时提议输入或打开决策点"):
        Plan(observation=observation, open_decision=True, goal_judgment=judgment)


def test_shop_returns_require_exact_text_roi_and_signature() -> None:
    adapter = load_adapter(GAMES_DIR / "synthetic-game")
    shop = frame("shop", ScreenType.SHOP)
    leave = proposal("离开", "shop-leave")
    roi = adapter.roi("shop-leave").to_pixels(1280, 720)
    assert not assess(adapter, shop, leave, target("离开", roi))
    with pytest.raises(ActionRefusedError, match="only-safe-return"):
        assess(adapter, shop, leave, target("离开", PixelRect(20, 30, 40, 50)))
    with pytest.raises(ActionRefusedError, match="only-safe-return"):
        assess(adapter, shop, leave, target("离开后继续购买", roi))
    conflict = replace(
        shop.screen_state,
        observations=(ScreenObservation(screen_type=ScreenType.SETTINGS, confidence=0.6),),
    )
    with pytest.raises(ActionRefusedError, match="only-safe-return"):
        assess(adapter, replace(shop, screen_state=conflict), leave, target("离开", roi))
    with pytest.raises(ValidationError, match="返回目标"):
        ReturnTarget(roi="shop-leave", text="确定")


def test_forbidden_text_and_business_choice_are_independent() -> None:
    adapter = load_adapter(GAMES_DIR / "synthetic-game")
    menu = frame("title-menu", ScreenType.MENU)
    click = proposal("开始", "menu-start")
    box = adapter.roi("menu-start").to_pixels(1280, 720)
    with pytest.raises(ActionRefusedError, match="target-text-forbidden"):
        assess(adapter, menu, click, target("人民币 ￥１２", box), authorized_decision=True)
    assert assess(adapter, menu, click, target("覆盖存档", box))
    choice = frame("choice", ScreenType.CHOICE)
    assert assess(adapter, choice, click, target("测试选项", box))
    assert not assess(adapter, choice, click, target("测试选项", box), authorized_decision=True)


def test_restart_needs_an_exact_signed_target_and_respects_other_restrictions() -> None:
    adapter = load_adapter(GAMES_DIR / "synthetic-game")
    ending = frame("bad-ending", ScreenType.BAD_ENDING)
    restart = replace(
        proposal("重来", "ending-retry"),
        expected=ExpectedEffect(EffectKind.SIGNATURE, signature="dialogue"),
    )
    box = adapter.roi("ending-retry").to_pixels(1280, 720)
    found = target("重来", box)
    assert not assess(adapter, ending, restart, found, authorized_restart=True)
    with pytest.raises(ActionRefusedError, match="screen-blocked"):
        assess(adapter, ending, restart, found)
    with pytest.raises(ActionRefusedError, match="screen-blocked"):
        assess(
            adapter,
            ending,
            restart,
            target("重来", PixelRect(20, 30, 40, 50)),
            authorized_restart=True,
        )
    restriction = ScreenObservation(screen_type=ScreenType.SETTINGS, confidence=0.9)
    conflicting = replace(ending.screen_state, observations=(restriction,))
    with pytest.raises(ActionRefusedError, match="screen-blocked"):
        assess(
            adapter,
            replace(ending, screen_state=conflicting),
            restart,
            found,
            authorized_restart=True,
        )
    risk = adapter.risk.model_copy(update={"forbidden_words": ("重来",)})
    with pytest.raises(ActionRefusedError, match="target-text-forbidden"):
        assess(
            adapter.model_copy(update={"risk": risk}),
            ending,
            restart,
            found,
            authorized_restart=True,
        )


def test_timer_validation_and_single_selection_cutoff() -> None:
    timer = DecisionTimer(roi="timer-text", prefix="剩余", suffix="秒")
    assert timer_deadline(timer, "剩余 ４.６ 秒", 100) == 4_500_000_100
    assert timer_deadline(DecisionTimer(roi="timer-text"), "01:02:020", 100) == 62_019_000_100
    assert timer_deadline(timer, "剩余 01:60.0 秒", 100) is None
    assert timer_deadline(timer, "等待 4.5 秒", 100) is None
    line = OcrLine("测试选项", PixelRect(1, 1, 3, 3), 1)
    evidence = DecisionEvidence("timed-choice", "测试", (line,), 80)
    decision = PendingDecision("d", evidence, (0,), 80, 1)
    decision.refresh_deadline(95)
    assert decision.deadline_ns == 80
    decision.accept(messages.DecisionResolved(id="d", index=0), 70, 10)
    assert decision.choose(70, 10) == (0, "default")
    assert decision.choose(80, 10) is None
    decision.candidates = ()
    assert decision.choose(74, 10) is None


def test_shortened_deadline_rechecks_an_operator_reply_received_under_the_old_cutoff() -> None:
    options = (
        OcrLine("甲", PixelRect(1, 1, 3, 3), 1),
        OcrLine("乙", PixelRect(1, 4, 3, 6), 1),
    )
    evidence = DecisionEvidence("timed-choice", "选择", options, 100)
    original = PendingDecision("id", evidence, (1, 0), 100, 1)
    early_enough = replace(original)
    late = messages.DecisionResolved(id="id", index=0)
    original.accept(late, 85, 10)
    original.refresh_deadline(90)
    assert original.choose(86, 10) == (1, "default")
    early_enough.accept(late, 78, 10)
    early_enough.refresh_deadline(90)
    assert early_enough.choose(86, 10) == (0, "operator")
