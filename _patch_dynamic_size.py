"""Patch: wire dynamic_notional into evaluate() and the fill path."""
import pathlib

# --- 1) risk.py: Decision carries the size; evaluate uses dynamic_notional ---
p = pathlib.Path('research/copywatch/risk.py')
s = p.read_text(encoding='utf-8')

old = '''@dataclass
class Decision:
    verdict: str
    signal_class: str | None
    checks: list[Check] = field(default_factory=list)
    cost_bps: float = 0.0'''
new = '''@dataclass
class Decision:
    verdict: str
    signal_class: str | None
    checks: list[Check] = field(default_factory=list)
    cost_bps: float = 0.0
    # The notional the gates were evaluated against. The fill must use this
    # same number: checking impact at one size and trading at another is how a
    # position slips past its own risk gate.
    notional: float = 0.0
    size_note: str = ""'''
assert old in s, 'MISS Decision'
s = s.replace(old, new, 1)

old = '''    checks: list[Check] = []
    notional = float(risk["notional_usdt"])'''
new = '''    checks: list[Check] = []
    from .sizing import dynamic_notional

    notional, size_note = dynamic_notional(float(event.usd), risk)'''
assert old in s, 'MISS evaluate head'
s = s.replace(old, new, 1)

old = '''    return Decision(
        verdict=verdict,'''
new = '''    return Decision(
        verdict=verdict,'''
assert old in s

# find the actual return statement to extend it
import re
m = re.search(r'return Decision\(\n(?:.*\n)*?    \)', s)
assert m, 'MISS return block'
block = m.group(0)
assert 'cost_bps=' in block, block
new_block = block.replace('    )', '        notional=notional,\n        size_note=size_note,\n    )')
s = s.replace(block, new_block, 1)
p.write_text(s, encoding='utf-8')
print('risk.py wired')

# --- 2) agent.py: the fill uses decision.notional, not a fresh fixed clip ---
p = pathlib.Path('research/copywatch/agent.py')
s = p.read_text(encoding='utf-8')

old = '''                    notional = float(risk_cfg["notional_usdt"])
                    qty = notional / fill_price'''
new = '''                    # The exact size the gates checked, so impact and cost
                    # were verified against the clip actually being filled.
                    notional = float(decision.notional)
                    qty = notional / fill_price'''
assert old in s, 'MISS fill notional'
s = s.replace(old, new, 1)

old = '''                        detail=f"{decision.signal_class} 信号 · 名义 ${notional:,.2f} · 手续费 ${fee_usd:.3f} · "
                               f"gas ${gas_usd:.2f} · 滑点 {slip_bps:.0f}bps · 相对源成交价偏离 {drift:+.1f}bps",'''
new = '''                        detail=f"{decision.signal_class} 信号 · 名义 ${notional:,.2f}（{decision.size_note}）· "
                               f"手续费 ${fee_usd:.3f} · gas ${gas_usd:.2f} · 滑点 {slip_bps:.0f}bps · "
                               f"相对源成交价偏离 {drift:+.1f}bps",'''
assert old in s, 'MISS fill detail'
s = s.replace(old, new, 1)

# --- 3) defaults in agent.py DEFAULT_CONFIG ---
old = '''    "risk": {
        "notional_usdt": 100.0,'''
new = '''    "risk": {
        "notional_usdt": 100.0,
        # Copy sizing: the clip scales with the target wallet's own fill
        # (0.5x), floored at $25 so a small signal is still worth filling and
        # capped at $150 so one whale cannot empty a $500 book.
        "copy_ratio": 0.5,
        "min_notional_usdt": 25.0,
        "max_notional_usdt": 150.0,'''
assert old in s, 'MISS default cfg'
s = s.replace(old, new, 1)
p.write_text(s, encoding='utf-8')
print('agent.py wired')

# --- 4) config.copywatch.json ---
import json
cp = pathlib.Path('user_data/config.copywatch.json')
d = json.loads(cp.read_text(encoding='utf-8'))
d['risk']['copy_ratio'] = 0.5
d['risk']['min_notional_usdt'] = 25.0
d['risk']['max_notional_usdt'] = 150.0
cp.write_text(json.dumps(d, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
print('config wired')
