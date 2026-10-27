# 真正云环境：可见性与锁定恢复验收

2026-10-27，**真实云端交付、Agent 工具可见性、销毁后在全新云环境恢复均已通过**。
GitHub 原地 `--full` 重建未完成；本报告不把它计作成功。两个专用测试实例均已删除，
原有其他项目的 Codespace 仍为 Shutdown，未对其执行操作。

本阶段没有修改 Skills Auditor 生产接口。继续使用原生 vendir 配置与 lock，以及已有
Auditor 的依赖验收、plan/apply/verify 和宿主注册。新增内容仅为实验脚本与报告。

## 实际环境与验收结果

GitHub Codespaces：2 核、8 GB 内存、32 GB 存储、SouthEastAsia；远端 Linux 内核为 Azure
构建。云资源由 GitHub API 创建/状态/删除回执证明，不以环境变量或本机容器代替。
固定使用 Codex CLI 0.162.1、vendir 0.46.3；实际 Python 为 3.14.2。

| 验收 | 结果 |
| --- | --- |
| 原生 locked 交付 | 两个独立云实例均恢复到 `9f1fd18022ecaaee14f8d2a67bf35821f48dc710`；配置与 lock 字节不变 |
| 输入一致性 | 原生配置、lock、Auditor 配置、wheel、vendir、两个验证脚本共 7 项摘要一致 |
| 交付内容 | 51 个跟踪文件的 SHA-256 与可执行位逐一相同 |
| 依赖与注册 | 14 个声明文件通过验收；在新宿主重新生成计划、应用、复验，没有搬运旧注册 |
| Potology 原有迁移测试 | 首轮与恢复轮各 12 项通过 |
| 原始云实例 Agent | 7 次 shell 调用全部成功，发现并实际读取 Skill、模板、inventory 与精确公共记录 |
| 新云实例 Agent | 首次验收拒绝了一条失败的合并读取；补充新会话的 12 次工具命令全部成功 |
| 工具文件系统可见性 | 两个实例的真实 Agent 均读取依赖文件并计算匹配摘要；不是仅看注册目录或 Agent 自述 |
| 最终状态 | 注册复验通过，交付仓库保持干净，证据取回后删除所有本次云资源 |

完整文件集合摘要为 `168af148724d62c099c60df11c09d2df32f2f007e3e09155b479e81e447a1a18`。
原生 lock 摘要为 `63970b4c47307cb9d5fe349747e1845f139150e39068c48614357b491e881daa`。

Agent 从可用技能目录发现 `$task-handoff`，提示没有提供 SKILL.md 路径。它通过工具读取注册后的
Skill 与 canonical packet template，校验实际 cwd、HEAD、origin 和工作区，执行 inventory、
`shared --public-only --ref HEAD`，再检索精确公共记录。最终解释与返回记录相符，并明确历史
请求不授权当前派发。没有执行任务派发、会话迁移或写入型续接。

## 环境重建实际发生了什么

1. 首个专用 Codespace 完成 baseline。交付目录及随机 canary 留在容器中，没有手动删除。
2. GitHub 接受 `--full` 请求，但约 27 分钟持续显示 Rebuilding；SSH 和日志连接超时。
   停止请求又返回“未运行”。这些事实不足以诊断根因，也不证明原地重建成功。
3. 在用户批准的延长时限内，删除该专用实例，并读回资源列表确认消失。
4. 创建第二个专用 Codespace。provider 名称/身份和容器 hostname 均不同，原交付目录不存在。
   只重新提供原生输入、固定来源 Git bundle、工具及 baseline 元数据；没有复制已安装 payload、
   宿主注册或认证。新实例通过官方设备流程独立登录。
5. 同一验收脚本执行 restore：比较原输入摘要，用 `vendir sync --locked` 重建交付，逐文件
   对比内容与可执行位，再生成新宿主注册。之后完成真实 Agent 验收与最终复验。

因此本次证明的是**整个云实例替换后的锁定恢复**。同一 Codespace 的原地 full rebuild 仍未验收。
SSH 会话没有暴露 `CODESPACE_NAME`；空值相等没有被当作身份通过。外部 provider 回执负责绑定
两次不同的实例及删除过程，运行时记录只提供容器和文件系统事实。

## 最小拓扑与复现

| 内容 | 本次位置与恢复方式 |
| --- | --- |
| 原生输入、固定工具、来源镜像 | `/workspaces/skills-auditor-cloud/inputs`；替换实例时重新提供并校验摘要 |
| 私有证据 | `/workspaces/skills-auditor-cloud/evidence`；删除前取回本机 |
| 实际交付、注册宿主、canary | `/home/codespace/skills-auditor-cloud-final`；必须由新环境重新生成 |
| Codex 认证 | 独立私有目录；每个实例官方登录，不复制本机或旧实例认证 |

[cloud_probe.py](cloud_probe.py) 是实验用两阶段验收脚本，不负责创建云资源、认证或长期环境管理。
先用 `baseline` 记录交付，再由外部操作者保存证据、重建或替换专用环境，最后用 `restore` 验收。
使用 `--inputs`、`--evidence`、`--delivery`、`--wheel`、`--vendir`、`--codex` 指定固定输入与路径；
可用 `--source-mirror` 指定已授权 Git 镜像。先运行 `--help` 查看参数。

restore 拒绝已有交付目录，要求容器身份变化、baseline 成功、输入摘要一致；它不会删除旧目录
伪装成恢复。若运行时暴露了两个 Codespace 名称，当前脚本要求名称一致；完整实例替换的 provider
身份由操作者另行核对。本次两端名称未暴露，外部回执明确记录替换，不依赖这项运行时检查。

本次 Codespace 的自动 GitHub 凭据绑定 Skills Auditor 仓库，因此 Potology 来源使用固定提交的
Git bundle 镜像。仅 vendir 子进程通过原生 Git `insteadOf` 读取该镜像；配置、lock 和 canonical
origin 不变。它证明固定来源内容可恢复，不证明跨互联网私有仓库凭据的自动发放。

## 工具可见性与失败证据

控制进程与 Agent 工具运行于不同挂载命名空间。首轮 Agent 的设备号为 45，新实例为 46；
两轮都实际计算出 `tools/context_protocol.py` 的 SHA-256：
`13a362a00dd84524d6bdf27a0db7901c30d1cd05ada032ff09a0ab380c6e9f8a`。
这与此前本机 `/dev/shm` “目录可发现但工具不可读”的反例形成对照。

登录 shell 可能重置工作目录。恢复轮首次 Agent 把模板绝对路径读取和多个相对路径读取放在同一
命令中；模板内容虽返回，后续文件读取失败使整条命令退出 1，验收器正确拒绝。保留该失败回执，
在相同交付和注册上启动补充只读会话，要求独立读取模板并显式 `cd`，12 条命令全部退出 0。
未放宽验收器、篡改失败结果、重新删除交付，或把 Agent 的“passed”自述当作证明。

前一轮验收器也暴露了合并输出解析问题：精确记录 JSON 与其他输出在同一个成功命令中。
检查器现同时解析整段 JSON 与独立 JSON 行，仍要求成功命令、实际记录 ID 和公共记录返回值；
此前工具无法读取 Skill 的反例继续被拒绝。

## 证据、清理和成本

原始命令、计划、回执与 Agent 事件保存在 gitignored 的
`.skills-auditor-local/vendir-cloud/`，不发布资源标识或登录材料：

- `before-rebuild/evidence/final/baseline/`：首轮完整成功证据。
- `replacement-final/evidence/final/restore/`：锁定恢复成功，但首次 Agent 模板读取验收失败的原始记录。
- `replacement-final/evidence/final/supplemental/`：补充成功会话、注册复验和干净工作区结果。
- `replacement-controller.json`、`replacement-verification.json`：不同云实例的绑定及汇总检查。
- `provider-after-cleanup.json`、`cleanup-summary.json`：删除和预算读回。

首次资源创建到最终删除确认约 76 分钟；用户明确将原 60 分钟上限延长到 90 分钟，预算仍为
US$1。按整个窗口保守计计算费用约 US$0.23，另有少量存储费；账单未结算，不能当作实际扣款。
两个专用实例顺序运行并已删除；本次未设置平台硬性消费上限。模型使用用户的官方登录，没有
复制 API key 或创建按 API 计费的模型凭据。

来源：[Codespaces 计费](https://docs.github.com/en/billing/concepts/product-billing/github-codespaces)、
[重建与持久化](https://docs.github.com/en/codespaces/developing-in-a-codespace/rebuilding-the-container-in-a-codespace)、
[完整重建命令](https://cli.github.com/manual/gh_codespace_rebuild)。

## 对最小接入的结论

现有分工足够支撑这条链路：vendir 固定来源并交付；Auditor 验收声明依赖并在目标宿主注册；
环境层负责工具可见路径、运行时、认证、来源保留、重建恢复和清理。需要补齐的是环境验收步骤，
尚无证据需要新增包协议、第二份 lock、下载器、后台同步服务或通用云适配层。

范围仍限于单个 Skill、两个 Codespaces 实例和只读实际使用；不代表 Kubernetes、所有 Agent
宿主、规模化发布、完整任务派发、自动 hooks 或原生会话恢复已通过。
