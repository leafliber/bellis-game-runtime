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
class PopupView:
    text: str
    ok: Button


type Screen = MenuView | DialogueView | ChoiceView | ShopView | EndingView


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
        draw.rectangle(_box(layout.rect(560, 60, 720, 420)), fill=PANEL)
        draw.ellipse(_box(layout.rect(590, 100, 690, 200)), fill=WHITE)
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

    def _popup(self, draw: ImageDraw.ImageDraw, view: PopupView) -> None:
        layout = self._layout
        panel = layout.popup_panel()
        radius = layout.font_size(14)
        draw.rounded_rectangle(_box(panel), radius=radius, fill=PANEL, outline=WHITE)
        self._wrapped(draw, layout.popup_text(), view.text, 30, center=True)
        self._button(draw, view.ok)


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
