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
    """ROI 内的 OCR 固定文字：各行依次连接、去掉空白后包含 `text` 即相符。"""

    roi: Ident
    text: Text


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
    """语言核对用的固定文字；为空即尚未标定。"""


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


class Risk(_Model):
    """风险。按画面类型的风险等级与危险画面签名 unsupported（G2）。"""

    forbidden_words: tuple[Text, ...] = ()
    """本游戏的禁用词，与通用词表一起生效。"""
    forbidden_zones: tuple[Ident, ...] = ()
    """静态禁区：ROI 名。"""
    preconditions: tuple[Precondition, ...] = ()
    """操作员前置条件。"""

    @model_validator(mode="after")
    def _unique(self) -> Self:
        _raise(
            _duplicates("forbidden_words", self.forbidden_words)
            + _duplicates("forbidden_zones", self.forbidden_zones)
            + _duplicates("preconditions", (p.id for p in self.preconditions))
        )
        return self


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
    rois: Mapping[Ident, Roi] = Field(default_factory=dict[str, Roi])
    pacing: Pacing | None = None
    """没有即尚未设定。"""
    signatures: tuple[Signature, ...] = ()
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

    def _screen_refs(self) -> Iterator[tuple[str, str]]:
        for index, item in enumerate(self.language.fixed_texts):
            yield f"language.fixed_texts[{index}].screen", item.screen
        for index, signature in enumerate(self.signatures):
            yield f"signatures[{index}].screen", signature.screen

    def _roi_refs(self) -> Iterator[tuple[str, str]]:
        for index, name in enumerate(self.whitelist.click_regions):
            yield f"whitelist.click_regions[{index}]", name
        for index, name in enumerate(self.risk.forbidden_zones):
            yield f"risk.forbidden_zones[{index}]", name
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

    def roi(self, name: str) -> NormalizedRect:
        """ROI 的归一化矩形。没有声明即 LookupError，尚未标定即 NotCalibratedError。"""
        roi = self.rois.get(name)
        if roi is None:
            raise LookupError(f"适配包 {self.id} 没有声明 ROI {name}")
        if roi.rect is None:
            raise NotCalibratedError(self.id, f"ROI {name}（{roi.title}）")
        return roi.rect

    def require_pacing(self) -> Pacing:
        """阅读节奏；没有写即 NotCalibratedError。"""
        if self.pacing is None:
            raise NotCalibratedError(self.id, "阅读节奏")
        return self.pacing

    def language_check(self) -> tuple[FixedText, ...]:
        """语言核对的固定文字；为空即 NotCalibratedError，核对不能通过。"""
        if not self.language.fixed_texts:
            raise NotCalibratedError(self.id, "语言核对的固定文字")
        return self.language.fixed_texts

    def uncalibrated(self) -> tuple[str, ...]:
        """尚未标定的项，给人读。"""
        items = [
            f"ROI {name}（{roi.title}）" for name, roi in self.rois.items() if roi.rect is None
        ]
        items += [f"签名 {s.id}" for s in self.signatures if not s.calibrated]
        if not self.language.fixed_texts:
            items.append("语言核对的固定文字")
        if self.pacing is None:
            items.append("阅读节奏")
        return tuple(items)


def _duplicates(field: str, values: Iterable[object]) -> list[str]:
    counts = Counter(values)
    return [f"{field}：{value} 重复" for value, count in counts.items() if count > 1]


def _raise(problems: list[str]) -> None:
    """交叉核对的问题逐行写进一个 ValueError，`load` 把它们拆回逐条。"""
    if problems:
        raise ValueError("\n".join(problems))
