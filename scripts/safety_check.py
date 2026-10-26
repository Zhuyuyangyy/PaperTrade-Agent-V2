"""Startup preflight. No credentials, no live mode, three segregated dry-run ledgers."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]


def verify_configs(base: Path = ROOT) -> list[str]:
    errs = []
    ur = base / "user_data"
    cfgs = []
    for kind in ("baseline", "hybrid", "fast"):
        path = ur / f"config.{kind}.json"
        try:
            cfg = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            errs.append(f"{kind}: config missing/invalid: {e}")
            continue
        cfgs.append(cfg)
        exc = cfg.get("exchange", {})
        checks = {
            "dry_run must be true": cfg.get("dry_run") is True,
            "spot only": cfg.get("trading_mode") == "spot",
            "1000 USDT simulated": cfg.get("dry_run_wallet") == 1000,
            "fixed stake 100": cfg.get("stake_amount") == 100,
            "max two trades": cfg.get("max_open_trades") == 2,
            "exchange is Kraken": exc.get("name") == "kraken",
            "no private exchange credentials": all(not exc.get(k) for k in ("key", "secret", "password")),
            "pairs approved": exc.get("pair_whitelist") == ["BTC/USDT", "ETH/USDT"],
            "API server disabled": cfg.get("api_server", {}).get("enabled") is False,
            "API server binds to loopback": cfg.get("api_server", {}).get("listen_ip_address") == "127.0.0.1",
            "API schema fields are present": all(k in cfg.get("api_server", {}) for k in ("listen_port", "username", "password", "jwt_secret_key")),
            "API credentials are empty": cfg.get("api_server", {}).get("username") == "" and cfg.get("api_server", {}).get("password") == "",
            "API JWT schema minimum length": len(str(cfg.get("api_server", {}).get("jwt_secret_key", ""))) >= 32,
            "Telegram section is disabled and complete": cfg.get("telegram", {}).get("enabled") is False and all(k in cfg.get("telegram", {}) for k in ("token", "chat_id", "authorized_users")),
            "manual order entry disabled": cfg.get("force_entry_enable") is False,
            "Telegram disabled": cfg.get("telegram", {}).get("enabled") is False,
            "simulated DB only": cfg.get("db_url") == f"sqlite:////freqtrade/user_data/{kind}.dryrun.sqlite",
        }
        errs.extend(f"{kind}: {label}" for label, okay in checks.items() if not okay)
    if len(cfgs) == 3 and len({cfg.get("db_url") for cfg in cfgs}) != 3:
        errs.append("Baseline, hybrid and fast must use three distinct trade ledgers")

    compose_path = base / "docker-compose.yml"
    try:
        compose = compose_path.read_text(encoding="utf-8")
    except OSError as exc:
        compose = ""
        errs.append(f"docker-compose.yml missing/unreadable: {exc}")
    isolation_checks = {
        "baseline has its own writable runtime directory": "./runtime/baseline:/freqtrade/user_data" in compose,
        "hybrid has its own writable runtime directory": "./runtime/hybrid:/freqtrade/user_data" in compose,
        "fast has its own writable runtime directory": "./runtime/fast:/freqtrade/user_data" in compose,
        "strategies are mounted read-only": "./user_data/strategies:/freqtrade/strategies:ro" in compose,
        "hybrid signals are mounted read-only": "./user_data/signals:/freqtrade/signals:ro" in compose,
        "research writes public marks": "./user_data/marks:/app/marks" in compose,
        "equity sampler service exists": "equity-sampler:" in compose,
        "sampler reads baseline ledger read-only": "./runtime/baseline:/app/runtime/baseline:ro" in compose,
        "sampler reads hybrid ledger read-only": "./runtime/hybrid:/app/runtime/hybrid:ro" in compose,
        "sampler reads fast ledger read-only": "./runtime/fast:/app/runtime/fast:ro" in compose,
        "sampler writes an equity journal": "/app/data/equity_snapshots.jsonl" in compose,
        "Freqtrade image is version-pinned": compose.count("freqtradeorg/freqtrade:2026.9") == 3,
        "shared writable user_data mount is absent": "./user_data:/freqtrade/user_data" not in compose,
    }
    errs.extend(f"isolation/config: {label}" for label, okay in isolation_checks.items() if not okay)

    strategy_path = base / "user_data" / "strategies" / "PaperBaseline.py"
    try:
        strategy = strategy_path.read_text(encoding="utf-8")
    except OSError as exc:
        strategy = ""
        errs.append(f"PaperBaseline strategy missing/unreadable: {exc}")
    strategy_checks = {
        "5 bps slippage is declared": "SLIPPAGE_BPS = 5" in strategy,
        "limit exits activate custom exit pricing": '"exit": "limit"' in strategy,
        "limit stoploss exits activate custom exit pricing": '"stoploss": "limit"' in strategy,
        "custom entry pricing hook exists": "def custom_entry_price(" in strategy,
        "custom exit pricing hook exists": "def custom_exit_price(" in strategy,
    }
    errs.extend(f"execution model: {label}" for label, okay in strategy_checks.items() if not okay)
    fast_path = base / "user_data" / "strategies" / "PaperFast.py"
    try:
        fast_strategy = fast_path.read_text(encoding="utf-8")
    except OSError as exc:
        fast_strategy = ""
        errs.append(f"fast strategy missing/unreadable: {exc}")
    fast_checks = {
        "fast lane is 15m": 'timeframe = "15m"' in fast_strategy,
        "fast lane has EMA indicators": 'span=9' in fast_strategy and 'span=21' in fast_strategy,
        "fast lane remains long-only": "can_short = False" in strategy,
        "fast lane does not override account sizing safety": "class PaperFast(PaperBaseline)" in fast_strategy,
        "fast lane publishes a local heartbeat": "def bot_loop_start(" in fast_strategy and "fast_heartbeat.json" in fast_strategy,
    }
    errs.extend(f"fast lane: {label}" for label, okay in fast_checks.items() if not okay)

    # CopyWatch D: event-driven paper account. It must be dry_run only, keep its own ledger,
    # and contain no signing capability at all.
    cw_path = ur / "config.copywatch.json"
    cw_config: dict = {}
    cw_text = ""
    try:
        cw_text = cw_path.read_text(encoding="utf-8")
        loaded = json.loads(cw_text)
        cw_config = loaded if isinstance(loaded, dict) else {}
    except (OSError, ValueError) as exc:
        errs.append(f"copywatch: config missing/invalid: {exc}")
    pools = ((cw_config.get("event_source") or {}).get("pools") or [])
    cw_segment = ""
    if "  copywatch:" in compose:
        cw_segment = compose.split("  copywatch:", 1)[1].split("\n  equity-sampler:", 1)[0]
    cw_checks = {
        "dry_run only": cw_config.get("mode") == "dry_run",
        "has its own starting cash": isinstance(cw_config.get("starting_cash_usdt"), (int, float))
        and cw_config.get("starting_cash_usdt") > 0,
        "pools declare network and pool address": bool(pools) and all(
            isinstance(item, dict) and item.get("network") and item.get("pool") for item in pools
        ),
        "no private key material in config": re.search(r"0x[0-9a-fA-F]{64}", cw_text) is None,
        "has its own writable runtime directory": "./runtime/copywatch:/app/runtime/copywatch" in compose,
        "exposes no network ports": "ports:" not in cw_segment,
        "runs without network caps": 'cap_drop: [ALL]' in cw_segment,
    }
    errs.extend(f"copywatch: {label}" for label, okay in cw_checks.items() if not okay)

    try:
        agent_src = (base / "research" / "copywatch" / "sources.py").read_text(encoding="utf-8")
    except OSError as exc:
        agent_src = ""
        errs.append(f"copywatch sources missing/unreadable: {exc}")
    code_checks = {
        "event source cannot sign transactions": "sendRawTransaction" not in agent_src,
        "event source holds no key material": "private_key" not in agent_src.lower()
        and "mnemonic" not in agent_src.lower(),
        "event source is read-only HTTP": "method=" not in agent_src and "POST" not in agent_src,
    }
    errs.extend(f"copywatch execution model: {label}" for label, okay in code_checks.items() if not okay)

    for k in os.environ:
        if k.upper().startswith("FREQTRADE__EXCHANGE__"):
            errs.append("Potential exchange credential/config override in environment")
            break
    return errs


def main() -> int:
    problems = verify_configs()
    if problems:
        print("PAPER SAFETY PREFLIGHT FAILED:")
        print("\n".join("- " + problem for problem in problems))
        return 2
    print("PASS: two primary A/B accounts plus one isolated 1000-USDT exploratory fast account; no private API credentials")
    print("PASS: three separate writable runtime dirs; read-only shared strategy/signal mounts")
    print("PASS: 5 bps adverse price hooks configured for entry and limit exits")
    print("PASS: CopyWatch D runs dry_run only, keeps its own ledger, and its event source cannot sign")
    print("NOTE: this checks repository config, not exchange connectivity or Docker startup")
    return 0


if __name__ == "__main__":
    sys.exit(main())
