<img src="https://cdn.nlark.com/yuque/0/2026/png/29413969/1776759843814-3835fde4-5808-4c90-94ce-b89dab60e582.png" width="1119" title="" crop="0,0,1,1" id="ud2685639" class="ne-image">

单纯用向量检索有一个盲区：**精确词汇匹配**。比如大家问「RRF 算法」，文档里写的是「Reciprocal Rank Fusion」，向量能摸到语义；但要是专有名词（「PO 号」「SKU-8821」「第3.2条」这类），有时向量反而不如全文准。

<img src="https://cdn.nlark.com/yuque/0/2026/png/29413969/1776759944316-7b2092cc-d6e9-4c0d-a677-963376ceea09.png" width="1065" title="" crop="0,0,1,1" id="udbf83c72" class="ne-image">

全文检索擅长精确词，向量检索擅长语义。鸡哥在这节把两路结果用 RRF 揉成一路排序。

<img src="https://cdn.nlark.com/yuque/0/2026/png/29413969/1776759956730-aaf6b225-603e-44ae-9fae-fdd5f37606f2.png" width="1118" title="" crop="0,0,1,1" id="u8c21606b" class="ne-image">

---

## 一、RRF 算法原理
<img src="https://cdn.nlark.com/yuque/0/2026/png/29413969/1776759964232-7c3b5e37-f983-4162-aa38-59e51fd61268.png" width="1113" title="" crop="0,0,1,1" id="uf01c5db4" class="ne-image">

RRF（Reciprocal Rank Fusion）：把多路检索结果融合成一个排序。

```plain
公式：RRF_score(doc) = Σ 1 / (k + rank_i)
k = 60（平滑参数，防止排名靠前的文档权重过大）

例子：
  文档 A：向量检索排名 1，全文检索排名 5
  文档 B：向量检索排名 3，全文检索排名 1
  文档 C：向量检索排名 2，全文检索未出现（等效排名为无穷大）
  
  RRF_score(A) = 1/(60+1) + 1/(60+5) = 0.01639 + 0.01538 = 0.03177
  RRF_score(B) = 1/(60+3) + 1/(60+1) = 0.01587 + 0.01639 = 0.03226
  RRF_score(C) = 1/(60+2) + 0         = 0.01613
  
  排名：B > A > C
```

---

## 二、全文检索的查询词处理
<img src="https://cdn.nlark.com/yuque/0/2026/png/29413969/1776759993219-23e3fc5c-ec80-4c95-9575-d78fab21993f.png" width="1112" title="" crop="0,0,1,1" id="uf32da2c7" class="ne-image">

PostgreSQL 全文检索需要把用户问题转成 `tsquery` 格式。中文比较特殊，`to_tsquery` 不支持直接传中文句子，需要提取关键词。

```java
package com.jichi.ragkb.service;

import lombok.extern.slf4j.Slf4j;
import org.springframework.stereotype.Component;

import java.util.Arrays;
import java.util.List;
import java.util.stream.Collectors;

/**
 * 将用户查询转换为 PostgreSQL tsquery 格式。
 *
 * 简单实现：按空格和常见分隔符切分，提取有意义的词。
 * 生产中可以接入 jieba 等中文分词工具效果更好。
 *
 * 不单独暴露 HTTP，由下面的 HybridRetrieverService 注入使用即可。
 */
@Component
@Slf4j
public class TsQueryBuilder {

    // 停用词（这些词在全文检索中无意义）
    private static final List<String> STOP_WORDS = List.of(
            "的", "了", "是", "在", "有", "和", "与", "或", "这", "那",
            "什么", "怎么", "如何", "为什么", "哪些", "怎样", "请问",
            "a", "an", "the", "is", "are", "what", "how"
    );

    /**
     * 将问题转为 tsquery 格式。
     * 例如："API 限流策略是什么" → "API & 限流 & 策略"
     */
    public String build(String query) {
        if (query == null || query.isBlank()) return null;

        // 按空格、标点切分
        String[] tokens = query.split("[\\s\\p{P}]+");

        List<String> keywords = Arrays.stream(tokens)
                .map(String::strip)
                .filter(t -> !t.isBlank())
                .filter(t -> t.length() >= 2)              // 过滤单字符
                .filter(t -> !STOP_WORDS.contains(t.toLowerCase()))
                .collect(Collectors.toList());

        if (keywords.isEmpty()) {
            // 降级：取整个查询的前20字符
            keywords = List.of(query.substring(0, Math.min(20, query.length())));
        }

        // 用 & 连接（AND 查询），至少含所有关键词
        String tsQuery = String.join(" & ", keywords);
        log.debug("[TsQuery] query='{}' → tsQuery='{}'", query, tsQuery);
        return tsQuery;
    }
}
```

---

## 三、HybridRetrieverService
```java
package com.jichi.ragkb.service;

import com.jichi.ragkb.entity.DocChunk;
import com.jichi.ragkb.repository.DocChunkRepository;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;

import java.util.*;
import java.util.stream.Collectors;

@Service
@RequiredArgsConstructor
@Slf4j
public class HybridRetrieverService {

    private final EmbeddingService embeddingService;
    private final DocChunkRepository chunkRepository;
    private final TsQueryBuilder tsQueryBuilder;

    @Value("${rag.retrieval.vector-top-k:20}")
    private int vectorTopK;

    @Value("${rag.retrieval.fulltext-top-k:20}")
    private int fulltextTopK;

    /** RRF 平滑参数，通常取 60 */
    private static final int RRF_K = 60;

    /**
     * 混合检索：向量检索 + 全文检索，RRF 融合排序。
     *
     * @param question 用户问题（原始，未向量化）
     * @param kbIds    要查询的知识库 ID 列表
     * @param topN     最终返回的 chunk 数量（RRF 排序后取 TopN）
     * @return 按 RRF 分数排序的 chunk 列表（含分数信息）
     */
    public List<ScoredChunk> retrieve(String question, List<Long> kbIds, int topN) {
        // Step 1：向量检索
        float[] queryEmbedding = embeddingService.embed(question);
        String embeddingStr = toVectorString(queryEmbedding);

        List<DocChunk> vectorResults = kbIds.stream()
                .flatMap(kbId -> chunkRepository.findByVectorSimilarity(kbId, embeddingStr, vectorTopK).stream())
                .collect(Collectors.toList());

        // Step 2：全文检索
        String tsQuery = tsQueryBuilder.build(question);
        List<DocChunk> fulltextResults = new ArrayList<>();
        if (tsQuery != null) {
            fulltextResults = kbIds.stream()
                    .flatMap(kbId -> chunkRepository.findByFullTextSearch(kbId, tsQuery, fulltextTopK).stream())
                    .collect(Collectors.toList());
        }

        log.debug("[HybridRetriever] 向量检索召回={}，全文检索召回={}",
                vectorResults.size(), fulltextResults.size());

        // Step 3：RRF 融合
        List<ScoredChunk> merged = rrfMerge(vectorResults, fulltextResults);

        // Step 4：取 TopN
        List<ScoredChunk> topResults = merged.stream()
                .limit(topN)
                .collect(Collectors.toList());

        log.info("[HybridRetriever] RRF 融合后 TopN={}，返回 {} 条", topN, topResults.size());
        return topResults;
    }

    /**
     * RRF 融合两路结果。
     * 去重：同一个 chunk 出现在两路结果中时，分数累加。
     */
    private List<ScoredChunk> rrfMerge(List<DocChunk> vectorList, List<DocChunk> fulltextList) {
        // key: chunkId → RRF 分数
        Map<Long, Double> scoreMap = new LinkedHashMap<>();
        Map<Long, DocChunk> chunkMap = new HashMap<>();

        // 向量检索结果计分
        for (int rank = 0; rank < vectorList.size(); rank++) {
            DocChunk chunk = vectorList.get(rank);
            double rrfScore = 1.0 / (RRF_K + rank + 1);
            scoreMap.merge(chunk.getId(), rrfScore, Double::sum);
            chunkMap.put(chunk.getId(), chunk);
        }

        // 全文检索结果计分（累加）
        for (int rank = 0; rank < fulltextList.size(); rank++) {
            DocChunk chunk = fulltextList.get(rank);
            double rrfScore = 1.0 / (RRF_K + rank + 1);
            scoreMap.merge(chunk.getId(), rrfScore, Double::sum);
            chunkMap.put(chunk.getId(), chunk);
        }

        // 按 RRF 分数降序排列
        return scoreMap.entrySet().stream()
                .sorted(Map.Entry.<Long, Double>comparingByValue().reversed())
                .map(e -> new ScoredChunk(chunkMap.get(e.getKey()), e.getValue()))
                .collect(Collectors.toList());
    }

    private String toVectorString(float[] embedding) {
        StringBuilder sb = new StringBuilder("[");
        for (int i = 0; i < embedding.length; i++) {
            if (i > 0) sb.append(",");
            sb.append(embedding[i]);
        }
        sb.append("]");
        return sb.toString();
    }

    /** 带分数的 Chunk 包装类 */
    public record ScoredChunk(DocChunk chunk, double score) {
        public Long id() { return chunk.getId(); }
        public String content() { return chunk.getContent(); }
    }
}
```

---

## 四、升级 RagQueryService 使用混合检索
```java
package com.jichi.ragkb.service;

import com.jichi.ragkb.entity.DocChunk;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.ai.chat.client.ChatClient;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;

import java.util.List;
import java.util.stream.Collectors;

/**
 * 升级版 RagQueryService：使用混合检索代替纯向量检索。
 * 后续 Reranker 节会在此基础上继续升级。
 */
@Service
@RequiredArgsConstructor
@Slf4j
public class RagQueryServiceV2 {

    private final HybridRetrieverService hybridRetriever;
    private final ChatClient chatClient;

    @Value("${rag.retrieval.return-top-n:5}")
    private int returnTopN;

    public String query(String question, List<Long> kbIds) {
        // Step 1：混合检索
        List<HybridRetrieverService.ScoredChunk> scoredChunks =
                hybridRetriever.retrieve(question, kbIds, returnTopN);

        if (scoredChunks.isEmpty()) {
            return buildNotFoundResponse();
        }

        // Step 2：生成回答
        List<DocChunk> chunks = scoredChunks.stream()
                .map(HybridRetrieverService.ScoredChunk::chunk)
                .collect(Collectors.toList());

        return generateAnswer(question, chunks);
    }

    private String generateAnswer(String question, List<DocChunk> chunks) {
        String context = buildContext(chunks);
        String systemPrompt = buildSystemPrompt(context);

        return chatClient.prompt()
                .system(systemPrompt)
                .user(question)
                .call()
                .content();
    }

    private String buildContext(List<DocChunk> chunks) {
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < chunks.size(); i++) {
            DocChunk c = chunks.get(i);
            sb.append("[参考").append(i + 1).append("]");
            if (c.getSectionTitle() != null) sb.append(" ").append(c.getSectionTitle());
            sb.append("\n").append(c.getContent()).append("\n\n");
        }
        return sb.toString().strip();
    }

    private String buildSystemPrompt(String context) {
        return """
                你是企业内部知识库的智能助手。根据以下参考内容回答用户问题。
                
                规则：
                1. 只基于参考内容回答，不使用自身知识推测
                2. 参考内容不足时，明确告知"未在知识库找到相关信息"
                3. 回答用中文，准确简洁
                4. 禁止编造参考内容之外的信息
                
                参考内容：
                ---
                %s
                ---
                """.formatted(context);
    }

    private String buildNotFoundResponse() {
        return "在您选择的知识库中未找到与该问题相关的内容。建议您：\n" +
               "1. 确认问题是否与知识库主题相关\n" +
               "2. 尝试用不同关键词提问\n" +
               "3. 联系相关部门获取准确信息";
    }
}
```

---

## 五、升级 RagQueryController（接入 RagQueryServiceV2）
<img src="https://cdn.nlark.com/yuque/0/2026/png/29413969/1776760015892-608a0a6d-1e50-42f0-9aa8-83a622f11993.png" width="1108" title="" crop="0,0,1,1" id="sx70F" class="ne-image">

之前基础 RAG 查询管道里的 `RagQueryController` 注入的是 `RagQueryService`。混合检索这版把实现类换成 `RagQueryServiceV2` 即可，路径和请求体不变，调用方无感升级。

```java
package com.jichi.ragkb.controller;

import com.jichi.ragkb.dto.ApiResponse;
import com.jichi.ragkb.dto.RagQueryRequest;
import com.jichi.ragkb.service.RagQueryServiceV2;
import lombok.RequiredArgsConstructor;
import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/api/v1/rag")
@RequiredArgsConstructor
public class RagQueryController {

    private final RagQueryServiceV2 ragQueryService;

    @PostMapping("/query")
    public ApiResponse<String> query(@RequestBody RagQueryRequest req) {
        return ApiResponse.ok(ragQueryService.query(req.getQuestion(), req.getKbIds()));
    }
}
```

### curl 测试
```bash
curl -X POST http://localhost:8080/api/v1/rag/query \
  -H "Content-Type: application/json" \
  -d '{"question":"年假是怎么规定的？","kbIds":[1]}'
```

---

## 六、混合检索效果对比测试
精确关键词场景——文档里确实写了"Commit Message"这个词，全文检索能直接命中，混合检索召回质量更高：

<img src="https://cdn.nlark.com/yuque/0/2026/png/29413969/1776760038352-05b9530c-d3ce-4e19-9b20-2fd8bb4bc579.png" width="1112" title="" crop="0,0,1,1" id="uc9f6d0ef" class="ne-image">

```bash
curl -X POST http://localhost:8080/api/v1/rag/query \
  -H "Content-Type: application/json" \
  -d '{"question":"Commit Message格式要求是什么？","kbIds":[2]}'
```

语义改写场景——用户问"新来的同事怎么配置开发电脑"，文档里写的是"入职第一天"和"领取笔记本电脑"，字面完全不一样，靠向量检索兜底：

<img src="https://cdn.nlark.com/yuque/0/2026/png/29413969/1776760045345-6f5098ea-3b2b-4034-9a25-dce28fde5130.png" width="1116" title="" crop="0,0,1,1" id="ufc21a861" class="ne-image">

```bash
curl -X POST http://localhost:8080/api/v1/rag/query \
  -H "Content-Type: application/json" \
  -d '{"question":"新来的同事怎么配置开发电脑？","kbIds":[1]}'
```

两个请求都跑一下，对比上一节纯向量检索的回答，能明显感觉到精确关键词场景下混合检索的答案更贴切。

<img src="https://cdn.nlark.com/yuque/0/2026/png/29413969/1776760056286-12e84e0c-3c10-46a4-9bba-2a737c157e6b.png" width="1119" title="" crop="0,0,1,1" id="u4036f13d" class="ne-image">

---
