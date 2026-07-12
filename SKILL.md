---
name: conversation-search
description: 在所有历史对话中做语义检索。从 opencode/Hanako 的 session JSONL 文件构建本地向量索引（DashScope text-embedding-v4 + SQLite），支持语义查询、关键词兜底、session 聚合、按日期过滤。用于查找过去的决定、讨论、上下文。
---

# conversation-search

给 agent 加长期记忆。会话向量化存入本地 SQLite，按语义相似度检索。

## 做什么

- `embed.py` 读 session JSONL，把每条消息向量化后存进 SQLite。默认增量运行，只处理新增或变更的 session。
- `search.py` 把查询也向量化，按余弦相似度排序，并按 session 聚合。匹配上的那条消息会和整个对话上下文一起返回。

依赖阿里云百炼 `text-embedding-v4`（1024 维，OpenAI 兼容接口）。不需要额外的向量数据库。

## 命令速查

```bash
# 建索引 / 刷新索引
python3 embed.py
python3 embed.py --full       # 清空重建
python3 embed.py --dry-run    # 只统计不写入

# 语义检索
python3 search.py --query "TTS 语音合成" --limit 5

# 关键词兜底
python3 search.py --keyword "Lo-Fi"

# 按日期范围
python3 search.py --query "毕业论文" --date-from 2026-04-01 --date-to 2026-04-27

# JSON 输出
python3 search.py --query "音乐生成" --format json

# 排除当前 session
python3 search.py --query "音乐生成" --exclude-session <session_id>

# 只看匹配的那条消息，不带上下文
python3 search.py --query "音乐生成" --no-full-session
```

### embed.py 参数

| 参数 | 作用 |
| --- | --- |
| `--full` | 清空数据库，从头重建 |
| `--dry-run` | 只统计消息数和预估费用，不写库 |

### search.py 参数

| 参数 | 作用 |
| --- | --- |
| `--query` | 语义查询，走向量检索 |
| `--keyword` | 纯 `LIKE` 文本匹配，不调 API |
| `--limit` | 返回几个 session（默认 5，最大 20） |
| `--date-from` / `--date-to` | 限制日期范围，格式 `YYYY-MM-DD` |
| `--exclude-session` | 排除某个 session id（避免回声当前对话） |
| `--no-full-session` | 只显示匹配的消息，不带完整 session |
| `--format` | `readable`（默认）或 `json` |

## 配置

所有配置走环境变量，参考 `.env.example`：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DASHSCOPE_API_KEY` | 无 | 必填 |
| `CONVERSATION_SESSIONS_DIR` | `~/.hanako/agents/hanako/sessions` | session JSONL 所在目录 |
| `CONVERSATION_DB_PATH` | `./conversations.db` | SQLite 索引文件路径 |
| `DASHSCOPE_BASE_URL` | `https://dashscope.aliyuncs.com/compatible-mode/v1` | |
| `EMBEDDING_MODEL` | `text-embedding-v4` | |
| `EMBEDDING_DIMENSIONS` | `1024` | |

## 典型用法

- 找回过去的决定：`python3 search.py --query "上次用哪个模型"`
- 找技术讨论：`python3 search.py --query "TTS 方案"`
- 翻历史想法：`python3 search.py --query "想做什么项目"`
- 时间窗口收窄：搭配 `--date-from` / `--date-to`
- 专有名词：上 `--keyword`，比如 `Lo-Fi` 这类向量分不开的词

## 结果怎么呈现给用户

1. 先给日期和标题，再给相似度分数
2. 展示整个 session，匹配的那条用 `>>` 标记，用户能看到上下文而不是孤零零一句
3. 找不到就直说，不要拿擦边结果凑数
4. 如果匹配到了当前正在进行的 session，用 `--exclude-session` 过滤掉

## 架构

```
conversations.db (SQLite)
├── sessions   session_id, filename, date, title, message_count, file_mtime
├── messages   session_id, msg_id, role, content, content_short, timestamp,
│              msg_index, embedding (BLOB, float32)
└── 检索       numpy 余弦相似度

数据流：
  Session JSONL → embed.py → SQLite + 百炼 API → 向量存储
  查询文本      → search.py → 百炼 API → numpy 余弦 → session 聚合
```

几个选择的理由：

- `text-embedding-v4` — 中英文都强，0.5 元/百万 token
- SQLite BLOB + numpy float32 — 不用额外服务，单向量 ~4 KB，个人规模够用
- 增量更新按 `mtime` + `msg_id` — 重跑便宜且幂等
- session 聚合 — 一个匹配带出整段对话，保留上下文

## 费用

按 `text-embedding-v4` 0.0005 元/千 token 估算：

- 1 万条消息全量重建约 0.5–1 元
- 每天增量几百条新消息，< 0.01 元
