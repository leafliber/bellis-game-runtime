"""定位与主进程的风险门禁。最终输入白名单、静态禁区和前台由实时核执行。"""

import asyncio
import re
import unicodedata
from dataclasses import dataclass
from typing import Final

from core_protocol import models
from game_runtime.adapter import Adapter, NotCalibratedError, PixelRect, ScreenType
from game_runtime.adapter.schema import ScreenRisk
from game_runtime.ocr import OcrLine, OcrResult, OcrService
from game_runtime.scene import SceneService, SceneUpdate, ScreenSource
from game_runtime.step_control.contracts import (
    Category,
    Click,
    EffectKind,
    Press,
    Scroll,
    StepProposal,
    TextTarget,
)
from game_runtime.step_control.phrase_location import (
    PhraseIdentity,
    PhraseLocationError,
    find_phrase,
    verify_phrase,
)

CHOICES: Final = frozenset({ScreenType.CHOICE, ScreenType.TIMED_CHOICE})
RETURN_ONLY: Final = frozenset({ScreenType.SETTINGS, ScreenType.SAVE_LOAD, ScreenType.SHOP})
STOPPED: Final = frozenset(
    {ScreenType.BAD_ENDING, ScreenType.CUTSCENE, ScreenType.LOADING, ScreenType.STUCK}
)
_FORBIDDEN: Final = (
    "充值",
    "购买",
    "付款",
    "支付",
    "删除",
    "举报",
    "发帖",
    "发送聊天",
    "添加好友",
    "联机",
    "安全设置",
    "purchase",
    "checkout",
    "delete",
    "reportplayer",
)
_IRREVERSIBLE: Final = ("出售", "销毁", "覆盖存档", "确认消耗", "overwrite", "discard", "sell")
_PRICE = re.compile(r"[¥$€£]|[0-9](?:元|人民币|usd|cny)")


def text_key(text: str) -> str:
    return "".join(unicodedata.normalize("NFKC", text).casefold().split())


class ActionRefusedError(ValueError):
    """没有足够的证据或规则允许这一次输入。"""


def forbidden_text(adapter: Adapter, text: str) -> bool:
    key = text_key(text)
    return _PRICE.search(key) is not None or any(
        text_key(word) in key for word in (*_FORBIDDEN, *adapter.risk.forbidden_words)
    )


def _inside(outer: PixelRect, box: PixelRect) -> bool:
    return (
        outer.left <= box.left < box.right <= outer.right
        and outer.top <= box.top < box.bottom <= outer.bottom
    )


def _near(point: tuple[int, int], box: PixelRect, radius: int) -> bool:
    x, y = point
    return (
        box.left - radius <= x < box.right + radius and box.top - radius <= y < box.bottom + radius
    )


@dataclass(frozen=True, slots=True)
class Located:
    point: tuple[int, int] | None = None
    line: OcrLine | None = None
    reads: tuple[OcrResult, ...] = ()
    phrase_identity: PhraseIdentity | None = None


def _target_rect(update: SceneUpdate, point: tuple[int, int], radius: int) -> PixelRect:
    x, y = point
    w, h = update.frame.width, update.frame.height
    if type(x) is not int or type(y) is not int or not (0 <= x < w and 0 <= y < h):
        raise ActionRefusedError("point-outside")
    return PixelRect(
        max(x - radius, 0), max(y - radius, 0), min(x + radius + 1, w), min(y + radius + 1, h)
    )


def verify_ocr(update: SceneUpdate, result: OcrResult) -> None:
    frame = update.frame
    if (
        result.frame_number != frame.frame_number
        or result.transform_version != frame.transform_version
        or not _inside(PixelRect(0, 0, frame.width, frame.height), result.rect)
    ):
        raise ActionRefusedError("stale-ocr")


async def _locate_phrase(
    scene: SceneService,
    ocr: OcrService,
    update: SceneUpdate,
    target: TextTarget,
    timeout: float | None,
) -> Located:
    if target.roi is None or target.text is None or target.phrase is None:
        raise ActionRefusedError("phrase-evidence-missing")
    if timeout is None or timeout <= 0:
        raise ActionRefusedError("phrase-ocr-timeout")
    frame = update.frame
    try:
        rect = ocr.adapter.roi(target.roi).to_pixels(frame.width, frame.height)
    except (LookupError, NotCalibratedError) as error:
        raise ActionRefusedError("phrase-roi-unavailable") from error
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    try:
        async with asyncio.timeout_at(deadline):
            source = await ocr.recognize(frame, target.roi)
        verify_ocr(update, source)
        if source.roi != target.roi or source.rect != rect:
            raise ActionRefusedError("stale-ocr")
        if scene.with_ocr(update, (source,)) is None:
            raise ActionRefusedError("scene-changed")
        candidate = find_phrase(source, target.phrase, source_text=target.text)
        verified = await verify_phrase(
            frame, candidate, ocr.recognize_rect, timeout=deadline - loop.time()
        )
    except TimeoutError:
        raise ActionRefusedError("phrase-ocr-timeout") from None
    except PhraseLocationError as error:
        raise ActionRefusedError(str(error)) from error
    reads = (source, verified.verification)
    current = scene.with_ocr(update, reads)
    if current is None:
        raise ActionRefusedError("scene-changed")
    if candidate.source_line not in current.screen_state.text_targets:
        raise ActionRefusedError("stale-ocr")
    return Located(verified.point, candidate.source_line, reads, candidate.identity)


async def locate(
    scene: SceneService,
    ocr: OcrService,
    update: SceneUpdate,
    target: TextTarget,
    *,
    radius: int,
    phrase_timeout: float | None = None,
) -> Located:
    if target.phrase is not None:
        return await _locate_phrase(scene, ocr, update, target, phrase_timeout)
    rect = None if target.point is None else _target_rect(update, target.point, radius)
    if target.roi is not None:
        result = await ocr.recognize(update.frame, target.roi)
    elif rect is not None:
        result = await ocr.recognize_rect(update.frame, rect)
    else:
        raise ActionRefusedError("target-region-missing")
    verify_ocr(update, result)
    current = scene.with_ocr(update, (result,))
    if current is None:
        raise ActionRefusedError("scene-changed")
    text = None if target.text is None else text_key(target.text)
    lines = current.screen_state.text_targets
    matches = tuple(
        line
        for line in lines
        if (text is None or text == text_key(line.text))
        and (target.point is None or _near(target.point, line.box, radius))
    )
    if len(matches) == 1:
        (line,) = matches
        box = line.box
        return Located(((box.left + box.right) // 2, (box.top + box.bottom) // 2), line, (result,))
    if len(matches) > 1:
        raise ActionRefusedError("ambiguous-target")
    # 有文字目标时，识别失败或目标文字不符均不可回退到坐标。
    if (
        target.point is not None
        and text is None
        and not result.lines
        and (target.roi is None or (rect is not None and _inside(rect, result.rect)))
    ):
        return Located(target.point, None, (result,))
    raise ActionRefusedError("target-not-found")


def requires_choice(update: SceneUpdate) -> bool:
    return bool(CHOICES & update.screen_state.restrictions)


def _safe_return(
    adapter: Adapter, update: SceneUpdate, proposal: StepProposal, found: Located
) -> bool:
    screen = update.state.screen
    target = None if screen is None else adapter.risk.returns.get(screen)
    classification = update.screen_state.classification
    if (
        target is None
        or classification.source is not ScreenSource.SIGNATURE
        or proposal.category is not Category.NAVIGATE
        or not isinstance(proposal.action, Click)
        or proposal.action.target.phrase is not None
        or proposal.action.button is not models.MouseButton.LEFT
        or found.line is None
        or text_key(found.line.text) != text_key(target.text)
    ):
        return False
    if update.screen_state.restrictions - {classification.screen_type}:
        return False
    rect = adapter.roi(target.roi).to_pixels(update.frame.width, update.frame.height)
    return _inside(rect, found.line.box)


def _built_in_restart(
    adapter: Adapter, update: SceneUpdate, proposal: StepProposal, found: Located
) -> bool:
    screen = update.state.screen
    target = None if screen is None else adapter.restarts.get(screen)
    if (
        target is None
        or update.screen_state.classification.source is not ScreenSource.SIGNATURE
        or update.screen_state.classification.screen_type is not ScreenType.BAD_ENDING
        or update.screen_state.restrictions - {ScreenType.BAD_ENDING}
        or proposal.category is not Category.NAVIGATE
        or proposal.action != Click(TextTarget(text=target.text, roi=target.roi))
        or proposal.expected.kind is not EffectKind.SIGNATURE
        or proposal.expected.signature != target.success_signature
        or found.line is None
        or text_key(found.line.text) != text_key(target.text)
    ):
        return False
    rect = adapter.roi(target.roi).to_pixels(update.frame.width, update.frame.height)
    box = found.line.box
    return _inside(rect, box) and found.point == (
        (box.left + box.right) // 2,
        (box.top + box.bottom) // 2,
    )


def assess(
    adapter: Adapter,
    update: SceneUpdate,
    proposal: StepProposal,
    found: Located,
    *,
    authorized_decision: bool = False,
    authorized_restart: bool = False,
) -> bool:
    """返回是否仍需打开决策点。异常表示拒绝。"""
    status = update.screen_state
    classification = status.classification
    restrictions = status.restrictions | {classification.screen_type}
    rule = None if update.state.screen is None else adapter.risk.screens.get(update.state.screen)
    if update.state.playing or rule is ScreenRisk.BLOCKED:
        raise ActionRefusedError("screen-blocked")
    if restrictions & STOPPED and (
        not authorized_restart or not _built_in_restart(adapter, update, proposal, found)
    ):
        raise ActionRefusedError("screen-blocked")
    if (restrictions & RETURN_ONLY or rule is ScreenRisk.RETURN_ONLY) and not _safe_return(
        adapter, update, proposal, found
    ):
        raise ActionRefusedError("only-safe-return")
    if ScreenType.UNKNOWN in restrictions and proposal.category is not Category.NAVIGATE:
        raise ActionRefusedError("unknown-screen")

    action = proposal.action
    if isinstance(action, Click) and action.target.phrase is not None:
        decision = adapter.decisions.get(update.state.screen or "")
        if ScreenType.TIMED_CHOICE in restrictions or (decision is not None and decision.timer):
            raise ActionRefusedError("timed-phrase-unsupported")
    if isinstance(action, Click) and action.button not in adapter.whitelist.click_buttons:
        raise ActionRefusedError("button-not-allowed")
    if isinstance(action, Press) and action.scan_code not in adapter.whitelist.press_keys:
        raise ActionRefusedError("key-not-allowed")
    if isinstance(action, Scroll) and (not adapter.whitelist.scroll or not action.notches):
        raise ActionRefusedError("scroll-not-allowed")
    if found.point is not None:
        x, y = found.point
        if not any(
            _inside(
                adapter.roi(name).to_pixels(update.frame.width, update.frame.height),
                PixelRect(x, y, x + 1, y + 1),
            )
            for name in adapter.whitelist.click_regions
        ):
            raise ActionRefusedError("outside-click-regions")

    if found.line is not None and forbidden_text(adapter, found.line.text):
        raise ActionRefusedError("target-text-forbidden")
    if (
        found.point is not None
        and found.line is None
        and (
            not found.reads
            or classification.source is not ScreenSource.SIGNATURE
            or restrictions - {classification.screen_type}
            or restrictions & (CHOICES | RETURN_ONLY | STOPPED | {ScreenType.UNKNOWN})
            or rule is not None
            or proposal.category is Category.SELECT
        )
    ):
        raise ActionRefusedError("unverified-coordinate")

    choice = (
        bool(restrictions & CHOICES)
        or rule is ScreenRisk.DECISION_ONLY
        or proposal.category is Category.SELECT
        or (
            found.line is not None
            and any(word in text_key(found.line.text) for word in _IRREVERSIBLE)
        )
    )
    if choice and (not isinstance(action, Click) or found.line is None):
        raise ActionRefusedError("selection-needs-visible-option")
    if choice and authorized_decision and ScreenType.UNKNOWN in restrictions:
        raise ActionRefusedError("unresolved-selection-screen")
    return choice and not authorized_decision


def core_action(proposal: StepProposal, found: Located, action_id: int) -> models.Action:
    match proposal.action:
        case Press(scan_code=code):
            return models.Action(root=models.KeyPress(id=action_id, scan_code=code))
        case Click(button=button) if found.point is not None:
            x, y = found.point
            return models.Action(root=models.Click(id=action_id, x=x, y=y, button=button))
        case Scroll(notches=notches) if found.point is not None:
            x, y = found.point
            return models.Action(root=models.Scroll(id=action_id, x=x, y=y, notches=notches))
        case _:
            raise ActionRefusedError("missing-coordinate")
