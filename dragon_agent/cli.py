"""Local CLI. Remote commands read market data; no command routes live orders."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import sys
import time

from .config import load_config
from .data import DataError, HithinkClient, SHANGHAI, credential_status, load_snapshot, save_json
from .strategy import analyze
from .dashboard import write_dashboard


def read_json(path):
    def reject(value):
        raise ValueError("JSON 含非有限数值")
    return json.loads(Path(path).read_text(encoding="utf-8"), parse_constant=reject)


def print_summary(value):
    print(json.dumps(value, ensure_ascii=False, allow_nan=False))


def read_connection_record(path):
    """Load bounded local display evidence without surfacing raw file errors."""
    from .realtime_dashboard import CONNECTION_FIELDS

    def reject(value):
        raise ValueError("nonfinite JSON")
    try:
        with Path(path).open(encoding="utf-8") as handle:
            raw = handle.read(65537)
        if len(raw) > 65536:
            return None
        value = json.loads(raw, parse_constant=reject)
        if not isinstance(value, dict):
            return None
        # The renderer independently validates the values of these fields.
        return {key: value[key] for key in CONNECTION_FIELDS if key in value}
    except (OSError, UnicodeError, ValueError, RecursionError):
        return None


def _research_files(out, report, snapshot=None, backtest=None):
    out = Path(out)
    save_json(out / "report.json", report)
    if snapshot:
        save_json(out / "snapshot.json", snapshot)
    if backtest:
        save_json(out / "replay.json", backtest)
    write_dashboard(out / "index.html", report, backtest)
    return {"report": str((out / "index.html").resolve()), "source": report["source"],
            "blocked": report["blocked"], "candidates": len(report["candidates"]),
            "research_eligible": sum(x["eligible"] for x in report["candidates"])}


def parser():
    p = argparse.ArgumentParser(description="龙头研究 Agent · 实时观察 / 日线研究 / 模拟交易 · 无实盘下单")
    p.add_argument("--config", help="配置 JSON 文件（见 config.example.json）")
    sub = p.add_subparsers(dest="command", required=True)
    d = sub.add_parser("demo", help="生成明确标注的合成演示与离线工作台")
    d.add_argument("--out", default="reports/demo")
    d.add_argument("--open", action="store_true", help="在默认浏览器打开工作台")
    d = sub.add_parser("diagnose", help="只检查凭据存在状态和禁用的券商接口")
    d = sub.add_parser("auth", help="在本机终端隐藏输入 API Key 并保存到用户凭据目录")
    d.add_argument("--replace", action="store_true", help="明确替换已存在的用户凭据文件")
    d = sub.add_parser("check-connection", help="少量真实只读请求，验证认证、行情和数据日期")
    d.add_argument("--out", default="reports/realtime/connection.json", help="不含密钥的验证报告")
    d = sub.add_parser("analyze", help="分析已有标准快照")
    d.add_argument("--input", required=True)
    d.add_argument("--out", default="reports/analysis")
    d.add_argument("--historical", action="store_true", help="仅跳过墙钟过期检查，不改变信息可用时间")
    d = sub.add_parser("collect", help="从同花顺读取行情池并分析；需要本地 API Key")
    d.add_argument("--date", default=None, help="上海日期 YYYY-MM-DD，默认今天")
    d.add_argument("--max-symbols", type=int, default=30)
    d.add_argument("--out", default="reports/market")
    d = sub.add_parser("replay", help="对按时间记录的快照数组做次会话开盘回放")
    d.add_argument("--input", required=True, help="JSON 数组；参考 demo 生成的 history.json")
    d.add_argument("--out", default="reports/replay")
    d = sub.add_parser("realtime", help="竞价/盘中观察名单；只读，不下单")
    d.add_argument("--input", help="导入实时特征快照，不传则调用真实接口")
    d.add_argument("--max-symbols", type=int, default=20)
    d.add_argument("--history-limit", type=int, default=None, help="最多补充多少只股票的昨日日线，默认同候选上限")
    d.add_argument("--out", default="reports/realtime")
    d.add_argument("--watch", action="store_true", help="持续刷新，Ctrl-C 停止；首次缓存昨日上下文")
    d.add_argument("--interval", type=int, default=60, help="刷新间隔秒，最低 30")
    d.add_argument("--cycles", type=int, default=0, help="watch 刷新次数，0 为持续运行")
    d.add_argument("--open", action="store_true", help="第一次采集后用系统浏览器打开工作台")
    d.add_argument("--assessments", help="V2 定性证据 JSON，每轮读取；必须含真实来源及可用时间")
    d = sub.add_parser("model-v2-template", help="生成 V2.0 空白证据表与完整计分目录")
    d.add_argument("--symbols", required=True, help="逗号分隔标准代码")
    d.add_argument("--date", default=None)
    d.add_argument("--out", default="examples/model-v2-assessments.json")
    d = sub.add_parser("agent-architecture", help="导出 Agent 架构清单，便于学习与扩展")
    d.add_argument("--out", default="reports/agent-architecture.json")
    d = sub.add_parser("paper-status", help="读取模拟账本")
    d.add_argument("--ledger", default="state/paper.json")
    d = sub.add_parser("paper-order", help="向本地模拟券商提交 JSON 订单")
    d.add_argument("--ledger", default="state/paper.json")
    d.add_argument("--input", required=True, help="订单对象，示例见 examples/paper-order.json")
    d = sub.add_parser("paper-halt", help="持久化停止模拟新开仓")
    d.add_argument("--ledger", default="state/paper.json")
    d = sub.add_parser("serve", help="在本机浏览工作台，只绑定 127.0.0.1")
    d.add_argument("--directory", default="reports")
    d.add_argument("--port", type=int, default=8765)
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        config = load_config(args.config)
        if args.command == "diagnose":
            from .execution import DisabledLiveBroker
            print_summary({"python": sys.version.split()[0], "credential": credential_status(),
                           "live_broker": DisabledLiveBroker().readiness(), "network_tested": False})
        elif args.command == "auth":
            from .auth import configure_interactive
            print_summary(configure_interactive(replace=args.replace))
        elif args.command == "check-connection":
            from .connection import verify_connection
            result = verify_connection()
            save_json(args.out, result)
            print_summary(result)
            return 0 if result["auth_verified"] and result["snapshot_valid"] else 2
        elif args.command == "model-v2-template":
            from .model_v2 import assessment_template
            from .v2_rules import FACTORS
            from .data import SYMBOL, _date
            symbols = list(dict.fromkeys(args.symbols.split(",")))
            if not 1 <= len(symbols) <= 100 or any(not SYMBOL.fullmatch(s) for s in symbols):
                raise ValueError("请提供 1..100 个完整股票代码")
            day = _date(args.date or datetime.now(SHANGHAI).date().isoformat()).isoformat()
            output = Path(args.out)
            if output.exists():
                raise ValueError("评估表已存在，请指定新路径，避免覆盖人工证据")
            save_json(output, assessment_template(day, symbols))
            save_json(output.with_name(output.stem + "-rules.json"), FACTORS)
            print_summary({"template": str(output.resolve()), "model": "2.0", "prefilled_scores": False})
        elif args.command == "agent-architecture":
            from .agents.research import architecture_manifest
            manifest = architecture_manifest()
            save_json(args.out, manifest)
            print_summary({"architecture": str(Path(args.out).resolve()),
                           "agents": len(manifest["agents"]), "live_order_route": "disabled"})
        elif args.command == "demo":
            from .demo import demo_snapshots, realtime_demo_frame
            from .backtest import replay
            from .realtime import recommend_realtime
            from .realtime_dashboard import write_realtime_dashboard
            frames = demo_snapshots()
            # Show a high-information session; the replay covers all fixture sessions.
            snapshot = frames[2]
            report, result = analyze(snapshot, config, historical=True), replay(frames, config)
            summary = _research_files(args.out, report, snapshot, result)
            save_json(Path(args.out) / "history.json", frames)
            frame = realtime_demo_frame()
            realtime_report = recommend_realtime(frame, config, now=frame["available_at"])
            from .v2_demo import attach_v2_demo
            realtime_report = attach_v2_demo(frame, realtime_report)
            from .agents import (AgentContext, AgentEvent, DataAgent, EvidenceAgent,
                                 PublisherAgent, ResearchAgent, SafetyAgent)
            from .agent_dashboard import write_agent_dashboard
            demo_context = AgentContext("demo", config, frame=frame, report=realtime_report)
            safety_summary = SafetyAgent().run(demo_context)
            summaries = {"market-data": "读取合成演示帧",
                         "research-scoring": "执行研究规则",
                         "v2-evidence": "生成合成双阶段证据",
                         "safety-guard": safety_summary,
                         "artifact-publisher": "生成离线演示工作台"}
            now = datetime.now(SHANGHAI).isoformat()
            agents = (DataAgent, ResearchAgent, EvidenceAgent, SafetyAgent, PublisherAgent)
            demo_context.events = [AgentEvent(cls.name, cls.role, "completed", now, now, 0,
                                              summaries[cls.name], cls.inputs, cls.outputs)
                                   for cls in agents]
            realtime_report = demo_context.report
            realtime_report["agent_trace"] = demo_context.public_trace()
            save_json(Path(args.out) / "realtime-frame.json", frame)
            save_json(Path(args.out) / "realtime-report.json", realtime_report)
            write_realtime_dashboard(Path(args.out) / "realtime.html", realtime_report)
            write_agent_dashboard(Path(args.out) / "agent.html", realtime_report)
            summary["realtime_report"] = str((Path(args.out) / "realtime.html").resolve())
            summary["agent_console"] = str((Path(args.out) / "agent.html").resolve())
            summary["synthetic_only"] = True
            print_summary(summary)
            if args.open:
                import webbrowser
                webbrowser.open((Path(args.out) / "index.html").resolve().as_uri())
        elif args.command in ("collect", "analyze"):
            if args.command == "collect":
                from .data import collect_snapshot
                day = args.date or datetime.now(SHANGHAI).date().isoformat()
                snapshot = collect_snapshot(HithinkClient(), day, args.max_symbols)
                historical = day != datetime.now(SHANGHAI).date().isoformat()
            else:
                snapshot, historical = load_snapshot(args.input), args.historical
            report = analyze(snapshot, config, historical=historical)
            print_summary(_research_files(args.out, report, snapshot))
        elif args.command == "replay":
            from .backtest import replay
            frames = read_json(args.input)
            result = replay(frames, config)
            report = analyze(frames[-1], config, historical=True)
            summary = _research_files(args.out, report, frames[-1], result)
            summary.update(metrics=result["metrics"], result_valid=result["result_valid"])
            print_summary(summary)
        elif args.command == "realtime":
            from .realtime_data import RealtimeCollector
            from .model_v2 import V2Tracker
            from .agents import (AgentContext, AgentPipeline, DataAgent, EvidenceAgent,
                                 PublisherAgent, ResearchAgent, SafetyAgent)
            if args.watch and args.input:
                raise ValueError("导入文件不能作为真实持续刷新源")
            if args.interval < 30 or args.cycles < 0:
                raise ValueError("刷新间隔不能低于 30 秒；次数不能小于 0")
            collector = None if args.input else RealtimeCollector(HithinkClient(), args.max_symbols,
                            args.max_symbols if args.history_limit is None else args.history_limit)
            tracker = V2Tracker(Path(args.out) / "v2-state.json")
            cycle = 0
            while True:
                out = Path(args.out)
                pipeline = AgentPipeline([
                    DataAgent((lambda: read_json(args.input)) if args.input else collector.collect),
                    ResearchAgent(),
                    EvidenceAgent(tracker, (lambda: read_json(args.assessments)) if args.assessments else None),
                    SafetyAgent(),
                    PublisherAgent(out, args.interval if args.watch else 0,
                                   lambda: read_connection_record(out / "connection.json")),
                ])
                context = pipeline.run(AgentContext("imported" if args.input else "hithink", config))
                report = context.report
                print_summary({"report": str((out / "index.html").resolve()), "source": report["source"],
                               "blocked": report["blocked"], "session": report["session_status"],
                               "candidates": len(report["candidates"]), "blockers": report["blockers"],
                               "agent_console": str((out / "agent.html").resolve()),
                               "run_id": context.run_id})
                if cycle == 0 and args.open:
                    import webbrowser
                    webbrowser.open((out / "index.html").resolve().as_uri())
                cycle += 1
                if not args.watch or (args.cycles and cycle >= args.cycles):
                    break
                time.sleep(args.interval)
        elif args.command.startswith("paper-"):
            from .execution import PaperBroker
            broker = PaperBroker(args.ledger, config)
            if args.command == "paper-order":
                order = read_json(args.input)
                allowed = {"order_id", "symbol", "side", "quantity", "quote", "asof", "exposure_cap"}
                if not isinstance(order, dict) or set(order) - allowed:
                    raise ValueError("模拟订单格式错误或含未知字段")
                print_summary(broker.submit(**order))
            elif args.command == "paper-halt":
                broker.halt(True)
                print_summary(broker.status())
            else:
                print_summary(broker.status())
        elif args.command == "serve":
            from functools import partial
            from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
            directory = Path(args.directory).resolve()
            if not directory.is_dir() or not 1024 <= args.port <= 65535:
                raise ValueError("目录不存在或端口不在 1024..65535")
            handler = partial(SimpleHTTPRequestHandler, directory=str(directory))
            with ThreadingHTTPServer(("127.0.0.1", args.port), handler) as server:
                print_summary({"url": f"http://127.0.0.1:{args.port}", "directory": str(directory)})
                print("Ctrl-C 停止本机报告服务", flush=True)
                server.serve_forever()
        return 0
    except KeyboardInterrupt:
        print_summary({"status": "stopped"})
        return 0
    except (DataError, ValueError, OSError, TypeError, KeyError) as exc:
        print_summary({"ok": False, "error": str(exc), "live_orders_sent": False})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
