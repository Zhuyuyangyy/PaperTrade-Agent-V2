"""情报汇总（Sprint 3）：三条线 → 一个 ``IntelDigest``，默认 advisory_only。"""
from __future__ import annotations

from .contracts import IntelDigest, NewsEvent, PredictionMarketEvent, WalletSignal


def build_digest(as_of_ms: int, *, news: tuple[NewsEvent, ...] = (),
                 markets: tuple[PredictionMarketEvent, ...] = (),
                 wallets: tuple[WalletSignal, ...] = (),
                 warnings: tuple[str, ...] = ()) -> IntelDigest:
    flags = list(warnings)
    injected = [n.event_id for n in news if n.injection_suspected]
    if injected:
        flags.append(f"INJECTION_SUSPECTED:{','.join(injected)}（只做标记，未执行任何指令）")
    non_researchable = [m.market_id for m in markets if not m.researchable]
    if non_researchable:
        flags.append(f"MARKETS_NOT_RESEARCHABLE:{','.join(non_researchable)}")
    return IntelDigest(
        as_of_ms=as_of_ms, news=news, markets=markets, wallets=wallets,
        warnings=tuple(flags), advisory_only=True,
    )


def attach_to_log(log_entry: dict, digest: IntelDigest | None) -> dict:
    """把情报附到决策日志；**不修改** selection / risk / fill。"""
    out = dict(log_entry)
    out["intel"] = digest.to_dict() if digest else None
    return out