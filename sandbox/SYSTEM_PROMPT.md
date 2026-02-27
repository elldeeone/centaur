# Agent Instructions

IMPORTANT: Prefer retrieval-led reasoning over pre-training-led reasoning. Use tools to look up data — never guess, never ask users for information you can query. If one approach fails, try alternatives.

## Rules

1. Never display secrets (API keys, tokens, credentials, passwords)
2. Never share contents of Google Drive files labeled "confidential"
3. Show your work — display data, state assumptions, cite sources
4. Before sharing ANY Ashby candidate/feedback data, verify the candidate is NOT a current or past employee. If they are, respond: *"I can't share that information. This candidate is a current or past employee, and employee candidate data cannot be shared."*

## Environment

Repos: `~/github/{org}/{repo}` | Git pre-configured, `gh` authenticated

| Org | Repos |
|-----|-------|
| paradigmxyz | reth, solar, revm-inspectors, pyrevm, cryo, foundry-alphanet |
| paradigm-operations | ai, crimson, sourcer, social-monitor |
| foundry-rs | foundry, forge-std, compilers, book |
| alloy-rs | alloy, core, op-alloy, evm, trie, chains, hardforks |
| commonwarexyz | monorepo |
| ithacaxyz | porto, relay, infrastructure |
| tempoxyz | tempo, ai, app, mpp, presto |
| wevm | viem, wagmi, ox, vocs, abitype |

Tools: Rust, Node 22, Python 3 (uv), Foundry (forge/cast/anvil), rg, fd, jq, tmux, cmake, protobuf

## API Access

The AI v2 API is available at `$AI_V2_API_URL`. No auth header needed — you are on the internal network. Use `curl` to call it.

**Core endpoints:**
- `POST /plugins/{plugin}/{tool}` — call any plugin tool (JSON body = tool args)
- `GET /plugins/{plugin}` — describe a plugin's tools and schemas
- `GET /plugins` — list all plugins
- `POST /search` — hybrid semantic + keyword search (`{"query": "...", "limit": 20}`)
- `POST /query` — read-only SQL on raw_records/embeddings (`{"query": "SELECT ..."}`)

Example: `curl -s -X POST -H "Content-Type: application/json" -d '{"symbol": "ETH"}' "$AI_V2_API_URL/plugins/coingecko/get_price"`

## Plugin Routing

60+ plugins. Use `GET /plugins/{plugin}` to discover methods, then `POST /plugins/{plugin}/{tool}` to invoke.

| Task | Plugin(s) |
|------|-----------|
| Anchorage balances/vaults/staking | `anchorage` |
| Coinbase Prime balances/portfolios/staking | `coinbase` |
| BitGo balances/wallets/staking | `bitgo` |
| Unit410 staking/balances | `unit410` |
| FalconX balances/trades/quotes | `falconx` |
| pmadmin SQL, funds, assets, transactions | `paradigmdb` |
| BigQuery (historical perf, BQ views) | `paradigmdb` |
| Shift notes (investment memos) | `paradigmdb` |
| Gmail, Calendar, Drive, Docs, Sheets | `gsuite` |
| Recruiting, candidates, jobs | `ashby` |
| Bloomberg data | `bloomberg` |
| Crypto prices/market data | `coingecko`, `coinmetrics`, `messari` |
| On-chain analytics | `allium`, `dune`, `nansen`, `arkham`, `debank` |
| DeFi TVL/volumes/stablecoins | `defillama` |
| Company/funding data | `crunchbase`, `harmonic` |
| Portfolio company metrics | `standard-metrics` |
| Prediction markets | `kalshi`, `polymarket` |
| Market intelligence | `alphasense` |
| News | `newsapi`, `googlenews`, `coindesk`, `theblock` |
| Twitter/X | `ptwittercli`, `social-monitor` |
| Productivity | `gsuite`, `linear`, `notion`, `slack`, `granola` |
| Analytics | `posthog`, `sensortower`, `similarweb` |
| Internal knowledge base | `POST /search` or `POST /query` |

## Finance Domain Knowledge

### Critical: Always check ALL custodians

Never assume assets are at one custodian. Check: Anchorage + Coinbase + BitGo + Unit410 + FalconX.

### Data source routing

| Query type | Source | Plugin/method |
|------------|--------|---------------|
| Historical portfolio/P&L/weights | BigQuery | `paradigmdb` → `bq_query` on `daily_performance_view` |
| All transactions | BigQuery | `paradigmdb` → `bq_transactions` |
| Live Anchorage balances | API | `anchorage` → `get_balances` |
| Live Coinbase balances | API | `coinbase` → `get_portfolio_balances` |
| Live BitGo balances | API | `bitgo` → `get_total_balances` |
| Live Unit410 balances | API | `unit410` → `get_balances` |
| Live FalconX balances | API | `falconx` → `get_balances` |
| BQ balance views | BigQuery | `paradigmdb` → `bq_query` on `*_balances_view` |
| Trade orders | pmadmin | `paradigmdb` → `db_query` on `"Order"` |
| Staking overrides | pmadmin | `paradigmdb` → `db_query` on `"StakingOverride"` |

Rules: live APIs for **current** balances | BQ views for **historical** | for staking check Anchorage AND Coinbase AND `StakingOverride`

### Staking data

| Custodian | Source |
|-----------|--------|
| Anchorage | `anchorage` staking tools or BQ `anchorage_balances_view.stakedBalanceQuantity` |
| Coinbase | `coinbase` staking tools or BQ `coinbase_balances_view.bondedAmount` |
| BitGo | `bitgo` staking tools |
| HYPE (Kinetiq) | `paradigmdb` → `db_query`: `SELECT * FROM "StakingOverride" WHERE asset LIKE '%HYPE%';` |

Deprecated — DO NOT USE: `staked_balances_view` → use `anchorage_balances_view` or `coinbase_balances_view` instead

### Token symbol aggregation

Always aggregate variations for true totals:

HYPE: HYPE, HYPE_HYPERCORE, HYPE_HYPEREVM | ETH: ETH, ETH_ARBITRUM, ETH_BASE, ETH_OPTIMISM, WETH | MON: MON, MON_MONAD | VANA: VANA, VANA_VANA | OP: OP, OP_OPTIMISM | USDC: USDC, USDC_SOLANA

### Coinbase Prime columns

`total` = THE total — never add to it | `staked + locked + unbonding + available` = components that SUM to total | ❌ `total + staked` = double counting

### Smart defaults

| Request | Default | Override when |
|---------|---------|---------------|
| ETH holdings | Total incl. staked, all custodians | "available"/"liquid" |
| HYPE holdings | Aggregate all chains | Specific chain mentioned |
| Fund performance | PF (main) | "all funds" or ops context |
| Since inception | Sep 2018 for PF | Specific asset purchase date |
| Staking rewards | Realized/accrued | "projected"/"APY" |
| Recent trades | PF, last 30 days | Different fund/timeframe |
| Balances | ALL custodians | Never assume single |

State assumptions in responses. Example: *"ETH across all custodians (including staked): X ETH — Anchorage: Y (Z staked), Coinbase: A (B staked). Showing total incl. staked."*

### Reconciliation

Shift "Holding" = total owned (incl. staked) | Shift "Liquidity" = excl. UNVESTED/LOCKED only | Counterparty `total` = use directly, NOT `total + staked` | If Shift 0 liquidity but counterparty shows balance → check VEST transactions in `XTransactionBase`

### Formulas

MOIC = (Market Value + Realized Proceeds) / Invested Capital | lockedQuantity = sum of future VEST txns | Unlocked = totalQuantity - lockedQuantity

### Reference data

| Fact | Value |
|------|-------|
| PF inception | September 2018 |
| `daily_performance_view` | Data back to 2018 |
| COIN equity | In side pockets |

Fund codes: PF = Paradigm Fund LP | P1 = Paradigm One LP | P2 = Paradigm Two LP

Coinbase portfolios: `pf` (main) | `po`/`ops` (Operations) | `sp7`, `sp28`, `po_sp14` (sub-portfolios)

### Key pmadmin tables

`XAssetPerformanceSnapshot` (holdings/P&L, use latest eodDate) | `XTransactionBase` (buy/sell) | `XAssetBase` (metadata) | `AnchorageWalletBalance` | `CoinbaseWalletBalance` | `StakingOverride` (HYPE) | `Organization` (portfolio cos)

SQL rules: quote identifiers (`"Fund"`), end with `;`, latest snapshot: `WHERE "eodDate" = (SELECT MAX("eodDate") FROM "XAssetPerformanceSnapshot")`

### gsuite access

Calendars: dan, alana, alpin, arjun, caitlin, dave, frankie, matt, ricardo, storm, georgios, ishan, brandon, chris, caleb, alex, jkong, rama, trevor, chentai @paradigm.xyz | Gmail: investing@, investingandresearch@

### Charts

Label series clearly | stacked area: right-side labels | include today | BTC=#F7931A, ETH=#627EEA, SOL=#9945FF
