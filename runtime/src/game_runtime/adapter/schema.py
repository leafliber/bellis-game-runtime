"""游戏适配包的声明，即 `adapter.toml` 的 Schema 源（GR-31）。

表、字段、单位、坐标约定与校验规则见 `docs/modules/game-adapter.md` 的“格式”。这里只描述并核对
声明本身；读文件在 `game_runtime.adapter.load`，目录内容的核对在 `game_runtime.adapter.check`。

声明里的标量不做类型转换（布尔、整数与字符串互不转换，小数字段可以写整数），列表读成元组，
任何一层有未知字段即不合法。
"""

from collections import Counter
from collections.abc import Iterable, Iterator, Mapping
from enum import StrEnum
from typing import Annotated, Any, Literal, NamedTuple, Self, cast
from unicodedata import normalize

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    Strict,
    StrictBool,
    StrictInt,
    StrictStr,
    StringConstraints,
    model_validator,
)

from core_protocol import models
from core_protocol.models import MouseButton
from game_runtime.core_link.capture import WindowRule

Ident = Annotated[StrictStr, StringConstraints(pattern=r"^[a-z][a-z0-9-]*$", max_length=48)]
"""适配包、ROI、画面、签名、笔记栏目与前置条件的标识。"""

Text = Annotated[StrictStr, StringConstraints(pattern=r"^\S(?:.*\S)?$")]
"""给人读的一行文字：非空，首尾不是空白。"""

Fraction = Annotated[float, Strict(), Field(ge=0.0, le=1.0)]
"""归一化坐标，[0, 1]。"""

Seconds = Annotated[float, Strict(), Field(ge=0.0, allow_inf_nan=False)]
PositiveSeconds = Annotated[float, Strict(), Field(gt=0.0, allow_inf_nan=False)]


class NotCalibratedError(Exception):
    """取用了尚未标定的项。使用方据此停用依赖它的功能并报告，不回退到缺省值，也不猜测坐标。"""

    def __init__(self, game: str, item: str) -> None:
        super().__init__(f"适配包 {game} 的{item}尚未标定")
        self.game = game
        self.item = item


# 坐标


class PixelRect(NamedTuple):
    """客户区物理像素的矩形：含左边与上边，不含右边与下边。"""

    left: int
    top: int
    right: int
    bottom: int


class NormalizedRect(NamedTuple):
    """客户区归一化坐标的矩形 `[左, 上, 右, 下]`。"""

    left: Fraction
    top: Fraction
    right: Fraction
    bottom: Fraction

    def to_pixels(self, width: int, height: int) -> PixelRect:
        """换算到 `width`×`height` 的客户区：各边乘以宽或高后取最近的整数（恰为 .5 时取偶数）。"""
        return PixelRect(
            round(self.left * width),
            round(self.top * height),
            round(self.right * width),
            round(self.bottom * height),
        )


class NormalizedPoint(NamedTuple):
    """客户区归一化坐标的点 `[x, y]`。"""

    x: Fraction
    y: Fraction


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Roi(_Model):
    title: Text
    rect: NormalizedRect | None = None
    """没有即尚未标定。"""

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        rect = self.rect
        if rect is not None and not (rect.left < rect.right and rect.top < rect.bottom):
            raise ValueError("rect 须满足 左 < 右、上 < 下")
        return self


# 必需项


def _file_name(value: str) -> str:
    if "/" in value or "\\" in value:
        raise ValueError("只写可执行文件名，不含目录")
    return value


class WindowMatch(_Model):
    """窗口识别：进程名加窗口类或标题，规则同 `WindowRule`。"""

    process_name: Annotated[Text, AfterValidator(_file_name)]
    window_class: Text | None = None
    title: Text | None = None

    @model_validator(mode="after")
    def _class_or_title(self) -> Self:
        if self.window_class is None and self.title is None:
            raise ValueError("window_class 与 title 至少给出一个")
        return self

    def rule(self) -> WindowRule:
        """采集配置的窗口识别规则。"""
        return WindowRule(
            process_name=self.process_name, window_class=self.window_class, title=self.title
        )


_Pixels = Annotated[StrictInt, Field(ge=1, le=16384)]


class Resolution(_Model):
    """期望的客户区分辨率（物理像素）。"""

    width: _Pixels
    height: _Pixels


class RoiText(_Model):
    """ROI 内的 OCR 固定文字：各行依次连接、去掉空白后包含同样去掉空白的 `text` 即相符。"""

    roi: Ident
    text: Text

    def matches(self, lines: Iterable[str]) -> bool:
        """OCR 在 ROI 内读出的各行（按阅读顺序）是否与这一项相符。"""
        return _squash(self.text) in _squash("".join(lines))


def _squash(text: str) -> str:
    """去掉全部空白（含全角空格）。"""
    return "".join(text.split())


class FixedText(RoiText):
    """语言核对的一项：出现在画面 `screen` 上。"""

    screen: Ident


class Language(_Model):
    tag: Annotated[
        StrictStr,
        StringConstraints(pattern=r"^[a-z]{2,3}(?:-[A-Z][a-z]{3})?(?:-(?:[A-Z]{2}|[0-9]{3}))?$"),
    ]
    """BCP 47 的语言加可选的文字与地区，如 `zh-Hans`。"""
    fixed_texts: tuple[FixedText, ...]
    """语言核对的固定文字一层，可为空：为空即只做文字系统一层，不算尚未标定。"""


ARK = "volcengine-ark"
"""火山方舟的厂商标识。"""

C2_ENDPOINTS = frozenset({"glm-5-3-flash-260828"})
"""C-2 中已确认启用安全沙箱的方舟接入点。"""

_DOUBAO = "doubao-"


def c2_allows(vendor: str, model: str) -> bool:
    """`vendor` 上的 `model` 是否在合规确认 C-2 的允许清单内。

    清单见 `docs/compliance.md` 的 C-2，形式见 `docs/modules/models.md` 的“允许清单”：方舟上的
    豆包系列（型号 ID 以 `doubao-` 开头）与已确认启用安全沙箱的接入点。这是清单在代码里唯一的一份，
    随 C-2 修改。
    """
    if vendor != ARK:
        return False
    return model in C2_ENDPOINTS or (model.startswith(_DOUBAO) and len(model) > len(_DOUBAO))


class AllowedModel(_Model):
    """允许接收游戏画面与文本的一个模型；须在 C-2 的允许清单内。"""

    vendor: Ident
    model: Annotated[StrictStr, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")]
    """请求 `model` 字段的完整值：型号 ID 或接入点 ID。"""

    @model_validator(mode="after")
    def _allowlist(self) -> Self:
        if not c2_allows(self.vendor, self.model):
            raise ValueError(f"{self.vendor} 上的 {self.model} 不在 C-2 的允许清单内")
        return self


class Compliance(_Model):
    """合规声明，字段的含义见模块文档的“合规声明的字段”。"""

    basis: Text
    text_source: Literal["screen"]
    training: StrictBool
    demonstrations: StrictBool
    recording_quota_gb: Annotated[StrictInt, Field(ge=0)]
    models: tuple[AllowedModel, ...]

    @model_validator(mode="after")
    def _unique(self) -> Self:
        _raise(_duplicates("models", (f"{m.vendor} 上的 {m.model}" for m in self.models)))
        return self


def _scan_code(code: int) -> int:
    prefix, low = code >> 8, code & 0xFF
    if prefix not in (0, 0xE0) or not 0 < low < 0x80:
        raise ValueError(f"{code:#06x} 不是 Set 1 的按下扫描码（扩展键在高字节带 0xE0）")
    return code


ScanCode = Annotated[StrictInt, Field(ge=1, le=0xFFFF), AfterValidator(_scan_code)]

PRESS_DURATION_LIMIT = 0.5
"""点击与短按的按住时长的上限（秒）；更长就是长按，须写进可按住的键。"""


class Whitelist(_Model):
    """动作白名单，字段的含义见模块文档的“动作白名单”。"""

    click_buttons: tuple[MouseButton, ...]
    press_keys: tuple[ScanCode, ...]
    scroll: StrictBool
    click_regions: tuple[Ident, ...]
    press_duration: Annotated[float, Strict(), Field(gt=0.0, le=PRESS_DURATION_LIMIT)]
    max_rate: Annotated[float, Strict(), Field(gt=0.0, allow_inf_nan=False)]
    hold_keys: tuple[ScanCode, ...]
    hold_buttons: tuple[MouseButton, ...]
    max_hold: PositiveSeconds | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        problems: list[str] = []
        for field in ("click_buttons", "press_keys", "click_regions", "hold_keys", "hold_buttons"):
            problems += _duplicates(field, getattr(self, field))
        if self.allows_hold and self.max_hold is None:
            problems.append("允许按住时必须写 max_hold")
        if not self.allows_hold and self.max_hold is not None:
            problems.append("hold_keys 与 hold_buttons 都为空（不允许长按）时不能写 max_hold")
        if (self.click_buttons or self.scroll) and not self.click_regions:
            problems.append("允许点击或滚动时 click_regions 不能为空")
        _raise(problems)
        return self

    @property
    def allows_hold(self) -> bool:
        return bool(self.hold_keys or self.hold_buttons)


# 画面类型映射


class ScreenType(StrEnum):
    """通用画面类型，分类见 `docs/modules/perception.md` 的“画面状态”；取值就是分类的名称。"""

    DIALOGUE = "对话"
    CHOICE = "选择"
    TIMED_CHOICE = "计时选择"
    MENU = "菜单"
    SETTINGS = "设置"
    SAVE_LOAD = "存读档"
    SHOP = "商店"
    LOADING = "加载"
    CUTSCENE = "过场"
    REALTIME_PLAY = "即时玩法"
    MAP = "地图"
    INVENTORY = "背包"
    BAD_ENDING = "坏结局"
    STUCK = "卡住"
    UNKNOWN = "未知"


RUNTIME_ONLY_TYPES = frozenset({ScreenType.STUCK, ScreenType.UNKNOWN})
"""由运行时给出、不对应游戏画面的类型：不能作为映射目标或扩展的 `base`。"""

GENERIC_NAMES = frozenset(member.value for member in ScreenType)


def _mappable(base: ScreenType) -> ScreenType:
    if base in RUNTIME_ONLY_TYPES:
        raise ValueError(f"{base} 由运行时给出，不能作为映射目标")
    return base


def _not_generic(name: str) -> str:
    if name in GENERIC_NAMES:
        raise ValueError(f"{name} 是通用画面类型，不能再作为扩展声明")
    return name


class ScreenTypeExtension(_Model):
    """本游戏扩展的画面类型。"""

    name: Annotated[Text, AfterValidator(_not_generic)]
    base: Annotated[ScreenType, AfterValidator(_mappable)]
    """所属的通用画面类型；风险等级与仲裁规则的缺省按它取用（G2）。"""


class Screen(_Model):
    """本游戏的一个画面。"""

    id: Ident
    title: Text
    type: Text
    """通用画面类型的名称或本包扩展的 `name`。"""


class Precondition(_Model):
    id: Ident
    text: Text


class ScreenRisk(StrEnum):
    DECISION_ONLY = "decision-only"
    RETURN_ONLY = "return-only"
    BLOCKED = "blocked"


_RETURN_LABELS = frozenset(
    {
        "返回",
        "关闭",
        "离开",
        "取消",
        "返回上一级",
        "back",
        "close",
        "cancel",
        "return",
        "leave",
        "戻る",
        "閉じる",
        "キャンセル",
        "뒤로",
        "닫기",
        "취소",
    }
)


def _return_label(text: str) -> str:
    if _squash(normalize("NFKC", text).casefold()) not in _RETURN_LABELS:
        raise ValueError("返回目标须是明确的返回、关闭或取消按钮")
    return text


class ReturnTarget(_Model):
    """通过当前帧的 OCR 核验的返回按钮。"""

    roi: Ident
    text: Annotated[Text, AfterValidator(_return_label)]


class Risk(_Model):
    """静态禁区、操作员前置条件和对通用画面策略的收紧。"""

    forbidden_words: tuple[Text, ...] = ()
    """本游戏的禁用词，与通用词表一起生效。"""
    forbidden_zones: tuple[Ident, ...] = ()
    """静态禁区：ROI 名。"""
    preconditions: tuple[Precondition, ...] = ()
    """操作员前置条件。"""
    screens: Mapping[Ident, ScreenRisk] = Field(default_factory=dict[Ident, ScreenRisk])
    returns: Mapping[Ident, ReturnTarget] = Field(default_factory=dict[Ident, ReturnTarget])

    @model_validator(mode="after")
    def _unique(self) -> Self:
        _raise(
            _duplicates("forbidden_words", self.forbidden_words)
            + _duplicates("forbidden_zones", self.forbidden_zones)
            + _duplicates("preconditions", (p.id for p in self.preconditions))
        )
        return self


class DecisionTimer(_Model):
    roi: Ident
    prefix: str = ""
    suffix: str = ""


class DecisionRegion(_Model):
    """选择画面的选项区域及可选提问、计时文字。"""

    options: Ident
    prompt: Ident | None = None
    timer: DecisionTimer | None = None


# 参数与快路径


class Pacing(_Model):
    """阅读节奏，字段的含义见模块文档的“阅读节奏”。"""

    min_action_interval: Seconds
    read_base: Seconds
    read_per_char: Seconds
    reread_base: Seconds
    """缺省同 `read_base`。"""
    reread_per_char: Seconds
    """缺省同 `read_per_char`。"""
    voice_unknown_wait: PositiveSeconds
    """配音未知时代替“配音为闲”的最长等待，从文字完整时算起。"""
    voice_busy_wait: PositiveSeconds
    """配音一直为忙时代替“配音为闲”的最长等待，从文字完整时算起。"""

    @model_validator(mode="before")
    @classmethod
    def _reread_defaults(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        fields = dict(cast(dict[str, Any], data))
        for reread, read in (("reread_base", "read_base"), ("reread_per_char", "read_per_char")):
            if reread not in fields and read in fields:
                fields[reread] = fields[read]
        return fields

    @model_validator(mode="after")
    def _reread_not_longer(self) -> Self:
        problems: list[str] = []
        if self.reread_base > self.read_base:
            problems.append("reread_base 不能比 read_base 长")
        if self.reread_per_char > self.read_per_char:
            problems.append("reread_per_char 不能比 read_per_char 长")
        _raise(problems)
        return self


class Anchor(_Model):
    """锚点模板：在 ROI 内匹配本地数据目录里按摘要引用的模板。"""

    roi: Ident
    template_sha256: Annotated[StrictStr, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
    min_score: Annotated[float, Strict(), Field(gt=0.0, le=1.0)]


class PixelCheck(_Model):
    """固定位置的像素颜色检查。"""

    at: NormalizedPoint
    color: Annotated[StrictStr, StringConstraints(pattern=r"^#[0-9A-Fa-f]{6}$")]
    tolerance: Annotated[StrictInt, Field(ge=0, le=255)]


class Signature(_Model):
    """画面签名：组成项全部通过即命中；没有组成项即尚未标定。"""

    id: Ident
    screen: Ident
    priority: StrictInt = 0
    """叠加优先级，大的优先。"""
    anchors: tuple[Anchor, ...] = ()
    pixels: tuple[PixelCheck, ...] = ()
    texts: tuple[RoiText, ...] = ()

    @property
    def calibrated(self) -> bool:
        return bool(self.anchors or self.pixels or self.texts)


class DialogueBox(_Model):
    """一个对白框：剧情记录按它读文字。

    引用的 ROI 与签名只须已声明，可以尚未标定；取用见 `Adapter.require_dialogue_boxes`。
    """

    screen: Ident | None = None
    """对白框所在的画面，类型须为“对话”或以“对话”为 base 的扩展：只在判为这个画面的帧上读。
    不写即通用缺省：在每个不是过场的帧上读，判别结果不限。"""
    text: Ident
    """对白文字的 ROI。"""
    speaker: Ident | None = None
    """名字栏的 ROI；没有名字栏的画面不写。"""
    continue_signature: Ident | None = None
    """继续标记的签名：指向同一画面（不写 `screen` 时指向一个对话类的画面），本帧命中即继续标记
    可见；游戏没有继续标记时不写。"""

    @model_validator(mode="after")
    def _distinct(self) -> Self:
        if self.speaker == self.text:
            raise ValueError("speaker 与 text 须是不同的 ROI")
        return self

    @property
    def rois(self) -> tuple[str, ...]:
        """引用的 ROI：名字栏（如有）与对白文字。"""
        return (self.text,) if self.speaker is None else (self.speaker, self.text)


class NoteSection(_Model):
    """planner 的一个笔记栏目；只是结构，不含从游戏派生的内容。"""

    id: Ident
    title: Text
    description: Text | None = None


# 适配包


class Adapter(_Model):
    """一个游戏适配包的声明。`game-adapter@1` 提供的就是它，只读。"""

    id: Ident
    name: Text
    version: Annotated[StrictStr, StringConstraints(min_length=1, max_length=32)]
    window: WindowMatch
    resolution: Resolution
    language: Language
    compliance: Compliance
    whitelist: Whitelist
    screens: tuple[Screen, ...]
    screen_types: tuple[ScreenTypeExtension, ...] = ()
    risk: Risk
    decisions: Mapping[Ident, DecisionRegion] = Field(default_factory=dict[Ident, DecisionRegion])
    rois: Mapping[Ident, Roi] = Field(default_factory=dict[str, Roi])
    pacing: Pacing | None = None
    """本游戏的阅读节奏覆盖；没有即用通用缺省（由使用方定义，G2）。不是标定项。"""
    signatures: tuple[Signature, ...] = ()
    dialogue_boxes: tuple[DialogueBox, ...] = ()
    notes: tuple[NoteSection, ...] = ()

    @model_validator(mode="after")
    def _cross_references(self) -> Self:
        _raise(list(self._problems()))
        return self

    def _problems(self) -> Iterator[str]:
        yield from _duplicates("screens", (screen.id for screen in self.screens))
        yield from _duplicates("screen_types", (ext.name for ext in self.screen_types))
        yield from _duplicates("signatures", (signature.id for signature in self.signatures))
        yield from _duplicates("notes", (note.id for note in self.notes))

        types = (GENERIC_NAMES - RUNTIME_ONLY_TYPES) | {ext.name for ext in self.screen_types}
        for index, screen in enumerate(self.screens):
            if screen.type in RUNTIME_ONLY_TYPES:
                yield f"screens[{index}].type：{screen.type} 由运行时给出，不能作为映射目标"
            elif screen.type not in types:
                yield (
                    f"screens[{index}].type：{screen.type} 不是通用画面类型，"
                    "也没有在 screen_types 中声明"
                )

        screens = {screen.id for screen in self.screens}
        for where, screen in self._screen_refs():
            if screen not in screens:
                yield f"{where}：没有声明画面 {screen}"
        for where, name in self._roi_refs():
            roi = self.rois.get(name)
            if roi is None:
                yield f"{where}：没有声明 ROI {name}"
            elif roi.rect is None:
                yield f"{where}：ROI {name} 尚未标定，不能引用"
        yield from self._dialogue_box_problems()
        yield from self._control_problems(screens)

    def _control_problems(self, screens: set[str]) -> Iterator[str]:
        for screen in self.risk.returns:
            if screen not in screens:
                continue
            if self.risk.screens.get(screen) is not ScreenRisk.RETURN_ONLY and self.generic_type(
                screen
            ) not in {ScreenType.SETTINGS, ScreenType.SAVE_LOAD, ScreenType.SHOP}:
                yield f"risk.returns.{screen} 只能声明限制为返回的画面的目标"
        for screen, decision in self.decisions.items():
            if screen not in screens:
                continue
            match self.generic_type(screen):
                case ScreenType.TIMED_CHOICE if decision.timer is None:
                    yield f"decisions.{screen} 计时选择须声明 timer"
                case ScreenType.CHOICE if decision.timer is not None:
                    yield f"decisions.{screen} 无计时选择不能声明 timer"
                case ScreenType.CHOICE | ScreenType.TIMED_CHOICE:
                    pass
                case _:
                    yield f"decisions.{screen} 须为选择或计时选择画面"

    def _dialogue_box_problems(self) -> Iterator[str]:
        """对白框：写了画面的，画面是对话类，每个画面至多一项；不写画面的至多一项；引用的 ROI 与
        签名已声明（可以尚未标定）。"""
        boxes = self.dialogue_boxes
        yield from _duplicates(
            "dialogue_boxes", (box.screen for box in boxes if box.screen is not None)
        )
        if sum(1 for box in boxes if box.screen is None) > 1:
            yield "dialogue_boxes：不写 screen 的对白框至多一项"
        signatures = {signature.id: signature for signature in self.signatures}
        for index, box in enumerate(boxes):
            where = f"dialogue_boxes[{index}]"
            if box.screen is not None and not self._is_dialogue(box.screen):
                yield (
                    f"{where}.screen：画面 {box.screen} 的类型是 {self._type_of(box.screen)}，"
                    f"对白框只能在{ScreenType.DIALOGUE}类的画面上"
                )
            for field, name in (("speaker", box.speaker), ("text", box.text)):
                if name is not None and name not in self.rois:
                    yield f"{where}.{field}：没有声明 ROI {name}"
            if box.continue_signature is not None:
                signature = signatures.get(box.continue_signature)
                if signature is None:
                    yield f"{where}.continue_signature：没有声明签名 {box.continue_signature}"
                elif box.screen is not None and signature.screen != box.screen:
                    yield (
                        f"{where}.continue_signature：签名 {signature.id} 指向画面 "
                        f"{signature.screen}，不是对白框所在的 {box.screen}"
                    )
                elif box.screen is None and not self._is_dialogue(signature.screen):
                    yield (
                        f"{where}.continue_signature：签名 {signature.id} 指向画面 "
                        f"{signature.screen}，类型是 {self._type_of(signature.screen)}，"
                        f"不是{ScreenType.DIALOGUE}类的画面"
                    )

    def _type_of(self, screen: str) -> str | None:
        return next((item.type for item in self.screens if item.id == screen), None)

    def _is_dialogue(self, screen: str) -> bool:
        """画面 `screen` 的类型是“对话”或以它为 base 的扩展；没有声明的画面另行报错，这里算是。"""
        screen_type = self._type_of(screen)
        if screen_type is None:
            return True
        bases = {ext.name: ext.base.value for ext in self.screen_types}
        return bases.get(screen_type, screen_type) == ScreenType.DIALOGUE

    def _screen_refs(self) -> Iterator[tuple[str, str]]:
        for field, group in (
            ("risk.screens", self.risk.screens),
            ("risk.returns", self.risk.returns),
            ("decisions", self.decisions),
        ):
            for screen in group:
                yield f"{field}.{screen}", screen
        for index, item in enumerate(self.language.fixed_texts):
            yield f"language.fixed_texts[{index}].screen", item.screen
        for index, signature in enumerate(self.signatures):
            yield f"signatures[{index}].screen", signature.screen
        for index, box in enumerate(self.dialogue_boxes):
            if box.screen is not None:
                yield f"dialogue_boxes[{index}].screen", box.screen

    def _roi_refs(self) -> Iterator[tuple[str, str]]:
        for index, name in enumerate(self.whitelist.click_regions):
            yield f"whitelist.click_regions[{index}]", name
        for index, name in enumerate(self.risk.forbidden_zones):
            yield f"risk.forbidden_zones[{index}]", name
        for screen, target in self.risk.returns.items():
            yield f"risk.returns.{screen}.roi", target.roi
        for screen, decision in self.decisions.items():
            yield f"decisions.{screen}.options", decision.options
            if decision.prompt is not None:
                yield f"decisions.{screen}.prompt", decision.prompt
            if decision.timer is not None:
                yield f"decisions.{screen}.timer.roi", decision.timer.roi
        for index, item in enumerate(self.language.fixed_texts):
            yield f"language.fixed_texts[{index}].roi", item.roi
        for index, signature in enumerate(self.signatures):
            for part, items in (("anchors", signature.anchors), ("texts", signature.texts)):
                for item_index, item in enumerate(items):
                    yield f"signatures[{index}].{part}[{item_index}].roi", item.roi

    # 取用

    def window_rule(self) -> WindowRule:
        """采集配置的窗口识别规则。"""
        return self.window.rule()

    def input_policy(self) -> models.InputPolicy:
        """把动作白名单与静态禁区换算成交给实时核的输入策略（`input.configure` 的 params）：
        ROI 名换成归一化矩形，时长换成毫秒（1–500）。引用的 ROI 未标定即 NotCalibratedError。"""
        whitelist = self.whitelist

        def rect(name: str) -> dict[str, float]:
            roi = self.roi(name)
            return {
                "left": float(roi.left),
                "top": float(roi.top),
                "right": float(roi.right),
                "bottom": float(roi.bottom),
            }

        press_duration_ms = min(500, max(1, round(whitelist.press_duration * 1000)))
        max_hold_ms = (
            None if whitelist.max_hold is None else max(1, round(whitelist.max_hold * 1000))
        )
        # 以 model_validate 构造：扫描码在模型里是 RootModel 包装，直接传整数由 pydantic 校验。
        return models.InputPolicy.model_validate(
            {
                "click_buttons": [button.value for button in whitelist.click_buttons],
                "press_keys": list(whitelist.press_keys),
                "scroll": whitelist.scroll,
                "click_regions": [rect(name) for name in whitelist.click_regions],
                "forbidden_zones": [rect(name) for name in self.risk.forbidden_zones],
                "press_duration_ms": press_duration_ms,
                "max_rate": whitelist.max_rate,
                "hold_keys": list(whitelist.hold_keys),
                "hold_buttons": [button.value for button in whitelist.hold_buttons],
                "max_hold_ms": max_hold_ms,
            }
        )

    def roi(self, name: str) -> NormalizedRect:
        """ROI 的归一化矩形。没有声明即 LookupError，尚未标定即 NotCalibratedError。"""
        roi = self.rois.get(name)
        if roi is None:
            raise LookupError(f"适配包 {self.id} 没有声明 ROI {name}")
        if roi.rect is None:
            raise NotCalibratedError(self.id, f"ROI {name}（{roi.title}）")
        return roi.rect

    def require_dialogue_boxes(self) -> tuple[DialogueBox, ...]:
        """对白框。没有声明即 LookupError；任一对白框引用的 ROI 或继续标记的签名尚未标定即
        NotCalibratedError，一次列出全部这样的项。"""
        if not self.dialogue_boxes:
            raise LookupError(f"适配包 {self.id} 没有声明对白框")
        signatures = {signature.id: signature for signature in self.signatures}
        items: list[str] = []
        for box in self.dialogue_boxes:
            for name in box.rois:
                roi = self.rois[name]
                if roi.rect is None:
                    items.append(f"ROI {name}（{roi.title}）")
            marker = box.continue_signature
            if marker is not None and not signatures[marker].calibrated:
                items.append(f"继续标记的签名 {marker}")
        if items:
            raise NotCalibratedError(self.id, f"对白框的{'、'.join(dict.fromkeys(items))}")
        return self.dialogue_boxes

    def generic_type(self, screen: str) -> ScreenType:
        """画面 `screen` 的通用画面类型：映射到扩展时取扩展的 `base`。没有声明即 LookupError。"""
        for item in self.screens:
            if item.id == screen:
                for ext in self.screen_types:
                    if ext.name == item.type:
                        return ext.base
                return ScreenType(item.type)
        raise LookupError(f"适配包 {self.id} 没有声明画面 {screen}")

    def uncalibrated(self) -> tuple[str, ...]:
        """尚未标定的项，给人读：没有 `rect` 的 ROI 与没有组成项的签名。空的固定文字与没写的阅读
        节奏不是标定项，不列。"""
        items = [
            f"ROI {name}（{roi.title}）" for name, roi in self.rois.items() if roi.rect is None
        ]
        items += [f"签名 {s.id}" for s in self.signatures if not s.calibrated]
        return tuple(items)


def _duplicates(field: str, values: Iterable[object]) -> list[str]:
    counts = Counter(values)
    return [f"{field}：{value} 重复" for value, count in counts.items() if count > 1]


def _raise(problems: list[str]) -> None:
    """交叉核对的问题逐行写进一个 ValueError，`load` 把它们拆回逐条。"""
    if problems:
        raise ValueError("\n".join(problems))
