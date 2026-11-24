// 文档中的示意图（fletcher 绘制）
#import "@preview/fletcher:0.5.8" as fletcher: diagram, node, edge
#import "../template/cpc-doc.typ": sans, ink2

#let c-svc = rgb("#e8f1fc")   // 平台服务
#let c-site = rgb("#e7f6ef")  // 站点
#let c-ext = rgb("#f4f3ef")   // 外部/基础设施
#let c-grid = rgb("#fdf1e6")  // 电网侧

#let lbl(body) = text(font: sans, size: 8pt, body)
#let box-node(pos, body, fill: c-svc, name: none, w: auto) = node(
  pos, text(font: sans, size: 8.5pt, body), fill: fill, stroke: 0.6pt + rgb("#8a8984"),
  corner-radius: 2pt, inset: 6pt, name: name, width: w, shape: fletcher.shapes.rect,
)

// 图：总体架构
#let arch = diagram(
  spacing: (9mm, 9mm),
  box-node((1.5, 0), [用户 / 负载生成器 \ （OpenAI 兼容请求，带来源区域）], fill: c-ext, name: <user>),
  box-node((1.5, 1), [*路由网关 gateway* \ 策略插件 · 广域网时延注入], name: <gw>),
  box-node((4, 1), [*容量控制器 controller* \ 采集 → 决策 → 执行], name: <ctl>),
  box-node((4, 0), [*电网信号服务 grid* \ 分时电价 · 碳强度 · 需求响应], fill: c-grid, name: <grid>),
  box-node((0, 2.3), [*站点代理 sh*], fill: c-site, name: <s1>),
  box-node((1, 2.3), [*站点代理 gd*], fill: c-site, name: <s2>),
  box-node((2, 2.3), [*站点代理 gz*], fill: c-site, name: <s3>),
  box-node((3, 2.3), [*站点代理 nm*], fill: c-site, name: <s4>),
  box-node((0.5, 3.4), [仿真节点池 \ 功率/性能模型], fill: c-ext, name: <emu>),
  box-node((2.5, 3.4), [真实推理实例（vLLM 等） \ 功率传感器（NVML/RAPL/BMC…）], fill: c-ext, name: <real>),
  box-node((4.6, 2.3), [Prometheus \ 记录规则（标准化能耗指标）], fill: c-ext, name: <prom>),
  box-node((4.6, 3.4), [Grafana 仪表盘], fill: c-ext, name: <graf>),
  edge(<user>, <gw>, "-|>", lbl[推理请求]),
  edge(<gw>, <s1>, "-|>"), edge(<gw>, <s2>, "-|>"), edge(<gw>, <s3>, "-|>"), edge(<gw>, <s4>, "-|>"),
  edge(<grid>, <gw>, "-|>", lbl[信号], label-pos: 0.3, label-side: left),
  edge(<grid>, <ctl>, "-|>", lbl[信号 / 预测]),
  edge(<ctl>, <gw>, "-|>", lbl[路由权重], label-side: right),
  edge(<ctl>, <s4>, "-|>", lbl[容量指令], label-pos: 0.6),
  edge(<s1>, <emu>, "-"), edge(<s2>, <emu>, "-"), edge(<s3>, <real>, "-"), edge(<s4>, <real>, "-"),
  edge(<prom>, <graf>, "-|>"),
  edge(<s4>, <prom>, "--|>", lbl[抓取 /metrics]),
)

// 图：节点状态机
#let states = diagram(
  spacing: (30mm, 16mm),
  box-node((0, 0), [off \ 关机/停服], fill: c-ext, name: <off>),
  box-node((1, 0), [booting \ 启动中], fill: c-grid, name: <boot>),
  box-node((2, 0), [on \ 服务中], fill: c-site, name: <on>),
  box-node((2, 1), [draining \ 排空中], fill: c-svc, name: <drain>),
  edge(<off>, <boot>, "-|>", lbl[扩容], bend: 25deg),
  edge(<boot>, <off>, "-|>", lbl[缩容（取消启动）], bend: 25deg),
  edge(<boot>, <on>, "-|>", lbl[就绪]),
  edge(<on>, <drain>, "-|>", lbl[缩容且有在途请求], bend: 25deg),
  edge(<drain>, <on>, "-|>", lbl[扩容（恢复）], bend: 25deg),
  edge(<drain>, (0, 1), <off>, "-|>", lbl[在途请求完成], label-pos: 0.3),
  edge(<on>, (2, -0.8), (0, -0.8), <off>, "-|>", lbl[缩容且空闲], label-pos: 0.5),
)

// 图：能耗感知四层
#let sensing = diagram(
  spacing: (6mm, 7mm),
  box-node((0, 0), [*设备层* GPU / NPU / CPU / 内存 \ NVML · nvidia-smi · AMD SMI · hwmon · RAPL · 昇腾 DCMI …], fill: c-ext, name: <dev>, w: 120mm),
  box-node((0, 1), [*服务器层*（IT 功率） \ 加速卡实测 + 主机部分（整机 BMC：IPMI / Redfish；或 RAPL；或功率模型）], fill: c-site, name: <srv>, w: 120mm),
  box-node((0, 2), [*设施层*（设施功率 = IT 功率 × PUE）], fill: c-svc, name: <fac>, w: 120mm),
  box-node((0, 3), [*电网层* 分时电价 · 碳排放因子 · 需求响应功率上限 → 用电成本速率、碳排放速率], fill: c-grid, name: <grid>, w: 120mm),
  edge(<dev>, <srv>, "-|>"), edge(<srv>, <fac>, "-|>"), edge(<fac>, <grid>, "-|>"),
)

// 图：两个时间尺度的控制
#let loops = diagram(
  spacing: (14mm, 12mm),
  box-node((0, 0), [电网信号 \ （分钟—小时级变化）], fill: c-grid, name: <g>),
  box-node((1.4, 0), [*容量控制器* \ 每个控制周期（如 15 s 实际 / 5 min 仿真）], name: <c>),
  box-node((2.9, 0), [站点代理 \ 开关节点 · 功率封顶], fill: c-site, name: <s>),
  box-node((1.4, 1.1), [*路由网关* \ 每个请求（毫秒级）], name: <gw>),
  box-node((2.9, 1.1), [站点状态快照 \ 每秒刷新], fill: c-site, name: <st>),
  edge(<g>, <c>, "-|>"), edge(<c>, <s>, "-|>", lbl[目标节点数 \ 封顶比例], label-side: left),
  edge(<c>, <gw>, "-|>", lbl[路由权重（可选）]),
  edge(<st>, <gw>, "-|>", lbl[负载、排队]), edge(<s>, <st>, "-"),
)
