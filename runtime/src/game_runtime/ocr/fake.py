"""按脚本回答的识别器，只用于自动测试：不需要模型文件，让依赖 `ocr@1` 的插件（如画面签名的
固定文字项）在没有模型的环境里也能测。生产代码不使用它。
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from game_runtime.adapter import PixelRect
from game_runtime.ocr.engine import Image, RecognizedLine


def scripted_line(
    text: str, box: Sequence[int] = (0, 0, 10, 10), confidence: float = 0.9
) -> RecognizedLine:
    """一行识别结果；`box` 是裁出的图像里的坐标。"""
    return RecognizedLine(text, PixelRect(*box), confidence)


@dataclass
class ScriptedRecognizer:
    """记下收到的每张图像，按顺序返回 `replies` 的下一项；用完后返回 `answer(image)`，缺省为空。"""

    replies: list[list[RecognizedLine]] = field(default_factory=list[list[RecognizedLine]])
    answer: Callable[[Image], list[RecognizedLine]] | None = None
    images: list[Image] = field(default_factory=list[Image])

    def recognize(self, image: Image) -> list[RecognizedLine]:
        self.images.append(image)
        if self.replies:
            return self.replies.pop(0)
        return [] if self.answer is None else self.answer(image)
