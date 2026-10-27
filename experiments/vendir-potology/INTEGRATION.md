# 最小接入与真实 Agent 验收

2026-10-27，完成第二阶段：原生 vendir 交付 → Auditor 依赖验收与注册 → Codex 实际使用。
前一阶段的锁定恢复实验见 [README](README.md)；操作步骤见
[接入文档](../../docs/vendir-integration.md)。

## 产品变化

- 继续直接使用 `vendir sync --locked`，保留原生 YAML 与 lock；没有新增下载器封装。
- 现有 `integrate` 新增可重复的 `--require-file`，配置中对应 `required_files`。
- 现有计划/回执可选记录依赖文件的规范路径、字节摘要与权限；apply 前后及 verify 检查这些文件。
- 缺失或漂移的外层依赖会阻止应用，或令复验失败；原有无依赖字段的计划保持兼容。
- 宿主注册复用 `--target-root`，本例明确使用 Codex 的 `.agents/skills`。旧版默认路径保持兼容，
  没有自动迁移已有安装。

[可运行配置](../../config/potology-vendir/skills-auditor.json)选择完整的 Potology 固定仓库，
并声明 12 个外层文件与 2 个原生交付输入。它使用现有 Auditor 配置格式的可选字段，不定义
Skill 包协议、版本解析规则、第二份 lock 或安装钩子。

## 验证结果

| 验收 | 结果与证据范围 |
| --- | --- |
| Auditor 单元与契约测试 | 212 项通过，分支覆盖率 92.2%；新增 8 项依赖行为测试 |
| 干净 wheel 冒烟 / 安装后端到端测试 | 5 / 3 项通过 |
| 构建、分发内容、Markdown 链接 | 通过 |
| Potology 原有迁移测试 | 12 项在交付后的 Linux 环境通过 |
| 原生 locked 交付 | 保持原锁不变，HEAD 为 `9f1fd18022ecaaee14f8d2a67bf35821f48dc710` |
| 跨文件系统 | 源 ext4、目标 overlay，设备号分别为 64785、46 |
| 缺失依赖反例 | 删除 `tools/context_protocol.py` 后，verify 退出 3；恢复后通过 |
| 示例配置完整链路 | 原生配置和 lock 字节不变；14 个声明文件完成 plan → apply → verify |
| 真实目标 Agent | Codex CLI 0.162.1；15 次 shell 工具调用全部退出 0 |
| 原工作区保护 | 交付仓库在 Agent 前后干净；实际 Potology 工作区原有未跟踪文件列表未变 |
| 临时资源 | 注册宿主、交付副本及远端专用测试目录已清理；证据保留本机私有目录 |

Agent 的初始工作目录是独立临时宿主，不是 Potology 来源目录。初始提示只点名 `$task-handoff`，
不提供 SKILL.md 路径；Agent 从目录条目报出 Auditor 注册路径，并通过工具读取它。随后它：

1. 读取 Skill、canonical packet template 和项目规则。
2. 校验实际 cwd、完整 HEAD、origin、干净工作区及 detached HEAD。
3. 执行 `context_migration.py inventory`，核对 51 个已提交文件。
4. 执行 `repo-context.py shared --public-only --ref HEAD`。
5. 执行 `repo-context.py record --public-only --ref HEAD --id …`，获取精确记录，解释其中的决策，
   给出下一步及当前授权边界；最后复查身份与文件状态。

验收依据包含实际工具命令、退出码和返回数据，不只依赖模型自述。事件检查器同时拒绝了前一轮
无法读取内容的失败运行。模型语义解释另经人工式核阅；事件检查器不声称验证任意任务的正确性。
测试 wheel 内的 `skills_auditor/` 文件与当前生产源码逐一比较一致。

## 实际发现的两个环境边界

目标镜像没有 pip/ensurepip。验收直接解开已构建的纯 Python wheel，以模块入口执行，没有改变
目标系统 Python 或安装新工具；正常 pip 安装通过独立干净 wheel 测试验证。

首次真实 Agent 运行使用 `/dev/shm` 交付。Auditor 校验和目录发现成功，但 Agent shell 有独立的
`/dev/shm` 挂载，读取 Skill 失败。修正为工具可见的 overlay 路径后，完整只读验收通过。
这说明环境管理必须保证交付路径在 Agent 工具的文件系统视图中可见。无需放宽 Agent 隔离设置，
也无需将这项环境职责塞进 vendir。前一阶段 ext4 → tmpfs 的锁定恢复证明仍然有效，不能将它等同
于此次真实 Agent 的可用性证明。

## 证据与复现

本机 gitignored 证据目录为 `.skills-auditor-local/vendir-integration/`：

- `unit.log`、`smoke.log`、`e2e.log`：软件测试结果。
- `live/run-02/`：目录可见、工具不可读的反例。
- `live/run-03/`：成功运行的计划、回执、交付输入、Agent JSONL 和最终输出。
- `live/sample-acceptance/`：使用最终示例配置完成的 14 文件验收。
- `live/agent-event-verification.json`：同一检查器对失败和成功事件的判定。
- `live/summary.json`：环境、工具版本和已测源码一致性摘要。

运行 [agent_probe.py](agent_probe.py) 需要已有授权登录的目标 Codex CLI、Linux 环境、固定 vendir
二进制、构建好的 wheel、原生锁文件，以及可读取的 Potology checkout。脚本 `--help` 列出参数；
每次使用不存在的输出目录。它不复制认证、修改 trust、激活项目 hooks 或创建桌面侧栏任务。
最终示例配置测试通过 Git 原生 `insteadOf` 临时配置使用已授权本地镜像，保持示例 YAML 和 lock
不变；未将互联网拉取或跨云网络列为已验证能力。

真正云环境的后续验收见 [CLOUD.md](CLOUD.md)。完整任务派发、原生会话恢复、自动 hooks、
写入型续接仍需各自验收。本阶段目标是
Skill 的交付、明确依赖验收、宿主注册与真实 Agent 的有界使用；这四项已完成。
