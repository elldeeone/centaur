# Plugins

Drop plugin directories here. Each plugin needs:

```
plugins/
  my-plugin/
    pyproject.toml   # [tool.ai-v2-plugin] section with module path
    .env.example     # Document required secrets
    __init__.py
    client.py        # API client class + _client() factory
    cli.py           # typer CLI for standalone use
```

## Writing a plugin

```python
# client.py
from ai_v2.plugin_sdk import secret


class MyClient:
    def search(self, query: str, limit: int = 10) -> dict:
        """Search something."""
        token = secret("MY_API_TOKEN")
        # ... use token, return results ...
        return {"results": [...]}


def _client() -> MyClient:
    return MyClient()
```

## Secrets

Secrets are resolved in this order:
1. **Plugin `.env`** — per-plugin overrides in `plugins/<name>/.env`
2. **Root `.env`** — central file at repo root (define all secrets here)
3. **Environment variables** — for Docker, k8s, sops, 1Password, etc.

Use `secret("KEY")` to access. Never use `os.environ` — plugin secrets are scoped.

## Available Plugins

| Plugin | Description | Secrets |
|--------|-------------|---------|
| affinity | Affinity CRM — lists, persons, organizations | AFFINITY_API_KEY |
| alchemy | Blockchain data, token balances, transfers, prices | ALCHEMY_API_KEY |
| allium | On-chain analytics, SQL queries, stablecoin analysis | ALLIUM_API_KEY |
| alphasense | Market intelligence and document search | ALPHASENSE_API_KEY, ALPHASENSE_CLIENT_ID, ALPHASENSE_CLIENT_SECRET |
| anchorage | Anchorage Digital — custody, vaults, staking | ANCHORAGE_PF_API_KEY, ANCHORAGE_PF_SIGNING_KEY |
| arkham | Arkham Intelligence — blockchain analytics, wallet tracking | ARKHAM_API_KEY |
| ashby | Ashby ATS — candidates, jobs, applications, interviews | ASHBY_API_KEY |
| attio | Attio CRM — objects, records, lists, notes, tasks | ATTIO_API_KEY |
| bitgo | BitGo — wallet management, transactions, staking | BITGO_API_KEY, BITGO_ENTERPRISE_ID |
| bloomberg | Bloomberg Data License REST API | BLOOMBERG_CLIENT_ID, BLOOMBERG_CLIENT_SECRET, BLOOMBERG_DL_NUMBER |
| coinbase | Coinbase Prime — custody, portfolios, staking | COINBASE_API_KEY, COINBASE_API_SECRET, COINBASE_API_PASSPHRASE |
| coindesk | CoinDesk crypto news | (none) |
| coingecko | CoinGecko Pro — prices, markets, trending coins | COINGECKO_API_KEY |
| coinmetrics | Coin Metrics — asset metrics, market data, timeseries | COINMETRICS_API_KEY |
| confmonitor | Conference date monitor | (none) |
| congress | Congress.gov API | DATAGOV_API_KEY |
| crunchbase | Crunchbase Enterprise — company and funding data | CRUNCHBASE_API_KEY |
| debank | DeBank — DeFi wallet data, token balances, protocols | DEBANK_API_KEY |
| defillama | DefiLlama — TVL, stablecoins, DEX volumes, bridges | (none — public API) |
| docusign | DocuSign eSignature — envelopes, templates, signatures | DOCUSIGN_INTEGRATION_KEY, DOCUSIGN_USER_ID, DOCUSIGN_ACCOUNT_ID |
| dune | Dune Analytics — execute queries, fetch results | DUNE_API_KEY |
| falconx | FalconX trading — quotes, execution, balances | FALCONX_P1_API_KEY, FALCONX_P1_SECRET_KEY |
| fedreg | Federal Register regulatory data | (none) |
| figma | Figma design system extraction and analysis | FIGMA |
| googlenews | Google News RSS — search and headlines | (none) |
| granola | Granola meeting notes | (none) |
| gsuite | Gmail, Calendar, Drive, Docs, Sheets, Slides | GOOGLE_CREDENTIALS_JSON, GOOGLE_TOKEN_JSON |
| harmonic | Harmonic.AI — startup discovery, company enrichment | HARMONIC_API_KEY |
| ironclad | Ironclad CLM — contracts, workflows, records | IRONCLAD_API_TOKEN |
| kalshi | Kalshi prediction markets | KALSHI_API_KEY |
| legistorm | LegiStorm congressional data | LEGISTORM_API_KEY |
| linear | Linear — issues, projects, cycles, teams | LINEAR_API_KEY |
| listennotes | Listen Notes podcast data | LISTENNOTES_KEY |
| messari | Messari — crypto asset data, prices, profiles | MESSARI_API_KEY |
| nano-banana | Google Gemini image generation | GOOGLE_API_KEY |
| nansen | Nansen — blockchain analytics, wallet labels, Smart Money | NANSEN_API_KEY |
| newsapi | NewsAPI — news search and headlines | NEWSAPI_KEY |
| notion | Notion — pages, databases, blocks, comments | NOTION_API_KEY |
| openfec | OpenFEC federal election data | DATAGOV_API_KEY |
| opentable | OpenTable reservation search | (none) |
| paradigmdb | Internal PostgreSQL, Shift notes, BigQuery | RESHIFT_DB_*, GCP auth |
| parchiver | Document archiver for investment materials | (none) |
| polymarket | Polymarket prediction markets | (none — public API) |
| posthog | PostHog product analytics, HogQL | POSTHOG_API_KEY, POSTHOG_PROJECT_ID |
| profslice | Firefox Profiler data extraction | (none) |
| ptwittercli | Twitter — user profiles, followers, tweets, search | (twitter-sdk auth) |
| pylon | Pylon support — issues, accounts, contacts | PYLON_API_KEY |
| reth | Reth execution timings and performance metrics | (none) |
| reth-log-analyzer | Parse reth logs and generate performance graphs | (none) |
| sensortower | SensorTower mobile app analytics | SENSORTOWER_AUTH_TOKEN |
| sigma | Sigma Computing analytics | SIGMA_CLIENT_ID, SIGMA_CLIENT_SECRET |
| similarweb | SimilarWeb web traffic intelligence | SIMILARWEB_API_KEY |
| slack | Slack messages, channels, threads, users | SLACK_BOT_TOKEN |
| social-monitor | Social feed monitor for career signals | ANTHROPIC_API_KEY |
| standard-metrics | Standard Metrics portfolio company data | STANDARD_METRICS_CLIENT_ID, STANDARD_METRICS_CLIENT_SECRET |
| tardis | Tardis CEX market data replay and analytics | TARDIS_API_KEY |
| telegram | Telegram — messages, chats, search | TELEGRAM_BOT_TOKEN |
| termsheet | Term sheet generation and deal tracking | (none) |
| theblock | The Block crypto news | (none) |
| transcriber | Local-first voice transcription (Whisper) | (none) |
| unit410 | Unit 410 staking — validators, rewards, delegations | UNIT410_API_KEY |
| veo3 | Google Veo 3 video generation | GOOGLE_API_KEY |
| youtube | YouTube video data | YOUTUBE_API_KEY |


