# 参与开发

感谢你用这个仓库学习或改进 Agent 工程。提交前请先阅读 [架构说明](docs/architecture.md) 和 [安全说明](SECURITY.md)。

## 本地开发

项目要求 Python 3.11+，运行时只用标准库。

```bash
python3 -m unittest discover -s tests -v
./run.command demo
./run.command agent-architecture
```

新增 Agent 时，请保持单一职责，通过 `AgentContext` 交换结构化对象，只在 `AgentEvent` 中记录可公开的摘要。不要把原始响应、凭据、账户、持仓或异常详情写入报告。

提交应包含清楚的问题说明、行为变化和必要测试。界面修改请同时检查桌面与窄屏；数据规则修改需要覆盖缺失值、未来时间、过期数据和失败关闭。

## Pull Request 检查

- 测试全部通过。
- 合成数据明确标注，不把演示结果写成真实业绩。
- 不新增真实下单路径或盈利承诺。
- 不提交 `reports/`、`state/`、`.env`、API Key 或用户数据。
- 新字段包含来源、观测时间、可用时间和缺失语义。
