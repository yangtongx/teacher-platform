---
name: rag
description: 教材/教参 RAG 索引与检索（SQLite FTS5 BM25 中文检索 + 可选 DeepSeek 重排）
metadata:
  {
    "openclaw":
      {
        "emoji": "🔎",
        "requires": { "bins": ["python3"], "libs": ["sqlite3"] },
      },
  }
---

# RAG 检索（教材/教参知识库）

两个脚本：

- `skills/rag/scripts/rag_index.py` — 建/更新索引
- `skills/rag/scripts/rag_query.py` — 检索，可选重排

## 何时使用

- **建索引**：老师上传了教材/教参、`tmp/` 里已有解析好的 txt，需要把内容进库时。
- **检索**：需要按教材原文/教参表述来写教案、出题时。这是有 RAG 库时的**唯一**素材来源，
  禁止凭印象编造教材原文（见 AGENTS.md 红线）。

库文件约定（契约固定，不要改名）：

| 库 | 内容 |
|----|------|
| `rag/textbook.db` | 教材 |
| `rag/teacher_guide.db` | 教参 |

数据库状态检查只允许 `ls rag/*.db 2>/dev/null && echo "DB_EXISTS" || echo "DB_MISSING"`，
**禁止自己写 SQL 去查库状态**。

---

## 一、建索引 rag_index.py

```bash
python3 skills/rag/scripts/rag_index.py --input tmp/{x}.txt --db rag/textbook.db \
  --source-file uploads/{x}.pdf --smart
```

（命令在工作区根目录 `workspaces/lesson` 下执行；`--smart` 用于「库已存在、检查教材是否变化」的场景，
首次初始化不加 `--smart`，直接全量建。）

### `--smart` 语义（契约固定）

| 情况 | 动作 |
|------|------|
| `source_file` 不在 `indexed_files` 中 | **追加**（新教材） |
| `source_file` 在库中且 MD5 相同 | **跳过**（内容没变，不重写任何行） |
| `source_file` 在库中但 MD5 不同 | 该文件的**全量重建**（先删该文件旧块，再重新切块入库） |

MD5 算的是 `--input` 指向的 txt 文件内容。`--smart` 的跳过是逐源的：库里有多个文件时，
只有变化的那一个会被重建，其他文件的块一个都不动。

### 参数

| 参数 | 必填 | 说明 |
|------|------|------|
| `--input` | 是 | `parse_doc.py` 产出的 txt |
| `--db` | 是 | 目标库，例如 `rag/textbook.db`（不存在会自动创建，含父目录） |
| `--source-file` | 是 | 源文件登记名，例如 `uploads/教材.pdf`；它是 `indexed_files` 的唯一键 |
| `--smart` | 否 | 智能判定（见上表） |
| `--rebuild` | 否 | 强制全量重建该源文件 |
| `--chunk-size` | 否 | 切块目标长度（字符），默认 `700`，允许 100–4000 |
| `--overlap` | 否 | 块间重叠字符数，默认 `80`，必须小于 `--chunk-size` |
| `--embed` | 否 | 额外生成向量写入 `vec_chunks`（需要 embedding 服务，见文末） |
| `--max-idle-days` | 否 | 可选：清理超过 N 天未更新的旧源文件登记 |

`--input` 与 `--db` 用相对路径时相对**工作区根目录**，不是相对脚本位置。

### 输出

```
[全量建索引] uploads/教材.pdf
切块数：312（chunk-size=700, overlap=80）
数据库：rag/textbook.db
向量检索未启用，当前使用 BM25：vec_chunks 保持空表。如需向量检索，请配置 EMBEDDING_BASE_URL / EMBEDDING_API_KEY / EMBEDDING_MODEL 并加 --embed。
```

退出码：`0` 成功 / `2` 输入或结构不合法 / `4` 写库失败。

### 表结构（契约固定，**没有 `documents` 表**）

| 表 | 用途 |
|----|------|
| `chunks` | 切块正文。字段：`chunk_id`(自增主键) / `source_file` / `heading` / `content` / `chunk_index` / `token_count` / `char_count` / `page_start` / `page_end` / `created_at` |
| `vec_chunks` | **向量表**。字段：`chunk_pk`(主键，= chunks.chunk_id) / `source_file` / `model` / `dim` / `vector`(JSON 文本) / `created_at`。**只有配置了 embedding 服务才填**，否则永远是空表 |
| `indexed_files` | **源文件登记**。字段：`id` / `source_file`(**唯一**) / `md5` / `size` / `chunk_count` / `indexed_at` / `input_file`。`--smart` 的判定就读这张表 |
| `fts_chunks` | **FTS5 辅助全文索引**（`CREATE VIRTUAL TABLE ... USING fts5(content, heading, content='chunks', content_rowid='chunk_id')`）。它只存倒排索引，正文仍回 `chunks` 取；由 `rag_index.py` 负责同步，**不要手写 SQL 改它** |

中文检索走 `fts_chunks` 的 BM25。写入时每条块的检索文本是「标题 + 正文」经 bigram 分词后的 token 流：
CJK 连续串切成重叠的 2 字组合（`分数的意义` → `分数 数的 的意义`），ASCII 单词转小写按词切分。
**索引和查询用的是同一个分词函数**，这是中文能命中的前提。

## 二、检索 rag_query.py

```bash
python3 skills/rag/scripts/rag_query.py --db rag/textbook.db \
  --query "{80~150字的假设性答案}" --top-k 5 --rerank
```

### `--query` 必须写 HyDE 假设性答案

禁止用「二次函数」这种短关键词。要写一段 80~150 字、以特级教师口吻直接回答问题的句子
（这就是 HyDE：拿一段假想答案去和教材正文做措辞对齐）。例如：

> 分数表示把单位「1」平均分成若干份，取其中的一份或几份的数。分母表示平均分的份数，
> 分子表示所取的份数；分子比分母小的分数叫真分数，分子大于或等于分母的叫假分数。
> 分数与除法关系密切，被除数相当于分子，除数相当于分母，因此分数值等于分子除以分母。

### 参数

| 参数 | 必填 | 说明 |
|------|------|------|
| `--db` | 是 | 索引库路径 |
| `--query` | 是（除非用 `--list-sources`） | 80~150 字假设性答案（HyDE） |
| `--source-prefix` | 否 | **只检索 `source_file` 以该前缀开头的片段**，例如 `uploads/高二/`。多学科拆 agent 后用它按年级圈定范围（见文末「多学科与多年级」） |
| `--list-sources` | 否 | **只列出库里已登记的材料及块数**（年级视角的目录），不检索；可与 `--json` 搭配 |
| `--top-k` | 否 | 返回条数，默认 `5` |
| `--rerank` | 否 | 用 DeepSeek 对话接口给候选打分重排 |
| `--json` | 否 | 以 JSON 输出（程序消费用） |
| `--max-chars` | 否 | 每条结果正文最多输出多少字符，默认 `1200` |

### 输出

默认给人看，每条含来源、章节标题、页码、分数与正文：

```
查询：分数表示把单位1平均分成若干份……
检索方式：bm25+deepseek-rerank

[1] uploads/教材.pdf / 第三章 分数的意义（第 1 页）
    相关度 1.0000
    分数表示把单位「1」平均分成若干份，取其中的一份或几份的数……
```

加了 `--rerank` 时第二行会带上分解：

```
    相关度 16.0000（BM25 归一 1.0000 + 1.5 × 模型分 10）
```

`--json` 输出结构（字段稳定，可直接程序消费）：

```json
{
  "query": "...",
  "engine": "bm25",
  "reranked": false,
  "vector_enabled": false,
  "vector_note": "向量检索未启用，当前使用 BM25……",
  "warnings": [],
  "hits": [
    {
      "rank": 1,
      "chunk_id": 12,
      "source_file": "uploads/教材.pdf",
      "heading": "第三章 分数的意义",
      "chunk_index": 11,
      "page_start": 42,
      "page_end": 42,
      "score": 1.0,
      "norm": 1.0,
      "bm25": 2.906,
      "rerank_score": null,
      "text": "……"
    }
  ]
}
```

- `score` 是**排序用的分数**（越大越相关），等于 `norm + 1.5 × rerank_score`。
- `norm` 是把**本次候选池**的 BM25 归一化到 `0~1` 后的值（第 1 名 1.0，最末名 0.0；
  候选分数全部相同时退化为第 1 名 1.0、其余 0.0，名次顺序仍稳定）。不用 BM25 绝对值当
  展示分数，是因为它随语料规模漂移，小语料里 IDF 甚至会让相关片段的 BM25 正好等于 `0`，
  看起来像"不相关"。
- `bm25` 是 SQLite `bm25()` 取负后的**原始分数**，只作诊断参考。它可能是 `0.0` 甚至负数，
  这不代表"没命中"——**能不能命中看有没有返回这条结果，先后看名次与 `score`**。
- `heading` 识别不出来时是空串；`rerank_score` 在没重排或模型没给这一条分数时是 `null`。

### 检索与重排语义

1. **BM25 是必选路径**。查询经同一套 bigram 分词后拼成 FTS5 MATCH 表达式，按
   `bm25(fts_chunks)` 升序取候选（SQLite 的 bm25 越小越相关）。
2. **字面兜底**：如果 FTS5 一个 token 都没命中，脚本退化为按字面片段覆盖率排序的兜底检索。
   这是纯字符串匹配，不是向量相似度；命中不了就是空结果，脚本**不会编造相似度**。
3. **`--rerank`**：把候选片段（含 chunk id 和正文）连同查询交给 DeepSeek 对话接口打 0~10 分，
   最终分 = `BM25 归一化(0~1) + 1.5 × 模型分(0~10)`，按最终分重排。
   先归一化再叠加，是因为 BM25 绝对值会随语料规模漂移，直接用会时而压过、时而盖不过模型分。
   这个权重下的实际行为：**语料词面匹配很干净时 BM25 说了算；模型在两个候选上给出
   1 分以上差距（10 分制里很小的一档）时就能改变顺序；模型给满分时可以压过 BM25 第 1 名。**
   重排只作用在前 10 条候选池内，不会把没召回的内容变出来。
   - 没有 `DEEPSEEK_API_KEY` → **不报错**，降级为纯 BM25，stderr 打印
     `警告：未设置 DEEPSEEK_API_KEY，--rerank 已降级为纯 BM25 结果（顺序不变）`，
     退出码仍是 0。
   - 调用失败/返回格式不对 → 同样降级，stderr 打印
     `警告：重排失败，已降级为纯 BM25 结果：...`。
   - 环境变量：`DEEPSEEK_API_KEY`（必需）、`DEEPSEEK_BASE_URL`（默认 `https://api.deepseek.com`）、
     `DEEPSEEK_MODEL`（默认 `deepseek-chat`）。

退出码：`0` 成功（含重排降级）/ `2` 参数或数据库不合法 / `4` 检索失败。

## 向量检索：**当前没有**（2026-09-17 核实并纠正）

> ⚠️ **不要把"配置了 EMBEDDING_*"当成"向量检索已生效"。**
> 实测（读 `rag_query.py` 源码）：检索路径**只有一条** —— `search()` 走 SQLite FTS5 的 BM25。
> `vec_chunks` 表由 `rag_index.py --embed` **写入**，但**没有任何代码读它**；
> `--json` 里的 `vector_enabled` 只是"环境变量配没配"的**状态报告**，
> 与"检索时用不用向量"无关。`engine` 字段永远是 `bm25` 或 `bm25+deepseek-rerank`。
>
> 也就是说：现在把 40 GB 教材全部 `--embed` 一遍，**检索结果不会有任何变化**，
> 只是多占磁盘。要真正启用向量检索，必须先**实现检索路径**（查询向量化 → 相似度检索 →
> 与 BM25 融合 → 保留来源/页码），这是一项独立开发任务，不是配几个环境变量。

### 为什么当初默认没有向量检索

这台机器上只有 DeepSeek 的**对话**接口，DeepSeek **不提供 embedding 接口**；
环境里也没有 `numpy` / `chromadb` / `faiss` / `sentence-transformers`。
所以中文检索用的是 SQLite FTS5 的 BM25（配合本项目自实现的 bigram 分词）。
`vec_chunks` 表按契约建好并保留，**在实现检索路径之前它只是"已备好的位置"**——
脚本**绝不伪造向量，也绝不伪造相似度**。

### 如果将来要启用（三件事都要做，缺一不可）

1. **有 embedding 服务**（三个环境变量齐全才算"能写入向量"）：

   ```bash
   export EMBEDDING_BASE_URL=https://<你的-embedding-服务>/v1
   export EMBEDDING_API_KEY=<key>
   export EMBEDDING_MODEL=<模型名>
   python3 skills/rag/scripts/rag_index.py --input tmp/教材.txt --db rag/textbook.db \
     --source-file uploads/教材.pdf --smart --embed
   ```

   `rag_index.py` 会把每块向量写进 `vec_chunks`（JSON 文本存储，避免依赖 numpy），
   并在输出里打印 `向量检索：已启用（EMBEDDING_MODEL=...）`；调用失败只 stderr 警告，
   `vec_chunks` 保持空表，BM25 索引照常可用。

2. **实现检索路径**（当前**没有**）：查询侧向量化 + 相似度检索 + 与 BM25 融合。
   可选做法：只把 `chunk_id` 存向量库、正文仍回本地 `chunks` 取（便于引用页码），
   或向量库里带 payload（省一次查询，但存储翻倍）。

3. **决定存储放哪**：向量条数 ≈ 块数，1024 维 float32 ≈ **4 KB/条** ——
   如果你只想上"某位老师那个年级"（几万条），本地 SQLite 就够，**不需要买向量库**；
   只有"全量 40 GB 语料"（可能几百万~上千万条、向量 10~40 GB）才值得考虑托管向量服务。
   成本与选型见 `docs/textbook-corpus-plan.md` 第 8 节。

## 失败排查

- `错误：输入 txt 不存在：...` → 先跑 `parse_doc.py`（见 `skills/doc-parser/SKILL.md`）。
- `错误：输入 txt 是空的：...` → 解析产物为空，多半是扫描版 PDF，没有文字层。
- `错误：数据库不存在：...` → 库还没建，先跑 `rag_index.py`；注意命令要在工作区根目录执行。
- `错误：数据库表结构不完整，缺少：...` → 库是别的工具建的或建到一半。契约表结构是
  `chunks` / `vec_chunks` / `indexed_files`（**没有 `documents` 表**），用 `rag_index.py` 重建。
- `错误：建 FTS5 索引失败（本机 sqlite3 可能不支持 FTS5）：...` → 该 sqlite3 没编译 FTS5。
  服务器上已验证 FTS5 可用；换环境时先用 `python3 -c "import sqlite3;print(sqlite3.sqlite_version)"`
  确认版本，并确认 `fts5` 在编译选项里。
- 检索结果为空 → ①查询写得太短、不是 HyDE 句子；②确认目标文件已经进库
  （看 `rag_index.py` 的输出，或换一个更明显的词查一次）；③确认 `--db` 指向的库确实包含该教材
  （教材 `rag/textbook.db`、教参 `rag/teacher_guide.db`，两个库别搞混）。
- 结果里 `heading` 为空 → 该块所在位置没有可识别的标题行，属正常；正文与页码仍然可用。
- 重复运行 `rag_index.py` 不会产生重复块：同一个 `source_file` 的旧块会先被删掉再写入，
  `--smart` 且 MD5 相同时则连块 id 都不变（幂等）。

## 多学科与多年级（2026-09-16 追加）

### 学科靠"一个学科一个工作区"隔离

按学科拆 agent 后，**每个学科有自己的 `rag/textbook.db` 与 `rag/teacher_guide.db`**
（工作区也各自独立，见 `docs/multi-subject-agents.md`），所以检索天然只落在本学科语料内 ——
物理库里不会出现数学教材。

### 年级靠 `source_file` 前缀区分（必须显式限定）

同一个学科通常有多个年级，材料放在 `uploads/<年级>/…`（**保持原文件名**，只按年级分目录）。
于是 `source_file` 就是 `uploads/高二/必修第三册.pdf` 这种形态，**前缀即年级**。

```bash
# 1) 先看这个库里有哪些年级的材料（不要凭猜直接查）
python3 skills/rag/scripts/rag_query.py --db rag/textbook.db --list-sources

# 2) 带年级前缀检索（HyDE 查询照旧）
python3 skills/rag/scripts/rag_query.py --db rag/textbook.db --source-prefix uploads/高二/ \
  --query "{80~150字假设性答案}" --top-k 5 --rerank
```

行为契约（有测试钉住，见 `tests/test_skill_rag.py::年级前缀过滤测试`）：

* 前缀只**缩小范围**，不改变检索与排序逻辑；不传前缀 = 全库检索（旧行为不变）；
* **前缀范围内没有材料时返回空结果**，绝不回退到全库 —— 否则"问高二却拿到高一的内容"
  会被当成命中，比"没命中"更糟；
* `--list-sources` 不传 `--query` 也能跑；两者都没有时报参数错误（退出码 2）。

### 缺本年级材料时的三条出路

明确说明「本学科暂无该年级材料」，然后让老师选：
① 按课程标准范围出题（**必须标注无教材原文依据、不给页码、不假称引用**）；
② 上传该年级教材/教参后再出题；③ 老师直接给范围描述。
无论哪种，都不得编造教材版本、章节目录、页码。
