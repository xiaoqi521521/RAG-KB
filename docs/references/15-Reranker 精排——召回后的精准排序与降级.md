<img src="https://cdn.nlark.com/yuque/0/2026/png/29413969/1776760228579-a0c05b94-d2dc-4574-ae3a-5d41686ddaab.png" width="1112" title="" crop="0,0,1,1" id="u0de2b98a" class="ne-image">

混合检索一口气拉回十几二十条，未必条条都贴题。Reranker（精排模型）干的就是这件事：把「问题 + 候选段落」成对送进模型，打出更细的相关性分数，再重排一遍。

<img src="https://cdn.nlark.com/yuque/0/2026/png/29413969/1776760244464-957e8741-f163-4b16-a0ac-6e8bdac3fd52.png" width="1119" title="" crop="0,0,1,1" id="ub8bd7e23" class="ne-image">

---

## 一、Reranker vs Embedding 的区别
<img src="https://cdn.nlark.com/yuque/0/2026/png/29413969/1776760269394-e11e8e25-b546-4a2a-81b2-552694c65800.png" width="1098" title="" crop="0,0,1,1" id="ub3f4c51c" class="ne-image">

```plain
Embedding 检索（Bi-Encoder）：
  问题 → 向量A
  文档 → 向量B
  相似度 = cosine(A, B)
  问题和文档独立编码，速度快，适合大规模召回

Reranker（Cross-Encoder）：
  (问题, 文档) → 一起输入模型 → 相关性分数
  两者一起编码，能捕捉细粒度交互，精度高但慢
  只用于少量候选（召回后 20 条）的精排
```

---

## 二、RerankerService——带超时降级
<img src="https://cdn.nlark.com/yuque/0/2026/png/29413969/1776760301131-9b7a60b9-22c0-4e55-bd95-f6f5c18afdde.png" width="1114" title="" crop="0,0,1,1" id="u9811928c" class="ne-image">

```java
package com.jichi.ragkb.service;

import com.fasterxml.jackson.annotation.JsonProperty;
import lombok.Data;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.HttpHeaders;
import org.springframework.http.MediaType;
import org.springframework.stereotype.Service;
import org.springframework.web.reactive.function.client.WebClient;
import reactor.core.publisher.Mono;

import java.time.Duration;
import java.util.*;
import java.util.stream.Collectors;

/** 由完整 RAG 管道调用，不单独暴露 HTTP。 */
@Service
@Slf4j
@RequiredArgsConstructor
public class RerankerService {

    private final WebClient.Builder webClientBuilder;
    private final TokenMetrics tokenMetrics;

    @Value("${reranker.endpoint}")
    private String endpoint;

    @Value("${reranker.api-key}")
    private String apiKey;

    @Value("${reranker.model:gte-rerank}")
    private String model;

    @Value("${reranker.timeout-ms:800}")
    private long timeoutMs;

    @Value("${reranker.top-n:5}")
    private int defaultTopN;

    /**
     * 对候选 chunk 进行精排，返回按相关性分数排序的结果。
     * 超时或 API 失败时自动降级（使用 RRF 分数排序）。
     *
     * @param question      用户问题
     * @param candidates    候选 chunk（混合检索结果）
     * @param topN          精排后保留数量
     * @return 精排后的 ScoredChunk 列表
     */
    public List<HybridRetrieverService.ScoredChunk> rerank(
            String question,
            List<HybridRetrieverService.ScoredChunk> candidates,
            int topN) {

        if (candidates.isEmpty()) return candidates;
        if (candidates.size() <= topN) {
            // 候选数量已经不多，不需要精排
            return candidates;
        }

        try {
            List<HybridRetrieverService.ScoredChunk> reranked =
                    callRerankApi(question, candidates, topN);
            log.info("[Reranker] 精排完成：候选={}，返回={}", candidates.size(), reranked.size());
            return reranked;

        } catch (Exception e) {
            log.warn("[Reranker] 精排失败或超时，降级使用 RRF 分数：{}", e.getMessage());
            // 降级：直接用 RRF 分数取 TopN
            return candidates.stream()
                    .limit(topN)
                    .collect(Collectors.toList());
        }
    }

    private List<HybridRetrieverService.ScoredChunk> callRerankApi(
            String question,
            List<HybridRetrieverService.ScoredChunk> candidates,
            int topN) {

        // 构建请求体（DashScope gte-rerank-v2 要求嵌套格式）
        List<String> docs = candidates.stream()
                .map(HybridRetrieverService.ScoredChunk::content)
                .collect(Collectors.toList());

        RerankRequest request = new RerankRequest();
        request.setModel(model);
        request.setInput(new RerankInput(question, docs));
        request.setParameters(new RerankParams(topN, false));

        WebClient client = webClientBuilder
                .baseUrl(endpoint)
                .defaultHeader(HttpHeaders.AUTHORIZATION, "Bearer " + apiKey)
                .defaultHeader(HttpHeaders.CONTENT_TYPE, MediaType.APPLICATION_JSON_VALUE)
                .build();

        RerankResponse response = client.post()
                .bodyValue(request)
                .retrieve()
                .bodyToMono(RerankResponse.class)
                .timeout(Duration.ofMillis(timeoutMs))  // 超时直接走降级
                .block();

        if (response == null || response.getOutput() == null || response.getOutput().getResults() == null) {
            throw new RuntimeException("Reranker API 返回空结果");
        }

        if (response.getUsage() != null && response.getUsage().getTotalTokens() > 0) {
            tokenMetrics.recordContextTokens(response.getUsage().getTotalTokens());
        }

        // 按精排分数组装结果
        return response.getOutput().getResults().stream()
                .sorted(Comparator.comparingDouble(RerankResult::getRelevanceScore).reversed())
                .map(r -> {
                    HybridRetrieverService.ScoredChunk original = candidates.get(r.getIndex());
                    return new HybridRetrieverService.ScoredChunk(
                            original.chunk(),
                            r.getRelevanceScore()
                    );
                })
                .collect(Collectors.toList());
    }

    // =================== DTO ===================

    @Data
    static class RerankRequest {
        private String model;
        private RerankInput input;
        private RerankParams parameters;
    }

    @Data
    static class RerankInput {
        private String query;
        private List<String> documents;

        RerankInput(String query, List<String> documents) {
            this.query = query;
            this.documents = documents;
        }
    }

    @Data
    static class RerankParams {
        @JsonProperty("top_n")
        private int topN;
        @JsonProperty("return_documents")
        private boolean returnDocuments;

        RerankParams(int topN, boolean returnDocuments) {
            this.topN = topN;
            this.returnDocuments = returnDocuments;
        }
    }

    @Data
    static class RerankResponse {
        private RerankOutput output;
        private RerankUsage usage;
    }

    @Data
    static class RerankOutput {
        private List<RerankResult> results;
    }

    @Data
    static class RerankResult {
        private int index;                       // 对应 candidates 列表中的下标
        @JsonProperty("relevance_score")
        private double relevanceScore;           // 精排分数 [0, 1]
    }

    @Data
    static class RerankUsage {
        @JsonProperty("total_tokens")
        private int totalTokens;
    }
}
```

---

## 三、置信度过滤
<img src="https://cdn.nlark.com/yuque/0/2026/png/29413969/1776760312920-59115c0c-a97b-4eaa-a26b-4bba61bcc3fa.png" width="1115" title="" crop="0,0,1,1" id="u17c71bd5" class="ne-image">

精排后还需要过滤掉分数太低的结果——Reranker 分数 < 0.3 的结果，与问题相关性太低，不应该传给模型。

```java
package com.jichi.ragkb.service;

import lombok.extern.slf4j.Slf4j;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;

import java.util.Comparator;
import java.util.List;
import java.util.stream.Collectors;

/**
 * 置信度过滤器：过滤掉低分 chunk，避免不相关内容影响生成质量。
 * 由 FullRagPipeline 注入使用，不单独暴露 HTTP。
 */
@Component
@Slf4j
public class ConfidenceFilter {

    @Value("${rag.retrieval.min-score:0.3}")
    private double minScore;

    /**
     * 过滤低置信度的 chunk。
     * 如果过滤后为空，保留分数最高的 1 个（不能完全没有内容）。
     */
    public List<HybridRetrieverService.ScoredChunk> filter(
            List<HybridRetrieverService.ScoredChunk> chunks) {

        List<HybridRetrieverService.ScoredChunk> filtered = chunks.stream()
                .filter(c -> c.score() >= minScore)
                .collect(Collectors.toList());

        if (filtered.isEmpty() && !chunks.isEmpty()) {
            // 至少保留分数最高的 1 个（上游已排序，但以防万一用 max 取最高分）
            HybridRetrieverService.ScoredChunk best = chunks.stream()
                    .max(Comparator.comparingDouble(HybridRetrieverService.ScoredChunk::score))
                    .orElse(chunks.get(0));
            log.debug("[ConfidenceFilter] 所有 chunk 低于阈值 {}，保留最高分1条（score={}）",
                    minScore, best.score());
            filtered = List.of(best);
        }

        int filteredCount = chunks.size() - filtered.size();
        if (filteredCount > 0) {
            log.debug("[ConfidenceFilter] 过滤低置信度 chunk：{}条", filteredCount);
        }

        return filtered;
    }
}
```

---

## 四、ContextTrimmerService（本课占位）
<img src="https://cdn.nlark.com/yuque/0/2026/png/29413969/1776760323881-7a44d09c-19fd-4e0d-8201-601253e815bb.png" width="1109" title="" crop="0,0,1,1" id="u908646f4" class="ne-image">

`FullRagPipeline` 里会调用 `ContextTrimmerService.trim()` 控制上下文长度。下一节「上下文裁剪」鸡哥会写带 **Token 预算** 的正式版；在大家还没贴那一节之前，可以先用下面这个**同名占位类**把工程跑起来——逻辑就是原样返回，相当于暂时不做裁剪。学完上下文裁剪那节后，**整文件替换**为那一节的实现即可。

```java
package com.jichi.ragkb.service;

import org.springframework.stereotype.Service;

import java.util.List;

/**
 * 占位：保证本节 FullRagPipeline 可编译运行。
 * 上下文裁剪那节用正式版整体替换本类。
 */
@Service
public class ContextTrimmerService {

    public List<HybridRetrieverService.ScoredChunk> trim(
            List<HybridRetrieverService.ScoredChunk> candidates) {
        return candidates;
    }
}
```

---

## 五、组装完整的查询管道
<img src="https://cdn.nlark.com/yuque/0/2026/png/29413969/1776760275514-1abe3d75-b204-491f-981a-b7b7b5b1c049.png" width="1107" title="" crop="0,0,1,1" id="u4693dca7" class="ne-image">

现在把所有步骤串起来，形成完整的 RAG 查询管道：

```java
package com.jichi.ragkb.service;

import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;

import java.util.List;
import java.util.stream.Collectors;

/**
 * 完整 RAG 查询管道（查询改写 + 混合检索 + Reranker + 上下文裁剪 + 生成）。
 * 这是最终版本，后续章节在此基础上添加流式输出、多轮对话等功能。
 */
@Service
@RequiredArgsConstructor
@Slf4j
public class FullRagPipeline {

    private final EnhancedRetrieverService enhancedRetriever;
    private final RerankerService rerankerService;
    private final ConfidenceFilter confidenceFilter;
    private final ContextTrimmerService contextTrimmer;
    private final org.springframework.ai.chat.client.ChatClient chatClient;

    @Value("${reranker.top-n:5}")
    private int rerankerTopN;

    /**
     * 执行完整 RAG 查询管道。
     *
     * @param question  用户问题
     * @param kbIds     知识库 ID 列表
     * @return 包含答案和来源的结构化响应
     */
    public RagResponse query(String question, List<Long> kbIds) {
        long pipelineStart = System.currentTimeMillis();

        // Step 1：增强检索（混合检索 + HyDE）
        List<HybridRetrieverService.ScoredChunk> candidates =
                enhancedRetriever.retrieveWithHyde(question, kbIds, 20);

        if (candidates.isEmpty()) {
            return RagResponse.notFound();
        }

        // Step 2：Reranker 精排
        List<HybridRetrieverService.ScoredChunk> reranked =
                rerankerService.rerank(question, candidates, rerankerTopN);

        // Step 3：置信度过滤
        List<HybridRetrieverService.ScoredChunk> filtered = confidenceFilter.filter(reranked);

        if (filtered.isEmpty()) {
            return RagResponse.notFound();
        }

        // Step 4：上下文裁剪（控制 Token 预算）
        List<HybridRetrieverService.ScoredChunk> trimmed = contextTrimmer.trim(filtered);

        // Step 5：生成回答
        String context = buildContext(trimmed);
        String answer = generateAnswer(question, context);

        // Step 6：组装来源信息
        List<RagResponse.Source> sources = buildSources(trimmed);

        long elapsed = System.currentTimeMillis() - pipelineStart;
        log.info("[FullRagPipeline] 完成：question={}，elapsed={}ms，sources={}",
                question.substring(0, Math.min(30, question.length())), elapsed, sources.size());

        return RagResponse.builder()
                .answer(answer)
                .sources(sources)
                .latencyMs((int) elapsed)
                .build();
    }

    private String buildContext(List<HybridRetrieverService.ScoredChunk> chunks) {
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < chunks.size(); i++) {
            var sc = chunks.get(i);
            sb.append("[参考").append(i + 1).append("]");
            if (sc.chunk().getSectionTitle() != null) {
                sb.append(" ").append(sc.chunk().getSectionTitle());
            }
            sb.append("\n").append(sc.content()).append("\n\n");
        }
        return sb.toString().strip();
    }

    private String generateAnswer(String question, String context) {
        return chatClient.prompt()
                .system("""
                        你是企业内部知识库的智能助手。根据以下参考内容回答用户问题。
                        
                        规则：
                        1. 只基于参考内容回答，不使用自身知识推测
                        2. 参考内容不足时，明确告知"未在知识库找到相关信息"
                        3. 回答用中文，准确简洁，适当列举要点
                        4. 禁止编造参考内容之外的信息
                        
                        参考内容：
                        ---
                        %s
                        ---
                        """.formatted(context))
                .user(question)
                .call()
                .content();
    }

    private List<RagResponse.Source> buildSources(List<HybridRetrieverService.ScoredChunk> chunks) {
        return chunks.stream()
                .map(sc -> RagResponse.Source.builder()
                        .chunkId(sc.id())
                        .docId(sc.chunk().getDocId())
                        .pageNum(sc.chunk().getPageNum())
                        .sectionTitle(sc.chunk().getSectionTitle())
                        .excerpt(sc.content().substring(0, Math.min(200, sc.content().length())))
                        .score(sc.score())
                        .build())
                .collect(Collectors.toList());
    }
}
```

### RagResponse
```java
package com.jichi.ragkb.dto;

import lombok.Builder;
import lombok.Data;

import java.util.List;

@Data
@Builder
@NoArgsConstructor
@AllArgsConstructor
public class RagResponse {

    private String answer;
    private List<Source> sources;
    private int latencyMs;
    private boolean notFound;

    @Data
    @Builder
    @NoArgsConstructor
    @AllArgsConstructor
    public static class Source {
        private Long chunkId;
        private Long docId;
        private Integer pageNum;
        private String sectionTitle;
        private String excerpt;        // 相关段落摘要（前200字）
        private double score;          // 相关性分数
    }

    public static RagResponse notFound() {
        return RagResponse.builder()
                .answer("在知识库中未找到与该问题相关的内容。建议您：\n" +
                        "1. 确认问题是否属于该知识库的覆盖范围\n" +
                        "2. 尝试更换关键词提问\n" +
                        "3. 联系相关部门获取准确信息")
                .sources(List.of())
                .notFound(true)
                .build();
    }
}
```

---

## 六、RagQueryController（返回结构化 RagResponse）
<img src="https://cdn.nlark.com/yuque/0/2026/png/29413969/1776760330653-deb5b953-bb6b-444c-b78e-52d428dd9e18.png" width="1120" title="" crop="0,0,1,1" id="ua0d0201a" class="ne-image">

前面基础 RAG 查询管道和混合检索那两节的问答接口返回的是纯文本 `String`。完整管道这版改成 `RagResponse`（答案 + 引用来源 + 耗时），Controller 也一并换掉。`RagQueryRequest` 仍用之前定义的即可。

```java
package com.jichi.ragkb.controller;

import com.jichi.ragkb.dto.ApiResponse;
import com.jichi.ragkb.dto.RagQueryRequest;
import com.jichi.ragkb.dto.RagResponse;
import com.jichi.ragkb.service.FullRagPipeline;
import lombok.RequiredArgsConstructor;
import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/api/v1/rag")
@RequiredArgsConstructor
public class RagQueryController {

    private final FullRagPipeline fullRagPipeline;

    @PostMapping("/query")
    public ApiResponse<RagResponse> query(@RequestBody RagQueryRequest req) {
        return ApiResponse.ok(fullRagPipeline.query(req.getQuestion(), req.getKbIds()));
    }
}
```

### curl 测试
```bash
curl -X POST http://localhost:8080/api/v1/rag/query \
  -H "Content-Type: application/json" \
  -d '{"question":"年假怎么申请？","kbIds":[1]}'
```

`reranker.*` 等配置要在 `application.yml` 里配好；精排接口挂了会自动降级成按 RRF 顺序截取，不至于整条管道崩掉。

Token 预算、上下文裁剪的正式实现放在下一节；大家把那里的 `ContextTrimmerService` 换上来之后，同一套 HTTP 接口不用改。

---
