# cpc-platform 项目约定

算电协同（能耗感知）跨数据中心 AI 计算任务调度平台。Python 包 `cpc`，命令行入口 `cpc`。
设计与部署文档：`docs/pdf/design.pdf`、`docs/pdf/guide.pdf`（源文件 `docs/design/`、`docs/guide/`，Typst）。

## 运行环境

- 服务端组件（电网信号服务、站点代理、路由网关、容量控制器、通用功率导出器）只面向 Linux（x86_64、aarch64）。
  不为 Windows、macOS 编写服务端适配代码。
- 开发环境为 Linux 或 WSL2（Ubuntu）。仓库应克隆在 Linux 文件系统内（如 `~/`），不放在 `/mnt/*` 下。

## 设计原则

1. **失败处理不叠加降级链。** 配置与环境错误在启动阶段报错退出；运行期的局部故障通过隔离限制影响范围，
   并以显式状态暴露（健康/就绪接口、状态字段、`*_up` 与 `*_errors_total` 指标、结构化日志）。
   不得以模型估计值、默认值或其他数据源静默替代缺失的实测值；缺失即标记为缺失。
2. **可替换部件经抽象接口与注册机制接入**：路由策略、容量策略、功率传感器、数据集（负载、电网信号、功率特性）、
   节点后端、执行器等。新增实现不修改平台主体。
3. **数据优先使用公开数据集**，经统一的数据集抽象接入（清单记录名称、版本、来源、许可、校验和、引用）；
   原始数据下载到缓存目录，不提交入库。合成数据仅用于单元测试。所有数据记录出处。
4. **仿真与在线平台共用**策略、模型与数据接口；节点状态机规则在 `src/cpc/site/core.py` 与 `src/cpc/sim/des.py` 中必须一致。
5. 平台内部的 HTTP 客户端使用 `trust_env=False`，服务间调用不经过系统代理。
6. 管理接口（`/admin/*`、电网事件注入）受 `CPC_ADMIN_TOKEN` 保护；不提交密钥、主机地址、个人信息。

## 语言与文档

- 文档、代码注释、日志与提交信息使用专业、技术性的书面语，避免口语化表达。
- 文档定位为项目技术文档，不针对特定读者群体撰写。
- 代码或接口变更须同步更新 Typst 文档并重新编译 PDF（`python docs/build.py`），PDF 随源码提交。
- 参考文献维护在 `docs/design/refs.yml`（GB/T 7714 输出）；事实性陈述须有可核查的出处。

## 开发流程

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev,nvml,docs]"
pytest                                   # 单元测试
python scripts/smoke_test.py             # 端到端冒烟测试（单进程启动全部服务）
python docs/build.py                     # 文档有改动时
```

- CI：`.github/workflows/ci.yml`；镜像：`.github/workflows/image.yml`（`ghcr.io/x1ngchui/cpc-platform`）。
  Docker Hub 镜像 `xingchui/cpc-platform` 由维护者手动推送（`docker buildx build --platform linux/amd64,linux/arm64 ... --push`）。
- 第三方 GitHub Actions 固定到提交 SHA，由 Dependabot 更新。
- 发布：语义化版本标签 `vX.Y.Z`。
