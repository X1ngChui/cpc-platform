#import "../../template/cpc-doc.typ": *

= 开发与发布 <g-dev>

== 约定

- 可替换部件（策略、传感器）经抽象接口与 entry points 注册接入（@g-policy）；新增实现不修改平台主体；
- 数据中心内部的状态与规则只在 `cpc.datacenter` 中实现，仿真器与站点代理都调用它；仿真与实验特有的部分分别在
  `cpc.sim` 与 `cpc.site` 中；
- 失败处理遵循"启动期报错、运行期隔离并显式报告"：不叠加降级链，不以默认值、模型估计值或其他数据源替代缺失的数据；
- 平台不包含原始数据的解析代码：新数据源以转换脚本整理为平台格式（`examples/datasets/`）；合成数据只用于测试；
- 平台内部的 HTTP 客户端使用 `trust_env=False`，服务间调用不经过系统代理；外部下载使用系统代理设置；
- 不提交密钥、主机地址与个人信息：本地部署文件已在 `.gitignore` 中排除（`deploy/cloud/*/scenario.yaml`、`.env` 等）；
- 代码风格：四空格缩进、`snake_case` 命名、类型注解；注释、日志与文档使用中文技术书面语。

== 测试

```bash
pytest                                    # 单元测试
# 端到端：各服务独立进程启动，含传感器故障与站点故障的隔离检查
python scripts/smoke_test.py --offline
python scripts/smoke_test.py              # 同上，使用默认场景（需准备数据集）
```

单元测试使用 `tests/data/scenario.yaml`：电网信号为随测试提交的转换结果（`tests/data/datasets/cn-grid-2026-07`），
负载为首次运行时生成的合成数据集。数据中心运行时的测试不依赖时钟，直接以显式时间调用（参考 `tests/test_datacenter.py`）；
没有真实硬件的传感器，以伪造的 sysfs 目录或命令输出测试解析逻辑（参考 `tests/test_sensors.py`）；涉及外部依赖的失败处理，
以 `httpx.MockTransport` 注入连接失败、503、超时等故障并断言状态与指标（参考 `tests/test_faults.py`）。

== 持续集成与发布

GitHub Actions 包含两个工作流：`ci.yml` 在 Linux x86_64 与 aarch64 上运行单元测试与离线冒烟测试，并编译文档；
`image.yml` 构建 amd64 与 arm64 镜像并推送到 `ghcr.io/<用户名>/cpc-platform`（推送 `main` 生成 `main` 与提交哈希标签，
推送 `v*` 标签生成版本号标签与 `latest`）。第三方 Actions 固定到提交 SHA，由 Dependabot 更新。

版本号遵循语义化版本，以 `vX.Y.Z` 标签发布。Docker Hub 镜像由维护者手动构建推送：

```bash
docker buildx build --platform linux/amd64,linux/arm64 \
       -t <用户名>/cpc-platform:<版本> --push .
```

== 文档

```bash
pip install -e ".[docs]"
python docs/build.py                # 编译 docs/pdf/design.pdf 与 docs/pdf/guide.pdf
python docs/figures/export.py       # 从 draw.io 源文件导出插图（需安装 draw.io 桌面版）
```

文档源文件为 Typst（`docs/design/`、`docs/guide/`），模板在 `docs/template/cpc-doc.typ`，示意图源文件在 `docs/figures/src/`
（draw.io，使用其内置图形库），导出的 PDF 插图在 `docs/figures/`；参考文献在 `docs/design/refs.yml`（GB/T 7714 格式输出）。
中文字体使用 Noto CJK（Debian/Ubuntu 软件包 `fonts-noto-cjk`）。代码或接口变更须同步更新文档并重新编译 PDF。
