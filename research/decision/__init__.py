"""决策提供者（DecisionProvider）：规则 / DeepSeek / Jev / 线性模型对照。

所有 provider 只产出研究记录（Decision），不进入 Selection、风控或成交。
谁更有效，由未见样本的净收益裁决。
"""
from .compare import consensus, run
from .contracts import CHOICES, Decision, MarketSnapshot, Provider, unavailable
from .dataset import DatasetRow, build_rows, labelled, purged_split, to_jsonl
from .providers.deepseek import DeepSeekProvider
from .providers.jev import JevProvider
from .providers.linear import FEATURES, LinearProvider, LogisticModel, flatten, train
from .providers.rules import RulesProvider

__all__ = [
    "CHOICES", "Decision", "MarketSnapshot", "Provider", "unavailable",
    "consensus", "run",
    "DatasetRow", "build_rows", "labelled", "purged_split", "to_jsonl",
    "DeepSeekProvider", "JevProvider", "LinearProvider", "RulesProvider",
    "FEATURES", "LogisticModel", "flatten", "train",
]