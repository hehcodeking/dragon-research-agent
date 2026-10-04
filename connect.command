#!/bin/zsh
set -e
cd -- "${0:A:h}"
print "龙头研究 Agent · 连接真实行情"
print "首次配置需要同花顺数据服务 API Key，输入隐藏且只保存到本机用户凭据目录。"
./run.command auth
./run.command check-connection --out reports/realtime/connection.json
./run.command realtime --watch --interval 60 --max-symbols 20 --open --out reports/realtime
