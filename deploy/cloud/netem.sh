#!/usr/bin/env bash
# 可选：用 Linux tc netem 在网络层模拟跨地域时延（替代网关的时延注入）。
#
# 适用：多台租用服务器位于同一地域，但希望"真实网络栈"上就带有跨数据中心时延（例如测试流式输出、
# 长连接的表现）。使用后请在场景文件中把 network.emulate_wan 设为 false，避免重复计入时延。
#
# 用法（在控制节点——网关所在服务器——上执行，需要 root）：
#   sudo ./netem.sh eth0 add 10.0.0.11=27 10.0.0.12=24 10.0.0.13=20   # 目的 IP=单向附加时延(ms)
#   sudo ./netem.sh eth0 del
# 说明：这里对"网关 → 站点"方向的报文加单向时延；若需对称 RTT，可在站点服务器上对网关 IP 再执行一次。
set -euo pipefail
DEV=${1:?网卡名，如 eth0}
ACTION=${2:?add 或 del}
shift 2

if [[ "$ACTION" == "del" ]]; then
  tc qdisc del dev "$DEV" root 2>/dev/null || true
  echo "已清除 $DEV 上的 tc 规则"
  exit 0
fi

tc qdisc del dev "$DEV" root 2>/dev/null || true
tc qdisc add dev "$DEV" root handle 1: prio bands 16
band=4
for pair in "$@"; do
  ip=${pair%%=*}
  ms=${pair##*=}
  tc qdisc add dev "$DEV" parent 1:$band handle $((band * 10)): netem delay "${ms}ms"
  tc filter add dev "$DEV" protocol ip parent 1:0 prio 1 u32 match ip dst "$ip"/32 flowid 1:$band
  echo "→ $ip 附加单向时延 ${ms} ms"
  band=$((band + 1))
done
tc qdisc show dev "$DEV"
