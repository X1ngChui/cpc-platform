#import "../template/cpc-doc.typ": *

#show: doc.with(
  title: "算电协同跨数据中心 AI 计算任务调度平台",
  cover-title: [算电协同跨数据中心 \ AI 计算任务调度平台],
  subtitle: "平台设计说明书",
  doc-id: "CPC-DS-001",
  version: "v0.3",
  date: "2026-11-25",
  org: "算电协同课题组",
  authors: ("平台开发组",),
  revisions: (
    ("v0.1", "2026-11-24", "平台开发组", "初稿"),
    ("v0.2", "2026-11-25", "平台开发组", "数据集改为描述文件与类别规范；数据中心运行时由仿真与实验共用；两级调度与服务器池；结果数据包与实验描述"),
    ("v0.3", "2026-11-25", "平台开发组", "服务器类型只以表给出；删除 Kubernetes 执行器等不必要的功能；实验运行（cpc live）写出与仿真相同的结果数据包"),
  ),
  abstract: [
    本文档说明算电协同跨数据中心 AI 计算任务调度平台的设计。平台以描述文件与类别规范接入负载、电网信号与功率特性数据，
    以场景描述数据中心、服务器池与电网环境，以全局（跨数据中心）与本地（数据中心内部）两级策略接口组织调度，
    并以同一套服务器模型、数据中心运行时与策略实现支持离散事件仿真与接入真实推理实例的实验。
    文中给出数据规范、场景与模型、数据中心运行时、策略接口、能耗测量与核算、服务接口、运行与结果格式以及扩展方式。
  ],
)

#include "ch1-intro.typ"
#include "ch2-arch.typ"
#include "ch3-data.typ"
#include "ch4-scenario.typ"
#include "ch5-runtime.typ"
#include "ch6-policies.typ"
#include "ch7-energy.typ"
#include "ch8-interfaces.typ"
#include "ch9-workflow.typ"
#include "ch10-extension.typ"
#include "refs.typ"
