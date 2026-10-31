"""渲染合成游戏的 GS-04 样本：真值已知，用来验证评测工具本身。

每句台词（显示完的与逐字显示到一半的）、每个名字、每个选项提问与选项、每个坏结局的文字与标题菜单的
三个按钮各一个样本；ROI 取合成游戏的适配包。画面用合成游戏的渲染器按画面快照直接画出，不经状态机。
"""

from dataclasses import dataclass
from pathlib import Path

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
from synthetic_game.story import NODES, UI, Choice, Dialogue, Ending

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


def write_synthetic_samples(directory: Path, width: int = 1280, height: int = 720) -> Path:
    """把样本写进 `directory`（图像在 `images/` 下），返回 `samples.toml` 的路径。"""
    (directory / IMAGES_DIR).mkdir(parents=True, exist_ok=True)
    renderer = Renderer(width, height)
    samples: list[Sample] = []
    for index, shot in enumerate(_shots(Layout(width, height))):
        image = f"{IMAGES_DIR}/{index + 1:03d}.png"
        write_png(directory / image, renderer.render(shot.view), width, height)
        for category, roi, text in shot.samples:
            samples.append(Sample(image=image, category=category, roi=roi, text=text))
    return write_sample_set(directory, GAME, samples)
