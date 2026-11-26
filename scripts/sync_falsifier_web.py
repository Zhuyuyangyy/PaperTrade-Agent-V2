"""把证伪引擎同步进 docs/falsifier/，页面与仓库引擎不可能漂移。

为什么需要这个脚本：页面的卖点是"你看到的每个数字都来自同一次本地执行，
用的就是仓库里那个引擎"。一旦网页里的 engine.py 是手工复制的旧版本，
这句承诺立刻变成谎言。所以同步必须只有一条路径，并且可校验。

权威源：skill/strategy-falsifier/agensi/scripts/verify.py
        （由 build_agensi.py 从 research/ 生成，单文件、纯标准库）
去向：  docs/falsifier/engine.py

用法：
    python scripts/sync_falsifier_web.py          # 同步
    python scripts/sync_falsifier_web.py --check  # 只校验，不一致则退出码 1
"""
from __future__ import annotations

import hashlib
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "skill" / "strategy-falsifier" / "agensi" / "scripts" / "verify.py"
DST = ROOT / "docs" / "falsifier" / "engine.py"

# 浏览器里用的演示 K 线：与引擎自带 samples 同源，保证页面与仓库跑出同一批数字
CANDLES_SRC = ROOT / "research" / "verify" / "samples" / "demo_candles.csv"
CANDLES_DST = ROOT / "docs" / "falsifier" / "demo_candles.csv"


def sha(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main(argv: list[str]) -> int:
    check_only = "--check" in argv
    missing = [str(p.relative_to(ROOT)) for p in (SRC, CANDLES_SRC) if not p.is_file()]
    if missing:
        print("缺少源文件: " + ", ".join(missing))
        return 2

    DST.parent.mkdir(parents=True, exist_ok=True)

    same_engine = DST.is_file() and sha(DST) == sha(SRC)
    same_candles = CANDLES_DST.is_file() and sha(CANDLES_DST) == sha(CANDLES_SRC)

    if check_only:
        ok = same_engine and same_candles
        print(f"engine.py        {'一致' if same_engine else '不一致'}  {sha(SRC)[:16]}")
        print(f"demo_candles.csv {'一致' if same_candles else '不一致'}  {sha(CANDLES_SRC)[:16]}")
        if not ok:
            print("\n网页里的引擎与仓库不一致。跑 python scripts/sync_falsifier_web.py 同步。")
        return 0 if ok else 1

    if not same_engine:
        shutil.copy2(SRC, DST)
        print(f"engine.py 已同步 -> {DST.relative_to(ROOT)}  sha256 {sha(DST)[:16]}")
    else:
        print("engine.py 已是最新")

    if not same_candles:
        shutil.copy2(CANDLES_SRC, CANDLES_DST)
        print(f"demo_candles.csv 已同步 -> {CANDLES_DST.relative_to(ROOT)}")
    else:
        print("demo_candles.csv 已是最新")

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))