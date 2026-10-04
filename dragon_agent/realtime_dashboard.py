"""Local real-time research report; self-contained and safe against HTML injection."""
import json
from datetime import datetime
import math
from pathlib import Path
import re
from zoneinfo import ZoneInfo

from .dashboard import TEMPLATE, save_html


CONNECTION_FIELDS = ("source", "status", "checked_at", "auth_verified", "snapshot_valid",
                     "symbol", "price", "quote_asof", "freshness")
CONNECTION_RECORD_MAX_AGE_SECONDS = 300
_FRESHNESS_LABELS = {
    "unverified": "批次行情时效尚未验证",
    "stale_snapshot": "验证时报价快照已过期",
    "market_inactive_latest_session_snapshot": "非交易时段，最近交易日快照",
    "recent_snapshot_per_symbol_unverified": "验证时批次较新，逐股时效未验证",
}
_FAILED_STATUSES = {"credential_missing_or_invalid", "auth_failed", "data_not_ready", "rate_limited",
                    "api_error", "network_error", "http_error", "invalid_response"}


def _connection_timestamp(value):
    if not isinstance(value, str) or len(value) > 100:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed.astimezone(ZoneInfo("Asia/Shanghai"))
    except (ValueError, TypeError, OverflowError):
        return None


def connection_summary(connection=None, now=None):
    """Allowlisted display evidence; never copy errors, secrets or raw responses.

    This describes the most recent diagnostic, not the current connectivity or
    eligibility of any recommendation. A five-minute expiry marks history only.
    """
    current = now or datetime.now(ZoneInfo("Asia/Shanghai"))
    summary = {"state": "unverified", "label": "最近连接未验证", "checked_at": None,
               "symbol": None, "price": None, "quote_asof": None,
               "freshness": "unverified", "freshness_label": _FRESHNESS_LABELS["unverified"],
               "record_freshness": "unverified", "record_note": "尚无可用的本地连接验证记录",
               "max_age_seconds": CONNECTION_RECORD_MAX_AGE_SECONDS}
    if not isinstance(connection, dict) or connection.get("source") != "hithink":
        return summary
    checked = _connection_timestamp(connection.get("checked_at"))
    quote = _connection_timestamp(connection.get("quote_asof"))
    summary["checked_at"] = checked.isoformat() if checked else None
    summary["quote_asof"] = quote.isoformat() if quote else None
    symbol = connection.get("symbol")
    if isinstance(symbol, str) and re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", symbol):
        summary["symbol"] = symbol
    price = connection.get("price")
    if type(price) in (int, float):
        try:
            if math.isfinite(price) and 0 < price <= 1_000_000_000:
                summary["price"] = price
        except OverflowError:
            pass
    freshness = connection.get("freshness")
    if isinstance(freshness, str) and freshness in _FRESHNESS_LABELS:
        summary["freshness"] = freshness
        summary["freshness_label"] = _FRESHNESS_LABELS[freshness]
    if checked is None:
        summary["record_note"] = "验证记录缺少有效的含时区时间，不能确认结果"
        return summary
    if checked > current or (quote is not None and (quote > current or quote > checked)):
        summary.update(record_freshness="invalid", record_note="验证或报价时间位于未来，记录不能作为有效连接证据",
                       freshness="unverified", freshness_label="时间证据无效，行情时效未验证")
        return summary
    age = (current - checked).total_seconds()
    summary["record_freshness"] = "expired" if age > CONNECTION_RECORD_MAX_AGE_SECONDS else "recent"
    summary["record_note"] = ("历史验证记录（已超过 5 分钟）；需重新验证连接"
                               if age > CONNECTION_RECORD_MAX_AGE_SECONDS else "5 分钟内的连接验证记录；不代表持续联网或逐股实时行情")
    if (connection.get("status") == "connected" and connection.get("auth_verified") is True
            and connection.get("snapshot_valid") is True and summary["symbol"] is not None
            and summary["price"] is not None and quote is not None):
        summary.update(state="passed", label="最近连接验证通过")
    elif isinstance(connection.get("status"), str) and connection["status"] in _FAILED_STATUSES:
        summary.update(state="failed", label="最近连接验证失败")
    else:
        summary["record_note"] = "验证记录字段不完整；尚不能确认连接和报价均通过"
    return summary


def write_realtime_dashboard(path, report, refresh_seconds=0, connection=None):
    payload = json.dumps(report, ensure_ascii=False, allow_nan=False).replace("<", "\\u003c")
    payload = payload.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    style = TEMPLATE.split("<style>", 1)[1].split("</style>", 1)[0]
    refresh = f'<meta http-equiv="refresh" content="{int(refresh_seconds)}">' if refresh_seconds >= 30 else ''
    evidence = json.dumps(connection_summary(connection), ensure_ascii=False, allow_nan=False).replace("<", "\\u003c")
    html = PAGE.replace("__STYLE__", style).replace("__PAYLOAD__", payload).replace("__REFRESH__", refresh)
    html = html.replace("__CONNECTION_PAYLOAD__", evidence)
    return save_html(path, html)


PAGE = r'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">__REFRESH__<title>实时选股 · 龙头研究 Agent</title><style>__STYLE__
.action{padding:3px 7px;border-radius:4px;font-size:11px;background:#20342f;color:var(--cyan)}.action.wait{background:#332d20;color:var(--yellow)}.action.avoid{background:#382b31;color:var(--red)}.sticky{position:sticky;top:0}.feature-table td,.feature-table th{white-space:normal}.feature-table td:nth-child(1){width:20%}.feature-table td:nth-child(2){width:17%}.feature-table td:nth-child(3){width:10%}.feature-table td:nth-child(4){width:53%}.help{font-size:11px;color:var(--muted);margin-top:6px}.details-grid{display:grid;grid-template-columns:1fr;gap:20px}.navigation{display:flex;gap:15px;align-items:center;flex-wrap:wrap}
.connection{margin:16px 0}.connection-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:12px}.connection-grid dt{font-size:11px;color:var(--muted);margin-bottom:5px}.connection-grid dd{margin:0;font-size:13px;overflow-wrap:anywhere}.connection-note{font-size:12px;color:var(--muted);margin-top:12px}.connection-result{font-size:15px;font-weight:600}
.v2-card{margin-top:20px}.v2-card[hidden]{display:none}.v2-score{font-size:16px;font-variant-numeric:tabular-nums}.v2-caption{margin-top:5px;color:var(--muted);font-size:11px;white-space:normal}.v2-stage{border-top:1px solid var(--line);padding-top:18px;margin-top:22px}.v2-stage-head{display:flex;align-items:center;gap:12px;flex-wrap:wrap}.v2-stage h3{margin:0}.v2-stage .warnings{margin-top:10px}.v2-groups{display:flex;gap:10px;flex-wrap:wrap;margin:15px 0}.v2-group{border:1px solid var(--line);border-radius:9px;padding:10px 12px;min-width:125px;background:var(--panel2)}.v2-group strong{display:block;margin-top:5px}.v2-factors td,.v2-factors th{white-space:normal;vertical-align:top}.v2-factors td:first-child{min-width:120px}.v2-factors td:nth-child(4){min-width:240px}.v2-factors td:last-child{min-width:170px;overflow-wrap:anywhere}.v2-veto{color:var(--red)}.v2-empty{padding:15px 0;color:var(--muted)}.navigation .button{text-decoration:none}.connection{background:linear-gradient(125deg,#112131,#101b27)}
</style></head><body><main class="shell"><header class="top"><div class="brand"><div class="logo">龙</div><div><div class="eyebrow">DRAGON / REALTIME RESEARCH</div><h1>实时选股观察台</h1></div></div><div><span class="badge" id="source"></span><div class="meta" id="time"></div></div></header><section class="hero"><div><div class="eyebrow">AUCTION · VOLUME · TURNOVER · PRIOR SESSION</div><h2>看今天的承接，核对昨天的封板。</h2><div class="sub">竞价强弱、量能、换手与上一交易日结构共同排序；点击股票展开全部依据。</div></div><div class="navigation"><a class="button" href="agent.html">Agent Console</a><button class="button" id="download">导出推荐依据 ↓</button><button class="button primary" id="reload">刷新报告 ↻</button></div></section>
<div class="banner" id="notice"></div><div class="metrics" id="metrics"></div>
<section class="card connection" aria-label="最近连接验证"><div class="card-head"><h2>数据接口连接</h2><span class="connection-result" id="connection-status">最近连接未验证</span></div><dl class="connection-grid"><div><dt>最近验证时间 / 上海时区</dt><dd id="connection-checked">—</dd></div><div><dt>验证用标的与报价 / CNY</dt><dd id="connection-quote">—</dd></div><div><dt>报价批次时间 / 上海时区</dt><dd id="connection-quote-time">—</dd></div><div><dt>验证时行情状态</dt><dd id="connection-freshness">尚未验证</dd></div></dl><p class="connection-note" id="connection-record">尚无可用的本地连接验证记录</p><p class="help">此处只显示最近一次只读连接验证。报价批次时间不等于逐股成交时间。连接通过不代表当前可交易；休市、推荐数据及风控门禁独立生效。</p></section>
<div class="toolbar"><input id="search" placeholder="搜索股票名称、代码或题材" aria-label="搜索"><select id="filter" aria-label="推荐状态"><option value="all">全部状态</option><option value="重点观察">重点观察</option><option value="等待确认">等待确认</option><option value="回避">回避</option></select><span class="muted" id="count"></span></div>
<article class="card"><div class="table-wrap"><table><thead><tr><th>股票 / 代码</th><th>研究结论</th><th>评分 / 覆盖率</th><th>竞价涨幅</th><th>竞价金额</th><th>竞价换手</th><th>当前量 / 昨量</th><th>盘中换手</th><th>昨日开板</th><th>昨日最终封板</th></tr></thead><tbody id="rows"></tbody></table></div><p class="help">— 表示接口没有提供或数据无效；缺失开板次数不会填成 0，竞价换手不会替代盘中换手。“覆盖率”是因子可用比例，不是盈利概率。</p></article>
<article class="card v2-card" id="v2-card" hidden><div class="card-head"><h2>V2.0 竞价与盘中评分</h2><span class="pill">独立研究评分 · 不改变推荐门禁</span></div><p class="sub" id="v2-note"></p><div class="table-wrap"><table><thead><tr><th>股票 / 代码</th><th>9:25 分数 / 证据覆盖</th><th>10:00 分数 / 证据覆盖</th><th>分数变化</th><th>阶段状态</th></tr></thead><tbody id="v2-rows"></tbody></table></div><p class="help">缺证据时，0 分表示尚未获得有效得分，不等于完整评估后得分低；缺失项目不会折算或补分。两个阶段证据不可比较时不显示分差。分数不代表胜率、买入指令或盈利承诺。</p></article>
<div class="details-grid full"><article class="card"><div class="card-head"><h2 id="detail-title">候选依据</h2><span class="pill">只读研究 · 不产生订单</span></div><div id="details" class="empty">选择上方一只股票查看逐项得分和数据缺口</div></article><article class="card"><h2>运行门禁与数据边界</h2><ul class="warnings" id="warnings"></ul><div class="status">真实接口缺少逐股行情时间戳和部分历史封板细节时，候选只能等待核验。券商实盘适配协议已预留，当前路由始终关闭。</div></article></div><footer class="footer"><span>REALTIME RESEARCH · NO ORDER ROUTING</span><span>排名不等于买入指令 · 不保证盈利 · 本页更新时间以采集记录为准</span></footer></main>
<script id="payload" type="application/json">__PAYLOAD__</script><script id="connection-payload" type="application/json">__CONNECTION_PAYLOAD__</script><script>'use strict';
const rawPayload=document.getElementById('payload').textContent,R=JSON.parse(rawPayload),$=id=>document.getElementById(id),esc=v=>String(v??'—').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const C=JSON.parse($('connection-payload').textContent);
const n=(v,d=1)=>typeof v==='number'&&Number.isFinite(v)?v.toFixed(d):'—',pp=v=>typeof v==='number'?n(v,2)+'%':'—',percent=v=>typeof v==='number'?n(v*100,0)+'%':'—';
const isExpired=()=>R.source!=='demo'&&(!Number.isFinite(Date.parse(R.asof))||Date.now()-Date.parse(R.asof)>(R.recommendation_config.max_age_seconds||180)*1000);
const actionFor=x=>isExpired()&&x.action==='重点观察'?'等待确认':x.action;
const actionClass=x=>x==='重点观察'?'':x==='回避'?'avoid':'wait';
const hasV2=Boolean(R.model_v2)||R.candidates.some(x=>x.model_v2);
const stageReady=s=>s&&s.status!=='waiting';
const stageStatus=s=>!stageReady(s)?'等待采集':s.status==='vetoed'?'触发否决项':s.complete===true?'证据完整':'证据不足';
function stageScore(s){if(!stageReady(s))return '<span class="muted">等待采集</span>';return `<div class="v2-score">${n(s.score)} <small>/ 100</small></div><div class="v2-caption">证据覆盖 ${percent(s.coverage)} · ${esc(stageStatus(s))}</div><div class="v2-caption">${s.complete===true?'完整评估得分':'仅计已核验项目，缺失项未补分'}</div>`;}
function v2Delta(d){if(!d||d.comparable!==true||typeof d.value!=='number'||!Number.isFinite(d.value))return `<span class="muted">不可比较</span><div class="v2-caption">${esc(d?.reason||'等待两个阶段的可比证据')}</div>`;return `<div class="num">${d.value>0?'+':''}${n(d.value)} 分</div><div class="v2-caption">${esc(d.label||'可比阶段分差')}</div>`;}
function renderV2(rows){$('v2-card').hidden=!hasV2;if(!hasV2)return;$('v2-note').textContent=R.model_v2?.note||'分别核验 9:25 竞价与 10:00 盘中证据；点击股票查看各阶段分组与逐项依据。';$('v2-rows').innerHTML=rows.map(x=>{const m=x.model_v2||{};return `<tr class="pick" data-symbol="${esc(x.symbol)}"><td><div class="name">${esc(x.name)}</div><div class="code">${esc(x.symbol)}</div></td><td>${stageScore(m.auction_0925)}</td><td>${stageScore(m.intraday_1000)}</td><td>${v2Delta(m.delta)}</td><td><div>9:25 · ${esc(stageStatus(m.auction_0925))}</div><div class="v2-caption">10:00 · ${esc(stageStatus(m.intraday_1000))}</div></td></tr>`;}).join('')||'<tr><td colspan="5" class="empty">当前没有符合条件的候选；等待采集阶段证据</td></tr>';}
function stageDetail(title,s){if(!stageReady(s))return `<section class="v2-stage"><h3>${esc(title)}</h3><div class="v2-empty">等待采集：${esc(s?.status_text||'尚未到达该阶段，或尚无该阶段可用的证据快照')}</div></section>`;const vetoes=Array.isArray(s.vetoes)?s.vetoes:[],warnings=Array.isArray(s.warnings)?s.warnings:[],factors=Array.isArray(s.factors)?s.factors:[],missing=factors.filter(f=>f.available!==true);return `<section class="v2-stage"><div class="v2-stage-head"><h3>${esc(title)}</h3><span class="action ${s.status==='vetoed'?'avoid':s.complete===true?'':'wait'}">${esc(stageStatus(s))}</span><span class="muted">模型分层 ${esc(s.grade)}</span></div><p class="sub">${n(s.score)} / 100 · 证据覆盖 ${percent(s.coverage)} · 可核验项目满分 ${n(s.available_points,0)} / 100 · 缺失 ${n(s.missing_count,0)} 项</p><p class="help">${esc(s.status_text||stageStatus(s))} · 阶段记录 ${esc(s.asof)}</p>${s.complete===true?'':'<p class="help">当前分数只累计可核验项目；缺证据造成的 0 分不能解释为完整评分低。</p>'}${vetoes.length?'<div class="v2-veto"><strong>否决项</strong><ul class="warnings">'+vetoes.map(v=>'<li>'+esc(v)+'</li>').join('')+'</ul></div>':''}<div class="v2-groups">${(s.groups||[]).map(g=>`<div class="v2-group">${esc(g.label)}<strong>${n(g.score)} / ${n(g.max_score,0)}</strong><div class="v2-caption">可核验满分 ${n(g.available_points,0)}</div></div>`).join('')}</div><div class="table-wrap"><table class="v2-factors"><thead><tr><th>因子 / 分类</th><th>观测值</th><th>得分 / 满分</th><th>判定依据</th><th>来源与证据时点</th></tr></thead><tbody>${factors.map(f=>`<tr><td>${esc(f.label)}<div class="v2-caption">${esc(f.category)}</div></td><td class="num">${f.available===true?esc(typeof f.value==='object'?JSON.stringify(f.value):f.value):'—（缺证据）'}</td><td class="num">${n(f.score)} / ${n(f.max_score,0)}<div class="v2-caption">${f.available===true?'已核验':'未计分 · 缺证据'}</div></td><td>${esc(f.reason)}</td><td>${esc(f.source)}<div class="v2-caption">观测 ${esc(f.observed_at)}</div><div class="v2-caption">可用 ${esc(f.available_at)}</div></td></tr>`).join('')||'<tr><td colspan="5" class="empty">尚无逐项证据</td></tr>'}</tbody></table></div>${missing.length?'<h4>缺失项目</h4><ul class="warnings">'+missing.map(f=>'<li>'+esc(f.label)+'：'+esc(f.reason||'尚无可核验证据')+'</li>').join('')+'</ul>':''}${warnings.length?'<h4>阶段说明</h4><ul class="warnings">'+warnings.map(v=>'<li>'+esc(v)+'</li>').join('')+'</ul>':''}</section>`;}
function v2Detail(x){if(!hasV2)return '';const m=x.model_v2||{};return '<div class="stack"><h2>V2.0 分阶段证据</h2><p class="help">评分只作研究；一票否决可降级候选，不能打开订单路由。未到阶段显示等待采集。</p>'+stageDetail('9:25 竞价版',m.auction_0925)+stageDetail('10:00 盘中版',m.intraday_1000)+'</div>';}
function renderConnection(){const checked=Date.parse(C.checked_at),quote=Date.parse(C.quote_asof),now=Date.now(),invalid=(Number.isFinite(checked)&&checked>now)||(Number.isFinite(quote)&&quote>now),expired=Number.isFinite(checked)&&now-checked>C.max_age_seconds*1000;
$('connection-status').textContent=invalid?'最近连接未验证':C.label;$('connection-status').style.color=invalid||C.state==='unverified'?'var(--yellow)':C.state==='passed'?'var(--cyan)':'var(--red)';
$('connection-checked').textContent=C.checked_at||'—';$('connection-quote').textContent=C.symbol&&typeof C.price==='number'?C.symbol+' · '+n(C.price,2)+' 元':'—';$('connection-quote-time').textContent=C.quote_asof||'—';
$('connection-freshness').textContent=invalid?'时间证据无效，行情时效未验证':C.freshness_label;$('connection-record').textContent=invalid?'验证或报价时间位于未来，记录不能作为有效连接证据':expired?'历史验证记录（已超过 5 分钟）；需重新验证连接':C.record_note;}renderConnection();
$('source').textContent=R.source==='demo'?'合成演示 / 非真实推荐':R.source==='hithink'?'同花顺接口 / 实时研究':'导入特征 / 实时研究';$('time').textContent='采集 '+R.asof;
$('notice').textContent=R.source==='demo'?'当前演示使用完全合成的数据与额外模拟字段，仅验证排序和交互；不代表现在的选股结果。':isExpired()?'报告已过期：等待采集进程更新，原有重点观察已暂停。':R.blocked?'当前推荐已被数据或时段门禁阻止；可查看原因和已采集的研究特征。':'当前为研究观察名单。排名按可核验特征计算，任何缺失、过期或时点不明的数据都会降低覆盖率或阻止重点观察。';
const sessions={auction:'集合竞价',continuous:'连续竞价',lunch:'午间休市',preopen:'盘前等待',closed:'已休市',unknown:'待核验'};
function renderMetrics(){$('metrics').innerHTML=[['当前会话',sessions[R.session_status]||R.session_status,'上海时区 / 交易日历'],['观察标的',String(R.candidates.length),'来自上一交易日涨停与炸板池'],['重点观察',String(R.candidates.filter(x=>actionFor(x)==='重点观察').length),'因子与时间门禁共同通过'],['等待确认',String(R.candidates.filter(x=>actionFor(x)==='等待确认').length),'缺失字段或证据尚不充分']].map(x=>`<div class="metric"><h3>${x[0]}</h3><div class="value" style="font-size:25px">${esc(x[1])}</div><small>${x[2]}</small></div>`).join('');}renderMetrics();
function render(){const q=$('search').value.trim().toLowerCase(),filter=$('filter').value;const rows=R.candidates.filter(x=>(x.name+' '+x.symbol+' '+x.theme).toLowerCase().includes(q)&&(filter==='all'||actionFor(x)===filter));$('count').textContent=rows.length+' 个结果';$('rows').innerHTML=rows.map(x=>{const a=x.auction_usable?(x.auction||{}):{},p=x.previous||{};return `<tr class="pick" data-symbol="${esc(x.symbol)}"><td><div class="name">${esc(x.name)}</div><div class="code">${esc(x.symbol)}</div></td><td><span class="action ${actionClass(actionFor(x))}">${esc(actionFor(x))}</span></td><td><span class="num">${n(x.score)} / ${percent(x.confidence)}</span></td><td class="num">${pp(a.auction_pct)}</td><td class="num">${typeof a.auction_amount==='number'?n(a.auction_amount/1e4,0)+' 万':'—'}</td><td class="num">${pp(a.auction_turnover_pct)}</td><td class="num">${pp(x.volume_participation_pct)}</td><td class="num">${pp(x.current_turnover_pct)}</td><td class="num">${n(p.open_times,0)}</td><td class="num">${esc(p.final_seal_time)}</td></tr>`}).join('')||'<tr><td colspan="10" class="empty">当前没有符合条件的候选</td></tr>';renderV2(rows);}
function detail(symbol){const x=R.candidates.find(x=>x.symbol===symbol);if(!x)return;$('detail-title').textContent=x.name+' · '+x.symbol+' · '+actionFor(x);
$('details').className='';$('details').innerHTML=`<div class="sub">评分 ${n(x.score)} / 100 · 因子覆盖率 ${percent(x.confidence)} · 标准化量速 ${n(x.normalized_volume_pace,2)}（线性时间启发式，非标准量比）</div><div class="table-wrap stack"><table class="feature-table"><thead><tr><th>因子</th><th>观测值</th><th>得分</th><th>口径 / 判断依据</th></tr></thead><tbody>${(x.factors||[]).map(f=>`<tr><td>${esc(f.label)}</td><td class="num">${esc(typeof f.value==='object'?JSON.stringify(f.value):f.value)}</td><td class="num">${n(f.score,1)} / ${n(f.max_score,0)}</td><td class="muted">${esc(f.reason)}</td></tr>`).join('')}</tbody></table></div><div class="grid stack"><div><h3>推荐依据</h3><ul class="warnings">${(x.reasons||[]).map(v=>'<li>'+esc(v)+'</li>').join('')}</ul></div><div><h3>缺口与排除项</h3><ul class="warnings">${[...(x.exclusions||[]),...(x.missing_factors||[])].map(v=>'<li>'+esc(v)+'</li>').join('')||'<li>无因子缺口，仍然仅供研究观察</li>'}</ul></div></div>`+v2Detail(x);}
$('search').oninput=render;$('filter').oninput=render;render();document.addEventListener('click',e=>{const row=e.target.closest('[data-symbol]');if(row)detail(row.dataset.symbol)});if(R.candidates.length)detail(R.candidates[0].symbol);
$('warnings').innerHTML=[...(R.blockers||[]),...(R.warnings||[])].map(v=>'<li>'+esc(v)+'</li>').join('');setInterval(()=>{renderConnection();if(isExpired()){$('notice').textContent='报告已过期：等待采集进程更新，原有重点观察已暂停。';render();renderMetrics();$('detail-title').textContent='报告已过期 · 请刷新后重新核验候选';}},15000);$('reload').onclick=()=>location.reload();$('download').onclick=()=>{const url=URL.createObjectURL(new Blob([rawPayload],{type:'application/json'}));const a=document.createElement('a');a.href=url;a.download='realtime-research-'+R.trading_date+'.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000)};
</script></body></html>'''
