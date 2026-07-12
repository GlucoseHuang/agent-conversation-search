# conversation-search

给 AI agent 的长期记忆。把所有历史会话建成可检索的本地向量库，agent 可以用自然语言问"上次我们是怎么决定的"，而不是靠关键词碰运气。

- 读 opencode / Hanako 的 session JSONL
- 用阿里云百炼 `text-embedding-v4` 把每条消息转成向量
- 向量存本地 SQLite
- 检索时按余弦相似度排序，匹配的消息和整个对话上下文一起返回

## 为什么做这个

大模型的上下文窗口是有限的。一句"上次 TTS 方案怎么定的"应该足够让 agent 找回当时的讨论、约束条件和结论，不用用户记得是哪天、哪条消息、用的什么措辞。

关键词检索遇到同义词和改述就废。纯摘要的 RAG 又会丢掉原对话的细节。这个工具把原始消息都留着，按意思排。

## 安装

```bash
git clone https://github.com/GlucoseHuang/conversation-search.git
cd conversation-search
pip install -r requirements.txt
cp .env.example .env
# 编辑 .env，填 DASHSCOPE_API_KEY
```

DashScope 密钥在 <https://dashscope.console.aliyun.com/> 申请。

## 配置

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DASHSCOPE_API_KEY` | — | 必填 |
| `CONVERSATION_SESSIONS_DIR` | `~/.hanako/agents/hanako/sessions` | session JSONL 所在目录 |
| `CONVERSATION_DB_PATH` | `./conversations.db` | SQLite 索引文件 |
| `DASHSCOPE_BASE_URL` | `https://dashscope.aliyuncs.com/compatible-mode/v1` | OpenAI 兼容端点 |
| `EMBEDDING_MODEL` | `text-embedding-v4` | |
| `EMBEDDING_DIMENSIONS` | `1024` | 推荐范围 512–1024 |

## 用法

### 建索引

```bash
python3 embed.py                # 增量：只处理新增/变更的 session
python3 embed.py --dry-run      # 只统计消息数和预估费用
python3 embed.py --full         # 清空数据库重建
```

可以反复跑没事。脚本会按 `mtime` 和 `msg_id` 去重，不会重复烧 API 额度。

### 搜索

```bash
# 语义检索，能理解同义词和改述
python3 search.py --query "TTS 语音合成" --limit 5

# 关键词，纯 SQL LIKE，不调 API
python3 search.py --keyword "Lo-Fi" --limit 10

# 限制日期范围
python3 search.py --query "毕业论文" --date-from 2026-04-01 --date-to 2026-04-27

# 排除当前 session，避免回声
python3 search.py --query "音乐生成" --exclude-session <session_id>

# 机器可读的输出
python3 search.py --query "音乐生成" --format json

# 只看匹配的那条消息，不带上下文
python3 search.py --query "音乐生成" --no-full-session
```

### 当 opencode / Hanako skill 用

把整个目录丢到 skills 路径下（比如 `~/.hanako/skills/conversation-search`），agent 就可以直接调 `embed.py` / `search.py`。`SKILL.md` 里的 description 是 skill loader 读的元信息。

## 输出长这样

```
Found 3 matching conversation(s):

============================================================
[1] 2026-04-12 - 音乐分享  (score: 0.802)
  [User] Lo-Fi Electronic 太上头了，能再来点带人声的吗？
>>  [Assistant] 好嘞～让我深挖一下你的歌单，找几首合适的。
  [User] 嗯，最好是晚上写代码听的

============================================================
[2] 2026-04-18 - TTS 实验  (score: 0.741)
  ...
```

`>>` 标记的是匹配的那条，其余是这个 session 的上下文。脱离上下文的结果没什么用，所以默认会把整段对话带出来。

## 费用

百炼 `text-embedding-v4` 报价 0.0005 元/千 token（约 0.5 元/百万 token）。

| 场景 | 大概费用 |
| --- | --- |
| 1 万条消息全量重建 | 0.5–1 元 |
| 每天几百条新消息的增量 | < 0.01 元 |

## 架构

```
Session JSONL ──► embed.py ──► SQLite + 百炼 API ──► 向量存储
                                                   │
查询文本   ──► search.py ──────────────────────────┤
                                                   ▼
                                          numpy 余弦相似度
                                                   │
                                                   ▼
                                        按 session 聚合，渲染
```

- 向量存 SQLite BLOB，单条 ~4 KB，float32 数组。不用额外服务、daemon、schema migration。
- 相似度用 numpy 算余弦，个人规模够快。消息数过 10 万再考虑换 FAISS 或 sqlite-vec。
- 增量更新靠文件 `mtime` 和消息 `msg_id`，幂等。
- session 聚合：一条匹配带出整个对话，不丢上下文。

## 隐私

SQLite 数据库（`conversations.db`）里是每条被索引消息的原文。**别提交到 git。** 已经在 `.gitignore` 里了。

多用户共用机器时，建议给每个用户设一个独立的 `CONVERSATION_DB_PATH`。

## License

MIT，详见 `LICENSE`。
