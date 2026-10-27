# vendir × Potology task-handoff 最小验证

验证日期：2026-10-27。结论：**原生 vendir 可以承担这个仓库型 Skill 的跨文件系统内容交付和锁定版本恢复。当前需要的是少量项目验收及环境绑定，不需要另造包管理器。**

这是隔离实验，不是生产集成。没有新增 manifest 协议、依赖求解器、lock 格式、后台服务或自动更新器。没有修改 Skills Auditor 生产代码或 Potology 的工作区与安装目录。

以上描述第一阶段实验；后续已完成的最小生产接入与真实 Agent 验收见 [INTEGRATION.md](INTEGRATION.md)。

## 文件

- [vendir.yml](vendir.yml)：原生 vendir 配置；使用完整 Git 获取。
- [run.py](run.py)：准备精确提交与合成更新，启动两个临时 Linux 环境并回收资源。
- [probe.py](probe.py)：本例专用验收，包含文件核对、原有行为测试、Auditor 注册和预期失败反例。

脚本属于测试装置。目标环境的 Git/Python 依赖来自已有镜像；脚本没有实现包安装器。

## 验证对象与环境

| 项目 | 本次选择 |
| --- | --- |
| Skill | Potology `.agents/skills/task-handoff` |
| 实际来源提交 | `9f1fd18022ecaaee14f8d2a67bf35821f48dc710` |
| 内容范围 | 该提交的完整仓库树，共 51 个普通文件，保留相对目录结构 |
| vendir | `v0.46.3`，Linux arm64，固定发布文件 SHA-256 |
| 目标运行时 | 已有 Potology 镜像中的 Linux arm64、Python 3.12、Git |
| 源文件系统 | 专用 Docker 卷，ext4，验收时只读 |
| 目标文件系统 | 每次新建容器的独立 tmpfs |
| 网络 | 验收容器 `--network=none` |
| 注册位置 | 仅临时容器 `/work/registered/skills` |

从现有 Potology 仓库只读获取指定 Git 提交，不取未提交文件。完整树包含已提交的公共上下文投影；不复制原生会话数据库、登录缓存、私钥、实际 Codex home 或现有项目卷。没有启动 Codex、激活 hooks 或派发任务。

为了验证分支推进，测试仅在私有实验镜像仓库中添加一条注释，产生合成后继提交。它不是 Potology 的新版本，不会推送到 Potology。锁文件固定的是原始真实提交，而非重新打包后虚构的来源提交。

Docker 服务无法绑定本机工作区路径，因此先通过 `docker cp` 把夹具送入新建卷。**真正由 vendir 执行并被测量的交付是 ext4 → tmpfs**；比较 mountinfo 和设备号，避免把两个不同路径误当作不同文件系统。这证明跨文件系统行为，不等同于跨云网络交付或真实云部署。

## 验证步骤

1. 固定 vendir 发布文件摘要、Potology 完整提交和现有运行镜像 ID。
2. 建立私有 Git 镜像；`probe-main` 最初指向真实来源提交。另建一个合成后继提交，暂不推进该分支。
3. 首个容器执行原生 `vendir sync`，生成 `vendir.lock.yml`。
4. 逐一对比 51 个已提交文件的内容摘要和 Git 可执行位；检查实际文件系统不同。
5. 检查本例声明的工具、schema、文档与 Skill 内部文件，解析 SKILL.md 中的相对文件链接。
6. 观察 vendir 保留的 Git HEAD、工作区和 transport origin。仅将临时目标 origin 绑定到已确认的 Potology canonical origin，以供项目指纹使用。
7. 在目标执行两个真实工具的帮助入口、工程迁移 inventory，以及 Potology 原有 12 项迁移行为测试。
8. 调用现有 Auditor 的 integrate → apply → verify，注册到临时目录。
9. 运行两个依赖反例：只交付完整 Skill 文件夹；删除 Skill 目录外的一个必需 Python 模块。
10. 销毁首个目标，推进镜像仓库的 `probe-main`，在第二个全新容器中使用首轮原生 lock 执行 `vendir sync --locked`。
11. 验证恢复内容与第一轮一致；另做 unlocked 对照，确认它实际取得合成新版。
12. 用 `depth: 1` 重做 locked 恢复，保留旧提交取不到的预期失败证据。
13. 删除实验容器和专用卷，保留本地证据供检查。

## 已观察的结果

成功运行的证据位于 gitignored 的 `.skills-auditor-local/vendir-potology/run-07/`。

| 验收项 | 结果 | 含义 |
| --- | --- | --- |
| 跨 ext4/tmpfs 交付 | passed | mount 类型与设备号不同；不是路径改名模拟 |
| 全部 51 个文件和可执行位 | passed | 与实际来源 Git tree 对应内容一致 |
| 声明的依赖与 Skill 相对链接 | passed | 完整仓库交付覆盖本例选定依赖 |
| 原生 Git 工作区 | passed | 本配置保留可读 HEAD 和干净工作区，无需补建 `.git` |
| 工具入口与实际 inventory | passed | 在目标运行真实 Python 工具，不只检查文件存在 |
| Potology 原有迁移测试 | passed | 12 项在两个目标中各通过一次；不是 24 个不同测试 |
| 现有 Auditor 注册/验收 | passed | 临时链接及回执验证成功；不代表原生 Agent 已发现 |
| 分支推进后的 locked 恢复 | passed | 新容器恢复原始提交，payload 摘要与第一轮一致 |
| unlocked 对照 | passed | 取得合成后继提交，证明来源确实已变化 |
| 完整 Skill 目录缺少外层依赖 | expected failure | vendir 交付成功，项目依赖检查明确报告缺失 |
| 丢失 `tools/context_protocol.py` | expected failure | 真实工具退出 1；Auditor 当前 Skill 目录回执仍 passed |
| `depth: 1` 恢复旧 lock | expected failure | vendir 退出 1，`reference is not a tree` |

两轮的文件集合摘要相同：

```text
168af148724d62c099c60df11c09d2df32f2f007e3e09155b479e81e447a1a18
```

该摘要只用于本次测试的文件及可执行位对比，不是新增产品 lock 格式；不包含 `.git` 内部数据。Git HEAD 与 origin 单独检查。

## 需要补什么，以及不需要补什么

### 直接复用 vendir

- Git 获取、目录交付、精确引用记录和 locked 恢复。
- 本次完整 Git 来源保留的原生工作区；不要假设所有来源类型或过滤配置都有同样行为。
- 更新仍通过原生配置/lock 产生候选结果，不增加竞争性的版本解析或锁文件。

### 最小补充范围

1. **仓库型 Skill 的交付范围。** `task-handoff` 需要外层工具和文档；初期明确交付整个固定仓库即可。不要把 Skill 文件夹的哈希当成全部依赖的验收。
2. **项目专用行为检查。** 保留已有 12 项测试和少量依赖检查。这里没有证明能自动推导任意自然语言 Skill 的依赖，也没有理由立即建设通用依赖解析器。
3. **来源保留策略。** 本例的 `depth: 1` 不足以恢复分支后面的锁定提交；采用完整获取，并确保旧对象仍能访问。完整获取也不能挽救已被来源彻底删除的对象，未来可以由持久镜像或制品保留策略解决。
4. **逻辑项目身份与传输地址。** 本例通过 `file://` 私有镜像交付，Potology 却根据 origin 生成项目指纹，因此目标 origin 需要绑定到已确认身份。这是环境配置，不是下载器职责。
5. **宿主层验收。** 本次只验证临时安装入口与 Auditor 回执。目标 Agent 是否发现、加载和按预期使用 Skill，后续仍须由宿主适配与真实任务验收完成。

运行时要求也必须明确：本例依赖 Linux 的 `fcntl`、Python/SQLite 能力以及 Git。此次通过不能外推为原生 Windows 或任意空白容器可直接运行。

**建议继续采用“原生 vendir + 项目验收 + 现有 Auditor”组合。** 暂不增加生产包管理模块；本例证明至少获取、锁定、恢复与完整 Git 工作区可以复用现成能力。

## 复现

需要 Python 3、Git、Docker CLI、可读取 Potology 的 Git 通道，以及已存在于 Docker 服务上的 Linux 镜像（Python 3.12、SQLite FTS5、Git）。执行器目前校验 Linux arm64/amd64 的固定 vendir 发布文件；本次只实测 arm64。

在 Skills Auditor 仓库根目录运行，使用一个不存在的输出目录：

```sh
python3 experiments/vendir-potology/run.py \
  --source-url https://github.com/the-mag-tech/potology.git \
  --commit 9f1fd18022ecaaee14f8d2a67bf35821f48dc710 \
  --project-origin https://github.com/the-mag-tech/potology.git \
  --image potology:0.162.1-context-v1 \
  --output .skills-auditor-local/vendir-potology/my-run
```

`--source-url` 可替换为已授权的只读 SSH Git 地址。本次实际通过现有远端工作区读取。运行器不拉取或构建容器镜像，先将镜像引用解析为本地 image ID；不自动安装全局工具。

输出文件：

- `source.json`：来源提交、合成后继提交、镜像 ID、工具与脚本摘要。
- `baseline.lock.yml`：未经改造的 vendir 原生 lock。
- `baseline.json` / `restore.json`：逐项检查、命令输出和预期失败结果。
- `cleanup.json`：专用卷回收结果。

证据目录保留本地 Git 夹具和下载工具，默认不提交。不能把其中的 `file:///input/source.git` 原样当成生产源地址。

## 调试记录与边界

保留了此前失败运行，避免只保存最终成功：首次 Mac bind mount 不可用；随后修正了传输目录 ownership，以及浅镜像/分支引用配置；完整获取实测推翻了“vendir 必然不保留 Git 工作区”的假设；浅克隆恢复失败被转为明确负向用例。

尚未验证：云端网络、凭据发放、原生宿主发现/执行、真实会话迁移、断网重试、部署中断后的切换、跨平台 ABI、任意 Skill 的依赖推导及业务状态回滚。发布文件使用固定 SHA-256 校验，本次未验证发行签名。

本次临时容器未挂载现有项目卷、Docker socket 或用户认证目录；使用只读根文件系统、无网络和专用临时目录。被测项目原工作区保持原样。
