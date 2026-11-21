// 从 Gamma 拉活跃加密市场，生成真实 market_mapping.yaml（P1 项）。
// 过滤：只保留距离到期 >48h 的市场（过期市场禁入研究）。
import { writeFileSync } from 'node:fs';

const GAMMA = 'https://gamma-api.polymarket.com/markets';

const res = await fetch(`${GAMMA}?limit=100&active=true&closed=false&order=volume24hr&ascending=false`, {
    headers: { 'User-Agent': 'probe/0.1' },
    signal: AbortSignal.timeout(20000),
});
const markets = await res.json();
const now = Date.now();

// 筛：问题里含 BTC/ETH/SOL/BNB 且带价格阈值的市场
const UNDERLYINGS = [
    [/bitcoin|\bbtc\b/i, 'BTC'],
    [/ethereum|\beth\b/i, 'ETH'],
    [/solana|\bsol\b/i, 'SOL'],
    [/binance coin|\bbnb\b/i, 'BNB'],
];
const THRESHOLD_RE = /\$\s?([\d,]+(?:\.\d+)?)/;

const picked = [];
const skippedExpired = [];
for (const m of markets) {
    const q = m.question || '';
    const u = UNDERLYINGS.find(([re]) => re.test(q));
    if (!u) continue;
    const th = THRESHOLD_RE.exec(q);
    if (!th) continue;
    const comparator = /above|reach|hit|over|exceed/i.test(q) ? 'ABOVE' : 'BELOW';
    const tokens = JSON.parse(m.clobTokenIds || '[]');
    if (tokens.length < 2) continue;
    const expiry = m.endDate ? new Date(m.endDate).toISOString() : '';
    const lifeMs = m.endDate ? new Date(m.endDate).getTime() - now : 0;
    if (lifeMs < 48 * 3600 * 1000) {
        skippedExpired.push(`${u[1]} ${q.slice(0, 40)} (剩 ${Math.round(lifeMs / 3600000)}h)`);
        continue;
    }
    picked.push({
        slug: m.slug || m.id,
        gamma_id: String(m.id),
        underlying: u[1],
        threshold: Number(th[1].replace(/,/g, '')),
        comparator,
        expiry,
        resolution_source: m.resolutionSource || 'uma',
        volume24h: m.volume24hr ?? 0,
        yes_token: tokens[0],
        no_token: tokens[1],
        question: q.slice(0, 90),
    });
    if (picked.length >= 6) break;
}

const lines = [
    '# Cross-Market v2.2 市场映射（2026-11-19 从 Gamma 实拉生成）。',
    '# 规则：只有登记在此且未过期的市场可研究；primary_horizon 预注册唯一主视界；',
    '# 未登记/已过期市场不可研究（fail-closed）。',
    '',
];
for (const p of picked) {
    lines.push(
        `${p.slug}:`,
        `  gamma_id: "${p.gamma_id}"`,
        `  underlying_asset: ${p.underlying}`,
        `  event_threshold: ${p.threshold}`,
        `  event_comparator: ${p.comparator}`,
        `  event_expiry: '${p.expiry}'`,
        `  resolution_source: ${p.resolution_source}`,
        `  target_horizons: [30s, 1m, 5m, 15m, 1h]`,
        `  primary_horizon: 5m`,
        `  clob_token_ids: ["${p.yes_token}", "${p.no_token}"]`,
        `  # ${p.question} | 24h量 $${Number(p.volume24h).toLocaleString()}`,
        '',
    );
}
writeFileSync('config/market_mapping.yaml', lines.join('\n'), 'utf8');
console.log(`生成 ${picked.length} 个市场映射:`);
for (const p of picked) console.log(`  ${p.underlying} ${p.comparator} $${p.threshold} (${p.slug.slice(0, 40)})`);
console.log(`跳过（48h 内到期）${skippedExpired.length} 个:`);
skippedExpired.slice(0, 4).forEach((s) => console.log(`  - ${s}`));

