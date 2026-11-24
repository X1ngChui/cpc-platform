#import "../template/cpc-doc.typ": *

#show: doc.with(
  title: "算电协同跨数据中心 AI 计算任务调度虚拟化平台",
  cover-title: [算电协同跨数据中心 \ AI 计算任务调度虚拟化平台],
  subtitle: "平台设计说明书",
  doc-id: "CPC-DS-001",
  version: "v0.1",
  date: "2026-11-24",
  org: "算电协同课题组 · 平台搭建资料",
  authors: ("平台搭建小组",),
  revisions: (
    ("v0.1", "2026-11-24", "搭建小组", "初稿：总体架构、能耗感知、请求路由、容量管理、接口与实验支撑"),
  ),
  abstract: [
    本文档说明一个面向"算电协同"研究的跨数据中心 AI 计算任务调度虚拟化平台的设计。平台把多个（虚拟）数据中心站点、
    电网侧信号（分时电价、碳排放因子、需求响应）、全局请求路由与节点容量管理组织为一套接口统一的系统，
    支持三种运行形态：离散事件仿真、基于容器的虚拟化部署（单机、Docker Compose、Kubernetes）以及接入真实 GPU 服务器
    （vLLM 推理实例与多种硬件功率传感器）。平台的调度策略以插件形式提供，本文给出的策略均为演示数据通路用的参考实现，
    具体调度算法不在本文范围内。
  ],
)

#include "ch1-intro.typ"
#include "ch2-arch.typ"
#include "ch3-energy.typ"
#include "ch4-routing.typ"
#include "ch5-capacity.typ"
#include "ch6-interfaces.typ"
#include "ch7-experiments.typ"
#include "ch8-extension.typ"
#include "refs.typ"
