# 同花顺数据适配边界

实现按用户提供的 `Financial-API-main/docs/api/` 契约编写，仅用 Python 标准库。没有执行下载包内的安装、登录、更新或 Key 签发指令。此版本已通过离线接口形状和失败路径测试，尚未使用真实 Key 做联网验收。

## 接口与调用

```python
from dragon_agent.data import HithinkClient, collect_snapshot, save_json

client = HithinkClient()  # 不把 Key 写进源码
snapshot = collect_snapshot(client, "2026-09-30", max_symbols=30)
save_json("data/snapshot-2026-09-30.json", snapshot)
```

`HithinkClient.get(path, params=None)` 返回成功信封中的 `data` 字典。成功必须同时满足 HTTP 200、整数 `code == 0`、`data` 是对象。`ApiError` 包含安全的 `code` 和 `request_id`；API 的原始 message、HTTP 异常正文和请求头不会被展示。认证错误、参数错误和数据未就绪不会重试；网络错误、HTTP 429/5xx、业务 4001/5xxx 使用有界退避。默认重试 2 次，最多允许重试 3 次。所有调用都为 GET，只接受相对 `/api/` 路径，固定主机 `https://fuyao.aicubes.cn`，拒绝全部重定向，Key 只置于 `X-api-key` 请求头。

凭据顺序为 `HITHINK_FINANCE_API_KEY` 环境变量，然后官方用户凭据文件。macOS 为 `~/Library/Application Support/hithink-finance/credentials.env`；Linux 为 `$XDG_CONFIG_HOME/hithink-finance/credentials.env`（默认 `~/.config`）；Windows 为 `%APPDATA%/hithink-finance/credentials.env`。文件只读取无引号的 `HITHINK_FINANCE_API_KEY=...`，从不作为 shell 执行。为避免错用其他服务密钥，本适配器不读取通用 `API_KEY`。`credential_status()` 仅返回存在状态和来源，不返回任何 Key 片段。

| 端点 | 使用方式 | 限制 |
|---|---|---|
| `/api/a-share/calendar/trading-days` | 无参数，核对所选日期和 date/date_ms 一致性 | 契约只覆盖近一年；窗口之外为未知，不用工作日推算交易日 |
| `/api/a-share/special-data/limit-up-pool` | 上海零点 date_ms，全量分页 | 200 条/页，最多 40 页；核对总数、页数、页大小、重复代码、页间时间戳 |
| `/api/a-share/special-data/limit-down-pool` | 同上 | 缺失或错误不会补成 0 |
| `/api/a-share/special-data/limit-break-pool` | 同上 | 缺失或错误不会补成 0 |
| `/api/a-share/prices/snapshot` | 仅当采集日等于所选交易日；按池内完整 thscode 批量请求 | 首版最多 100 只，默认 30；不传不存在的日期、排序或分页参数 |

先完成全市场三个股票池，再按连板数、封单额、完整代码排序截取研究列表。因此 `market.limit_up_count` / `limit_down_count` / `broken_count` 保留完整市场口径，不会变成 30 只样本统计。池分页不完整时对应计数为 `null`，并设置 `coverage.complete=false`。日历未知也会阻止完整性确认。行情缺失与市场池覆盖分开：不会因为行情失败伪造市场计数，也不会切换为模拟数据。

## 时间与复盘

- `asof` 是已验证日期一致的池/行情批次中最新数据时间；各池原始时间戳保存在 provenance。
- `available_at` 和 `captured_at` 是本次采集结束的真实时钟时间。今天下载的历史股票池不能写成历史当天已知，不能直接用于历史无前视回测。
- 历史股票池不会拼接今日行情，也不会用最新前复权序列补出历史事实。无独立逐时记录时，真实历史可得性未被证明。
- `prices/snapshot.data.timestamp` 只是整批中最新上游有效时间。它保存为 `batch_quote_asof`，不会伪装成逐股 `quote_asof`。逐股 `quote_asof` 保留 `null`。
- 任一使用中的时间戳晚于本机采集结束时刻将直接报错。时间戳缺失、跨日错配、分页期间变更都会形成数据质量问题。
- 连板天梯每档最多 4 只且含次日封板字段 `seal_nextday`，因此未接入排名。未来扩展不能把该字段用于当日信号。
- 竞价快照时间戳是响应组装时间，也不具备历史日期参数；没有可信逐股行情时间时不把它作为已通过新鲜度校验的订单依据。竞价风向标可以按日期取数，但同样需要保存采集可得时间。

## 字段和执行边界

钱以元（CNY）保存，成交量为股，百分比字段保留百分数原值，例如 `10.0` 表示 10%，不会再乘或除 100。空值保持 `null`，不会将未知 ST、新股或停牌状态写成 false。`theme` 只是涨停原因代理，不是经过核实的完整题材分类。

池的 `is_new` 仅指“未开板新股”，不等于上市前 5 交易日等完整涨跌幅限制资格。快照没有停牌状态、明确涨跌停价格和逐股数据时间，因此真实采集股票始终 `execution_verified=false`，`suspended`、`limit_up`、`limit_down`、`quote_asof` 为空。研究分数或条件仓位不代表可成交。未来券商/可信行情适配器必须补全这些事实，并核实上市资格，才有资格进入模拟或实盘执行门禁。

`save_json()` 使用 UTF-8、原子替换，拒绝 NaN/Infinity。`load_snapshot()` 检查 schema、时区、可得时间顺序、完整代码、重复标的和主要类型；更严格的策略时序和执行约束由相应模块再次验证。快照及审计不保存 Key。不存在已真实联网验证的隐含保证。

## 离线验证

`tests/test_data.py` 覆盖接口成功/错误信封、HTTP 200 业务失败、重试上限、重定向拒绝、错误信息脱敏、凭据优先级、分页漂移和缺页、日期错配、空值单位、样本上限不影响市场总数、历史行情防串日、采集时间不回填、JSON 存取等路径。没有发出网络请求。

## 2026-10-04 真实联调修订

- 股票池 `timestamp` 在历史 date_ms 查询中返回当前组装时间。现在仅作为响应可用时间，查询交易日由明确请求参数保留，不再推断行情新鲜度；当前累计换手原值保留但缺行情时戳时不参与实时评分。
- 日历 date_ms 实际符合上海零点。节假日通过当日日历响应的窗口覆盖确认，组装时钟只容许同日最多2秒偏差。
- 竞价量单位必须由量价额一致性核验，不自动把看似手数的原值乘100。
- 9:25/10:00规则来源与接口数据是两种输入；模型原文不授予外部操作或券商下单权限。
