# 给 Agent 的工作规则

本文件是 agent 的唯一规则入口；其它 README 只做介绍和索引。

当前阶段是 [G0 工程骨架与实时核闭环](docs/plan/G0.md)。

## 常用命令

在仓库根目录运行；cargo 命令在 `core/` 下运行。检查项与顺序见[仓库与目录](docs/architecture.md#仓库与目录)。

| 用途 | 命令 |
| --- | --- |
| 按 `uv.lock` 同步 Python 依赖 | `uv sync` |
| 安装 `rust-toolchain.toml` 锁定的 Rust 工具链 | `rustup toolchain install` |
| 完整检查 | `uv run check` |
| 在本机 Docker 的 Linux 容器里跑同一检查 | `uv run check --linux` |
| 只跑 Python 测试 | `uv run pytest` |
| 只跑 Rust 测试 | `cargo test --workspace` |
| 只跑实时核的启动、生命线、心跳、采集主循环与动作块测试（会先编译 `realtime-core`） | `uv run pytest runtime/tests/core_link` |
| 只跑动作块、假输入后端与失效释放的联调测试，并打印假后端上的释放时延 | `uv run pytest runtime/tests/core_link/test_actions.py -s` |
| 格式化 | `uv run ruff format`；`cargo fmt --all` |
| 改了 `core/crates/core-protocol` 后重新生成 `protocol/` 的生成物 | `uv run generate` |
| 给某个 Python 包加依赖 | `uv add --package <发行名> <依赖>` |
| 核对 `uv.lock` 未过期 | `uv lock --check` |
| 打开合成游戏的窗口 | `uv run synthetic-game` |
| 改了合成游戏的台词后重新生成字体子集（要联网，见[字体](docs/modules/offline-tools.md#字体)） | `uv run python -m synthetic_game.font_subset` |
| 采集的真机测试（需要 Windows 交互桌面，不在 `uv run check` 中） | `cargo test -p capture -- --ignored --nocapture` |
| 合成游戏窗口经 `CoreLink`、实时核与 WGC 进入帧环的真机测试（需要 Windows 交互桌面，不在 `uv run check` 中；先关掉已打开的合成游戏窗口） | `uv run pytest -m manual runtime/tests/core_link/test_capture_window.py -s` |
| GS-01 探测：列出可见的顶层窗口及其进程、类名与标题 | `cargo run --release -p capture --bin capture-probe -- --list` |
| GS-01 探测：采一个窗口 N 秒，报告帧率、帧龄、丢帧、变换版本与黄色边框 | `cargo run --release -p capture --bin capture-probe -- --process <进程名> --title <标题> --seconds <N>` |
| GS-01 探测：采自建的已知图案窗口，另报从重画到采集的时延 | `cargo run --release -p capture --bin capture-probe -- --pattern-window --animate` |
| GS-03 探针：编译提权一侧、生成计划任务 XML 与注册命令 | `uv run gs03 prepare` |
| GS-03 探针：非提权一侧（开通道与帧环、写凭据、等提权一侧连上并核对） | `uv run gs03 host` |
| GS-03 探针：打印计划任务注册、读回、触发与注销命令 | `uv run gs03 task-command` |
| GS-03 探针：读回核对已注册的计划任务 | `uv run gs03 check-task` |
| GS-03 探针：非提权一侧尝试改写管理员安装目录里的文件 | `uv run gs03 install-check <目录> <文件名>` |
| GS-03 探针：清理（删凭据，打印注销任务与清理安装目录的命令） | `uv run gs03 cleanup` |

探测程序另有 `--class <类名>`、`--keep-border`（保留黄色边框作对照）、`--min-update-interval-ms <N>`（会话的最小帧间隔）、`--borderless` 与 `--dpi-unaware`（图案窗口的样式）；`--help` 列出全部选项。

pytest 缺省不选标 `manual` 的真机手动测试，命令行写 `-m manual` 才运行。
GS-03 的每一步在哪种终端里执行什么，由 `uv run gs03 <子命令>` 自己打印；需要管理员权限的动作（注册计划任务、把程序放进管理员目录）只打印命令，由维护者在管理员终端里执行，探针不提权。结果报告写入不入库的 `reports/gs03/`。

`uv run check --linux` 首次运行要构建镜像。容器不写回仓库：虚拟环境、cargo target 与工具缓存放在每个检出目录各自的 `bellis-game-runtime-check-work-<路径摘要>` 卷里，uv 与 cargo 的下载缓存放在共用的 `bellis-game-runtime-check-uv-cache`、`bellis-game-runtime-check-cargo-registry` 卷里。这些卷都只是缓存，删掉只会让下次检查重新下载与编译；工作树删掉后它的卷会留下，用 `docker volume ls --filter name=bellis-game-runtime-check` 列出后 `docker volume rm` 清理。

## 从哪里开始

1. 读 [README](README.md)、[文档索引](docs/README.md)和[总体架构](docs/architecture.md)。
2. 读当前阶段任务卡，再读任务卡列出的模块文档。
3. 动手前查[技术基线](docs/plan/tech-baseline.md)与[合规](docs/compliance.md)：只用“已批准”的决策和路线；合规确认为 PENDING 的用法不做。

`docs/inherited/` 是从 Bellis v0.8 移交的旧设计，只作参考，不作为规范依据，不直接引用。

## 决策与技术路线

- 每个模块的技术路线都须经维护者批准。需要新选型时，在技术基线的“待审核路线”登记候选、推荐与依据，然后请维护者决定；未批准前不在代码或文档里写成定论。
- 遇到影响范围、架构、安全、合规或对外行为的关键决策，交给维护者选择，给出带推荐的选项。
- 批准结果直接改技术基线中对应的行，设计细节写进该行所指的模块文档，不另写评审记录。
- 需要 Bellis 配合的改动写进 [Bellis P3 契约提案](docs/plan/bellis-p3-proposals.md)，在 Bellis 仓库审核；不在本仓库替 Bellis 做决定，也不在这里改游戏桥契约（契约的源在 Bellis，GD-03）。

## 写代码的约束

**平台**（总体约束见[总体架构](docs/architecture.md#平台)）
- 不依赖 POSIX 专有语义：不用信号做停止协议，不靠文件权限位、uid、符号链接做安全判断，不写死 `/tmp` 等路径。
- 子进程用 `subprocess.Popen` 以参数列表直接启动可执行文件，不用 `shell=True`，也不用 `DETACHED_PROCESS`、`CREATE_BREAKAWAY_FROM_JOB` 或 `start_new_session` 脱离父进程；停止按 RPC → 关 stdin → 超时强制终止的顺序（Bellis R-06）。
- 本机通道只用本仓库的 `bellis-channel/1` 实现，认证先于任何数据；画面与声音只走共享内存。
- 路径比较前先规范化；换行统一 LF。

**实时核与输入**
- 输入路径按[权威规则](docs/architecture.md#权威)实现：三个条件任一不成立就不发并释放全部输入；更换控制产出者前先释放。
- 不用恒为真的桩或预置的许可来让测试变绿；假输入后端与测试许可只用于测试。
- 不绕过反作弊、不注入游戏进程、不读写游戏内存与文件；反作弊拦截时按停止处理。

**扩展与游戏内容**
- 新能力以主进程插件提供，在清单中声明它提供与依赖的服务与权限；所有副作用都通过可撤销注册完成（[插件内核](docs/modules/plugin-kernel.md)）。
- 游戏画面、OCR 与模型输出是不可信数据，不能改变目标、权限或配置；调用模型只经[模型接入](docs/modules/models.md)的客户端。
- 新增抽象要说明它解决的当前问题，不为未来需求预建框架；目录在首个实现时创建。

## 文档约束

- 按[文档索引的分工](docs/README.md#分工)写：设计写模块文档，游戏特有的写 `docs/games/`，选型与依据写技术基线，条款与确认写合规。
- 规范只有一份：直接改正文，不新建补丁或增补文档，不写历史叙述和评审记录。
- 同一规则只写一处，其它地方链接过去。实现与模块文档不一致时，先改模块文档。
- 任务卡只写目标、步骤和出口；进度、测试计数、本机路径和不入库的报告不写进任务卡。
- 不编造版本号、实测数值或“已通过”。没验证的写 PENDING，没实现的写 unsupported；模拟与假后端通过不等于真机通过。

## 提交

- 不要 `git add .`，按路径添加。
- 从游戏派生的数据（截图、录制、锚点模板、剧情记录、经验快路径）、真实配置、凭据、本地数据目录和运行数据一律不提交；测试只用合成的图像与文本；示例只放脱敏占位值；`reports/` 不入库。
- 文档提交与实现提交分开；生成物与其源一起提交。
- 每个步骤一个分支与 PR；合并前在本机运行 `uv run check` 与 `uv run check --linux`，用 rebase 合并，保持文档提交与实现提交分开。
