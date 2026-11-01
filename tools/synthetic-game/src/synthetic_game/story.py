"""合成游戏的全部台词、界面文字与剧情图。

台词是为本夹具原创的简短中文，不取自任何游戏。画面上出现的每一段文字都必须在这里定义，
字体子集按 `all_text()` 生成；改了这里的文字而没有重新生成子集，字体测试就失败。
"""

import dataclasses
from collections.abc import Iterator
from dataclasses import dataclass

TITLE = "title"
"""剧情图里表示“回到标题菜单”的去向。"""

START = "intro"
"""开始游戏与坏结局重来时进入的节点。"""


@dataclass(frozen=True)
class Line:
    speaker: str
    text: str


@dataclass(frozen=True)
class Dialogue:
    """逐字显示的若干句台词；读完后先弹出 `popup_after`（如有），再进入 `next`。"""

    lines: tuple[Line, ...]
    next: str
    popup_after: str | None = None
    reward: int = 0


@dataclass(frozen=True)
class Option:
    label: str
    next: str


@dataclass(frozen=True)
class Choice:
    """选项；`timeout_next` 不为空时是计时选择，到点即进入它。"""

    prompt: str
    options: tuple[Option, ...]
    timeout_next: str | None = None


@dataclass(frozen=True)
class Ending:
    """坏结局；重来回到 `START`。"""

    text: str


@dataclass(frozen=True)
class Cutscene:
    """播片：整屏的动画放 `GameConfig.cutscene_seconds` 秒，其间在对白文字的位置上依次显示
    `subtitles`；输入都忽略，放完进入 `next`。"""

    subtitles: tuple[str, ...]
    next: str


type Node = Dialogue | Choice | Ending | Cutscene

NODES: dict[str, Node] = {
    "intro": Dialogue(
        lines=(
            Line("旁白", "雨停了，钟楼的指针停在七点。"),
            Line("阿遥", "你听见了吗？地下传来敲门声。"),
            Line("阿遥", "门后也许有路，也许什么都没有。"),
        ),
        next="door",
    ),
    "door": Choice(
        prompt="要打开地下室的门吗？",
        options=(
            Option("推门进去", "stairs"),
            Option("敲门回应", "knock"),
            Option("回去睡觉", "sleep"),
        ),
    ),
    "knock": Dialogue(
        lines=(Line("旁白", "门那边安静了一会儿，又敲了三下。"),),
        next="echo",
    ),
    "echo": Cutscene(
        subtitles=("雨夜里，钟声传得很远。", "门后的石阶上，又响起了敲门声。"),
        next="door",
    ),
    "stairs": Dialogue(
        lines=(
            Line("旁白", "门后是向下的石阶，空气里有烟味。"),
            Line("阿遥", "尽头有两扇门，左边那扇在发烫。"),
        ),
        next="fire",
        popup_after="获得：旧提灯",
    ),
    "fire": Choice(
        prompt="火光逼近，选一扇门！",
        options=(
            Option("左边的门", "burn"),
            Option("右边的门", "roof"),
        ),
        timeout_next="late",
    ),
    "roof": Dialogue(
        lines=(
            Line("旁白", "右边的门通向钟楼顶上，风很凉。"),
            Line("阿遥", "原来敲门的是风。我们回去吧。"),
        ),
        next=TITLE,
        reward=30,
    ),
    "sleep": Ending("你睡着了，敲门声再也没有响起。"),
    "burn": Ending("门把烫得握不住，烟雾吞没了石阶。"),
    "late": Ending("你犹豫太久，火光封住了去路。"),
}


@dataclass(frozen=True)
class ShopItem:
    id: str
    name: str
    price: int


SHOP_ITEMS: tuple[ShopItem, ...] = (
    ShopItem("matches", "火柴", 5),
    ShopItem("bread", "面包", 8),
    ShopItem("rope", "绳子", 10),
    ShopItem("umbrella", "雨伞", 15),
    ShopItem("map", "旧地图", 25),
    ShopItem("key", "铜钥匙", 30),
    ShopItem("watch", "怀表", 45),
    ShopItem("telescope", "望远镜", 80),
)

STARTING_COINS = 50


@dataclass(frozen=True)
class Ui:
    """界面文字。带 `{}` 的是格式模板，只代入 ASCII 数字。"""

    game_title: str = "雨夜钟楼"
    menu_start: str = "开始"
    menu_shop: str = "商店"
    menu_help: str = "说明"
    help_text: str = "点击或按回车继续，方向键选择。"
    shop_title: str = "商店"
    shop_leave: str = "离开"
    coins: str = "金币：{coins}"
    price: str = "{price} 金币"
    owned: str = "已拥有"
    not_enough_coins: str = "金币不足。"
    popup_ok: str = "确定"
    remaining: str = "剩余 {seconds} 秒"
    bad_ending: str = "坏结局"
    retry: str = "重来"


UI = Ui()


def all_text() -> Iterator[str]:
    """画面上可能出现的全部文字（格式模板按原样给出）。"""
    for field in dataclasses.fields(UI):
        yield str(getattr(UI, field.name))
    for node in NODES.values():
        match node:
            case Dialogue(lines=lines, popup_after=popup_after):
                for line in lines:
                    yield line.speaker
                    yield line.text
                if popup_after is not None:
                    yield popup_after
            case Choice(prompt=prompt, options=options):
                yield prompt
                for option in options:
                    yield option.label
            case Ending(text=text):
                yield text
            case Cutscene(subtitles=subtitles):
                yield from subtitles
    for item in SHOP_ITEMS:
        yield item.name
