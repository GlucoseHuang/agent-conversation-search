# agent-conversation-search

跨 agent 的本地对话检索。它不是“再做一个聊天记录搜索”，而是让一个 agent 能找到另一个 agent 之前做过什么，并且能回到原始文件核对。

支持索引：

| Agent | 本地数据 |
| --- | --- |
| Claude Code | `~/.claude/projects/**/*.jsonl`，含 `subagents/*.jsonl` |
| Claude Desktop Code | `~/Library/Application Support/Claude*/local-agent-mode-sessions/**/.claude/projects/**/*.jsonl`，含 `.claude/tasks/*.json` |
| Codex | `~/.codex/sessions/**/*.jsonl`、`archived_sessions/*.jsonl.zst`、`state_5.sqlite`、`goals_1.sqlite` |
| Kimi Code | `~/.kimi-code/sessions/**/agents/*/wire.jsonl` 与 `state.json` |
| OpenCode | `~/.local/share/opencode/opencode.db` 的 `session` / `message` / `part` / `todo` |
| DeepSeek Harness (DSH) | `~/.dsh/sessions/**/session.v4.jsonl.zstd` |
| HanaAgent | `~/.hanako/agents/*/sessions/**/*.jsonl`、`subagent-sessions/**/*.jsonl` |
| WorkBuddy | `~/.workbuddy/projects/**/*.jsonl`，以及 `~/.workbuddy/tasks/*/*.json` |

Claude Desktop 的 **Cowork / 普通聊天** 记录目前不在本地完整落盘，只有 Code 模式的沙箱 `.claude` 可以索引。这是有意为之，不是遗漏。

## 它保存什么

统一成一张事件流，而不是只存“消息文本”：

- `message`：user / assistant 消息
- `reasoning`：思考过程
- `tool_call` / `tool_result`：工具名、参数、输出、状态
- `goal`：目标、阶段、预算
- `todo`：任务、状态、阻塞关系
- `attachment`：图片/文件的元数据与路径
- `subagent`：子代理会话与父子关系
- `file_change`：patch / 文件快照

每条事件都带 **raw locator**：

```json
{"kind":"file","path":"/Users/glucose/.claude/projects/.../x.jsonl","line":42,"hash":"..."}
```

或 SQLite：

```json
{"kind":"sqlite","path":"/Users/glucose/.local/share/opencode/opencode.db","table":"part","pk":"prt_...","column":"data"}
```

搜索、`get`、`handoff` 都会返回它。结果不理想时，agent 可以直接读原始行或原始数据库行，不依赖索引的二手描述。

## 安装

```bash
git clone https://github.com/GlucoseHuang/agent-conversation-search.git
cd agent-conversation-search
python3 -m venv .venv
.venv/bin/pip install zstandard
```

写入 embedding 配置（只放在本地，不要提交）：

```bash
mkdir -p ~/.agent-conversation-search
cat > ~/.agent-conversation-search/.env <<'EOF'
ACS_EMBEDDING_BASE_URL=https://your-openai-compatible-endpoint/v1
ACS_EMBEDDING_API_KEY=...
ACS_EMBEDDING_MODEL=qwen3.7-text-embedding
EOF
chmod 600 ~/.agent-conversation-search/.env
```

macOS 也可以把 key 放进 Keychain，配置文件只留 base URL 和模型：

```bash
security add-generic-password -a "$USER" -s agent-conversation-search -w '<key>' -U
```

读取顺序：环境变量 → `.env` → Keychain。

安装 skill 到 `~/.agents/skills/agent-conversation-search`，并按需给各 agent 建软链：

```bash
.venv/bin/python acs.py install
# 直接替换旧的 Hanako conversation-search skill
.venv/bin/python acs.py install --replace-old --force
```

安装会同时创建 `~/.local/bin/acs`。如果某个 agent 不识别 `~/.agents/skills`，安装脚本会在它已有的 skills 目录下建软链；没有的目录不会强行创建。

## 使用

```bash
# 全量/增量索引（默认增量；第一次会自动建库）
acs index

# 只建全文索引，不调 embedding
acs index --no-embed

# 搜索
acs search --query "每日简报" --limit 5
acs search --query "TTS 方案" --agent claude-code --date-from 2026-04-01
acs search --keyword "Lo-Fi" --agent opencode

# 读完整会话（含事件、工具调用、goal/todo、原始位置）
acs get --session "claude-desktop-code:7d41a726-f39f-4adc-981e-7db13784f69a"

# 结构化接手包
acs handoff --session "claude-desktop-code:7d41a726-f39f-4adc-981e-7db13784f69a" --recent 12

# 回到原始行
acs raw --event "<event_id>"

# 查看索引来源与统计
acs sources
acs stats
```

`search`、`get`、`handoff` 默认输出 JSON；加 `--format md` 可读版本。agent 调用建议用 JSON。

### 搜索过滤

`--agent`、`--project`、`--cwd`、`--date-from`、`--date-to`、`--role`、`--kind`、`--tool`、`--subagent`。

### 混合检索

- SQLite FTS5 全文/关键词，保证专有名词、代码、路径能命中。
- OpenAI 兼容 embedding 做语义召回；默认只 embedding 消息、reasoning、提问，工具调用/结果仍由 FTS 精确检索，避免把大量工具输出全部向量化。
- 两路结果用 RRF 合并；embedding 不可用时自动退回 FTS，不阻塞搜索。

## 接手包（handoff）

`handoff` 是**结构化导航包**，不是摘要替代品：

- source：agent、session、cwd、原始路径、事件数
- goals / todos / blockers
- files：工具调用和 patch 里出现过的路径
- decisions：带决策词的原文片段（不改写）
- recent_messages / key_messages：最近和关键消息原文
- tool_state：工具调用状态统计
- raw_refs：每一项对应的原始位置
- full_context：指向 `acs get` 的命令

它参考了 handoff 的公开实践：高耦合任务保留完整 trace 的可回溯性，低耦合部分才压缩；结构化字段必须带来源，避免摘要幻觉。如果 handoff 缺信息，调用方应该用 `get` 或 `raw` 回原文。

## 隐私

- 索引默认只存**脱敏后的文本**；API key、token、password、Bearer 等会在入库和发送 embedding 前打码。
- 原始 JSONL / SQLite **不复制、不修改**；只存路径、行号/主键和哈希。
- `~/.agent-conversation-search/.env` 权限 600，已加入 `.gitignore`。
- embedding 会把选中的事件文本发到你配置的 OpenAI 兼容端点；如果不想出本机，用 `--no-embed`，只走 FTS。
- 附件默认只存元数据，不复制文件、不做 OCR。

## 限制

- Claude Desktop 普通聊天 / Cowork 本地没有完整正文，暂不支持；Code 模式支持。
- WorkBuddy 的 task 目录与 session id 不一定能一一对应；匹配不到时任务不会强行挂到某个会话。
- `handoff` 不生成 LLM 摘要；它只做确定性提取。需要摘要时由调用方基于 `get` 的原文自行生成。
- 索引是缓存，原始文件删除后 raw locator 会失效；`--snapshot-raw` 尚未实现，后续可加。

## 架构

```
agent 原始文件 / SQLite
        │  adapters/*
        ▼
  归一化 Session / Event / ToolCall / Goal / Todo / Attachment
        │  redact + truncate
        ▼
  ~/.agent-conversation-search/index.db
  ├── sessions / events / tool_calls / goals / todos / attachments
  ├── events_fts (FTS5)
  └── embeddings (float32 BLOB)
        │
        ├── acs search  → FTS + cosine → RRF → session 聚合
        ├── acs get     → 完整事件流 + raw locator
        ├── acs handoff → 结构化导航包 + raw refs
        └── acs raw     → 直接读原始行/数据库行
```

## License

MIT