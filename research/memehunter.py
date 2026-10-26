"""MemeHunter: a paper-only Solana meme desk that grades the token, not the crowd.

The CopyWatch experiment (account D) tested whether following active wallets
carried information. It did not: 0 of 34 wallets beat the round-trip cost. So
this desk asks a different question -- can a meme token be screened on its own
on-chain quality before the crowd arrives?

Inputs are Jupiter's public token statistics, which expose things a price chart
hides:

* `organicScore` and the `OrganicVolume` split separate real demand from wash
  trading. A token whose volume is 95% inorganic is being manufactured.
* `numNetBuyers` / `numOrganicBuyers` say whether buyers are broad or one desk.
* `audit.topHoldersPercentage` plus mint/freeze authority flags catch the rug
  shapes before entry rather than after.

Every gate below exists because its absence is a known way to lose money on this
asset class. The reasoning is attached to each rule so a losing trade can be
traced to a specific assumption rather than blamed on bad luck.

Everything is paper. No wallet, no key, no signing.
"""

from __future__ import annotations

import json
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

JUP = "https://lite-api.jup.ag"
# Search terms are rotated rather than looped so one scan stays inside the
# endpoint's rate limit while still covering the whole list over a few minutes.
SEARCH_TERMS = (
    "dog", "cat", "pepe", "meme", "bonk", "wif", "frog", "trump",
    "ai", "pump", "mog", "boomer", "chill", "fart", "goat", "ponke",
)
SEARCH_TERMS_PER_SCAN = 4

# A $100 clip on a Solana meme pool pays DEX fee both legs, priority fee, and
# crosses a wide spread. 120 bps round trip is what a retail taker really
# loses; assuming anything gentler makes the equity curve a lie.
ROUND_TRIP_COST_BPS = 120.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS scans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scanned_at_ms INTEGER NOT NULL,
    tokens_seen INTEGER NOT NULL,
    tokens_eligible INTEGER NOT NULL,
    errors TEXT
);
CREATE TABLE IF NOT EXISTS candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id INTEGER NOT NULL,
    scanned_at_ms INTEGER NOT NULL,
    mint TEXT NOT NULL,
    symbol TEXT NOT NULL,
    price_usd REAL NOT NULL,
    liquidity_usd REAL NOT NULL,
    fdv_usd REAL NOT NULL,
    organic_score REAL NOT NULL,
    organic_label TEXT,
    holder_count INTEGER NOT NULL,
    top_holders_pct REAL NOT NULL,
    chg_5m REAL NOT NULL,
    chg_1h REAL NOT NULL,
    chg_24h REAL NOT NULL,
    organic_buy_5m REAL NOT NULL,
    organic_sell_5m REAL NOT NULL,
    net_buyers_5m INTEGER NOT NULL,
    vol_1h REAL NOT NULL,
    score REAL NOT NULL,
    eligible INTEGER NOT NULL,
    reject_reason TEXT,
    llm_score INTEGER,
    llm_flags TEXT,
    llm_reason TEXT
);
CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    opened_at_ms INTEGER NOT NULL,
    mint TEXT NOT NULL,
    symbol TEXT NOT NULL,
    score REAL NOT NULL,
    notional_usd REAL NOT NULL,
    entry_ref_price REAL NOT NULL,
    entry_fill_price REAL NOT NULL,
    qty REAL NOT NULL,
    entry_slippage_bps REAL NOT NULL,
    entry_cost_usd REAL NOT NULL,
    entry_reason TEXT NOT NULL,
    status TEXT NOT NULL,
    mark_price REAL,
    mark_at_ms INTEGER,
    peak_price REAL,
    exit_at_ms INTEGER,
    exit_reason TEXT,
    exit_fill_price REAL,
    exit_cost_usd REAL,
    pnl_usd REAL,
    pnl_pct REAL
);
CREATE TABLE IF NOT EXISTS equity (
    at_ms INTEGER PRIMARY KEY,
    cash_usd REAL NOT NULL,
    positions_usd REAL NOT NULL,
    equity_usd REAL NOT NULL,
    complete INTEGER NOT NULL
);
"""

DEFAULT_CONFIG: dict[str, Any] = {
    "starting_cash_usdt": 1000.0,
    "notional_usdt": 100.0,
    "max_open_positions": 5,
    "cooldown_minutes": 45,
    "round_trip_cost_bps": ROUND_TRIP_COST_BPS,
    # --- survival vetoes: failing these risks total loss, so they are absolute ---
    "veto_top_holders_pct": 55.0,
    "veto_min_holders": 300,
    "veto_min_liquidity_usd": 40_000.0,
    "veto_min_volume_1h_usd": 20_000.0,
    "veto_max_fdv_usd": 80_000_000.0,
    "veto_collapse_5m_pct": 12.0,
    # Below this, most printed volume is not organic demand; it is manufactured.
    "veto_min_organic_share": 0.10,
    # --- quality inputs, priced into the score rather than gated ---
    "min_organic_score": 45.0,
    "min_organic_buy_ratio": 1.05,
    "min_organic_buy_usd": 3_000.0,
    # --- anti-chase / anti-fade (penalties, not vetoes) ---
    "min_chg_5m": -3.0,
    "max_chg_1h": 55.0,
    "min_chg_1h": -12.0,
    # A token below this score is not worth a clip even if it survives the vetoes.
    "min_score_to_buy": 52.0,
    # --- exits ---
    "take_profit_pct": 55.0,
    "stop_loss_pct": -12.0,
    "timeout_minutes": 180,
    "trailing_giveback_pct": 30.0,
}


class SourceError(RuntimeError):
    """The public data source is unreachable, throttled, or returned garbage."""


_last_call = 0.0
_MIN_GAP_S = 1.2


def _get(url: str, timeout: float = 20.0) -> Any:
    global _last_call
    gap = time.time() - _last_call
    if gap < _MIN_GAP_S:
        time.sleep(_MIN_GAP_S - gap)
    _last_call = time.time()
    request = urllib.request.Request(
        url, headers={"User-Agent": "PaperTrade-MemeHunter/0.2 (paper research)"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        raise SourceError(f"HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise SourceError(f"数据源不可达: {exc}") from exc


def _f(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if out == out else default


@dataclass
class Token:
    mint: str
    symbol: str
    name: str
    price_usd: float
    liquidity_usd: float
    fdv_usd: float
    organic_score: float
    organic_label: str
    holder_count: int
    top_holders_pct: float
    mint_disabled: bool
    freeze_disabled: bool
    chg_5m: float
    chg_1h: float
    chg_24h: float
    organic_buy_5m: float
    organic_sell_5m: float
    organic_buy_1h: float
    organic_sell_1h: float
    net_buyers_5m: int
    net_buyers_1h: int
    vol_1h: float
    vol_24h: float
    created_at: str = ""
    source: str = ""
    raw: dict = field(default_factory=dict)


def _parse_token(row: dict, source: str) -> Token | None:
    mint = row.get("id")
    price = _f(row.get("usdPrice"))
    if not isinstance(mint, str) or not mint or price <= 0:
        return None
    s5 = row.get("stats5m") or {}
    s1 = row.get("stats1h") or {}
    s24 = row.get("stats24h") or {}
    audit = row.get("audit") or {}
    return Token(
        mint=mint,
        symbol=str(row.get("symbol") or "?")[:16],
        name=str(row.get("name") or ""),
        price_usd=price,
        liquidity_usd=_f(row.get("liquidity")),
        fdv_usd=_f(row.get("fdv")) or _f(row.get("mcap")),
        organic_score=_f(row.get("organicScore")),
        organic_label=str(row.get("organicScoreLabel") or ""),
        holder_count=int(_f(row.get("holderCount"))),
        top_holders_pct=_f(audit.get("topHoldersPercentage")),
        mint_disabled=audit.get("mintAuthorityDisabled") is True,
        freeze_disabled=audit.get("freezeAuthorityDisabled") is True,
        chg_5m=_f(s5.get("priceChange")),
        chg_1h=_f(s1.get("priceChange")),
        chg_24h=_f(s24.get("priceChange")),
        organic_buy_5m=_f(s5.get("buyOrganicVolume")),
        organic_sell_5m=_f(s5.get("sellOrganicVolume")),
        organic_buy_1h=_f(s1.get("buyOrganicVolume")),
        organic_sell_1h=_f(s1.get("sellOrganicVolume")),
        net_buyers_5m=int(_f(s5.get("numNetBuyers"))),
        net_buyers_1h=int(_f(s1.get("numNetBuyers"))),
        vol_1h=_f(s1.get("buyVolume")) + _f(s1.get("sellVolume")),
        vol_24h=_f(s24.get("buyVolume")) + _f(s24.get("sellVolume")),
        created_at=str(row.get("createdAt") or ""),
        source=source,
        raw=row,
    )


def fetch_universe() -> tuple[list[Token], list[str]]:
    """Collect candidates from several Jupiter rankings.

    Using more than one list matters: `toporganicscore` surfaces established
    tokens with genuine demand, while `recent` surfaces new pools the score
    ranking has not weighed yet. Dedupe by mint so scoring sees each once.
    """
    errors: list[str] = []
    seen: dict[str, Token] = {}
    # The two ranking families cover opposite extremes and nothing in between:
    # `recent` is full of minutes-old tokens with single-digit holder counts and
    # ~$3k pools, while `toporganicscore` is dominated by tokenised equities
    # (SPYx, QQQx, TTWO, AMC) that keep their mint authority and are vetoed for
    # it. Neither surfaces a real mid-cap memecoin, which is the only thing this
    # desk can actually trade. Keyword search does, so it is a first-class source.
    sources = (
        ("toporganicscore/1h", "organic1h"),
        ("toporganicscore/5m", "organic5m"),
        ("toporganicscore/24h", "organic24h"),
        ("recent", "recent"),
    )
    for endpoint, label in sources:
        try:
            rows = _get(f"{JUP}/tokens/v2/{endpoint}")
        except SourceError as exc:
            errors.append(f"{label}: {exc}")
            continue
        if not isinstance(rows, list):
            errors.append(f"{label}: 返回结构异常")
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            token = _parse_token(row, label)
            if token is None:
                continue
            existing = seen.get(token.mint)
            if existing is None or token.organic_score > existing.organic_score:
                seen[token.mint] = token

    # Keyword search rotates four terms per scan. The endpoint throttles hard,
    # so a 429 on one term drops that term and keeps the rest of the scan.
    window = int(time.time() // 60)
    start = window % len(SEARCH_TERMS)
    for offset in range(SEARCH_TERMS_PER_SCAN):
        term = SEARCH_TERMS[(start + offset) % len(SEARCH_TERMS)]
        try:
            rows = _get(f"{JUP}/tokens/v2/search?query={urllib.parse.quote(term)}")
        except SourceError as exc:
            errors.append(f"search:{term}: {exc}")
            continue
        if not isinstance(rows, list):
            errors.append(f"search:{term}: 返回结构异常")
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            token = _parse_token(row, f"search:{term}")
            if token is None:
                continue
            existing = seen.get(token.mint)
            if existing is None or token.organic_score > existing.organic_score:
                seen[token.mint] = token
    return list(seen.values()), errors


def veto(token: Token, cfg: dict) -> tuple[bool, str]:
    """一票否决：只拦会导致本金归零的条件。

    An all-must-pass screen is wrong for this asset class -- most meme tokens
    are structurally defective, so a conjunction of ten gates selects nothing.
    Survival risks veto; everything else is priced into the score.
    """
    if not (token.mint_disabled and token.freeze_disabled):
        # Supply can be inflated against us, or our tokens frozen. Unrecoverable.
        return False, "AUTHORITY_NOT_REVOKED"
    if token.top_holders_pct > cfg["veto_top_holders_pct"]:
        # A handful of wallets holding the float is how rugs are executed.
        return False, "HOLDERS_TOO_CONCENTRATED"
    if token.holder_count < cfg["veto_min_holders"]:
        return False, "TOO_FEW_HOLDERS"
    if token.liquidity_usd < cfg["veto_min_liquidity_usd"]:
        return False, "LIQUIDITY_TOO_THIN"
    if token.vol_1h < cfg["veto_min_volume_1h_usd"]:
        # No recent trading means no exit, whatever the quoted price says.
        return False, "NO_LIQUIDITY_FLOW"
    if token.chg_5m <= -cfg["veto_collapse_5m_pct"]:
        # A five-minute double-digit drop is a dump in progress, not a dip. A
        # 100 USDT clip cannot outrun the crowd holding the same bag.
        return False, "COLLAPSING"
    if token.fdv_usd > cfg["veto_max_fdv_usd"]:
        # JUP trades at a ~$1.9B FDV. A 100 USDT clip there cannot move, and a
        # blue chip's 5-minute momentum is noise rather than a meme run-up.
        return False, "TOO_LARGE_TO_BE_MEME"
    if token.vol_1h > 0:
        organic_total = token.organic_buy_1h + token.organic_sell_1h
        if organic_total <= 0:
            # No organic data at all is not the same claim as "the volume is
            # fake". Reporting the two as one reason would have labelled every
            # mid-cap memecoin a wash trade on the strength of a missing field.
            return False, "NO_ORGANIC_DATA"
        organic_share = organic_total / token.vol_1h
        if organic_share < cfg["veto_min_organic_share"]:
            # baton printed an organicScore of 60 while only 2.2% of its hourly
            # volume was organic. The clean label and the traded reality disagree,
            # and wash volume is what a wash-traded chart looks like right before
            # it collapses. Trust the share, not the badge.
            return False, "VOLUME_IS_WASH_TRADED"
    return True, ""


def score(token: Token, cfg: dict) -> float:
    """0-100, weighted toward evidence of genuine demand.

    Each component is capped so one spectacular input cannot carry a token, and
    explicit penalties encode the ways this asset class usually hurts you.
    """
    organic = min(30.0, token.organic_score * 0.30)
    if token.organic_sell_1h > 0:
        ratio = token.organic_buy_1h / token.organic_sell_1h
    else:
        ratio = 3.0 if token.organic_buy_1h > 0 else 0.0
    pressure = max(0.0, min(25.0, (ratio - 1.0) * 18.0))
    # Activity is absolute traded value, not volume divided by pool depth. Using
    # the ratio inverted the ranking: a $45k pool with the same turnover scored
    # 72.9 against 55.1 for a $400k pool, so the desk preferred the venue it
    # could not exit.
    activity = max(0.0, min(12.0, token.vol_1h / 25_000.0))
    depth = max(0.0, min(8.0, token.liquidity_usd / 50_000.0))
    breadth = max(0.0, min(15.0, token.holder_count / 1_500.0))
    momentum = max(0.0, min(10.0, token.chg_5m * 1.5 + token.chg_1h * 0.15))
    penalty = 0.0
    if token.chg_1h > cfg["max_chg_1h"]:
        penalty += 20.0          # already vertical: buying the top of the move
    if token.chg_5m < 0:
        # Graded, not a cliff. A fixed -15 turned the same token from 59.5 to
        # 44.5 across four minutes of noise, so the buy decision was decided by
        # the scan's timing rather than by the token.
        penalty += min(8.0, -token.chg_5m * 0.8)
    if ratio < 1.0:
        penalty += 12.0          # organic sellers outnumber organic buyers
    if token.net_buyers_5m <= 0:
        penalty += 10.0          # net distribution in the last five minutes
    if token.organic_buy_1h < cfg["min_organic_buy_usd"]:
        penalty += 10.0          # real demand too thin to read
    # Concentration is the strongest single predictor of a rug, yet it only
    # mattered as a veto at 55%. Between 25% and that line the score was blind
    # to it, so 52% and 18% ranked identically.
    penalty += min(15.0, max(0.0, token.top_holders_pct - 25.0) * 0.5)
    return round(max(0.0, organic + pressure + activity + depth + breadth + momentum - penalty), 2)


def entry_slippage_bps(notional: float, liquidity_usd: float) -> float:
    """Constant-product impact against one side of the pool, floored at the
    spread a taker crosses on a thin meme book."""
    if liquidity_usd <= 0:
        return 10_000.0
    impact = (notional / (liquidity_usd / 2.0)) * 10_000.0
    return max(35.0, impact)


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn



def ensure_candidate_columns(conn: sqlite3.Connection) -> None:
    """Ledgers created before the JUDGE agent need the llm_* columns."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(candidates)")}
    for name, ddl in (
        ("llm_score", "ALTER TABLE candidates ADD COLUMN llm_score INTEGER"),
        ("llm_flags", "ALTER TABLE candidates ADD COLUMN llm_flags TEXT"),
        ("llm_reason", "ALTER TABLE candidates ADD COLUMN llm_reason TEXT"),
    ):
        if name not in columns:
            conn.execute(ddl)
    conn.commit()


def now_ms() -> int:
    return int(time.time() * 1000)


def record_candidates(
    conn: sqlite3.Connection, scan_id: int, started_ms: int, tokens: list, cfg: dict
) -> dict:
    """Persist one scan's full candidate snapshot, rejects included.

    Both the pipeline's SEARCH agent and the standalone CLI go through here, so
    the candidates table -- the replay engine's only source of truth -- keeps
    recording no matter which entry point produced the round.
    """
    eligible = 0
    ranked: list[tuple[Token, float]] = []
    for token in tokens:
        ok, reason = veto(token, cfg)
        pts = score(token, cfg) if ok else 0.0
        buyable = ok and pts >= float(cfg["min_score_to_buy"])
        if buyable:
            eligible += 1
            ranked.append((token, pts))
        organic_ratio = (
            token.organic_buy_5m / token.organic_sell_5m
            if token.organic_sell_5m > 0
            else (99.0 if token.organic_buy_5m > 0 else 0.0)
        )
        conn.execute(
            "INSERT INTO candidates (scan_id, scanned_at_ms, mint, symbol, price_usd, "
            "liquidity_usd, fdv_usd, organic_score, organic_label, holder_count, "
            "top_holders_pct, chg_5m, chg_1h, chg_24h, organic_buy_5m, organic_sell_5m, "
            "net_buyers_5m, vol_1h, score, eligible, reject_reason) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                scan_id, started_ms, token.mint, token.symbol, token.price_usd,
                token.liquidity_usd, token.fdv_usd, token.organic_score,
                token.organic_label, token.holder_count, token.top_holders_pct,
                token.chg_5m, token.chg_1h, token.chg_24h, token.organic_buy_5m,
                token.organic_sell_5m, token.net_buyers_5m, token.vol_1h,
                pts, 1 if buyable else 0,
                reason or (None if buyable else "SCORE_BELOW_FLOOR"),
            ),
        )
    conn.execute("UPDATE scans SET tokens_eligible=? WHERE id=?", (eligible, scan_id))
    conn.commit()
    ranked.sort(key=lambda pair: pair[1], reverse=True)
    return {"scan_id": scan_id, "seen": len(tokens), "eligible": eligible, "ranked": ranked}


class Desk:
    """Owns the E-account ledger. All money movement goes through here."""

    def __init__(self, root: Path, cfg: dict | None = None, db_name: str = "memehunter.sqlite"):
        self.root = root
        self.cfg = {**DEFAULT_CONFIG, **(cfg or {})}
        self.runtime = root / "runtime" / "memehunter"
        self.runtime.mkdir(parents=True, exist_ok=True)
        self.db_path = self.runtime / db_name
        self.conn = connect(self.db_path)

    # ---------- cash ----------
    def cash(self) -> float:
        # Principal must leave the account when the position opens, not just the
        # fee. Subtracting only entry_cost_usd left the notional in cash *and*
        # counted the holding's market value, which printed equity of 1,099 on a
        # 1,000 account the moment the first position filled.
        spent = self.conn.execute(
            "SELECT COALESCE(SUM(notional_usd + entry_cost_usd),0) FROM positions"
        ).fetchone()[0]
        back = self.conn.execute(
            "SELECT COALESCE(SUM(exit_fill_price*qty - exit_cost_usd),0) FROM positions "
            "WHERE status='closed' AND exit_fill_price IS NOT NULL"
        ).fetchone()[0]
        return float(self.cfg["starting_cash_usdt"]) - float(spent) + float(back)

    def open_positions(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM positions WHERE status='open'"))

    def _last_open_ms(self, mint: str) -> int | None:
        row = self.conn.execute(
            "SELECT MAX(opened_at_ms) FROM positions WHERE mint=?", (mint,)
        ).fetchone()
        return int(row[0]) if row and row[0] else None

    # ---------- scanning ----------
    def scan(self) -> dict:
        started = now_ms()
        tokens, errors = fetch_universe()
        result = record_candidates(self.conn, self.conn.execute(
            "INSERT INTO scans (scanned_at_ms, tokens_seen, tokens_eligible, errors) "
            "VALUES (?,?,?,?)",
            (started, len(tokens), 0, json.dumps(errors, ensure_ascii=False)),
        ).lastrowid, started, tokens, self.cfg)
        result["errors"] = errors
        return result

    def can_open(self, token: Token, at_ms: int) -> tuple[bool, str]:
        if len(self.open_positions()) >= int(self.cfg["max_open_positions"]):
            return False, "MAX_POSITIONS"
        if any(p["mint"] == token.mint for p in self.open_positions()):
            return False, "ALREADY_OPEN"
        last = self._last_open_ms(token.mint)
        if last and at_ms - last < int(self.cfg["cooldown_minutes"]) * 60_000:
            return False, "COOLDOWN"
        if self.cash() < float(self.cfg["notional_usdt"]):
            return False, "INSUFFICIENT_CASH"
        return True, ""

    def open_position(self, token: Token, why: str, notional: float | None = None) -> dict:
        at = now_ms()
        ok, reason = self.can_open(token, at)
        if not ok:
            return {"opened": False, "reason": reason, "symbol": token.symbol}
        notional = float(notional if notional is not None else self.cfg["notional_usdt"])
        slip = entry_slippage_bps(notional, token.liquidity_usd)
        fill = token.price_usd * (1.0 + slip / 10_000.0)
        qty = notional / fill
        cost = notional * (float(self.cfg["round_trip_cost_bps"]) / 2.0) / 10_000.0
        cur = self.conn.execute(
            "INSERT INTO positions (opened_at_ms, mint, symbol, score, notional_usd, "
            "entry_ref_price, entry_fill_price, qty, entry_slippage_bps, entry_cost_usd, "
            "entry_reason, status, mark_price, mark_at_ms, peak_price) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,'open',?,?,?)",
            (
                at, token.mint, token.symbol, score(token, self.cfg), notional, token.price_usd,
                fill, qty, slip, cost, why, token.price_usd, at, token.price_usd,
            ),
        )
        self.conn.commit()
        return {
            "opened": True, "id": cur.lastrowid, "symbol": token.symbol,
            "fill": fill, "slippage_bps": slip, "cost_usd": cost,
            "price_usd": token.price_usd,
        }

    # ---------- marking ----------
    def quote(self, mints: list[str]) -> dict[str, float]:
        """Live USD price per mint. Missing mints are simply absent, so callers
        can tell "no quote" from "price is zero"."""
        if not mints:
            return {}
        try:
            payload = _get(f"{JUP}/price/v3?ids={','.join(mints)}")
        except SourceError:
            return {}
        out: dict[str, float] = {}
        if isinstance(payload, dict):
            for mint, row in payload.items():
                price = _f((row or {}).get("usdPrice"))
                if price > 0:
                    out[mint] = price
        return out

    def update(self) -> dict:
        """Mark open positions to live prices, then apply exits.

        A position without a fresh quote keeps its previous mark and is flagged
        incomplete in the equity sample -- never carried as if current.
        """
        at = now_ms()
        opens = self.open_positions()
        quotes = self.quote([p["mint"] for p in opens])
        marked, closed, stale = 0, [], []
        for pos in opens:
            price = quotes.get(pos["mint"])
            if not price:
                stale.append(pos["symbol"])
                continue
            peak = max(float(pos["peak_price"] or 0.0), price)
            self.conn.execute(
                "UPDATE positions SET mark_price=?, mark_at_ms=?, peak_price=? WHERE id=?",
                (price, at, peak, pos["id"]),
            )
            marked += 1
            entry = float(pos["entry_fill_price"])
            change = (price / entry - 1.0) * 100.0
            reason = None
            if change >= float(self.cfg["take_profit_pct"]):
                reason = "TAKE_PROFIT"
            elif change <= float(self.cfg["stop_loss_pct"]):
                reason = "STOP_LOSS"
            elif at - int(pos["opened_at_ms"]) >= int(self.cfg["timeout_minutes"]) * 60_000:
                reason = "TIMEOUT"
            elif peak > entry:
                giveback = (peak - price) / peak * 100.0
                if giveback >= float(self.cfg["trailing_giveback_pct"]) and change > 0:
                    reason = "TRAILING_STOP"
            if reason:
                closed.append(self._close(pos, price, reason, at))
        self.conn.commit()
        self._sample_equity(at)
        return {"marked": marked, "closed": closed, "stale": stale}

    def _close(self, pos: sqlite3.Row, price: float, reason: str, at: int) -> dict:
        notional = float(pos["notional_usd"])
        exit_cost = notional * (float(self.cfg["round_trip_cost_bps"]) / 2.0) / 10_000.0
        proceeds = price * float(pos["qty"]) - exit_cost
        pnl = proceeds - notional - float(pos["entry_cost_usd"])
        pnl_pct = pnl / notional * 100.0
        self.conn.execute(
            "UPDATE positions SET status='closed', exit_at_ms=?, exit_reason=?, "
            "exit_fill_price=?, exit_cost_usd=?, pnl_usd=?, pnl_pct=?, mark_price=? WHERE id=?",
            (at, reason, price, exit_cost, pnl, pnl_pct, price, pos["id"]),
        )
        return {
            "id": pos["id"], "symbol": pos["symbol"], "reason": reason,
            "price": price, "pnl_usd": round(pnl, 4), "pnl_pct": round(pnl_pct, 2),
        }

    def _sample_equity(self, at: int) -> None:
        cash = self.cash()
        opens = self.open_positions()
        complete = all(p["mark_at_ms"] for p in opens)
        holdings = sum(
            float(p["mark_price"]) * float(p["qty"]) for p in opens if p["mark_price"]
        )
        self.conn.execute(
            "INSERT OR REPLACE INTO equity (at_ms, cash_usd, positions_usd, equity_usd, complete) "
            "VALUES (?,?,?,?,?)",
            (at, cash, holdings, cash + holdings, 1 if complete else 0),
        )

    # ---------- reporting ----------
    def report(self) -> dict:
        at = now_ms()
        opens = self.open_positions()
        cash = self.cash()
        holdings = 0.0
        rows = []
        for p in opens:
            mark = float(p["mark_price"] or 0.0)
            value = mark * float(p["qty"])
            holdings += value
            rows.append(
                {
                    "symbol": p["symbol"], "mint": p["mint"],
                    "entry": float(p["entry_fill_price"]), "mark": mark,
                    "change_pct": (mark / float(p["entry_fill_price"]) - 1.0) * 100.0 if mark else None,
                    "value_usd": round(value, 4),
                    "age_min": (at - int(p["opened_at_ms"])) / 60_000.0,
                    "score": float(p["score"]),
                }
            )
        closed = list(
            self.conn.execute(
                "SELECT * FROM positions WHERE status='closed' ORDER BY exit_at_ms DESC"
            )
        )
        wins = sum(1 for c in closed if (c["pnl_usd"] or 0) > 0)
        realized = sum(float(c["pnl_usd"] or 0.0) for c in closed)
        return {
            "cash_usd": round(cash, 4),
            "positions_usd": round(holdings, 4),
            "equity_usd": round(cash + holdings, 4),
            "starting_usd": float(self.cfg["starting_cash_usdt"]),
            "pnl_usd": round(cash + holdings - float(self.cfg["starting_cash_usdt"]), 4),
            "realized_usd": round(realized, 4),
            "open": rows,
            "closed_count": len(closed),
            "win_rate": (wins / len(closed) * 100.0) if closed else None,
            "closed": [
                {
                    "symbol": c["symbol"], "reason": c["exit_reason"],
                    "pnl_usd": round(float(c["pnl_usd"] or 0), 4),
                    "pnl_pct": round(float(c["pnl_pct"] or 0), 2),
                }
                for c in closed[:10]
            ],
        }
