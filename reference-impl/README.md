# 审查标识与生命周期（ledger）—— 参考实现

这套代码把 `../SKILL.md`「审查标识与生命周期」一节里描述的协议落成可运行的最小实现，方便你的 Agent 读懂机制后按自己的仓库结构生成等价代码。**不是 drop-in 二进制**：直接照抄大概率跑不起来，你至少要改「用之前要改的地方」那几处。下面的快速上手命令是实际跑通过的，不是示意伪代码。

## 这套代码解决什么问题

多个方案/任务并发走双审时，光靠对话记录容易出现两类遗忘：
1. 「宣称已送审但实际没送审」
2. 「审查结果回来了但没人处理，方案继续往下推」

`review_ledger.py` 用一个状态机 + 内容寻址 manifest，把每次审查尝试机械绑定到具体的请求、材料、模型/档位钉参、session id 上，事后可用 `verify` 命令核对整条链路自洽，而不必信任任何一方的口头汇报。

## 状态机

```
open ─pre approve─> plan_approved ─impl-start─> implementing ─post approve─> post_approved ─close─> closed
```

- `open`：新建，等待方案审（pre phase）。
- `plan_approved`：pre 审查通过 + 记录用户确认摘录后进入。
- `implementing`：开始实施，绑定 `base_sha`。
- `post_approved`：post 审查（实施后审）通过，且批准区间（`base_sha`..`covered_commits` 里最后一个 commit，**不是**当前分支 tip）与 Git 历史逐一核对一致。
- `closed`：终态，不可重开；返工需要开新 `review_id` 并在 topic 里注明承接关系。

`request-changes` 只追加一条新 attempt，不改变 phase 内的状态。**rebase/改写/在批准后继续往分支上加新 commit 都不会被自动发现**：只有操作者主动执行 `impl-start --reason rebase|revision` 才会让已批准的 `post_approved` 失效退回 `implementing`；`verify` 不会对照当前分支自动侦测这类漂移，这是本机制"防遗忘 + 只读对账"定位的一部分，不是自动强制。

## 三路角色合同

`review_launcher.py` 固定跑三个角色：`regular`（常规审）、`adversarial`（对抗审，premortem 挑刺，不给 verdict）、`aggregate`（聚合两路完整输出，给最终 verdict）。每路产出一份 receipt（模型/档位钉参、prompt/output 的 sha256、session id、禁分叉核验结果），`finalize` 把三份 receipt 组装成一份不可变、内容寻址的 manifest。`dual-run` 用两个独立进程并发跑 regular/adversarial，互不可见对方输出，再把两路完整输出一起送 aggregate ——这是「双 lens」的落地方式。

## 用之前要改的地方

1. **`REVIEW_MODEL` / `REVIEW_EFFORT`**（`review_ledger.py` 头部，走环境变量）：占位符是 `<your-strongest-reviewer-model>`。改成你自己订阅内、跟你日常用的模型不同厂商的最强模型——`export REVIEW_MODEL=... REVIEW_EFFORT=...`，不要硬编码进代码。
2. **`CODEX_BIN`**（`review_launcher.py` 的 `run_role()`）：默认退回 PATH 里的 `codex`。如果你用别的 CLI（`claude`、其他厂商 headless 模式），要连带改 `argv` 拼装和 `trace_facts()` 里的 NDJSON 字段解析规则——不同 CLI 的 `--json` 输出 schema 不一样。**这是适配时最容易出安全问题的地方**：如果新 CLI 的事件流里有 `trace_facts()` 不认识的字段结构，禁分叉检测可能悄悄失效却依然标记 `fork_check.verified=true`（假阴性），而不是报错——改完这部分务必自己找一个"确实分叉了"的场景验证检测生效。
3. **`protected-paths.example.json`**：这只是一份 schema 示例，**本参考实现没有附带读取它的 pre-push 检查器**。想要"改保护路径必须先有 closed 的 review"这层强制，你需要自己写一个钩子去消费它；不写钩子的话，把这份文件放进仓库不会拦下任何东西。
4. **`review-templates/*.md`**：这三份角色合同已经是通用文本，可以直接用；想调整审查风格（比如三问的具体措辞）就改这里，脚本按脚本所在目录自动定位这三个文件，不依赖你把 `reference-impl/` 放在仓库的哪个位置。
5. **`cross-review/exemptions/` 里的机器标签**：`review_ledger.py exempt` 默认用本机 hostname 生成豁免记录的文件名，这个文件名会被 git 追踪。如果你的 hostname 带真实姓名或公司名，运行前设置 `export EXEMPT_HOST_LABEL=dev-1` 之类的通用标签覆盖它。

## 这版相对早期草稿修的几个坑（写出来是提醒你适配时也检查这几类问题）

- **审查原始轨迹不能进 tracked 工作树**：`run_role()` 现在把 raw NDJSON/stderr 写到 `git rev-parse --git-common-dir`（也就是 `.git/` 或其等价目录）之下，这个位置天然不被 git 追踪。早期草稿写进了工作树里的一个未加 `.gitignore` 的目录，容易被误提交——如果你改了落盘位置，记得确认新位置同样不会被 git 追踪。
- **归档进 tracked receipt 的命令行只留 basename**：`argv[0]`（CLI 可执行文件）如果是绝对路径，会把本机用户名和目录结构写进长期保留的 git 历史。现在只记录 `os.path.basename(argv[0])`，真实路径只在本次进程调用时使用、不落盘。
- **历史校验不该被"以后升级模型"打破**：`validate_manifest_data()` 早期版本拿当前的 `REVIEW_MODEL`/`REVIEW_EFFORT` 全局值去校验所有历史 receipt——模型换代或环境变量变了，旧记录会集体校验失败。现在拆成两层：`verify`（读历史）只检查 receipt 自报的钉参和 argv 是否自洽；只有 `finalize`（写新 attempt）才要求钉参必须等于当前 policy。
- **`plan-approve` 必须核对材料绑定，不能靠事后 `verify` 才发现**：早期版本只核对用户确认摘录的哈希，没检查 `--plan-file` 是不是真的等于 approve manifest 里被审查过的那份材料——方案 A 通过审查后，理论上可以误传方案 B 直接进入 `plan_approved`。现在 `plan-approve` 会先核对 `--plan-file` 的 sha256 落在 manifest 绑定的材料集合里，不匹配直接拒绝。
- **`EXEMPT_HOST_LABEL` 覆盖值原来没做清洗**：塞路径分隔符或 `..` 理论上能让豁免记录的文件名逃出 `exemptions/` 目录。现在覆盖值和默认 hostname 走同一条字符白名单（只留小写字母数字连字符，截断 32 字符）。
- **`impl-start` 原来不校验 `base_sha` 是否是有效 commit**：传一个不存在的 SHA 会让状态机卡在 `implementing` 且无法重新 `impl-start` 纠正。现在写状态前先 `git cat-file -e` 校验一次。
- **历史 commit 绑定原来接受短 SHA**：短 SHA 的长度会随仓库对象数增长或 Git 缩写配置变化而变，同一份材料可能在未来毫无修改的情况下重新校验失败。现在 `_verify_covered_commits()` 只认完整 40 位 SHA。
- **角色合同模板和模型钉参是同一类坑，之前只修了模型那一半**：新 attempt 原来只验证角色合同快照内部自洽，没有核对这份快照是否真的等于当前 `review-templates/*.md`——改了审查风格后，旧快照理论上还能继续拿来 `finalize` 新 attempt。现在 `finalize` 时会额外核对快照哈希等于当前模板文件哈希；`verify` 读历史时仍只看内部自洽，不受你日后改模板影响。
- **同一组 receipt 原来可以被重复 `finalize` 成多个 attempt**（只要改 `--n`）：现在 `finalize` 会扫描该 review 下所有已存在的 manifest，任何 receipt 的内容哈希或 session_id 已经被用过就拒绝，防止一次真实审查被登记成好几轮独立 attempt。
- **传错 `--n` 会写出没人认的孤儿 manifest、顺带把 receipt 烧掉**：`finalize` 原来只做 manifest 内部自洽校验，不核对台账当前实际期望第几个 attempt——传错 `--n` 会成功写出一份 `attempt` 命令永远不认的文件，而这份文件引用的 receipt 又会被上面那条复用检测认成"已用过"，导致这批 receipt 永久报废。现在 `finalize` 一开始就会问台账"这轮到底该是第几个 attempt"，对不上直接拒绝，不写任何文件。
- **失效后的旧 `post_approved` 能被正常命令重新"续上"**：`impl-start --reason rebase|revision` 让 `post_approved` 失效退回 `implementing` 后，原来的 `post-approve` 仍然会用 `latest_approve()` 捞到失效前那个旧 attempt，状态机看起来往前推进了，实际上从没经过失效后的新审查。现在失效时会记一个"水位"，`post-approve` 必须看到比水位更多的 post attempt 才放行，逼着你先跑一轮新的 post 审查。

以上修复都在一个全新临时仓库里走完整条 `new → dual-run → finalize → attempt → plan-approve → impl-start → post-approve → close → verify → render` 链路实测过，包括故意传错 `base_sha`、故意复用 receipt、故意用错误 `--n` 触发孤儿 manifest、故意在失效后不经新审查直接重新 `post-approve` 这几类负例，不是只改了代码没验证。

## 已知限制（继承自协议正文，不重复展开）

能力边界、可绕过路径、隐私与证据保留策略见上一级 `../SKILL.md`「审查标识与生命周期」节的「能力边界」「证据保留与隐私」两段——这是唯一权威定义处，这里不重复。

这版参考实现经过多轮跨厂商双审，属于**教学/参考级别，不是生产加固版**；下面这几条判断为"值得知道、但不适合为了一份参考实现去做"的结构性限制，用之前请自己评估是否可接受：

- **完整 prompt/output 长期留在 tracked 归档里**（影响面最大的一条）：`SECRET_PATTERNS` 只能拦住几类形似真实密钥的字符串，拦不住本机路径、内部代号、组织架构这类非凭证型隐私。要彻底解决需要重新设计"哪些证据进 git、哪些留本机"这层分级存储，工作量接近一次小型重构，这版没有做。静态文本扫描通过 ≠ 完整脱敏，进 tracked 归档前建议自己再人工过一遍。
- **`verify --tip` 的快照范围只覆盖 `cross-review/`、`tools/` 两个目录**：`_snapshot_root()` 只用 `git archive` 提取这两个目录的内容。如果你的 `material_file`/`user_request_file` 放在仓库其他位置（比如根目录下的 `plan.md`），任何状态下 `--tip` 校验都会因为找不到这些引用文件而失败——这不是状态相关的限制，是快照范围的限制，建议把审查用到的材料文件也放进 `cross-review/` 下。另外只要 review 状态到了 `post_approved`/`closed`（会触发 `_verify_covered_commits()` 里的 Git 历史核对），临时目录没有 `.git` 上下文这一层还会再失败一次。想验证这类状态的历史 tip，目前只能 `git worktree add`/`checkout` 出一份带完整 `.git` 上下文的副本，在里面跑 `verify`（不带 `--tip`）。
- **不支持 linked worktree / 部分子模块场景**：raw 轨迹写入 `git rev-parse --git-common-dir`，这个函数在标准仓库里等于 `.git/`，但在 `git worktree add` 出的副本里指向主工作树的共享目录。如果你的 common-dir 落在当前工作树之外，`run_role()` 可能在已经写入 prompt/output/contract 之后，才在计算 raw 归档的相对路径时失败——留下部分已落盘的 tracked 产物。单一标准仓库（没有 linked worktree）不受影响。
- **模板 policy 只约束"新 attempt 生成时"，不约束"迟到的旧 manifest 登记"**：`finalize` 会强制新 manifest 匹配当前角色模板，但如果你手上有一份用旧版脚本、旧模板生成的 manifest 文件，`attempt` 命令登记它时走的是历史校验路径（只看内部自洽），不会因为模板已经更新而拒绝。这版实现只保证"全新启用"，不保证"早期草稿产出的 manifest 原地升级兼容"。
- **没有覆盖上述失败分支的自动化测试**：这版靠人工在临时仓库里跑 happy path + 若干负例验证，没有配套的 conformance test 套件。
- 豁免记录跨两个文件写入不是事务性的（中途失败可能留下悬空引用）；90 天原始轨迹清理和"每目录最多 1000 个文件"都只在下一次调用前尝试回收，不是运行时强制的硬上限——某次运行结束后，实际文件数可能短暂超过 1000，等下次调用时才会被清理掉；换 CLI 时的适配范围目前只覆盖 Codex 的 NDJSON 事件 schema，没有针对其他 CLI 的一致性测试；`fcntl` 文件锁意味着实现是 POSIX-only；`full_sha()` 只认 40 位 SHA-1，不支持 Git 新一代的 SHA-256 object format；浅克隆/部分克隆/对象被 GC 清理后，依赖 Git 历史核对的校验可能失败；全局 `verify` 只遍历 ledger 引用到的 manifest，不会发现游离在外的孤儿文件。

## 审查结果记录（review-log，可选）

如果你想要一份人可读的审查历史汇总（而不是每次都翻 ledger JSON），`review_ledger.py render` 已经能从 `cross-review/ledger/*.json` 生成一份 Markdown 表格（默认写到 `cross-review/review-log.md`，闭环一个 review 后跑一次即可）。字段定义如下：

| 字段 | 含义 |
|---|---|
| date | 该 review 的创建日期 |
| ref | 可长期访问的审查归档引用，如 `archive/<review_id>@<commit短hash>` |
| topic | 短标题，建议前缀 `pre:`/`post:` + 类别 |
| mode | `single-review` / `dual-lens` |
| source | 双 lens 时问题主要来源：`regular` / `adversarial` / `both` / `mixed`；非双 lens 填 `-` |
| issues | 最终输出的问题数（阻断/非阻断） |
| adopted/rejected/deferred | 发起方对问题的逐条判定 |
| rounds | 该 phase 的尝试轮数 |
| note | 一句话说明（mode 选择理由、降级原因、关键分歧），无则留空 |

不走审查流程的简单任务不必记这张表；这张表也不能替代审查原文归档本身。

## 快速上手（已实跑验证）

以下命令在一个全新的临时仓库里跑通过：`new` → `dual-run` → `finalize` → `attempt` → `plan-approve`，跑完状态从 `open` 走到 `plan_approved`。`post` phase（实施后审）走同样的 `dual-run` → `finalize` → `attempt` 序列，再跟 `impl-start` / `post-approve` / `close` 配合把批准区间绑定到具体 commit range——字段语义见 `review_ledger.py` 里 `validate_manifest_data()` / `verify_state()`，这两个函数本身就是最准确的协议文档。

```bash
cd 你的仓库根目录   # 必须是一个 Git 工作树；reference-impl/ 可以放在仓库内任意位置
export REVIEW_MODEL="你的最强第二厂商模型"
export REVIEW_EFFORT="high"

# 材料文件路径都是相对仓库根目录写的，下面假设 reference-impl/ 就放在根目录下
python3 reference-impl/review_ledger.py new --review-id 2026-01-01-example-change --topic "示例改动"

python3 reference-impl/review_launcher.py dual-run --review-id 2026-01-01-example-change --phase pre \
  --user-request-file request.md --material-file plan.md > dualrun.json
# dualrun.json 是形如 {"regular": "...", "adversarial": "...", "aggregate": "..."} 的 JSON，三条都是 receipt 路径

REGULAR=$(python3 -c "import json;print(json.load(open('dualrun.json'))['regular'])")
ADVERSARIAL=$(python3 -c "import json;print(json.load(open('dualrun.json'))['adversarial'])")
AGGREGATE=$(python3 -c "import json;print(json.load(open('dualrun.json'))['aggregate'])")

python3 reference-impl/review_launcher.py finalize --review-id 2026-01-01-example-change --phase pre --n 1 --mode dual \
  --receipt regular=$REGULAR --receipt adversarial=$ADVERSARIAL --receipt aggregate=$AGGREGATE \
  --material-file plan.md --user-quote-file user-confirm.md > manifest.txt

python3 reference-impl/review_ledger.py attempt --review-id 2026-01-01-example-change --manifest "$(cat manifest.txt)"

# 只有聚合结论是 approve 时才能进这一步；user-confirm.md 内容需与上面 --user-quote-file 一致
python3 reference-impl/review_ledger.py plan-approve --review-id 2026-01-01-example-change \
  --plan-file plan.md --user-quote-file user-confirm.md

python3 reference-impl/review_ledger.py show --review-id 2026-01-01-example-change   # 确认 status 已是 plan_approved
python3 reference-impl/review_ledger.py verify                                       # 核对整条链路自洽
```
