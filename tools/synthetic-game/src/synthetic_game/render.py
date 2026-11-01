"""用 Pillow 把画面渲染到 BGRA8 像素缓冲，格式与帧环一致：行跨度为宽乘 4，alpha 恒为 255。

渲染只读 `View`；`View` 由状态机生成，是画面上全部可见内容的不可变快照，内容相同的两帧像素相同。
"""

from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from synthetic_game.layout import SHOP_VISIBLE_ROWS, Layout, Rect

PIXEL_FORMAT = "BGRA8"
BYTES_PER_PIXEL = 4

FONT_DIR = Path(__file__).resolve().parent / "fonts"
FONT_FILE = FONT_DIR / "SyntheticGameSansSC-Regular.otf"


@dataclass(frozen=True)
class Frame:
    """一帧：`data` 是 BGRA8 像素，自上而下逐行排列，行跨度为 `stride`。"""

    data: bytes
    width: int
    height: int
    number: int

    @property
    def stride(self) -> int:
        return self.width * BYTES_PER_PIXEL


@dataclass(frozen=True)
class Button:
    rect: Rect
    label: str
    selected: bool


@dataclass(frozen=True)
class MenuView:
    title: str
    buttons: tuple[Button, ...]


@dataclass(frozen=True)
class DialogueView:
    node: str
    speaker: str
    text: str
    finished: bool


@dataclass(frozen=True)
class ChoiceView:
    prompt: str
    buttons: tuple[Button, ...]
    timer_text: str | None
    timer_fraction: float | None


@dataclass(frozen=True)
class ShopRow:
    rect: Rect
    name: str
    price: str
    owned: bool
    selected: bool


@dataclass(frozen=True)
class ShopView:
    title: str
    coins: str
    rows: tuple[ShopRow, ...]
    leave: Button
    offset: int
    total: int


@dataclass(frozen=True)
class EndingView:
    title: str
    text: str
    retry: Button


@dataclass(frozen=True)
class CutsceneView:
    elapsed_ms: int
    """播片开始以来的毫秒数：动画的每一格都随它变。"""
    subtitle: str
    """当前的字幕；没有时为空。"""


@dataclass(frozen=True)
class PopupView:
    text: str
    ok: Button


type Screen = MenuView | DialogueView | ChoiceView | ShopView | EndingView | CutsceneView


@dataclass(frozen=True)
class View:
    screen: Screen
    popup: PopupView | None


type Color = tuple[int, int, int]

WHITE: Color = (245, 245, 240)
INK: Color = (30, 30, 36)
MUTED: Color = (150, 150, 160)
BUTTON: Color = (58, 64, 92)
BUTTON_SELECTED: Color = (214, 168, 72)
PANEL: Color = (24, 26, 38)

MENU_BACKGROUND: Color = (26, 36, 64)
CHOICE_BACKGROUND: Color = (34, 40, 56)
SHOP_BACKGROUND: Color = (40, 56, 48)
ENDING_BACKGROUND: Color = (72, 14, 18)
TIMER_FILL: Color = (220, 90, 60)
TIMER_TRACK: Color = (70, 70, 80)

# 各对话节点的背景色；未列出的节点用缺省色。不用 hash()，它对字符串是随机化的。
DIALOGUE_BACKGROUNDS: dict[str, Color] = {
    "intro": (46, 58, 86),
    "knock": (52, 48, 70),
    "stairs": (66, 52, 40),
    "roof": (70, 96, 120),
}
DIALOGUE_BACKGROUND: Color = (50, 50, 60)

# 播片：色块与图形的各通道都在这个范围里，避开各画面签名所看的深色与白色，播片中不命中任何签名。
CUTSCENE_LOW, CUTSCENE_HIGH = 110, 230
CUTSCENE_COLUMNS, CUTSCENE_ROWS = 16, 9
"""整屏的色块：设计稿上每块 80×80。"""
SUBTITLE_BAND_ALPHA = 200
"""字幕底下半透明的深色条（盖住名字栏与对白框）的不透明度。"""
_MASK = (1 << 64) - 1


class Renderer:
    def __init__(self, width: int, height: int) -> None:
        self._layout = Layout(width, height)
        self._fonts: dict[int, ImageFont.FreeTypeFont] = {}

    def render(self, view: View) -> bytes:
        """把一个画面渲染成 BGRA8 像素。"""
        image = Image.new("RGBA", (self._layout.width, self._layout.height), (0, 0, 0, 255))
        draw = ImageDraw.Draw(image)
        match view.screen:
            case MenuView():
                self._menu(draw, view.screen)
            case DialogueView():
                self._dialogue(draw, view.screen)
            case ChoiceView():
                self._choice(draw, view.screen)
            case ShopView():
                self._shop(draw, view.screen)
            case EndingView():
                self._ending(draw, view.screen)
            case CutsceneView():
                self._cutscene(ImageDraw.Draw(image, "RGBA"), view.screen)
        if view.popup is not None:
            shade = Image.new("RGBA", image.size, (0, 0, 0, 150))
            image = Image.alpha_composite(image, shade)
            self._popup(ImageDraw.Draw(image), view.popup)
        return image.tobytes("raw", "BGRA")

    def font(self, base_size: float) -> ImageFont.FreeTypeFont:
        size = self._layout.font_size(base_size)
        font = self._fonts.get(size)
        if font is None:
            # 固定用基本排版引擎，结果不随系统是否装有 raqm 而变。
            font = ImageFont.truetype(FONT_FILE, size, layout_engine=ImageFont.Layout.BASIC)
            self._fonts[size] = font
        return font

    def _background(self, draw: ImageDraw.ImageDraw, color: Color) -> None:
        draw.rectangle(_box(self._layout.full), fill=color)

    def _button(self, draw: ImageDraw.ImageDraw, button: Button, size: float = 30) -> None:
        fill = BUTTON_SELECTED if button.selected else BUTTON
        ink = INK if button.selected else WHITE
        radius = self._layout.font_size(10)
        draw.rounded_rectangle(_box(button.rect), radius=radius, fill=fill)
        draw.text(button.rect.center, button.label, font=self.font(size), fill=ink, anchor="mm")

    def _wrapped(
        self, draw: ImageDraw.ImageDraw, rect: Rect, text: str, size: float, *, center: bool
    ) -> None:
        font = self.font(size)
        line_height = round(font.size * 1.5)
        lines = _wrap(text, font, rect.width)
        if center:
            top = rect.center[1] - line_height * len(lines) // 2
            for index, line in enumerate(lines):
                xy = (rect.center[0], top + index * line_height + line_height // 2)
                draw.text(xy, line, font=font, fill=WHITE, anchor="mm")
        else:
            for index, line in enumerate(lines):
                xy = (rect.left, rect.top + index * line_height)
                draw.text(xy, line, font=font, fill=WHITE, anchor="la")

    def _menu(self, draw: ImageDraw.ImageDraw, view: MenuView) -> None:
        self._background(draw, MENU_BACKGROUND)
        draw.text(
            self._layout.menu_title(), view.title, font=self.font(64), fill=WHITE, anchor="mm"
        )
        for button in view.buttons:
            self._button(draw, button)

    def _dialogue(self, draw: ImageDraw.ImageDraw, view: DialogueView) -> None:
        layout = self._layout
        self._background(draw, DIALOGUE_BACKGROUNDS.get(view.node, DIALOGUE_BACKGROUND))
        # 一座简化的钟楼剪影，让不同节点的画面不只是底色不同。
        draw.rectangle(_box(layout.clock_tower()), fill=PANEL)
        draw.ellipse(_box(layout.clock_face()), fill=WHITE)
        draw.rectangle(_box(layout.dialogue_box()), fill=PANEL, outline=MUTED)
        speaker = layout.speaker_box()
        draw.rectangle(_box(speaker), fill=BUTTON)
        draw.text(speaker.center, view.speaker, font=self.font(26), fill=WHITE, anchor="mm")
        self._wrapped(draw, layout.dialogue_text(), view.text, 32, center=False)
        if view.finished:
            marker = layout.continue_marker()
            draw.polygon(
                [
                    (marker.left, marker.top),
                    (marker.right, marker.top),
                    (marker.center[0], marker.bottom),
                ],
                fill=WHITE,
            )

    def _choice(self, draw: ImageDraw.ImageDraw, view: ChoiceView) -> None:
        layout = self._layout
        self._background(draw, CHOICE_BACKGROUND)
        prompt = layout.choice_prompt()
        draw.rectangle(_box(prompt), fill=PANEL)
        self._wrapped(draw, prompt, view.prompt, 36, center=True)
        for button in view.buttons:
            self._button(draw, button)
        if view.timer_fraction is not None and view.timer_text is not None:
            bar = layout.timer_bar()
            draw.rectangle(_box(bar), fill=TIMER_TRACK)
            filled = bar.left + round(bar.width * view.timer_fraction)
            if filled > bar.left:
                draw.rectangle((bar.left, bar.top, filled - 1, bar.bottom - 1), fill=TIMER_FILL)
            draw.text(
                layout.timer_text(), view.timer_text, font=self.font(28), fill=WHITE, anchor="mm"
            )

    def _shop(self, draw: ImageDraw.ImageDraw, view: ShopView) -> None:
        layout = self._layout
        self._background(draw, SHOP_BACKGROUND)
        draw.text(layout.shop_title(), view.title, font=self.font(48), fill=WHITE, anchor="lm")
        draw.text(layout.shop_coins(), view.coins, font=self.font(32), fill=WHITE, anchor="rm")
        font = self.font(32)
        pad = layout.font_size(30)
        for row in view.rows:
            fill = BUTTON_SELECTED if row.selected else BUTTON
            ink = INK if row.selected else WHITE
            draw.rectangle(_box(row.rect), fill=fill)
            middle = row.rect.center[1]
            draw.text((row.rect.left + pad, middle), row.name, font=font, fill=ink, anchor="lm")
            price_ink = MUTED if row.owned and not row.selected else ink
            draw.text(
                (row.rect.right - pad, middle), row.price, font=font, fill=price_ink, anchor="rm"
            )
        track = layout.shop_scrollbar()
        draw.rectangle(_box(track), fill=TIMER_TRACK)
        if view.total > 0:
            unit = track.height / view.total
            visible = min(SHOP_VISIBLE_ROWS, view.total)
            top = track.top + round(view.offset * unit)
            bottom = track.top + round((view.offset + visible) * unit)
            draw.rectangle((track.left, top, track.right - 1, bottom - 1), fill=WHITE)
        self._button(draw, view.leave)

    def _ending(self, draw: ImageDraw.ImageDraw, view: EndingView) -> None:
        layout = self._layout
        self._background(draw, ENDING_BACKGROUND)
        draw.text(layout.ending_title(), view.title, font=self.font(72), fill=WHITE, anchor="mm")
        self._wrapped(draw, layout.ending_text(), view.text, 32, center=True)
        self._button(draw, view.retry)

    def _cutscene(self, draw: ImageDraw.ImageDraw, view: CutsceneView) -> None:
        """整屏的色块每毫秒换一次颜色，两个图形来回移动；字幕在对白文字的位置上，底下是半透明的
        深色条。`draw` 按 RGBA 混合。动画只用整数运算，同样的毫秒数得到同样的像素。"""
        layout = self._layout
        ms = view.elapsed_ms
        for row in range(CUTSCENE_ROWS):
            for column in range(CUTSCENE_COLUMNS):
                block = layout.rect(column * 80, row * 80, (column + 1) * 80, (row + 1) * 80)
                draw.rectangle(_box(block), fill=_shade(column, row, ms))
        x, y, r = 120 + _bounce(ms, 2600, 1040), 90 + _bounce(ms, 1700, 300), 90
        draw.ellipse(_box(layout.rect(x - r, y - r, x + r, y + r)), fill=(228, 196, 120))
        x, y, r = 1160 - _bounce(ms, 3100, 1040), 150 + _bounce(ms, 2300, 250), 80
        draw.polygon(
            [layout.point(x, y - r), layout.point(x + r, y + r), layout.point(x - r, y + r)],
            fill=(120, 196, 228),
        )
        draw.rectangle(_box(layout.subtitle_band()), fill=(*PANEL, SUBTITLE_BAND_ALPHA))
        if view.subtitle:
            self._wrapped(draw, layout.dialogue_text(), view.subtitle, 32, center=True)

    def _popup(self, draw: ImageDraw.ImageDraw, view: PopupView) -> None:
        layout = self._layout
        panel = layout.popup_panel()
        radius = layout.font_size(14)
        draw.rounded_rectangle(_box(panel), radius=radius, fill=PANEL, outline=WHITE)
        self._wrapped(draw, layout.popup_text(), view.text, 30, center=True)
        self._button(draw, view.ok)


def _shade(column: int, row: int, ms: int) -> Color:
    """色块在第 `ms` 毫秒的颜色：按位置与时刻散列，各通道在 `CUTSCENE_LOW`–`CUTSCENE_HIGH`。"""
    value = (
        column * 0x9E3779B97F4A7C15 + row * 0xC2B2AE3D27D4EB4F + ms * 0x165667B19E3779F9
    ) & _MASK
    value ^= value >> 30
    value = (value * 0xBF58476D1CE4E5B9) & _MASK
    value ^= value >> 27
    value = (value * 0x94D049BB133111EB) & _MASK
    value ^= value >> 31
    span = CUTSCENE_HIGH - CUTSCENE_LOW + 1
    return (
        CUTSCENE_LOW + value % span,
        CUTSCENE_LOW + (value >> 21) % span,
        CUTSCENE_LOW + (value >> 42) % span,
    )


def _bounce(ms: int, period: int, span: int) -> int:
    """在 0 与 `span` 之间来回的三角波，周期 `period` 毫秒。"""
    phase = ms % period
    half = period // 2
    return span * phase // half if phase < half else span * (period - phase) // (period - half)


def _box(rect: Rect) -> tuple[int, int, int, int]:
    """Pillow 的矩形包含右下角，半开矩形要减一。"""
    return rect.left, rect.top, rect.right - 1, rect.bottom - 1


def _wrap(text: str, font: ImageFont.FreeTypeFont, width: int) -> list[str]:
    """按字折行：中文没有词间空格，逐字累加到超出宽度为止。"""
    lines: list[str] = []
    current = ""
    for char in text:
        if current and font.getlength(current + char) > width:
            lines.append(current)
            current = char
        else:
            current += char
    if current or not lines:
        lines.append(current)
    return lines
