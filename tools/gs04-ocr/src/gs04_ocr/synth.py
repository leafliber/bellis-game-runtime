"""渲染 GS-04 的合成样本，真值已知，写进同一个 `samples.toml`：

- 合成游戏：每句台词（显示完的与逐字显示到一半的）、每个名字、每个选项提问与选项、每个坏结局的文字与
  标题菜单的三个按钮各一个样本；ROI 取合成游戏的适配包。画面用合成游戏的渲染器按画面快照直接画出，
  不经状态机。用来验证评测工具本身。
- 泛化：原创的对白按多种字体、字号、底色与字形渲染（`gs04_ocr.general`），按矩形识别，带标签。
"""

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from gs04_ocr import general
from gs04_ocr.general import FontChoice
from gs04_ocr.samples import Sample, write_png, write_sample_set
from synthetic_game.layout import Layout
from synthetic_game.render import (
    Button,
    ChoiceView,
    DialogueView,
    EndingView,
    MenuView,
    Renderer,
    View,
)
from synthetic_game.story import NODES, UI, Choice, Cutscene, Dialogue, Ending

GAME = "synthetic-game"
IMAGES_DIR = "images"


@dataclass(frozen=True)
class _Shot:
    view: View
    samples: tuple[tuple[str, str, str], ...]
    """（类别、ROI、真值）。"""


def _shots(layout: Layout) -> list[_Shot]:
    shots: list[_Shot] = []
    for node_id, node in NODES.items():
        match node:
            case Dialogue(lines=lines):
                for line in lines:
                    finished = View(DialogueView(node_id, line.speaker, line.text, True), None)
                    shots.append(
                        _Shot(
                            finished,
                            (
                                ("dialogue", "dialogue", line.text),
                                ("name", "speaker", line.speaker),
                            ),
                        )
                    )
                    half = line.text[: len(line.text) // 2]
                    partial = View(DialogueView(node_id, line.speaker, half, False), None)
                    shots.append(_Shot(partial, (("dialogue-partial", "dialogue", half),)))
            case Choice(prompt=prompt, options=options, timeout_next=timeout_next):
                buttons = tuple(
                    Button(layout.choice_option(index), option.label, index == 0)
                    for index, option in enumerate(options)
                )
                timed = timeout_next is not None
                timer_text = UI.remaining.format(seconds="3.5") if timed else None
                view = View(ChoiceView(prompt, buttons, timer_text, 0.7 if timed else None), None)
                labels = "\n".join(option.label for option in options)
                shots.append(
                    _Shot(
                        view, (("prompt", "choice-prompt", prompt), ("choice", "choices", labels))
                    )
                )
            case Ending(text=text):
                retry = Button(layout.ending_retry(), UI.retry, True)
                view = View(EndingView(UI.bad_ending, text, retry), None)
                shots.append(_Shot(view, (("ending", "ending-text", text),)))
            case Cutscene():
                pass  # 播片的字幕叠在每毫秒都变的动画上，不作样本
    labels = (UI.menu_start, UI.menu_shop, UI.menu_help)
    menu = View(
        MenuView(
            UI.game_title,
            tuple(
                Button(layout.menu_button(index), label, index == 0)
                for index, label in enumerate(labels)
            ),
        ),
        None,
    )
    rois = ("menu-start", "menu-shop", "menu-help")
    shots.append(
        _Shot(menu, tuple(("menu", roi, label) for roi, label in zip(rois, labels, strict=True)))
    )
    return shots


def _general_samples(
    directory: Path, width: int, height: int, fonts: Sequence[FontChoice]
) -> list[Sample]:
    (directory / general.IMAGES_DIR).mkdir(parents=True, exist_ok=True)
    samples: list[Sample] = []
    for variant in general.variants(fonts):
        rendered = general.render(variant, width, height)
        suffix = "-partial" if variant.partial else ""
        image = f"{general.IMAGES_DIR}/{variant.font.tag}-{variant.seed + 1:02d}{suffix}.png"
        write_png(directory / image, rendered.bgra, width, height)
        rect = rendered.rect
        samples.append(
            Sample(
                image=image,
                category=general.PARTIAL_CATEGORY if variant.partial else general.CATEGORY,
                rect=(rect.left, rect.top, rect.right, rect.bottom),
                text=rendered.truth,
                tags={
                    "font": variant.font.tag,
                    "size": str(rendered.size),
                    "background": variant.background,
                    "style": variant.style,
                },
            )
        )
    return samples


def write_synthetic_samples(
    directory: Path,
    width: int = 1280,
    height: int = 720,
    *,
    fonts: Sequence[FontChoice] | None = None,
) -> Path:
    """把样本写进 `directory`（合成游戏的图像在 `images/` 下，泛化的在 `general/` 下），返回
    `samples.toml` 的路径。`fonts` 为泛化样本所用的字体，缺省为 `general.available_fonts()`；
    给空序列即不写泛化样本。"""
    (directory / IMAGES_DIR).mkdir(parents=True, exist_ok=True)
    renderer = Renderer(width, height)
    samples: list[Sample] = []
    for index, shot in enumerate(_shots(Layout(width, height))):
        image = f"{IMAGES_DIR}/{index + 1:03d}.png"
        write_png(directory / image, renderer.render(shot.view), width, height)
        for category, roi, text in shot.samples:
            samples.append(Sample(image=image, category=category, roi=roi, text=text))
    chosen = general.available_fonts() if fonts is None else fonts
    samples += _general_samples(directory, width, height, chosen)
    return write_sample_set(directory, GAME, samples)
