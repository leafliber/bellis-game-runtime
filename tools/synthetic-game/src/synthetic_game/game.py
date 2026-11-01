"""无窗口的状态机：接受输入、随注入的时钟推进，生成画面快照并渲染成帧。

同样的输入序列在同样的时钟读数下得到同样的状态与同样的帧；状态机自己从不睡眠。
`truth()` 只给测试与假后端的断言用：运行时只能从像素感知画面，不能读它。
"""

import math
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Final

from synthetic_game.clock import Clock, MonotonicClock
from synthetic_game.inputs import Click, Input, Key, MouseButton, Press, Scroll
from synthetic_game.layout import SHOP_VISIBLE_ROWS, Layout, Rect
from synthetic_game.render import (
    Button,
    ChoiceView,
    CutsceneView,
    DialogueView,
    EndingView,
    Frame,
    MenuView,
    PopupView,
    Renderer,
    Screen,
    ShopRow,
    ShopView,
    View,
)
from synthetic_game.story import (
    NODES,
    SHOP_ITEMS,
    START,
    STARTING_COINS,
    TITLE,
    UI,
    Choice,
    Cutscene,
    Dialogue,
    Ending,
)

SHOP = "shop"
"""商店在真值里的节点名；它不在剧情图里。"""

MIN_WIDTH, MIN_HEIGHT = 320, 180
MAX_WIDTH, MAX_HEIGHT = 7680, 4320

SUBTITLES_FROM: Final = 3 / 8
"""播片的字幕从播片的这个比例处开始，依次等分其后的时间：`scene@1` 约 2 秒才判出播片，缺省 4 秒的
播片里字幕在 1.5–2.75 秒与 2.75–4 秒，判出之后都还看得见。"""


class SceneType(StrEnum):
    """合成游戏会出现的画面。

    取值是通用画面类型的名称，分类见 `docs/modules/perception.md` 的“画面状态”。
    """

    MENU = "菜单"
    DIALOGUE = "对话"
    CHOICE = "选择"
    TIMED_CHOICE = "计时选择"
    SHOP = "商店"
    CUTSCENE = "过场"
    BAD_ENDING = "坏结局"


@dataclass(frozen=True)
class GameConfig:
    """分辨率（客户区物理像素）、文字速度（每秒显示的字数）、计时选择与播片的时长（秒）。"""

    width: int = 1280
    height: int = 720
    text_cps: float = 30.0
    choice_seconds: float = 5.0
    cutscene_seconds: float = 4.0

    def __post_init__(self) -> None:
        if not (MIN_WIDTH <= self.width <= MAX_WIDTH and MIN_HEIGHT <= self.height <= MAX_HEIGHT):
            raise ValueError(
                f"分辨率须在 {MIN_WIDTH}×{MIN_HEIGHT} 与 {MAX_WIDTH}×{MAX_HEIGHT} 之间："
                f"{self.width}×{self.height}"
            )
        if not (math.isfinite(self.text_cps) and self.text_cps > 0):
            raise ValueError(f"文字速度须为正数：{self.text_cps}")
        if not (math.isfinite(self.choice_seconds) and self.choice_seconds > 0):
            raise ValueError(f"计时时长须为正数：{self.choice_seconds}")
        if not (math.isfinite(self.cutscene_seconds) and self.cutscene_seconds > 0):
            raise ValueError(f"播片时长须为正数：{self.cutscene_seconds}")


@dataclass(frozen=True)
class Target:
    """一个可点目标：`rect` 内的左键点击会触发它。"""

    id: str
    rect: Rect
    label: str


class EffectKind(StrEnum):
    HIT = "hit"
    """左键点击落在某个可点目标内并触发了它。"""
    MISS = "miss"
    """左键点击没有落在任何可点目标内（有弹窗时，弹窗之外的点击都算未命中）。"""
    KEY = "key"
    """短按被当前画面处理。"""
    SCROLL = "scroll"
    """滚动落在可滚动的区域内并被处理。"""
    IGNORED = "ignored"
    """当前画面不处理这个输入：非左键点击、没有绑定的键、可滚动区域之外的滚动、播片中的任何输入。"""


@dataclass(frozen=True)
class Effect:
    """一个输入的效果。`target` 是被触发、选中或滚动的目标；`changed` 表示状态是否改变。"""

    input: Input
    kind: EffectKind
    target: str | None
    changed: bool


@dataclass(frozen=True)
class Overlay:
    """叠加在画面上的弹窗。通用画面类型里没有弹窗，画面类型仍是底下那个画面的。"""

    text: str


@dataclass(frozen=True)
class Truth:
    """当前状态的真值，只给测试用。"""

    scene: SceneType
    node: str
    overlay: Overlay | None
    targets: tuple[Target, ...]
    """当前能点的目标；有弹窗时只有弹窗上的目标，播片时没有。"""
    selected: str | None
    """键盘光标所在的目标。"""
    text: str
    """画面的主要文字全文：台词、提问、结局文字、播片当前的字幕（没有时为空），菜单与商店为标题。"""
    visible_text: str
    """已经显示出来的部分；只有对话逐字显示时与 `text` 不同。"""
    timer_remaining: float | None
    coins: int
    owned: frozenset[str]
    last_effect: Effect | None
    cutscene_start: float | None
    """播片时，播片开始的时钟读数（秒）；其余为 None。"""
    cutscene_end: float | None
    """播片时，播片放完、回到下一节点的时钟读数（秒）；其余为 None。"""


@dataclass(frozen=True)
class _Popup:
    text: str
    then: str | None
    """关闭后进入的节点；为空时回到底下的画面。"""


@dataclass(frozen=True)
class _State:
    scene: SceneType
    node: str
    coins: int
    owned: frozenset[str]
    line: int = 0
    line_started: float = 0.0
    """对话里本句开始的时刻；播片里播片开始的时刻。"""
    revealed: bool = False
    cursor: int = 0
    deadline: float | None = None
    """计时选择的截止时刻；播片放完的时刻。"""
    shop_offset: int = 0
    popup: _Popup | None = None


MENU_ENTRIES: tuple[tuple[str, str], ...] = (
    ("menu.start", UI.menu_start),
    ("menu.shop", UI.menu_shop),
    ("menu.help", UI.menu_help),
)


def _node[T: Dialogue | Choice | Ending | Cutscene](node_id: str, kind: type[T]) -> T:
    node = NODES[node_id]
    if not isinstance(node, kind):
        raise TypeError(f"节点 {node_id} 不是 {kind.__name__}")
    return node


class SyntheticGame:
    def __init__(self, config: GameConfig | None = None, clock: Clock | None = None) -> None:
        self.config = config if config is not None else GameConfig()
        self._clock = clock if clock is not None else MonotonicClock()
        self._layout = Layout(self.config.width, self.config.height)
        self._state = _State(
            scene=SceneType.MENU, node=TITLE, coins=STARTING_COINS, owned=frozenset()
        )
        self._last_effect: Effect | None = None
        self._renderer: Renderer | None = None
        self._frame: Frame | None = None
        self._frame_view: View | None = None

    # 公开接口

    def handle(self, event: Input) -> Effect:
        """在当前时钟读数下处理一个输入，返回它的效果。"""
        now = self._sync()
        before = self._state
        kind, target, after = self._dispatch(before, event, now)
        self._state = after
        self._last_effect = Effect(event, kind, target, changed=after != before)
        return self._last_effect

    def frame(self) -> Frame:
        """当前画面的帧。画面内容变化时重新渲染并把帧号加一，没变时返回同一帧。"""
        now = self._sync()
        view = self._view(self._state, now)
        if self._frame is None or view != self._frame_view:
            if self._renderer is None:
                self._renderer = Renderer(self.config.width, self.config.height)
            number = 1 if self._frame is None else self._frame.number + 1
            data = self._renderer.render(view)
            self._frame = Frame(data, self.config.width, self.config.height, number)
            self._frame_view = view
        return self._frame

    def truth(self) -> Truth:
        now = self._sync()
        state = self._state
        targets = self._targets(state)
        text, visible = self._texts(state, now)
        remaining = None
        if (
            state.scene is SceneType.TIMED_CHOICE
            and state.deadline is not None
            and state.popup is None
        ):
            remaining = max(0.0, state.deadline - now)
        start = end = None
        if state.scene is SceneType.CUTSCENE:
            start, end = state.line_started, state.deadline
        return Truth(
            scene=state.scene,
            node=state.node,
            overlay=Overlay(state.popup.text) if state.popup is not None else None,
            targets=targets,
            selected=self._selected(state),
            text=text,
            visible_text=visible,
            timer_remaining=remaining,
            coins=state.coins,
            owned=state.owned,
            last_effect=self._last_effect,
            cutscene_start=start,
            cutscene_end=end,
        )

    # 时间

    def _sync(self) -> float:
        """读一次时钟并处理到点的计时与播片；到点的转换发生在截止时刻，与何时被处理无关。"""
        now = self._clock.now()
        state = self._state
        while (
            state.scene in (SceneType.TIMED_CHOICE, SceneType.CUTSCENE)
            and state.popup is None
            and state.deadline is not None
            and now >= state.deadline
        ):
            if state.scene is SceneType.CUTSCENE:
                after = _node(state.node, Cutscene).next
            else:
                timeout_next = _node(state.node, Choice).timeout_next
                if timeout_next is None:
                    raise TypeError(f"节点 {state.node} 不是计时选择")
                after = timeout_next
            state = self._enter(state, after, state.deadline)
        self._state = state
        return now

    def _visible_chars(self, state: _State, text: str, now: float) -> int:
        if state.revealed:
            return len(text)
        shown = math.floor((now - state.line_started) * self.config.text_cps)
        return max(0, min(len(text), shown))

    def _subtitle(self, state: _State, now: float) -> str:
        """播片此刻的字幕：从播片的 `SUBTITLES_FROM` 处起依次等分其后的时间；之前没有字幕。"""
        subtitles = _node(state.node, Cutscene).subtitles
        length = self.config.cutscene_seconds
        start = length * SUBTITLES_FROM
        elapsed = now - state.line_started
        if not subtitles or elapsed < start:
            return ""
        index = math.floor((elapsed - start) * len(subtitles) / (length - start))
        return subtitles[min(index, len(subtitles) - 1)]

    # 转换

    def _enter(self, state: _State, node_id: str, now: float) -> _State:
        """进入一个节点；金币与已购物品跨画面保留，其余状态重置。"""
        base = _State(scene=SceneType.MENU, node=TITLE, coins=state.coins, owned=state.owned)
        if node_id == TITLE:
            return base
        if node_id == SHOP:
            return replace(base, scene=SceneType.SHOP, node=SHOP)
        match NODES[node_id]:
            case Dialogue():
                return replace(base, scene=SceneType.DIALOGUE, node=node_id, line_started=now)
            case Choice(timeout_next=None):
                return replace(base, scene=SceneType.CHOICE, node=node_id)
            case Choice():
                deadline = now + self.config.choice_seconds
                return replace(base, scene=SceneType.TIMED_CHOICE, node=node_id, deadline=deadline)
            case Ending():
                return replace(base, scene=SceneType.BAD_ENDING, node=node_id)
            case Cutscene():
                end = now + self.config.cutscene_seconds
                return replace(
                    base, scene=SceneType.CUTSCENE, node=node_id, line_started=now, deadline=end
                )

    def _dispatch(
        self, state: _State, event: Input, now: float
    ) -> tuple[EffectKind, str | None, _State]:
        if state.scene is SceneType.CUTSCENE:
            return EffectKind.IGNORED, None, state  # 播片中的输入都忽略
        match event:
            case Click(button=MouseButton.LEFT):
                for target in self._targets(state):
                    if target.rect.contains(event.x, event.y):
                        return EffectKind.HIT, target.id, self._activate(state, target.id, now)
                return EffectKind.MISS, None, state
            case Click():
                return EffectKind.IGNORED, None, state
            case Press():
                return self._press(state, event.key, now)
            case Scroll():
                return self._scroll(state, event)

    def _activate(self, state: _State, target_id: str, now: float) -> _State:
        """触发一个目标：点击命中它，或按回车、空格时光标在它上面。"""
        if state.popup is not None:
            # 弹窗是模态的，只有“确定”可用。
            then = state.popup.then
            cleared = replace(state, popup=None)
            return cleared if then is None else self._enter(cleared, then, now)
        match target_id.split("."):
            case ["menu", "start"]:
                return self._enter(state, START, now)
            case ["menu", "shop"]:
                return self._enter(state, SHOP, now)
            case ["menu", "help"]:
                cursor = [entry[0] for entry in MENU_ENTRIES].index(target_id)
                return replace(state, cursor=cursor, popup=_Popup(UI.help_text, None))
            case ["dialogue", "box"]:
                return self._advance_dialogue(state, now)
            case ["choice", index]:
                return self._enter(state, _node(state.node, Choice).options[int(index)].next, now)
            case ["shop", "leave"]:
                return self._enter(state, TITLE, now)
            case ["shop", "item", index]:
                return self._buy(state, int(index))
            case ["ending", "retry"]:
                return self._enter(state, START, now)
            case _:
                raise ValueError(f"未知的目标：{target_id}")

    def _advance_dialogue(self, state: _State, now: float) -> _State:
        node = _node(state.node, Dialogue)
        text = node.lines[state.line].text
        if self._visible_chars(state, text, now) < len(text):
            return replace(state, revealed=True)
        if state.line + 1 < len(node.lines):
            return replace(state, line=state.line + 1, line_started=now, revealed=False)
        finished = replace(state, revealed=True, coins=state.coins + node.reward)
        if node.popup_after is not None:
            return replace(finished, popup=_Popup(node.popup_after, node.next))
        return self._enter(finished, node.next, now)

    def _buy(self, state: _State, index: int) -> _State:
        item = SHOP_ITEMS[index]
        state = replace(state, cursor=index)
        if item.id in state.owned:
            return state
        if state.coins < item.price:
            return replace(state, popup=_Popup(UI.not_enough_coins, None))
        return replace(state, coins=state.coins - item.price, owned=state.owned | {item.id})

    def _press(self, state: _State, key: Key, now: float) -> tuple[EffectKind, str | None, _State]:
        confirm = key in (Key.ENTER, Key.SPACE)
        move = {Key.UP: -1, Key.DOWN: 1}.get(key)
        if state.popup is not None:
            if confirm or key is Key.ESCAPE:
                return EffectKind.KEY, "popup.ok", self._activate(state, "popup.ok", now)
            return EffectKind.IGNORED, None, state
        selected = self._selected(state)
        match state.scene:
            case SceneType.MENU | SceneType.CHOICE | SceneType.TIMED_CHOICE if move is not None:
                count = (
                    len(MENU_ENTRIES)
                    if state.scene is SceneType.MENU
                    else self._option_count(state)
                )
                moved = replace(state, cursor=(state.cursor + move) % count)
                return EffectKind.KEY, self._selected(moved), moved
            case SceneType.SHOP if move is not None:
                moved = _shop_with_cursor(state, state.cursor + move)
                return EffectKind.KEY, self._selected(moved), moved
            case SceneType.SHOP if key is Key.ESCAPE:
                return EffectKind.KEY, "shop.leave", self._activate(state, "shop.leave", now)
            case _ if confirm and selected is not None:
                return EffectKind.KEY, selected, self._activate(state, selected, now)
            case _:
                return EffectKind.IGNORED, None, state

    def _scroll(self, state: _State, event: Scroll) -> tuple[EffectKind, str | None, _State]:
        in_list = self._layout.shop_list().contains(event.x, event.y)
        if state.scene is not SceneType.SHOP or state.popup is not None or not in_list:
            return EffectKind.IGNORED, None, state
        limit = max(0, len(SHOP_ITEMS) - SHOP_VISIBLE_ROWS)
        offset = min(max(state.shop_offset - event.notches, 0), limit)
        cursor = min(max(state.cursor, offset), offset + SHOP_VISIBLE_ROWS - 1)
        return EffectKind.SCROLL, "shop.list", replace(state, shop_offset=offset, cursor=cursor)

    # 查询

    def _option_count(self, state: _State) -> int:
        return len(_node(state.node, Choice).options)

    def _targets(self, state: _State) -> tuple[Target, ...]:
        layout = self._layout
        if state.popup is not None:
            return (Target("popup.ok", layout.popup_ok(), UI.popup_ok),)
        match state.scene:
            case SceneType.MENU:
                return tuple(
                    Target(target_id, layout.menu_button(index), label)
                    for index, (target_id, label) in enumerate(MENU_ENTRIES)
                )
            case SceneType.DIALOGUE:
                return (Target("dialogue.box", layout.dialogue_box(), ""),)
            case SceneType.CHOICE | SceneType.TIMED_CHOICE:
                return tuple(
                    Target(f"choice.{index}", layout.choice_option(index), option.label)
                    for index, option in enumerate(_node(state.node, Choice).options)
                )
            case SceneType.SHOP:
                rows = tuple(
                    Target(f"shop.item.{index}", layout.shop_row(slot), SHOP_ITEMS[index].name)
                    for slot, index in enumerate(_visible_items(state))
                )
                return (*rows, Target("shop.leave", layout.shop_leave(), UI.shop_leave))
            case SceneType.CUTSCENE:
                return ()
            case SceneType.BAD_ENDING:
                return (Target("ending.retry", layout.ending_retry(), UI.retry),)

    def _selected(self, state: _State) -> str | None:
        if state.popup is not None:
            return "popup.ok"
        match state.scene:
            case SceneType.MENU:
                return MENU_ENTRIES[state.cursor][0]
            case SceneType.DIALOGUE:
                return "dialogue.box"
            case SceneType.CHOICE | SceneType.TIMED_CHOICE:
                return f"choice.{state.cursor}"
            case SceneType.SHOP:
                return f"shop.item.{state.cursor}"
            case SceneType.CUTSCENE:
                return None
            case SceneType.BAD_ENDING:
                return "ending.retry"

    def _texts(self, state: _State, now: float) -> tuple[str, str]:
        if state.scene is SceneType.MENU:
            return UI.game_title, UI.game_title
        if state.scene is SceneType.SHOP:
            return UI.shop_title, UI.shop_title
        match NODES[state.node]:
            case Dialogue(lines=lines):
                text = lines[state.line].text
                return text, text[: self._visible_chars(state, text, now)]
            case Choice(prompt=prompt):
                return prompt, prompt
            case Ending(text=text):
                return text, text
            case Cutscene():
                subtitle = self._subtitle(state, now)
                return subtitle, subtitle

    def _view(self, state: _State, now: float) -> View:
        selected = None if state.popup is not None else self._selected(state)
        buttons = tuple(
            Button(target.rect, target.label, target.id == selected)
            for target in self._targets(replace(state, popup=None))
        )
        screen: Screen
        match state.scene:
            case SceneType.MENU:
                screen = MenuView(UI.game_title, buttons)
            case SceneType.DIALOGUE:
                line = _node(state.node, Dialogue).lines[state.line]
                shown = self._visible_chars(state, line.text, now)
                screen = DialogueView(
                    state.node, line.speaker, line.text[:shown], shown == len(line.text)
                )
            case SceneType.CHOICE | SceneType.TIMED_CHOICE:
                node = _node(state.node, Choice)
                timer_text = timer_fraction = None
                if state.deadline is not None:
                    # 显示精确到 0.1 秒，向上取整，到点前不会显示 0.0。
                    remaining = max(0.0, state.deadline - now)
                    tenths = math.ceil(remaining * 10)
                    timer_text = UI.remaining.format(seconds=f"{tenths / 10:.1f}")
                    timer_fraction = remaining / self.config.choice_seconds
                screen = ChoiceView(node.prompt, buttons, timer_text, timer_fraction)
            case SceneType.SHOP:
                rows = tuple(
                    ShopRow(
                        button.rect,
                        button.label,
                        UI.owned if item.id in state.owned else UI.price.format(price=item.price),
                        item.id in state.owned,
                        button.selected,
                    )
                    for button, item in zip(
                        buttons[:-1],
                        (SHOP_ITEMS[index] for index in _visible_items(state)),
                        strict=True,
                    )
                )
                screen = ShopView(
                    UI.shop_title,
                    UI.coins.format(coins=state.coins),
                    rows,
                    buttons[-1],
                    state.shop_offset,
                    len(SHOP_ITEMS),
                )
            case SceneType.CUTSCENE:
                # 动画按毫秒变：时钟读数不同的两帧画面不同。
                elapsed_ms = round((now - state.line_started) * 1000)
                screen = CutsceneView(elapsed_ms, self._subtitle(state, now))
            case SceneType.BAD_ENDING:
                screen = EndingView(UI.bad_ending, _node(state.node, Ending).text, buttons[0])
        popup = None
        if state.popup is not None:
            popup = PopupView(state.popup.text, Button(self._layout.popup_ok(), UI.popup_ok, True))
        return View(screen, popup)


def _visible_items(state: _State) -> range:
    end = min(len(SHOP_ITEMS), state.shop_offset + SHOP_VISIBLE_ROWS)
    return range(state.shop_offset, end)


def _shop_with_cursor(state: _State, cursor: int) -> _State:
    """移动商店光标并滚动列表，让光标所在行保持可见。"""
    cursor = min(max(cursor, 0), len(SHOP_ITEMS) - 1)
    offset = state.shop_offset
    if cursor < offset:
        offset = cursor
    elif cursor >= offset + SHOP_VISIBLE_ROWS:
        offset = cursor - SHOP_VISIBLE_ROWS + 1
    return replace(state, cursor=cursor, shop_offset=offset)
