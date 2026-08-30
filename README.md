# cross-review

`cross-review` 是一个跨厂商只读独立审查 skill：在重要方案定稿前和重要修改完成后，让第二厂商模型从问题发现、简化审查、最佳实践调研三条独立路线检查原始材料，再由主 Agent 裁决并完成最终交付。

## 核心流程

```text
原始需求 + 待审原文
        │
        ├─ Issues：正确性、遗漏、阻断项
        ├─ Simplification：冗余、过度防御、维护负担
        └─ Research：官方资料、成熟实现、行业实践
        │
        └─ 主 Agent 逐条裁决 → 实施 → 全新会话独立验收 → 最终交付
```

三路共享同一份原始材料，但角色合同不同，不要求每一路重复回答同一组通用问题。审查方只读，不修改工作区，也不替主 Agent 决策。

完整、可移植的运行合同见 [`SKILL.md`](SKILL.md)。公开仓是该合同的唯一维护源；具体机器上的 CLI 路径、权限和调用包装应放在本地适配层，不复制或改写核心协议。

## 安装

Claude Code：

```bash
git clone https://github.com/ruodou233/cross-review.git ~/.claude/skills/cross-review
```

Codex：

```bash
git clone https://github.com/ruodou233/cross-review.git ~/.agents/skills/cross-review
```

其他支持 `SKILL.md` 的平台：把本仓放入对应的 skills 目录。

## 使用示例

- “定稿前 cross-review 这个技术方案。”
- “重要修改已经完成，用独立会话做实施后验收。”
- “给我一个跨厂商第二意见，尤其看看有没有不必要的复杂度。”

首次使用与写入权限规则见 [`SKILL.md`](SKILL.md)。

## 反馈

发现问题或有改进方案，欢迎在本仓提交 issue 或 PR。
