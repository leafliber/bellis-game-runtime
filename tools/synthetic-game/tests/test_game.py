"""状态机：每种画面的进入与退出、逐字显示、计时到点、弹窗、坏结局与重来、点击命中与未命中、确定性。

全部用手动时钟推进，不真睡；时间取二进制下精确的小数，避免浮点误差影响到点判断。
"""

from collections.abc import Callable

import pytest

from synthetic_game import (
    Click,
    Effect,
    EffectKind,
    GameConfig,
    Input,
    Key,
    ManualClock,
    MouseButton,
    Press,
    SceneType,
    Scroll,
    SyntheticGame,
    Target,
)
from synthetic_game.story import NODES, SHOP_ITEMS, START, STARTING_COINS, TITLE, Choice, Dialogue

TEXT_CPS = 8.0
CHOICE_SECONDS = 4.0
MENU_TARGETS = ("menu.start", "menu.shop", "menu.help")


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock()


@pytest.fixture
def game(clock: ManualClock) -> SyntheticGame:
    return SyntheticGame(GameConfig(text_cps=TEXT_CPS, choice_seconds=CHOICE_SECONDS), clock)


def target(game: SyntheticGame, target_id: str) -> Target:
    targets = {target.id: target for target in game.truth().targets}
    assert target_id in targets, f"{target_id} 不在当前可点目标 {sorted(targets)} 中"
    return targets[target_id]


def click(game: SyntheticGame, target_id: str) -> Effect:
    x, y = target(game, target_id).rect.center
    return game.handle(Click(x, y))


def press(game: SyntheticGame, key: Key) -> Effect:
    return game.handle(Press(key))


def read_dialogue(game: SyntheticGame, clock: ManualClock) -> None:
    """点完当前对话的每一句：先等到整句显示，再点一下进入下一句。"""
    node = game.truth().node
    while game.truth().node == node and game.truth().overlay is None:
        assert game.truth().scene is SceneType.DIALOGUE
        clock.advance(60)
        assert click(game, "dialogue.box").kind is EffectKind.HIT


def to_door_choice(game: SyntheticGame, clock: ManualClock) -> None:
    click(game, "menu.start")
    read_dialogue(game, clock)
    assert game.truth().node == "door"


def to_fire_choice(game: SyntheticGame, clock: ManualClock) -> None:
    to_door_choice(game, clock)
    click(game, "choice.0")
    read_dialogue(game, clock)
    click(game, "popup.ok")
    assert game.truth().scene is SceneType.TIMED_CHOICE


def test_story_graph_references_only_existing_nodes() -> None:
    assert START in NODES
    for node_id, node in NODES.items():
        match node:
            case Dialogue(next=following):
                destinations = [following]
            case Choice(options=options, timeout_next=timeout_next):
                destinations = [option.next for option in options]
                if timeout_next is not None:
                    destinations.append(timeout_next)
            case _:
                destinations = []
        for destination in destinations:
            assert destination == TITLE or destination in NODES, (node_id, destination)


def test_starts_at_the_title_menu(game: SyntheticGame) -> None:
    truth = game.truth()

    assert truth.scene is SceneType.MENU
    assert truth.node == TITLE
    assert truth.overlay is None
    assert tuple(target.id for target in truth.targets) == MENU_TARGETS
    assert truth.selected == "menu.start"
    assert truth.coins == STARTING_COINS
    assert truth.last_effect is None


@pytest.mark.parametrize("target_id", MENU_TARGETS)
def test_click_at_a_target_centre_hits_that_target(game: SyntheticGame, target_id: str) -> None:
    effect = click(game, target_id)

    assert effect.kind is EffectKind.HIT
    assert effect.target == target_id
    assert effect.changed
    assert game.truth().last_effect == effect


def test_target_rectangles_are_half_open(game: SyntheticGame) -> None:
    rect = target(game, "menu.start").rect

    assert game.handle(Click(rect.left, rect.top)).target == "menu.start"
    game = SyntheticGame(game.config, ManualClock())
    assert game.handle(Click(rect.right, rect.bottom - 1)).kind is EffectKind.MISS
    assert game.handle(Click(rect.left, rect.bottom)).kind is EffectKind.MISS


@pytest.mark.parametrize("point", [(0, 0), (5, 700), (1279, 719), (-1, -1), (2000, 100)])
def test_click_outside_every_target_misses_and_changes_nothing(
    game: SyntheticGame, point: tuple[int, int]
) -> None:
    before = game.truth()

    effect = game.handle(Click(*point))

    assert effect == Effect(Click(*point), EffectKind.MISS, None, changed=False)
    after = game.truth()
    assert (after.scene, after.node, after.selected) == (before.scene, before.node, before.selected)


@pytest.mark.parametrize("button", [MouseButton.RIGHT, MouseButton.MIDDLE])
def test_only_the_left_button_activates_targets(game: SyntheticGame, button: MouseButton) -> None:
    x, y = target(game, "menu.start").rect.center

    effect = game.handle(Click(x, y, button))

    assert effect.kind is EffectKind.IGNORED
    assert game.truth().scene is SceneType.MENU


def test_menu_cursor_wraps_and_enter_activates_it(game: SyntheticGame) -> None:
    assert press(game, Key.UP).target == "menu.help"
    assert press(game, Key.DOWN).target == "menu.start"

    effect = press(game, Key.ENTER)

    assert (effect.kind, effect.target) == (EffectKind.KEY, "menu.start")
    assert (game.truth().scene, game.truth().node) == (SceneType.DIALOGUE, START)


def test_dialogue_reveals_text_character_by_character(
    game: SyntheticGame, clock: ManualClock
) -> None:
    click(game, "menu.start")
    text = game.truth().text
    assert game.truth().visible_text == ""

    clock.advance(0.25)  # 8 字每秒，0.25 秒显示 2 个字
    assert game.truth().visible_text == text[:2]

    clock.advance(1.0)
    assert game.truth().visible_text == text[:10]

    clock.advance(60)
    assert game.truth().visible_text == text


def test_advancing_while_typing_first_shows_the_whole_line(
    game: SyntheticGame, clock: ManualClock
) -> None:
    click(game, "menu.start")
    clock.advance(0.5)
    first = game.truth().text

    assert click(game, "dialogue.box").changed
    assert game.truth().visible_text == first

    press(game, Key.SPACE)
    assert game.truth().text != first
    assert game.truth().visible_text == ""


def test_dialogue_leads_to_a_choice_and_choices_lead_on(
    game: SyntheticGame, clock: ManualClock
) -> None:
    to_door_choice(game, clock)
    truth = game.truth()
    assert truth.scene is SceneType.CHOICE
    assert [target.label for target in truth.targets] == ["推门进去", "敲门回应", "回去睡觉"]
    assert truth.timer_remaining is None

    click(game, "choice.1")
    assert (game.truth().scene, game.truth().node) == (SceneType.DIALOGUE, "knock")

    read_dialogue(game, clock)
    assert (game.truth().scene, game.truth().node) == (SceneType.CHOICE, "door")


def test_choice_cursor_wraps_and_enter_chooses(game: SyntheticGame, clock: ManualClock) -> None:
    to_door_choice(game, clock)

    assert press(game, Key.UP).target == "choice.2"
    press(game, Key.ENTER)

    assert (game.truth().scene, game.truth().node) == (SceneType.BAD_ENDING, "sleep")


def test_popup_after_a_dialogue_is_modal_and_starts_nothing_until_dismissed(
    game: SyntheticGame, clock: ManualClock
) -> None:
    to_door_choice(game, clock)
    click(game, "choice.0")
    box = target(game, "dialogue.box").rect
    read_dialogue(game, clock)

    truth = game.truth()
    assert truth.scene is SceneType.DIALOGUE  # 弹窗是叠加层，画面类型仍是底下的对话
    assert truth.overlay is not None
    assert truth.overlay.text == "获得：旧提灯"
    assert [target.id for target in truth.targets] == ["popup.ok"]
    assert game.handle(Click(*box.center)).kind is EffectKind.MISS
    assert press(game, Key.UP).kind is EffectKind.IGNORED

    clock.advance(100)
    assert game.truth().overlay is not None

    assert click(game, "popup.ok").kind is EffectKind.HIT
    truth = game.truth()
    assert (truth.scene, truth.node, truth.overlay) == (SceneType.TIMED_CHOICE, "fire", None)
    assert truth.timer_remaining == CHOICE_SECONDS  # 计时从弹窗关闭时开始


def test_timed_choice_counts_down_and_times_out_at_the_deadline(
    game: SyntheticGame, clock: ManualClock
) -> None:
    to_fire_choice(game, clock)

    clock.advance(1.5)
    assert game.truth().timer_remaining == CHOICE_SECONDS - 1.5
    clock.advance(CHOICE_SECONDS - 1.5 - 0.25)
    assert game.truth().scene is SceneType.TIMED_CHOICE

    clock.advance(0.25)  # 恰好到点也算超时
    truth = game.truth()
    assert (truth.scene, truth.node) == (SceneType.BAD_ENDING, "late")
    assert truth.timer_remaining is None


def test_input_after_the_deadline_lands_on_the_timeout_screen(
    game: SyntheticGame, clock: ManualClock
) -> None:
    to_fire_choice(game, clock)
    right_door = target(game, "choice.1").rect

    clock.advance(CHOICE_SECONDS + 1)
    effect = game.handle(Click(*right_door.center))

    assert effect.kind is EffectKind.MISS
    assert (game.truth().scene, game.truth().node) == (SceneType.BAD_ENDING, "late")


def test_choosing_in_time_finishes_the_story_and_pays_the_reward(
    game: SyntheticGame, clock: ManualClock
) -> None:
    to_fire_choice(game, clock)
    clock.advance(CHOICE_SECONDS - 0.5)

    click(game, "choice.1")
    assert (game.truth().scene, game.truth().node) == (SceneType.DIALOGUE, "roof")
    read_dialogue(game, clock)

    truth = game.truth()
    assert (truth.scene, truth.node) == (SceneType.MENU, TITLE)
    assert truth.coins == STARTING_COINS + 30


def test_bad_ending_retry_restarts_the_story(game: SyntheticGame, clock: ManualClock) -> None:
    to_fire_choice(game, clock)
    click(game, "choice.0")
    truth = game.truth()
    assert (truth.scene, truth.node) == (SceneType.BAD_ENDING, "burn")
    assert [target.id for target in truth.targets] == ["ending.retry"]

    assert click(game, "ending.retry").kind is EffectKind.HIT

    truth = game.truth()
    assert (truth.scene, truth.node, truth.visible_text) == (SceneType.DIALOGUE, START, "")


def test_bad_ending_retry_by_keyboard(game: SyntheticGame, clock: ManualClock) -> None:
    to_door_choice(game, clock)
    click(game, "choice.2")
    assert press(game, Key.ESCAPE).kind is EffectKind.IGNORED

    assert press(game, Key.ENTER).target == "ending.retry"
    assert game.truth().node == START


def test_help_popup_opens_from_the_menu_and_escape_closes_it(game: SyntheticGame) -> None:
    click(game, "menu.help")
    truth = game.truth()
    assert truth.scene is SceneType.MENU
    assert truth.overlay is not None
    assert truth.selected == "popup.ok"

    effect = press(game, Key.ESCAPE)

    assert (effect.kind, effect.target) == (EffectKind.KEY, "popup.ok")
    assert game.truth().overlay is None
    assert game.truth().selected == "menu.help"


def test_shop_buys_items_and_refuses_when_coins_run_short(game: SyntheticGame) -> None:
    click(game, "menu.shop")
    truth = game.truth()
    assert truth.scene is SceneType.SHOP
    assert [target.id for target in truth.targets] == [
        "shop.item.0",
        "shop.item.1",
        "shop.item.2",
        "shop.item.3",
        "shop.leave",
    ]

    assert click(game, "shop.item.0").changed
    assert game.truth().coins == STARTING_COINS - SHOP_ITEMS[0].price
    assert game.truth().owned == {SHOP_ITEMS[0].id}
    again = click(game, "shop.item.0")
    assert (again.kind, again.changed) == (EffectKind.HIT, False)

    for _ in range(len(SHOP_ITEMS)):
        press(game, Key.DOWN)
    assert game.truth().selected == f"shop.item.{len(SHOP_ITEMS) - 1}"
    press(game, Key.ENTER)
    truth = game.truth()
    assert truth.overlay is not None
    assert truth.overlay.text == "金币不足。"
    assert truth.coins == STARTING_COINS - SHOP_ITEMS[0].price

    press(game, Key.ENTER)
    assert press(game, Key.ESCAPE).target == "shop.leave"
    assert game.truth().scene is SceneType.MENU
    assert game.truth().owned == {SHOP_ITEMS[0].id}  # 已购物品跨画面保留


def test_shop_list_scrolls_inside_the_list_only(game: SyntheticGame) -> None:
    click(game, "menu.shop")
    row = target(game, "shop.item.0").rect

    down = game.handle(Scroll(*row.center, notches=-1))
    assert (down.kind, down.target, down.changed) == (EffectKind.SCROLL, "shop.list", True)
    assert game.truth().targets[0].id == "shop.item.1"
    assert game.truth().selected == "shop.item.1"  # 光标随列表留在可见范围内

    game.handle(Scroll(*row.center, notches=-100))
    assert game.truth().targets[0].id == f"shop.item.{len(SHOP_ITEMS) - 4}"
    at_end = game.handle(Scroll(*row.center, notches=-1))
    assert (at_end.kind, at_end.changed) == (EffectKind.SCROLL, False)

    leave = target(game, "shop.leave").rect
    assert game.handle(Scroll(*leave.center, notches=1)).kind is EffectKind.IGNORED

    game.handle(Scroll(*row.center, notches=100))
    assert game.truth().targets[0].id == "shop.item.0"


def test_scroll_outside_the_shop_is_ignored(game: SyntheticGame) -> None:
    effect = game.handle(Scroll(640, 360, 1))

    assert (effect.kind, effect.changed) == (EffectKind.IGNORED, False)


def test_unbound_keys_are_ignored(game: SyntheticGame) -> None:
    click(game, "menu.start")

    for key in (Key.ESCAPE, Key.UP, Key.DOWN):
        assert press(game, key).kind is EffectKind.IGNORED


def run_script(script: list[tuple[float, Input]]) -> tuple[list[Effect], bytes, int]:
    clock = ManualClock()
    game = SyntheticGame(GameConfig(text_cps=TEXT_CPS, choice_seconds=CHOICE_SECONDS), clock)
    effects: list[Effect] = []
    for delay, event in script:
        clock.advance(delay)
        effects.append(game.handle(event))
        game.frame()
    frame = game.frame()
    return effects, frame.data, frame.number


def test_the_same_inputs_give_the_same_results() -> None:
    game = SyntheticGame(GameConfig(), ManualClock())
    start = target(game, "menu.start").rect.center
    script: list[tuple[float, Input]] = [
        (0.5, Press(Key.DOWN)),
        (0.25, Press(Key.UP)),
        (0.0, Click(*start)),
        (0.125, Press(Key.ENTER)),
        (0.375, Click(10, 10)),
        (1.0, Press(Key.SPACE)),
        (0.0625, Scroll(10, 10, 1)),
    ]

    assert run_script(script) == run_script(script)


@pytest.mark.parametrize(
    "make",
    [
        pytest.param(lambda: GameConfig(width=319), id="too-narrow"),
        pytest.param(lambda: GameConfig(height=5000), id="too-tall"),
        pytest.param(lambda: GameConfig(text_cps=0.0), id="zero-speed"),
        pytest.param(lambda: GameConfig(text_cps=float("nan")), id="nan-speed"),
        pytest.param(lambda: GameConfig(choice_seconds=-1.0), id="negative-timer"),
        pytest.param(lambda: GameConfig(choice_seconds=float("inf")), id="infinite-timer"),
    ],
)
def test_invalid_configuration_is_rejected(make: Callable[[], GameConfig]) -> None:
    with pytest.raises(ValueError, match="须"):
        make()
