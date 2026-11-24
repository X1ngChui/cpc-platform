#import "../template/cpc-doc.typ": *

#show: doc.with(
  title: "算电协同跨数据中心 AI 计算任务调度虚拟化平台",
  cover-title: [算电协同跨数据中心 \ AI 计算任务调度虚拟化平台],
  subtitle: "部署与使用手册",
  doc-id: "CPC-UG-001",
  version: "v0.1",
  date: "2026-11-24",
  org: "算电协同课题组 · 平台搭建资料",
  authors: ("平台搭建小组",),
  revisions: (
    ("v0.1", "2026-11-24", "搭建小组", "初稿：本机、Docker Compose、kind、云服务器四种部署；能耗感知操作；开发指南；常见问题"),
  ),
  abstract: [
    本手册给出平台的安装、部署与日常操作步骤，覆盖本机单进程、Docker Compose、Kubernetes（kind）与云服务器（真实 GPU）
    四种部署方式，以及多硬件功率采集、负载生成、仿真对比、策略插件开发等常用操作。每种部署方式都附有验证步骤，
    读者可以按章节顺序从零开始搭建出完整的实验环境。平台的设计原理见《平台设计说明书》（CPC-DS-001）。
  ],
)

#include "ch1-setup.typ"
#include "ch2-deploy.typ"
#include "ch3-cloud.typ"
#include "ch4-ops.typ"
#include "ch5-dev.typ"
