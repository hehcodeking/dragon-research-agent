# Internal module contract

Python 3.11+, standard library only. Paths rooted in this folder. Modules use plain JSON dictionaries with strict validation, ISO dates, timezone-aware Asia/Shanghai timestamps; money CNY, volume shares, percentage fields in percentage points. No silent real-to-demo fallback.

## Snapshot v1

`{schema_version:1, source:"demo"|"hithink"|"imported", trading_date:"YYYY-MM-DD", asof:"ISO timestamp", available_at:"ISO timestamp", coverage:{complete:bool, issues:[str]}, market:{limit_up_count:int|null, limit_down_count:int|null, broken_count:int|null, max_height:int|null}, stocks:[...], provenance:{...}}`

Each stock: `symbol` full thscode, `name`, `price`, `prev_close`, `change_pct`, `turnover` (CNY), `turnover_pct`, `board_count`, `seal_money`, `max_seal_money`, `theme` (pool reason is only a proxy), `first_limit_time`, `is_st`, `is_new`, `suspended`, `quote_asof`, `limit_up`, `limit_down`. Optional/unavailable numbers and booleans must remain null. Values such as theme/board/seal come from limit-up pool and quote enrichment may be unavailable. Never infer missing ST/new as false. Preserve complete pool market counts, even if stocks are capped for quote enrichment. `coverage.complete` refers to all market pools, not quote availability. `available_at` cannot precede `asof`. Real historical pool availability is conservatively capture time unless independently recorded point-in-time data exist. Do not backdate capture availability. Only main-board `.SH` 600/601/603/605 or `.SZ` 000/001/002/003 supported for hypothetical execution; ranking can explain other exclusions.

## Adapter (owner: data agent)

`dragon_agent/data.py`: `HithinkClient(api_key=None, ...)`, `collect_snapshot(client, trading_date:str, max_symbols:int=30) -> dict`, `credential_status() -> dict` (presence/source only), `load_snapshot(path) -> dict`, `save_json(path, value)`. Class client offers `get(path, params=None)`, bounded retries/pagination. Document actual signature if different. Read credentials environment then documented user credential location without exposing values. Capture raw response evidence to optional local cache if useful. Tests in `tests/test_data.py`. Data adapter need not import strategy. No edits elsewhere.

## Strategy (owner: root)

`dragon_agent/strategy.py`: `analyze(snapshot, config=None, now=None, historical=False) -> dict`. Return report with regime, market stats, ranked candidates including reasons/exclusions, and research plans. No live orders. Configuration defaults in `config.py`. `report['candidates']` entries contain `symbol`, `name`, `score`, `eligible`, `reasons`, `exclusions`, `target_weight` (0..1), plus original feature fields. `report['regime']` contains `name`, `exposure_cap` (0..1), `allow_entries` bool. `report['blocked']` bool for structural/time/coverage failures. `historical=True` disables wall-clock stale check only, still rejects information available after decision asof; decisions use snapshot.available_at, preserve evidence.

## Simulation (owner: execution agent)

`dragon_agent/execution.py`: persistent `PaperBroker` API choose and document in module; main-board only, T+1, idempotent order IDs, cash/positions, fee/slippage, unknown limits reject, locked-board/suspension rejection, stale/future quotes validation, risk kill switch, max daily loss/max drawdown checks based on marked equity. Broker extension interface + disabled live adapter; no network order routing. Tests `tests/test_execution.py`.

`dragon_agent/backtest.py`: `replay(snapshots:list[dict], config:dict|None=None) -> dict`; import strategy lazily if necessary. Signals at snapshot available_at, fills strictly later on next provided session open. Snapshot stock optional OHLC fields `open`, `high`, `low`, `volume`; use next snapshot daily open as execution only, never next-day features to decide buys. Use explicit `limit_up`/`limit_down`, suspension and availability gates. Missing required execution fields skip, missing held symbol retains mark with explicit stale mark failure / block new buys. Liquidation exits for stop/trailing/max holding or regime, all based only on prior close then next open. Report equity curve, order audit, total return, max drawdown, fees, skipped counts, assumptions. This is daily replay only, no minute/backtest profitability claim. Independent frame validation if strategy unavailable. Demo fixtures root provides.

## Integration (owner: root)

CLI, demo fixture, README, dashboard HTML generator, integration tests. Other agents send module API and limitations when complete. Avoid editing shared files except assigned files. Runtime Python `/Users/heh/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3`.
