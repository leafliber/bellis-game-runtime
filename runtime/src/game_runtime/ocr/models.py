"""`ocr@1` 的模型制品：钉住的来源、版本与摘要（`docs/modules/perception.md` 的“OCR”）。

这里列出的是 GS-04 要评测的候选，不是定论；版本与许可在 GS-04 后写回技术基线 GR-10。

PP-OCRv5 的模型作者（PaddlePaddle）只发布 Paddle 格式的权重，转成 ONNX 要装 PaddlePaddle；
ONNX 文件取自 RapidOCR 项目（RapidAI）在魔搭社区的模型仓库，钉在标签 v3.10.0 上，摘要与
RapidOCR 3.10.0 的 `default_models.yaml` 一致。识别用的字表取自模型作者的 PaddleOCR 仓库，
与 ONNX 文件内嵌的字表逐行相同。
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal

from game_runtime.artifacts import Artifact

_RAPIDOCR = "https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v3.10.0/onnx/PP-OCRv5"
_RAPIDOCR_VERSION = "rapidocr-v3.10.0"
_RAPIDOCR_LICENSE = "Apache-2.0（RapidAI/RapidOCR 模型仓库；原始权重 PaddlePaddle，Apache-2.0）"


def _rapidocr(name: str, path: str, sha256: str, size: int, upstream: str) -> Artifact:
    return Artifact(
        name=name,
        version=_RAPIDOCR_VERSION,
        file=path.rsplit("/", 1)[1],
        sha256=sha256,
        size=size,
        url=f"{_RAPIDOCR}/{path}",
        source=(
            "RapidAI 在魔搭社区的 RapidOCR 模型仓库，标签 v3.10.0；由 PaddlePaddle 发布的 "
            f"{upstream}（https://huggingface.co/PaddlePaddle/{upstream}）转换为 ONNX"
        ),
        license=_RAPIDOCR_LICENSE,
    )


PPOCRV5_DICT: Final = Artifact(
    name="ppocrv5-dict",
    version="paddleocr-v3.0.0",
    file="ppocrv5_dict.txt",
    sha256="d1979e9f794c464c0d2e0b70a7fe14dd978e9dc644c0e71f14158cdf8342af1b",
    size=74012,
    url=(
        "https://raw.githubusercontent.com/PaddlePaddle/PaddleOCR/v3.0.0/"
        "ppocr/utils/dict/ppocrv5_dict.txt"
    ),
    source="PaddlePaddle/PaddleOCR 仓库标签 v3.0.0 的 ppocr/utils/dict/ppocrv5_dict.txt",
    license="Apache-2.0（PaddlePaddle/PaddleOCR）",
)
"""PP-OCRv5 识别模型的字表：每行一个字，共 18383 行。"""

PPOCRV5_MOBILE_DET: Final = _rapidocr(
    "ppocrv5-mobile-det",
    "det/ch_PP-OCRv5_det_mobile.onnx",
    "4d97c44a20d30a81aad087d6a396b08f786c4635742afc391f6621f5c6ae78ae",
    4819576,
    "PP-OCRv5_mobile_det",
)
PPOCRV5_MOBILE_REC: Final = _rapidocr(
    "ppocrv5-mobile-rec",
    "rec/ch_PP-OCRv5_rec_mobile.onnx",
    "5825fc7ebf84ae7a412be049820b4d86d77620f204a041697b0494669b1742c5",
    16631306,
    "PP-OCRv5_mobile_rec",
)
PPOCRV5_SERVER_DET: Final = _rapidocr(
    "ppocrv5-server-det",
    "det/ch_PP-OCRv5_det_server.onnx",
    "0f8846b1d4bba223a2a2f9d9b44022fbc22cc019051a602b41a7fda9667e4cad",
    88118768,
    "PP-OCRv5_server_det",
)
PPOCRV5_SERVER_REC: Final = _rapidocr(
    "ppocrv5-server-rec",
    "rec/ch_PP-OCRv5_rec_server.onnx",
    "e09385400eaaaef34ceff54aeb7c4f0f1fe014c27fa8b9905d4709b65746562a",
    84577022,
    "PP-OCRv5_server_rec",
)

type ModelSetName = Literal["ppocrv5-mobile", "ppocrv5-server"]


@dataclass(frozen=True)
class ModelSet:
    """一组一起使用的检测模型、识别模型与字表。"""

    name: ModelSetName
    det: Artifact
    rec: Artifact
    dictionary: Artifact
    languages: frozenset[str]
    """在合成样本上核对过的语言版本（BCP 47 的语言加文字）。"""

    @property
    def artifacts(self) -> tuple[Artifact, Artifact, Artifact]:
        return self.det, self.rec, self.dictionary

    def supports(self, tag: str) -> bool:
        """适配包的语言版本 `tag`（如 `zh-Hans` 或 `zh-Hans-CN`）是否在 `languages` 内。"""
        return any(tag == language or tag.startswith(f"{language}-") for language in self.languages)


MODEL_SETS: Final[Mapping[ModelSetName, ModelSet]] = {
    "ppocrv5-mobile": ModelSet(
        "ppocrv5-mobile",
        PPOCRV5_MOBILE_DET,
        PPOCRV5_MOBILE_REC,
        PPOCRV5_DICT,
        frozenset({"zh-Hans"}),
    ),
    "ppocrv5-server": ModelSet(
        "ppocrv5-server",
        PPOCRV5_SERVER_DET,
        PPOCRV5_SERVER_REC,
        PPOCRV5_DICT,
        frozenset({"zh-Hans"}),
    ),
}

DEFAULT_MODEL_SET: Final[ModelSetName] = "ppocrv5-mobile"
"""缺省的一组；GS-04 后按实测重新确定。"""


def all_artifacts() -> tuple[Artifact, ...]:
    """全部模型组用到的制品，不重复，按出现的顺序。"""
    seen: dict[tuple[str, str], Artifact] = {}
    for model_set in MODEL_SETS.values():
        for artifact in model_set.artifacts:
            seen.setdefault((artifact.name, artifact.version), artifact)
    return tuple(seen.values())
