"""聪明钱跟单研究（Sprint 3）——只读、只评估"可复制性"。

纪律：
  - 已实现盈亏与未实现盈亏分开统计，不把浮盈当战绩；
  - 只监测"看起来赚钱"的钱包 = 幸存者偏差，必须显式标注；
  - 复制成本：我们的延迟、我们能拿到的价格、相对流动性的大小；任一不可行 → 不可复制；
  - 产出是研究信号（``advisory_only``），不是跟单指令。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .contracts import WalletSignal

DEFAULT_MAX_DELAY_MS = 5_000
DEFAULT_LIQUIDITY_USD = Decimal("100000")
DEFAULT_MAX_SIZE_SHARE = Decimal("0.05")


@dataclass(frozen=True)
class WalletTrade:
    wallet: str
    token: str
    side: str                 # BUY / SELL
    tx_ts_ms: int
    price_usd: Decimal
    size_usd: Decimal
    realized_pnl_usd: Decimal = Decimal("0")
    unrealized_pnl_usd: Decimal = Decimal("0")


def analyze(trades: list[WalletTrade], now_ms: int,
            *, max_delay_ms: int = DEFAULT_MAX_DELAY_MS,
            liquidity_usd: Decimal = DEFAULT_LIQUIDITY_USD,
            max_size_share: Decimal = DEFAULT_MAX_SIZE_SHARE) -> tuple[WalletSignal, ...]:
    out: list[WalletSignal] = []
    max_size = liquidity_usd * max_size_share
    for t in trades:
        delay = max(0, now_ms - t.tx_ts_ms)
        reasons: list[str] = []
        if delay > max_delay_ms:
            reasons.append(f"DELAY_TOO_LONG:{delay}ms>{max_delay_ms}ms")
        if t.size_usd > max_size:
            reasons.append(f"SIZE_TOO_LARGE:{t.size_usd}>{max_size}")
        if t.realized_pnl_usd <= 0 and t.unrealized_pnl_usd > 0:
            reasons.append("UNREALIZED_ONLY")
        out.append(WalletSignal(
            wallet=t.wallet, token=t.token, side=t.side, tx_ts_ms=t.tx_ts_ms,
            price_usd=t.price_usd, size_usd=t.size_usd,
            realized_pnl_usd=t.realized_pnl_usd, unrealized_pnl_usd=t.unrealized_pnl_usd,
            delay_ms=delay, copyable=not reasons, reasons=tuple(reasons),
            advisory_only=True,
        ))
    return tuple(out)


def scorecard(trades: list[WalletTrade], universe_size: int | None = None) -> dict:
    """钱包层面的战绩与偏差检查。"""
    realized = sum((t.realized_pnl_usd for t in trades), Decimal("0"))
    unrealized = sum((t.unrealized_pnl_usd for t in trades), Decimal("0"))
    wallets = {t.wallet for t in trades}
    profitable = {t.wallet for t in trades if t.realized_pnl_usd > 0}
    notes: list[str] = []
    if trades and len(profitable) == len(wallets):
        notes.append("SURVIVORSHIP: 样本内所有钱包都是盈利的，说明样本很可能只包含赢家")
    if unrealized != 0:
        notes.append("未实现盈亏未计价入账（浮盈不是战绩）")
    coverage = (Decimal(len(wallets)) / Decimal(universe_size)) if universe_size else None
    if coverage is not None and coverage > Decimal("0.5"):
        notes.append("COVERAGE: 观测钱包占全市场比例过高，可能是全量而非聪明钱筛选")
    return {
        "wallets": len(wallets),
        "trades": len(trades),
        "realized_pnl_usd": str(realized),
        "unrealized_pnl_usd": str(unrealized),
        "coverage": None if coverage is None else str(coverage.quantize(Decimal("0.001"))),
        "notes": notes,
    }