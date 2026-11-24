#import "../template/cpc-doc.typ": *

#show: doc.with(
  title: "算电协同跨数据中心 AI 计算任务调度平台",
  cover-title: [算电协同跨数据中心 \ AI 计算任务调度平台],
  subtitle: "使用手册",
  doc-id: "CPC-UG-001",
  version: "v0.2",
  date: "2026-11-25",
  org: "算电协同课题组",
  authors: ("平台开发组",),
  revisions: (
    ("v0.1", "2026-11-24", "平台开发组", "初稿"),
    ("v0.2", "2026-11-25", "平台开发组", "数据准备改为转换脚本与数据集校验；仿真结果数据包与实验描述；服务器池与真实服务器接入"),
  ),
  abstract: [
    本手册给出平台的安装、数据准备、仿真、实验部署、观测运维与开发的操作步骤。实验系统的部署覆盖本机单进程、
    各服务独立进程、Docker Compose、Kubernetes（kind）与云服务器，并说明真实推理实例与功率传感器的接入方法。
    平台的设计见《平台设计说明书》（CPC-DS-001）。
  ],
)

#include "ch1-setup.typ"
#include "ch2-data.typ"
#include "ch3-sim.typ"
#include "ch4-deploy.typ"
#include "ch5-ops.typ"
#include "ch6-dev.typ"
