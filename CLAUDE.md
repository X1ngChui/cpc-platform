# cpc-platform 项目约定

算电协同（能耗感知）跨数据中心 AI 计算任务调度平台。Python 包 `cpc`，命令行入口 `cpc`。
设计说明书与使用手册有中文与英文两版：`docs/pdf/{design,guide}-{zh,en}.pdf`（Typst 源文件 `docs/{zh,en}/{design,guide}/`，
共用模板 `docs/template/cpc-doc.typ`）。

## 运行环境

- 服务端组件（电网信号服务、站点代理、路由网关、容量控制器）只面向 Linux（x86_64、aarch64）。
  不为 Windows、macOS 编写服务端适配代码。
- 开发环境为 Linux 或 WSL2（Ubuntu）。仓库应克隆在 Linux 文件系统内（如 `~/`），不放在 `/mnt/*` 下。

## 设计原则

1. **失败处理不叠加降级链。** 配置与环境错误在启动阶段报错退出；运行期的局部故障通过隔离限制影响范围，
   并以显式状态暴露（健康/就绪接口、状态字段、`*_up` 与 `*_errors_total` 指标、结构化日志）。
   不得以模型估计值、默认值或其他数据源静默替代缺失的实测值；缺失即标记为缺失。
2. **可替换部件经抽象接口与注册机制接入**：全局策略（路由、容量）、本地策略（派发、电源管理）、功率传感器以
   entry points 注册（`cpc.registry`）；推理后端为任何 OpenAI 兼容服务；执行器经钩子接口接入。新增实现不修改平台主体。
3. **数据集为描述文件 + 数据文件**（Data Package v2 子集 + `cpc` 类别规范，`src/cpc/datasets/`）：每类数据规定核心字段，
   其余为扩展字段。平台只做校验与加载，不含任何原始数据的解析代码；原始数据的转换范例在 `examples/datasets/`，
   转换参数与出处记录在描述文件中。原始数据与转换结果放在 `CPC_DATA_DIR`，不提交入库；合成数据只用于测试。
4. **仿真与实验共用**场景、服务器模型、策略与数据中心运行时：数据中心内部的状态与规则只在 `src/cpc/datacenter.py` 中实现，
   由仿真器（`src/cpc/sim/des.py`）与站点代理（`src/cpc/site/core.py`）分别驱动。
5. 平台内部的 HTTP 客户端使用 `trust_env=False`，服务间调用不经过系统代理。
6. 管理接口（`/admin/*`、电网事件注入）受 `CPC_ADMIN_TOKEN` 保护；不提交密钥、主机地址、个人信息。

## 语言与文档

- 代码中的注释、文档字符串、日志、命令行输出与错误信息，以及配置与示例文件中的注释、提交信息，一律使用英文。
- 文档提供中文与英文两版，内容保持一致；`README.md` 为英文，`README.zh-CN.md` 为中文。
- 文档、代码注释、日志与提交信息使用专业、技术性的书面语，避免口语化表达。
- 文档定位为项目技术文档，不针对特定读者群体撰写。
- 代码或接口变更须同步更新两种语言的 Typst 文档并重新编译 PDF（`python docs/build.py`），PDF 随源码提交。
- 插图源文件为 draw.io（`docs/figures/src/{zh,en}/*.drawio`），以黑白配色为主；导出后须渲染检查，排除异常连线、
  元素遮挡与文字溢出。
- 参考文献维护在 `docs/refs.yml`（中文版 GB/T 7714、英文版 IEEE 输出）；事实性陈述须有可核查的出处。

## 开发流程

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev,nvml,docs]"
pytest                                   # 单元测试
python scripts/smoke_test.py --offline   # 端到端冒烟测试（各服务独立进程；测试场景，不需要下载数据）
pyflakes src tests scripts examples      # 静态检查
python docs/build.py                     # 文档有改动时（--lang zh / en 只编译一种语言）
python docs/figures/export.py            # 插图源文件有改动时，需要 draw.io 桌面版
```

- CI：`.github/workflows/ci.yml`；镜像：`.github/workflows/image.yml`（`ghcr.io/x1ngchui/cpc-platform`）。
  Docker Hub 镜像 `xingchui/cpc-platform` 由维护者手动推送（`docker buildx build --platform linux/amd64,linux/arm64 ... --push`）。
- 第三方 GitHub Actions 固定到提交 SHA，由 Dependabot 更新。
- 发布：语义化版本标签 `vX.Y.Z`；变更记录 `CHANGELOG.md`（英文）。
