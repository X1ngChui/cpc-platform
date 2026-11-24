#import "../template/cpc-doc.typ": *

= Docker Compose 部署 <g-compose>

== 启动

```bash
docker compose -f deploy/compose/docker-compose.yml up -d --build
docker compose -f deploy/compose/docker-compose.yml ps
```

启动的容器：`grid`、`site-sh`、`site-gd`、`site-gz`、`site-nm`、`gateway`、`controller`、`prometheus`、`grafana`。
各服务通过容器名互相访问（地址文件 `deploy/compose/endpoints.yaml`），并映射到宿主机端口：

#figure(
  tbl(
    columns: (8em, 1fr),
    header: ("服务", "宿主机地址"),
    [网关], [`http://localhost:8080`],
    [电网信号], [`http://localhost:9000`],
    [控制器], [`http://localhost:9300`],
    [站点 sh/gd/gz/nm], [`http://localhost:9101` – `9104`],
    [Prometheus], [`http://localhost:9090`],
    [Grafana], [`http://localhost:3000`（匿名只读；管理员 `admin` / 环境变量 `GRAFANA_PASSWORD`，默认 `admin`）],
  ),
  caption: [Compose 部署的访问地址],
)

可在 `deploy/compose/.env` 中设置环境变量：`CPC_SPEEDUP`（信号时钟加速，默认 60）、`CPC_ROUTING`、`CPC_CAPACITY`（默认策略）、
`CPC_ADMIN_TOKEN`（管理接口令牌）、`GRAFANA_PASSWORD`。

== 压测与观察

```bash
docker compose -f deploy/compose/docker-compose.yml run --rm loadgen        # 默认 10 分钟
CPC_LOAD_SECONDS=1800 docker compose -f deploy/compose/docker-compose.yml run --rm loadgen
```

负载生成器容器以只读方式挂载宿主机的数据集缓存目录（`${CPC_DATA_DIR:-~/.cache/cpc/datasets}`），须先在宿主机执行
`cpc data fetch azure-llm-2024-conv`；到达率可用环境变量 `CPC_RATE_SCALE` 缩放。

打开 Grafana 的"算电协同调度平台"仪表盘，可以看到
设施总功率、用电成本速率、碳排放速率、请求速率、SLO 达成率，以及各站点的功率、电价、在线节点数、碳强度、请求率、
TTFT P99、需求响应余量、每 token 能耗、排队数与 GPU 功率。请求日志与决策日志在 `logs/compose/`。

在 Prometheus 页面（`http://localhost:9090`）可以直接查询标准化指标，例如：

```text
site:facility_power_watts:sum                       各站点设施功率
sum(site:energy_cost_yuan_per_hour)                 全局用电成本速率（元/小时）
increase(cpc_node_energy_joules_total[10m]) / 3.6e6 各节点最近 10 分钟 IT 用电（kWh）
histogram_quantile(0.99, sum by (le, site) (rate(cpc_gateway_ttft_seconds_bucket[5m])))
```

== 验证与停止

```bash
python scripts/smoke_test.py --external -e configs/endpoints.compose-host.yaml
docker compose -f deploy/compose/docker-compose.yml down
```

= Kubernetes（kind）部署 <g-kind>

== 一键部署

```bash
python deploy/k8s/kind_up.py
```

脚本依次完成：按场景生成 Kubernetes 清单与 kind 集群配置（`deploy/k8s/generate.py`）→ 创建名为 `cpc` 的 kind 集群
（1 个控制面节点 + 每个站点 1 个 worker 节点，worker 带 `topology.kubernetes.io/region=<站点 id>` 标签）→ 构建平台镜像 →
把平台、Prometheus、Grafana 镜像加载到集群节点 → `kubectl apply` → 等待全部 Deployment 就绪。完成后：

#figure(
  tbl(
    columns: (8em, 1fr),
    header: ("服务", "地址（NodePort 映射到宿主机）"),
    [网关], [`http://localhost:30080`],
    [Grafana], [`http://localhost:30300`],
    [Prometheus], [`http://localhost:30090`],
    [电网信号 / 控制器], [`http://localhost:30900` / `http://localhost:30930`],
    [站点], [`http://localhost:30101` 起],
  ),
  caption: [kind 部署的访问地址],
)

```bash
kubectl --context kind-cpc -n cpc get pods -o wide     # 站点 Pod 分布在各自 region 的节点上
kubectl --context kind-cpc -n cpc logs deploy/controller -f
python scripts/smoke_test.py --external -e configs/endpoints.kind-host.yaml
python deploy/k8s/kind_up.py --down                     # 删除集群
```

== 清单结构

`deploy/k8s/generated/` 中的清单由生成器产生，修改场景（如增加站点）后重新运行 `python deploy/k8s/generate.py` 即可：

- `00-namespace.yaml`：命名空间 `cpc`；
- `10-config.yaml`：地址文件、Prometheus 配置与记录规则、Grafana 数据源与仪表盘（ConfigMap）；
- `20-grid.yaml`、`22-gateway.yaml`、`23-controller.yaml`：控制面服务；
- `21-sites.yaml`：每个站点一个 Deployment + Service，`nodeSelector` 固定到对应 region 的节点；
- `30-monitoring.yaml`：Prometheus 与 Grafana。

#note(title: "代理环境")[如果宿主机通过 `127.0.0.1` 上的代理上网，kind 节点会继承这个代理地址，但在节点容器内它是不可达的，
直接拉取镜像会失败。`kind_up.py` 因此先在宿主机准备好全部镜像再加载到节点中（必要时导出单一平台的镜像包），节点本身不需要联网。]

== 走向真实 Kubernetes 集群

在真实集群（例如在云服务器上用 k3s 搭建）中，推荐的做法是：用 NVIDIA GPU Operator 安装设备插件与 dcgm-exporter；
给节点打上 `topology.kubernetes.io/region` 标签表示站点；每个站点部署一个 vLLM Deployment（每个副本占一张 GPU）；
容量控制器使用 Kubernetes 执行器直接伸缩副本数：

```bash
cpc controller -s scenario.yaml --actuator kubernetes --k8s-namespace cpc --k8s-template "vllm-{site}"
```

此时站点代理可以只作为状态与功率的汇报方（`backend: openai`，上游指向该站点 vLLM 的 Service），或由网关直接访问 Service。
