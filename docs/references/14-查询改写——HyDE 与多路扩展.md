<img src="https://cdn.nlark.com/yuque/0/2026/png/29413969/1776760093794-ca0e0051-c9aa-4664-9571-6a9e53b3e763.png" width="1106" title="" crop="0,0,1,1" id="u2894abb0" class="ne-image">

很多人口头问法跟文档标题、正文用词对不上：嘴里说「怎么请假」，文档标题是「假期申请流程」。拿原句直接做向量检索，有时就是差一口气。

<img src="https://cdn.nlark.com/yuque/0/2026/png/29413969/1776760109249-6032afe7-7957-4ec1-aecc-a73d852131ae.png" width="1122" title="" crop="0,0,1,1" id="uacae1a75" class="ne-image">

这节实现两种查询改写策略：**HyDE**（假设性文档嵌入）和**多路查询扩展**。

---

## 一、两种策略的原理
<img src="https://cdn.nlark.com/yuque/0/2026/png/29413969/1776760116623-b70480f7-8de2-4c7f-8fd3-82377172e7f4.png" width="1119" title="" crop="0,0,1,1" id="ude3e1836" class="ne-image">

### HyDE（Hypothetical Document Embeddings）
<img src="https://cdn.nlark.com/yuque/0/2026/png/29413969/1776760123784-603ab187-9f42-4098-befc-12cfecbb266f.png" width="1120" title="" crop="0,0,1,1" id="u2c2ade9a" class="ne-image">

思路：让模型先假设如果知识库里有答案，那答案长什么样，然后用这个假设答案的向量去检索，而不是用问题的向量。

```plain
原始问题："年假怎么申请？"
↓
让模型生成假设答案：
  "员工申请年假需要提前3个工作日在OA系统提交申请，
   填写休假日期和交接事项，经直属Leader审批后生效..."
↓
用假设答案的向量做检索
→ 比用"年假怎么申请？"的向量检索效果更好
```

为什么有效？因为假设答案和文档里的真实内容在语义空间上更接近。

### 多路查询扩展
<img src="https://cdn.nlark.com/yuque/0/2026/png/29413969/1776760130119-b3cc9ae9-f6c9-42ee-b2ba-2daf1bbb5420.png" width="1118" title="" crop="0,0,1,1" id="u2dc64f7e" class="ne-image">

思路：把一个问题扩展成 3 个不同角度的问题，分别检索，合并结果。

```plain
原始问题："年假怎么申请？"
↓
扩展为：
  1. "年假申请流程是什么？"
  2. "员工请假需要哪些步骤？"
  3. "OA系统如何提交假期申请？"
↓
三路检索结果合并 → RRF 融合排序
```

<img src="https://cdn.nlark.com/yuque/0/2026/png/29413969/1776760155492-f64e5161-8683-4325-8f4f-495d5755283e.png" width="1116" title="" crop="0,0,1,1" id="uba218719" class="ne-image">

---

## 二、QueryRewriterService
```java
package com.jichi.ragkb.service;

import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.ai.chat.client.ChatClient;
import org.springframework.cache.annotation.Cacheable;
import org.springframework.stereotype.Service;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;
import java.util.stream.Collectors;

@Service
@RequiredArgsConstructor
@Slf4j
public class QueryRewriterService {

    private final ChatClient chatClient;
    private final TokenMetrics tokenMetrics;
    private final ContextTrimmerService contextTrimmer;

    /**
     * HyDE：生成假设性回答，用于向量检索。
     * 缓存：相同问题的假设回答缓存 10 分钟，避免重复调模型。
     */
    @Cacheable(value = "hyde-cache", key = "#question.hashCode()")
    public String generateHypotheticalAnswer(String question) {
        log.debug("[QueryRewriter] HyDE 生成假设回答：{}", question);

        String prompt = """
                请根据以下问题，生成一个简洁的假设性回答（2-4句话）。
                这个回答不需要准确，只需要在语义上覆盖可能的答案内容。
                直接给出答案内容，不要任何前缀说明。
                
                问题：%s
                """.formatted(question);

        try {
            String hypothetical = chatClient.prompt()
                    .user(prompt)
                    .call()
                    .content();
            log.debug("[QueryRewriter] HyDE 结果：{}", hypothetical);

            int genTokens = contextTrimmer.countTokens(hypothetical);
            tokenMetrics.recordGenerationTokens(genTokens);

            return hypothetical;
        } catch (Exception e) {
            // HyDE 失败不影响主流程，降级返回原始问题
            log.warn("[QueryRewriter] HyDE 生成失败，使用原始问题：{}", e.getMessage());
            return question;
        }
    }

    /**
     * 多路查询扩展：把问题扩展成多个角度。
     * 返回的列表包含原始问题 + 扩展问题。
     */
    public List<String> expandQuery(String question) {
        log.debug("[QueryRewriter] 多路扩展：{}", question);

        String prompt = """
                请将以下问题改写成3个不同表达方式的查询，要求：
                1. 保持原始意图不变
                2. 每个查询角度略有不同（换词、换句式、从不同维度提问）
                3. 每行一个查询，不要编号，不要任何额外说明
                
                原始问题：%s
                """.formatted(question);

        try {
            String expanded = chatClient.prompt()
                    .user(prompt)
                    .call()
                    .content();

            int genTokens = contextTrimmer.countTokens(expanded);
            tokenMetrics.recordGenerationTokens(genTokens);

            List<String> queries = new ArrayList<>();
            queries.add(question);  // 原始问题也要检索

            Arrays.stream(expanded.split("\n"))
                    .map(String::strip)
                    .filter(q -> !q.isBlank() && !q.equals(question))
                    .limit(3)
                    .forEach(queries::add);

            log.debug("[QueryRewriter] 扩展结果：{}", queries);
            return queries;

        } catch (Exception e) {
            log.warn("[QueryRewriter] 查询扩展失败，使用原始问题：{}", e.getMessage());
            return List.of(question);
        }
    }
}
```

`QueryRewriterService`、`EnhancedRetrieverService` 都不单独暴露 HTTP：前者给后者用，后者再被 Reranker 精排那节的 `FullRagPipeline` 调；对外开放仍是 `RagQueryController`（注入 `FullRagPipeline` 的那一版）。

---

## 三、升级 HybridRetrieverService 集成查询改写
```java
package com.jichi.ragkb.service;

import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;

import java.util.*;
import java.util.stream.Collectors;

/**
 * 带查询改写的检索服务。
 * 策略：原始问题 + HyDE 假设答案 → 各自向量化 → 多路检索 → RRF 融合。
 */
@Service
@Slf4j
@RequiredArgsConstructor
public class EnhancedRetrieverService {

    private final HybridRetrieverService hybridRetriever;
    private final QueryRewriterService queryRewriter;
    private final EmbeddingService embeddingService;
    private final com.jichi.ragkb.repository.DocChunkRepository chunkRepository;

    @Value("${rag.retrieval.vector-top-k:20}")
    private int vectorTopK;

    @Value("${rag.retrieval.fulltext-top-k:20}")
    private int fulltextTopK;

    private static final int RRF_K = 60;

    /**
     * 带 HyDE 的增强检索。
     * 用原始问题 + HyDE 假设答案的向量分别检索，RRF 融合。
     *
     * @param question 用户原始问题
     * @param kbIds    知识库 ID 列表
     * @param topN     返回数量
     */
    public List<HybridRetrieverService.ScoredChunk> retrieveWithHyde(
            String question, List<Long> kbIds, int topN) {

        // 路线1：原始问题的混合检索结果
        List<HybridRetrieverService.ScoredChunk> originalResults =
                hybridRetriever.retrieve(question, kbIds, vectorTopK);

        // 路线2：HyDE 假设答案的向量检索结果
        String hydeAnswer = queryRewriter.generateHypotheticalAnswer(question);
        float[] hydeEmbedding = embeddingService.embed(hydeAnswer);
        String hydeEmbeddingStr = toVectorString(hydeEmbedding);

        List<com.jichi.ragkb.entity.DocChunk> hydeResults = kbIds.stream()
                .flatMap(kbId -> chunkRepository.findByVectorSimilarity(kbId, hydeEmbeddingStr, vectorTopK).stream())
                .collect(Collectors.toList());

        log.debug("[EnhancedRetriever] 原始检索={}，HyDE检索={}", originalResults.size(), hydeResults.size());

        // RRF 融合两路结果
        Map<Long, Double> scoreMap = new LinkedHashMap<>();
        Map<Long, com.jichi.ragkb.entity.DocChunk> chunkMap = new HashMap<>();

        // 原始结果按已有 RRF 分数参与融合
        for (int rank = 0; rank < originalResults.size(); rank++) {
            HybridRetrieverService.ScoredChunk sc = originalResults.get(rank);
            double rrfScore = 1.0 / (RRF_K + rank + 1);
            scoreMap.merge(sc.id(), rrfScore, Double::sum);
            chunkMap.put(sc.id(), sc.chunk());
        }

        // HyDE 结果
        for (int rank = 0; rank < hydeResults.size(); rank++) {
            com.jichi.ragkb.entity.DocChunk chunk = hydeResults.get(rank);
            double rrfScore = 1.0 / (RRF_K + rank + 1);
            scoreMap.merge(chunk.getId(), rrfScore, Double::sum);
            chunkMap.put(chunk.getId(), chunk);
        }

        return scoreMap.entrySet().stream()
                .sorted(Map.Entry.<Long, Double>comparingByValue().reversed())
                .limit(topN)
                .map(e -> new HybridRetrieverService.ScoredChunk(chunkMap.get(e.getKey()), e.getValue()))
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
}
```

---

## 四、Redis 缓存配置（HyDE 缓存）
在 `application.yml` 中添加缓存配置：

```yaml
spring:
  cache:
    type: redis
    redis:
      time-to-live: 600000    # 10 分钟
      cache-null-values: false
```

在 `RagKbApplication.java` 上添加：

```java
@EnableCaching   // 开启 Spring Cache
```

---

## 五、RagQueryServiceV3——接入查询改写
<img src="https://cdn.nlark.com/yuque/0/2026/png/29413969/1776760175136-59fa96e6-33fa-4cb2-8ebc-ce0fd3aef2cd.png" width="1117" title="" crop="0,0,1,1" id="uc91c4a27" class="ne-image">

`EnhancedRetrieverService` 写好了但没人调，我们再写一版 `RagQueryServiceV3`，把查询改写接进去，同时复用之前的生成逻辑。

```java
package com.jichi.ragkb.service;

import com.jichi.ragkb.entity.DocChunk;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.ai.chat.client.ChatClient;
import org.springframework.stereotype.Service;

import java.util.List;
import java.util.stream.Collectors;

@Service
@Slf4j
@RequiredArgsConstructor
public class RagQueryServiceV3 {

    private final EnhancedRetrieverService enhancedRetriever;
    private final ChatClient chatClient;

    public String query(String question, List<Long> kbIds) {
        List<HybridRetrieverService.ScoredChunk> scoredChunks =
                enhancedRetriever.retrieveWithHyde(question, kbIds, 5);

        if (scoredChunks.isEmpty()) {
            return "在您选择的知识库中未找到与该问题相关的内容。";
        }

        List<DocChunk> chunks = scoredChunks.stream()
                .map(HybridRetrieverService.ScoredChunk::chunk)
                .collect(Collectors.toList());

        return generateAnswer(question, chunks);
    }

    private String generateAnswer(String question, List<DocChunk> chunks) {
        String context = buildContext(chunks);

        String systemPrompt = """
                你是企业内部知识库的智能助手。根据提供的参考内容回答问题。
                规则：只根据参考内容回答，不要编造；如果参考内容不够，告诉用户未找到相关信息。
                
                参考内容：
                ---
                %s
                ---
                """.formatted(context);

        return chatClient.prompt()
                .system(systemPrompt)
                .user(question)
                .call()
                .content();
    }

    private String buildContext(List<DocChunk> chunks) {
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < chunks.size(); i++) {
            DocChunk chunk = chunks.get(i);
            sb.append(String.format("[参考%d]", i + 1));
            if (chunk.getSectionTitle() != null) {
                sb.append(" ").append(chunk.getSectionTitle());
            }
            sb.append("\n").append(chunk.getContent()).append("\n\n");
        }
        return sb.toString().strip();
    }
}
```

然后把 `RagQueryController` 的注入从 `RagQueryServiceV2` 切到 `RagQueryServiceV3`：

```java
@RestController
@RequestMapping("/api/v1/rag")
@RequiredArgsConstructor
public class RagQueryController {

    private final RagQueryServiceV3 ragQueryService;

    @PostMapping("/query")
    public ApiResponse<String> query(@RequestBody RagQueryRequest req) {
        return ApiResponse.ok(ragQueryService.query(req.getQuestion(), req.getKbIds()));
    }
}
```

---

## 六、curl 测试
<img src="https://cdn.nlark.com/yuque/0/2026/png/29413969/1776760193970-c58e8e17-9ce2-46f0-b271-c6ca64eaa2b9.png" width="1120" title="" crop="0,0,1,1" id="u4083c9e4" class="ne-image">

口语化提问——用户说"新员工第一天要干嘛"，文档里写的是"入职第一天""领取工牌和电脑"，词汇完全不匹配。HyDE 会先生成一段假设回答，用假设回答的向量去检索，召回质量比直接用原始问题好很多：

```bash
curl -X POST http://localhost:8080/api/v1/rag/query \
  -H "Content-Type: application/json" \
  -d '{"question":"新员工第一天要干嘛","kbIds":[1]}'
```

精确关键词场景——多路扩展会把"代码提交规范"扩展成"Commit Message 格式""分支管理策略"等变体，全文检索和向量检索各自多了命中的机会：

```bash
curl -X POST http://localhost:8080/api/v1/rag/query \
  -H "Content-Type: application/json" \
  -d '{"question":"代码提交规范","kbIds":[2]}'
```

跟上一节同样的问题对比一下回答，能感觉到查询改写后答案更完整、覆盖面更广。
