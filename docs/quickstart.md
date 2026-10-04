# 快速开始

## 1. 离线体验

```bash
git clone YOUR_REPOSITORY_URL
cd dragon-research-agent
python3 -m unittest discover -s tests -v
./run.command demo --open
```

演示会生成三个自包含页面：

- `reports/demo/index.html`：策略研究与回放工作台。
- `reports/demo/realtime.html`：竞价、量能、换手和昨日结构观察台。
- `reports/demo/agent.html`：Agent 链路、事件和安全边界控制台。

演示全部使用合成数据。

## 2. 真实只读数据

```bash
./run.command auth
./run.command check-connection
./run.command realtime --watch --interval 60 --open
```

`auth` 在本机隐藏输入并把 Key 保存到仓库外。不要把 Key 写入命令参数、Issue、截图或提交历史。真实数据仍只生成研究观察，不会发送订单。

## 3. 查看 Agent 架构

```bash
./run.command agent-architecture
cat reports/agent-architecture.json
```

扩展前请阅读 [Agent 架构](architecture.md) 与 [贡献说明](../CONTRIBUTING.md)。
