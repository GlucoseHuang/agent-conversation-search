---
name: agent-conversation-search
description: Search and read conversations across local AI agents (Claude Code, Claude Desktop Code, Codex, Kimi Code, OpenCode, DeepSeek Harness, HanaAgent, WorkBuddy). Use when the user asks to find a past conversation, inspect tool calls/subagents/goals, or hand off work from another agent. Every result includes the original file/SQLite location so the agent can verify against the raw source.
---

# agent-conversation-search

跨 agent 对话检索。目标不是“搜到一段文字”，而是让当前 agent 找到另一个 agent 的原始会话、工具调用、子代理、goal/todo，并能回到原始文件核对。

## 什么时候用

- “找一下 Claude Desktop 之前那个对话”
- “上次我们在 Codex 里怎么决定这个方案的？”
- “把 Kimi 那个任务的上下文接过来”
- “这个功能在哪个 agent 的会话里做过？当时用了哪些工具？”
- 需要跨 agent 查历史决策、约束、文件改动、未完成任务

## 命令

优先用已安装的 `acs`；没有 PATH 时用 `python3 ~/.agents/skills/agent-conversation-search/acs.py`。

```bash
# 刷新索引（默认增量；skill 调用前可先跑一次）
acs index

# 语义 + 关键词搜索
acs search --query "每日简报" --limit 5 --format json

# 限定 agent / 日期 / 工具
acs search --keyword "TTS" --agent claude-code --date-from 2026-04-01 --format json
acs search --query "文件改动" --tool Edit --format json

# 读完整会话（事件、tool calls、goals、todos、attachments）
acs get --session "<session_id>" --format json

# 结构化接手包（导航用，不替代 get）
acs handoff --session "<session_id>" --recent 12 --format json

# 回到原始行 / 原始 SQLite 行
acs raw --event "<event_id>"
```

## 输出契约

`search` 返回 session 列表，每个结果包含：

- `session_id`、`agent`、`title`、`project`、`created_at`、`updated_at`
- `match`：命中的事件（`event_id`、`role`、`kind`、`snippet`、`raw`）
- `score`、`matches`

`get` 返回完整 session：

- `events[]`：每条都有 `id`、`seq`、`role`、`kind`、`text`、`raw`
- `tool_calls[]`、`goals[]`、`todos[]`、`attachments[]`
- `raw_locator`：会话级原始位置

`handoff` 返回：

- `source`（agent/session/cwd/raw/event_count）
- `goals`、`todos`、`blockers`、`files`、`decisions`
- `recent_messages`、`key_messages`、`tool_state`
- `raw_refs`（每项对应的原始位置）
- `full_context.command`（指向 `get` 的完整上下文）

## 使用原则

1. **先 search，再 get，最后 handoff。** handoff 只是导航包；需要完整细节时一定用 `get`。
2. **每条结果都要看 `raw`。** 如果 search 的 snippet 不够，直接用 `acs raw --event <id>` 回到原始行；SQLite 源用 `raw` 里的 `table`/`pk` 回查。
3. **不要把 handoff 当摘要。** 它只做确定性提取，不编造结论；`decisions` 是原文片段，不是模型总结。
4. **不确定就回原文。** 这是这个 skill 的核心设计：索引可以漏，但 raw locator 不会。
5. **搜索时排除当前会话**，避免回声；用 `--exclude-session`（如果知道当前 session id）。

## 支持的 agent 名称

`claude-code`、`claude-desktop-code`、`codex`、`kimi`、`opencode`、`dsh`、`hana`、`workbuddy`。

## 典型跨 agent 接手流程

```bash
# 1. 找会话
acs search --query "每日简报" --format json

# 2. 读完整事件 + 工具调用 + 任务
acs get --session "claude-desktop-code:7d41a726-..." --format json

# 3. 生成接手包
acs handoff --session "claude-desktop-code:7d41a726-..." --recent 12 --format json

# 4. 如果 handoff 缺细节，回到原始 JSONL
acs raw --event "<event_id>"
```

## 安装与配置

- 索引库：`~/.agent-conversation-search/index.db`
- 配置：`~/.agent-conversation-search/.env`（600 权限，不提交）
- 安装：`python3 acs.py install --replace-old --force`
- 如果 agent 不识别 `~/.agents/skills`，安装脚本会在它已有的 skills 目录建软链。

## 隐私

入库和 embedding 前会自动脱敏 API key/token/password；原始文件不复制、不修改。`--no-embed` 可以完全本地运行，只用 FTS。