"""画面几何：状态机的点击判定与渲染共用同一组矩形，保证画出来的按钮就是能点中的区域。

设计稿按 1280×720 给出，按配置的分辨率在横纵两个方向分别缩放；字号按较小的缩放比缩放。
坐标是客户区的物理像素。
"""

from dataclasses import dataclass

BASE_WIDTH = 1280
BASE_HEIGHT = 720

SHOP_VISIBLE_ROWS = 4


@dataclass(frozen=True)
class Rect:
    """半开矩形：包含左边与上边，不含右边与下边。"""

    left: int
    top: int
    right: int
    bottom: int

    def contains(self, x: int, y: int) -> bool:
        return self.left <= x < self.right and self.top <= y < self.bottom

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top

    @property
    def center(self) -> tuple[int, int]:
        return (self.left + self.right) // 2, (self.top + self.bottom) // 2


class Layout:
    """按分辨率缩放后的各画面矩形。"""

    def __init__(self, width: int, height: int) -> None:
        self.width = width
        self.height = height
        self._sx = width / BASE_WIDTH
        self._sy = height / BASE_HEIGHT

    def rect(self, left: float, top: float, right: float, bottom: float) -> Rect:
        """把设计稿坐标换算为本分辨率的矩形。"""
        return Rect(
            round(left * self._sx),
            round(top * self._sy),
            round(right * self._sx),
            round(bottom * self._sy),
        )

    def point(self, x: float, y: float) -> tuple[int, int]:
        return round(x * self._sx), round(y * self._sy)

    def font_size(self, base: float) -> int:
        return max(8, round(base * min(self._sx, self._sy)))

    @property
    def full(self) -> Rect:
        return Rect(0, 0, self.width, self.height)

    # 标题菜单
    def menu_title(self) -> tuple[int, int]:
        return self.point(640, 160)

    def menu_button(self, index: int) -> Rect:
        top = 300 + index * 88
        return self.rect(460, top, 820, top + 64)

    # 对话
    def dialogue_box(self) -> Rect:
        return self.rect(40, 470, 1240, 690)

    def speaker_box(self) -> Rect:
        return self.rect(60, 425, 280, 475)

    def dialogue_text(self) -> Rect:
        return self.rect(80, 500, 1200, 670)

    def continue_marker(self) -> Rect:
        return self.rect(1190, 650, 1214, 672)

    # 选项与计时选择
    def choice_prompt(self) -> Rect:
        return self.rect(140, 70, 1140, 170)

    def choice_option(self, index: int) -> Rect:
        top = 230 + index * 96
        return self.rect(360, top, 920, top + 72)

    def timer_bar(self) -> Rect:
        return self.rect(240, 590, 1040, 614)

    def timer_text(self) -> tuple[int, int]:
        return self.point(640, 650)

    # 商店
    def shop_title(self) -> tuple[int, int]:
        return self.point(80, 80)

    def shop_coins(self) -> tuple[int, int]:
        return self.point(1200, 80)

    def shop_list(self) -> Rect:
        return self.rect(80, 130, 1180, 590)

    def shop_row(self, slot: int) -> Rect:
        top = 130 + slot * 115
        return self.rect(80, top, 1180, top + 100)

    def shop_scrollbar(self) -> Rect:
        return self.rect(1195, 130, 1215, 590)

    def shop_leave(self) -> Rect:
        return self.rect(1000, 615, 1200, 685)

    # 弹窗
    def popup_panel(self) -> Rect:
        return self.rect(340, 220, 940, 500)

    def popup_text(self) -> Rect:
        return self.rect(380, 250, 900, 390)

    def popup_ok(self) -> Rect:
        return self.rect(540, 410, 740, 470)

    # 坏结局
    def ending_title(self) -> tuple[int, int]:
        return self.point(640, 200)

    def ending_text(self) -> Rect:
        return self.rect(200, 290, 1080, 430)

    def ending_retry(self) -> Rect:
        return self.rect(540, 480, 740, 550)
