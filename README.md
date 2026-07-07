# cross-review

跨厂商双审：让另一家公司的最强模型独立审你的方案。

## 这是什么 / 解决什么问题

同一个模型写方案时，常会把自己的盲区也带进总结和自检里。`cross-review` 把重要方案、diff 或执行结果交给第二厂商模型做只读独立审查，强制保留用户原始需求、待审内容原文和固定开放三问。它适合在定稿前、重要修改落地后、或你想要第二意见时使用。核心原则是无副作用：审查方只读材料，只输出判断和问题，不替你修改工作区。

## 核心功能/亮点

- 固定“审查输入三要素”：用户原始需求、待审内容原文、固定开放三问。
- 要求结构化输出：`Verdict`、三分类 `Issues`、`Summary` 和三问必答。
- 默认使用第二厂商 CLI 的只读沙箱，失败时明确标注降级，不伪装成完整双审。
- 支持双 lens 审查：常规位、对抗位、聚合位分别独立工作。
- 提供本地环境自适应模板，让使用者按自己的 CLI 和订阅情况配置。

## 安装

Claude Code：

```bash
git clone https://github.com/ruodou233/cross-review.git ~/.claude/skills/cross-review
```

Codex：

```bash
git clone https://github.com/ruodou233/cross-review.git ~/.agents/skills/cross-review
```

其他支持 `SKILL.md` 的平台：放入其 skills 目录即可。

## 使用示例

- “定稿前帮我 cross-review 这个技术方案。”
  - Agent 会构造三要素送审包，调用第二厂商 CLI 只读审查，并按 `Verdict / Issues / Summary` 回读结果。
- “这个 diff 已经改完，重要修改落地后做一次复审。”
  - Agent 会把用户原始需求和 diff 原文送审，重点检查回归、遗漏和未覆盖风险。
- “我想要第二意见，看看有没有更优做法。”
  - Agent 会保留完整待审内容原文，让独立模型回答更优做法、冗余删改和未覆盖风险。

## 首次使用：环境自适应

首次触发时，你的 Agent 应只读探测可用 CLI（如 `claude`、`codex`、`gemini` 等），试调用确认可用最强档，失败则降档。写入本地配置前，Agent 必须说明将写到哪里、写什么内容，并获得明确同意。

本地配置读取顺序：

1. `~/.config/agentops-skills/cross-review/local-config.md`
2. skill 目录内 `local-config.md`
3. 无配置时按 `SKILL.md` 的默认流程执行，并在本次会话内使用探测结果

配置格式见 `local-config.example.md`。只有一家厂商可用时，可以降级为同厂独立会话或子代理审查，但结论必须标注“同厂审查，独立性弱于跨厂”。

## Changelog

| 时间 | 变更 |
|---|---|
| 2026-07 | 首次开源发布 |

## 反馈与作者

这个 skill 我长期维护。如果你有修改方案、发现问题、或者改出了更好的版本，欢迎通过以下任一渠道找到我：

- GitHub：本仓库提 issue 或 PR
- 小红书：错误乱码
- 微信公众号：能工智人错误乱码
- B站：若逗道人

## 相关 Skill 推荐

<!-- 本表由维护脚本生成，勿手工编辑 -->
- [agent-orchestration](https://github.com/ruodou233/agent-orchestration)：长任务治理：主代理指挥、子代理干活、状态落盘、断点续跑
- [upgrade-audit](https://github.com/ruodou233/upgrade-audit)：升级审计：让 Agent 定期把对话里的知识沉淀进文档体系
- [de-ai-taste](https://github.com/ruodou233/de-ai-taste)：中文去 AI 味：逐条检测 AI 生成痕迹并给修改建议

完整目录见 [GitHub 主页](https://github.com/ruodou233)。
