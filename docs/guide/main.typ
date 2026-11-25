#import "../template/cpc-doc.typ": *

#show: doc.with(
  title: "算电协同跨数据中心 AI 计算任务调度平台",
  cover-title: [算电协同跨数据中心 \ AI 计算任务调度平台],
  subtitle: "使用手册",
  doc-id: "CPC-UG-001",
  version: "v0.3",
  date: "2026-11-25",
  org: "算电协同课题组",
  authors: ("平台开发组",),
  revisions: (
    ("v0.1", "2026-11-24", "平台开发组", "初稿"),
    ("v0.2", "2026-11-25", "平台开发组", "数据准备改为转换脚本与数据集校验；仿真结果数据包与实验描述；服务器池与真实服务器接入"),
    ("v0.3", "2026-11-25", "平台开发组", "按操作流程重新组织：快速上手、数据、场景、仿真、实验、策略编写；cpc check、cpc live、cpc profile"),
  ),
  abstract: [
    本手册说明平台的安装与使用：用公开数据或自己的数据准备数据集，编写场景，运行仿真并分析结果，以真实时间运行实验并接入
    真实推理实例与功率传感器，编写自己的调度策略，以及观测、运维与开发。各章按操作顺序给出完整的命令与配置示例。
    平台的设计见《平台设计说明书》（CPC-DS-001）。
  ],
)

#include "ch1-start.typ"
#include "ch2-data.typ"
#include "ch3-scenario.typ"
#include "ch4-sim.typ"
#include "ch5-live.typ"
#include "ch6-policy.typ"
#include "ch7-ops.typ"
#include "ch8-dev.typ"
