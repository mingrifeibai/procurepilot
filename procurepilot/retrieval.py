"""可离线复现的分段词法检索基线，返回可回读的原文偏移。

中文采用相邻双字、英文采用词项，BM25 和原文短语排名使用 RRF 融合，
再按查询词覆盖率重排。此实现没有向量模型，不宣称具备语义向量召回。
检索分数代表相关度而非事实可信度；正文始终是不可信的外部数据。
"""

import hashlib
import math
import re
from collections import Counter
from .service import fail

METHOD = "lexical_bm25_phrase_rrf_v1"
CHUNK_SIZE = 800
OVERLAP = 200


def tokenize(text):
    """词项仅用于评分，原文不改写，Unicode 大小写折叠不影响字符偏移。"""
    tokens = []
    for match in re.finditer(
        r"[\u3400-\u9fff]+|[a-z0-9]+(?:[-_.][a-z0-9]+)*", text.casefold()
    ):
        term = match.group()
        if "\u3400" <= term[0] <= "\u9fff":
            tokens.extend(term[i : i + 2] for i in range(len(term) - 1))
            # 单字检索仍能召回；较长中文查询主要由双字区分相关性。
            tokens.extend(term)
        else:
            tokens.append(term)
    return tokens


def chunks(artifacts, chunk_size=CHUNK_SIZE, overlap=OVERLAP):
    """按原始字符切段并重叠，拒绝超限而不静默漏掉后面的附件。"""
    if sum(len(a["text"]) for a in artifacts) > 2_000_000:
        fail("当前离线检索最多处理工作区200万字符，请拆分工作区或接入外部索引", 422)
    result = []
    for artifact in artifacts:
        text = artifact["text"]
        digest = hashlib.sha256(text.encode()).hexdigest()
        for start in range(0, len(text), chunk_size - overlap):
            end = min(start + chunk_size, len(text))
            snippet = text[start:end]
            result.append(
                {
                    "artifact_id": artifact["id"],
                    "filename": artifact.get("filename", ""),
                    "offset": start,
                    "end_offset": end,
                    "snippet": snippet,
                    "source": f"artifact:{artifact['id']}:offset:{start}",
                    "content_sha256": digest,
                    "untrusted": True,
                }
            )
            if end == len(text):
                break
    return result


def search(artifacts, query, limit=20):
    """有界多路词法召回，输出包括源文件、原文片段、排名方法和分数。

    同一附件最多三个不大幅重叠的片段，避免单个长文件占满全部证据预算。
    BM25 参数固定为 k1=1.2、b=0.75，RRF 常数为60，便于对照评测。
    """
    query = query.strip()
    if not 1 <= len(query) <= 200 or not 1 <= limit <= 20:
        fail("检索词长度应为1到200，结果数量应为1到20", 422)
    terms = set(tokenize(query))
    if not terms:
        fail("检索词需要包含中文、字母或数字", 422)
    docs = chunks(artifacts)
    if not docs:
        return []
    counts = [Counter(tokenize(d["snippet"])) for d in docs]
    lengths = [sum(c.values()) for c in counts]
    average = max(1, sum(lengths) / len(docs))
    frequencies = {t: sum(t in c for c in counts) for t in terms}
    bm25 = {}
    phrases = []
    coverage = {}
    for i, (doc, count) in enumerate(zip(docs, counts)):
        matched = terms & count.keys()
        if not matched:
            continue
        coverage[i] = len(matched) / len(terms)
        score = 0.0
        for term in matched:
            frequency = count[term]
            idf = math.log(
                1 + (len(docs) - frequencies[term] + 0.5) / (frequencies[term] + 0.5)
            )
            score += (
                idf
                * frequency
                * 2.2
                / (frequency + 1.2 * (0.25 + 0.75 * lengths[i] / average))
            )
        bm25[i] = score
        if query.casefold() in doc["snippet"].casefold():
            phrases.append(i)
    ranked = sorted(
        bm25, key=lambda i: (-bm25[i], docs[i]["artifact_id"], docs[i]["offset"])
    )
    phrases.sort(key=lambda i: (-coverage[i], -bm25[i], i))
    fused = Counter()
    for ranking in (ranked, phrases):
        for rank, index in enumerate(ranking, 1):
            fused[index] += 1 / (60 + rank)
    # 覆盖率只调整排名，不推断原文未出现的参数、兼容关系或承诺。
    final = sorted(fused, key=lambda i: (-(fused[i] * (0.5 + coverage[i])), i))
    selected = []
    for i in final:
        doc = docs[i]
        siblings = [d for d in selected if d["artifact_id"] == doc["artifact_id"]]
        if len(siblings) >= 3 or any(
            abs(d["offset"] - doc["offset"]) < CHUNK_SIZE // 2 for d in siblings
        ):
            continue
        selected.append(
            {
                **doc,
                "score": round(fused[i] * (0.5 + coverage[i]), 8),
                "matched_terms": sorted(terms & counts[i].keys()),
                "method": METHOD,
            }
        )
        if len(selected) == limit:
            break
    return selected


def requirement_query(requirement):
    """只从结构化需求构造短查询，不执行或接受附件中的指令。"""
    categories = {"computer": "电脑 内存", "monitor": "显示器", "dock": "扩展坞"}
    parts = [requirement.get("title", "")[:80]]
    for need in requirement.get("needs", []):
        parts.append(categories.get(need.get("category"), ""))
        parts.append(need.get("interface", ""))
        parts.append(need.get("purpose", ""))
    return " ".join(dict.fromkeys(parts))[:200].strip() or "采购"


def focus_excerpt(hit, query, limit=400):
    """从命中的分段选取与查询最相关的连续原文，保留可直接回读的字符区间。"""
    text = hit["snippet"]
    tail = max(0, len(text) - limit)
    stride = max(1, limit // 4)
    starts = {*range(0, tail + 1, stride), tail}
    query = query.strip()
    phrase = re.search(re.escape(query), text, re.IGNORECASE) if query else None
    preferred = min(tail, max(0, phrase.start() - stride)) if phrase else 0
    starts.add(preferred)
    terms = set(tokenize(query))

    def score(start):
        window = text[start : start + limit]
        # 完整短语优先，同分窗口优先保留短语后的内容，减少尾部证据截断。
        return (
            bool(query and query.casefold() in window.casefold()),
            sum(len(term) for term in terms.intersection(tokenize(window))),
            -abs(start - preferred),
            -start,
        )

    start = max(starts, key=score)
    preview = text[start : start + limit]
    offset = hit["offset"] + start
    return {
        "preview": preview,
        "preview_offset": offset,
        "preview_end": offset + len(preview),
    }
