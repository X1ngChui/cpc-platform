#import "../template/cpc-doc.typ": *

= 开发指南 <g-dev>

== 代码组织与约定

- 包结构见《平台设计说明书》"代码结构"一节。服务之间只通过 HTTP/JSON 通信，数据结构定义在 `cpc/schemas.py`；
- 策略、传感器使用注册装饰器，平台主体不需要因新增策略而修改；
- 仿真站点（`cpc/site/core.py`）与离散事件仿真器（`cpc/sim/des.py`）的节点状态机规则必须保持一致，修改其一请同步另一处；
- 平台内部的 HTTP 客户端一律 `trust_env=False`，避免服务间调用被系统代理转发；
- 代码风格：四空格缩进，`snake_case` 命名，类型注解；注释与文档使用中文；
- 不把密钥、IP、模型路径等写进仓库：本地部署文件已在 `.gitignore` 中排除（`deploy/cloud/*/scenario.yaml`、`.env` 等）。

== 编写策略插件

完整示例见《平台设计说明书》"扩展指南"一章。要点：

1. 新建模块（如 `my_policies.py`），继承 `RoutingPolicy` 或 `CapacityPolicy`，用 `@register_routing` / `@register_capacity` 注册；
2. 构造函数的关键字参数来自场景文件的 `params`；
3. 策略中使用 `view.now`（信号时间），不要读墙钟，以保证在仿真与在线平台中行为一致；
4. 先用 `CPC_PLUGINS=my_policies cpc compare ...` 在仿真中比较，再 `cpc up --routing <名>` 在线运行；
5. 部署到容器时，把插件模块挂载进容器并设置 `CPC_PLUGINS` 与 `PYTHONPATH`。

== 测试

```bash
pytest                                    # 单元测试：电网信号解析、功率与性能模型、站点状态机、策略、传感器解析、仿真守恒
python scripts/smoke_test.py              # 端到端：启动全部服务并检查
python scripts/real_gpu_check.py --restart  # 有真实 GPU 站点时：压测 + 功率轨迹 + 关停重启
```

新增功能时请附带单元测试；没有真实硬件的传感器，用伪造的 sysfs 目录或命令输出测试解析逻辑（参考 `tests/test_sensors.py`）。

== 持续集成与镜像发布

仓库的 GitHub Actions 包含两个工作流：

- `ci.yml`：在 Linux、Windows、macOS 上运行单元测试与冒烟测试；在 Linux 上编译文档并上传 PDF；
- `image.yml`：构建 amd64 与 arm64 镜像并推送到 `ghcr.io/<用户名>/cpc-platform`（推送 `main` 生成 `main` 与提交哈希标签，
  推送 `v*` 标签生成版本号标签与 `latest`）。

发布新版本：

```bash
git tag v0.1.1 && git push origin v0.1.1
# 如需同时推送 Docker Hub（需先 docker login）：
docker buildx build --platform linux/amd64,linux/arm64 -t <用户名>/cpc-platform:0.1.1 --push .
```

== 编译文档

```bash
pip install typst
python docs/build.py            # 生成 docs/pdf/design.pdf 与 docs/pdf/guide.pdf
```

文档源文件为 Typst（`docs/design/`、`docs/guide/`），模板在 `docs/template/cpc-doc.typ`，示意图在 `docs/figures/diagrams.typ`，
参考文献在 `docs/design/refs.yml`（GB/T 7714 格式输出）。Typst 包缓存在 `docs/.typst-packages`，可离线编译。
Linux 上若缺少中文字体，安装 `fonts-noto-cjk`。

= 常见问题 <g-faq>

#let qa(q, a) = block(below: 1em, { set par(justify: false, first-line-indent: 0em); [#strong(q) \ #a] })

#qa[端口被占用怎么办？][`cpc up` 默认使用 9000、8080、9300、9101 起的端口。可准备一个地址文件指定其他端口，用 `-e` 传入；
Compose 部署修改 `docker-compose.yml` 中的端口映射。Windows 上用 `netstat -ano | findstr :8080` 查看占用进程。]

#qa[请求很慢或连接失败，且机器上设置了 HTTP 代理？][平台内部调用已不读取代理环境变量；但 `curl` 等工具仍会走代理，
访问本机服务时加 `--noproxy '*'`。Docker 容器与 kind 节点继承的 `127.0.0.1` 代理地址在容器内不可达，见 @g-kind 的说明。]

#qa[Compose 启动后 Grafana 没有数据？][确认 Prometheus 的 Targets 页面（`http://localhost:9090/targets`）各目标为 UP；
仪表盘的时间范围默认最近 30 分钟，需先运行负载生成器。]

#qa[SLO 达成率偏低？][常见原因：容量策略缩容过快、节点启动时间（`boot_s`）相对信号时钟加速过长、负载超过总容量。
演示时信号加速 60 倍，`reactive` 策略的扩容来不及跟上快速变化的日内负载是预期现象；正式实验应使用真实时间。]

#qa[NVML 不可用（`pynvml` 导入失败或 `NVML Shared Library Not Found`）？][安装 `pip install nvidia-ml-py`；Linux 容器中需要
NVIDIA Container Toolkit 并以 `--gpus all` 启动；Windows 需安装 NVIDIA 驱动。可先用 `nvidia-smi` 验证驱动。]

#qa[RAPL 读不到？][`/sys/class/powercap/intel-rapl:*/energy_uj` 在多数发行版上仅 root 可读，且大多数云虚拟机不提供 RAPL。
在裸金属上以 root 运行，或用 `ipmi` / `redfish` 获取整机功率。]

#qa[vLLM 启动失败或显存不足？][减小 `MAX_MODEL_LEN`、降低 `--gpu-memory-utilization`、换更小的模型；查看 `docker compose logs vllm-0`。
新架构 GPU 需要较新的 vLLM 版本（本手册在 RTX 5070 Ti 上验证了 vLLM v0.34.0）。]

#qa[模型下载慢？][在宿主机用 `huggingface-cli download` 预先下载并挂载缓存目录，容器设置 `HF_HUB_OFFLINE=1`；
或使用 `HF_ENDPOINT=https://hf-mirror.com` / `VLLM_USE_MODELSCOPE=True`。]

#qa[管理接口返回 401？][服务端设置了 `CPC_ADMIN_TOKEN`。调用时加 `-H "Authorization: Bearer <令牌>"`；
控制器与 `cpc event` 会自动读取同名环境变量。]

#qa[kind 部署卡在等待就绪？][`kubectl --context kind-cpc -n cpc get pods` 查看状态；`ErrImagePull` 多为镜像未加载，重新运行
`python deploy/k8s/kind_up.py --skip-build`；`describe pod` 查看事件。]

#qa[仿真结果每次都一样吗？][负载生成使用场景中的 `workload.seed`，相同场景与策略的仿真结果可复现；改变种子可评估随机性的影响。]
