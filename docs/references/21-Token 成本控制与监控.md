<img src="https://cdn.nlark.com/yuque/0/2026/png/29413969/1776761688096-4db8e21b-7abb-4da6-a1de-2d443bc192f5.png" width="1122" title="" crop="0,0,1,1" id="u0d19384f" class="ne-image">

鸡哥先给大家泼盆冷水：RAG 系统最容易被忽视的不是效果问题，是成本问题。一次 RAG 请求看着不贵，但架不住企业场景量大——几百人每天问几十个问题，一个月下来账单可能比大家想象的高不少。更关键的是，RAG 的 Token 消耗不只是生成那一块，Embedding、Reranker、上下文拼接都在烧钱。

<img src="https://cdn.nlark.com/yuque/0/2026/png/29413969/1776761669198-8ee7da24-c606-46fb-8323-b53730bc8ba9.png" width="1106" title="" crop="0,0,1,1" id="u83626760" class="ne-image">

<img src="https://cdn.nlark.com/yuque/0/2026/png/29413969/1776761645349-d653f8ca-3ff8-4c4b-a342-b74ecbd03f6a.png" width="1116" title="" crop="0,0,1,1" id="u60d233b0" class="ne-image">

所以这节鸡哥带大家把成本拆清楚，该缓存的缓存，该监控的监控，花了多少钱心里得有数。

---

## 一、成本来源拆分
<img src="https://cdn.nlark.com/yuque/0/2026/png/29413969/1776761685995-609e697f-48b0-4933-9a26-1944b7b2b92e.png" width="1110" title="" crop="0,0,1,1" id="u3a4ad47d" class="ne-image">

```plain
一次 RAG 请求的 Token 消耗：

① Embedding（查询改写 + HyDE）
   - 原始问题向量化：约 50 Token
   - HyDE 假设答案向量化：约 200 Token
   - 成本：输入 Token 单价（比生成便宜约 10 倍）

② Reranker
   - 按 document 数量计费（不是 Token）
   - 约 $0.0003 / 1000 次调用

③ 生成模型
   - 输入：System Prompt + Context + 问题 ≈ 3500 Token
   - 输出：回答 ≈ 500 Token
   - 成本最高
   
控制成本的关键：
  1. 查询缓存（相同问题复用 Embedding + 生成结果）
  2. Embedding 缓存（相同 chunk 不重复向量化）
  3. Context Token 控制（不超过 3000 Token）
```

---

## 二、查询缓存——减少重复生成
<img src="https://cdn.nlark.com/yuque/0/2026/png/29413969/1776761694353-e75733de-8ee7-4f8b-8725-6c83f392bb99.png" width="1115" title="" crop="0,0,1,1" id="uf7ad63ce" class="ne-image">

```java
package com.jichi.ragkb.service;

import com.jichi.ragkb.dto.RagResponse;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.data.redis.core.RedisTemplate;
import org.springframework.stereotype.Service;

import java.time.Duration;
import java.util.List;

/**
 * 查询结果缓存服务。
 * 相同问题 + 相同知识库 → 缓存 10 分钟，不重复调模型。
 *
 * 适用场景：同一部门内，相同问题被多人反复问（FAQ 类场景命中率高）。
 */
@Service
@RequiredArgsConstructor
@Slf4j
public class QueryCacheService {

    private final RedisTemplate<String, Object> redisTemplate;

    private static final String CACHE_PREFIX = "rag:query:";
    private static final Duration QUERY_TTL = Duration.ofMinutes(10);

    /**
     * 查询缓存。
     */
    public RagResponse getFromCache(String question, List<Long> kbIds) {
        String key = buildKey(question, kbIds);
        Object cached = redisTemplate.opsForValue().get(key);
        if (cached instanceof RagResponse resp) {
            log.info("[QueryCache] 命中缓存：question={}", question.substring(0, Math.min(30, question.length())));
            return resp;
        }
        return null;
    }

    /**
     * 写入缓存。
     */
    public void putToCache(String question, List<Long> kbIds, RagResponse response) {
        // 无效答案不缓存（notFound 的结果可能因为文档更新而变化）
        if (response.isNotFound()) return;

        String key = buildKey(question, kbIds);
        redisTemplate.opsForValue().set(key, response, QUERY_TTL);
        log.debug("[QueryCache] 写入缓存：key={}", key);
    }

    private String buildKey(String question, List<Long> kbIds) {
        // 排序 kbIds 保证相同知识库集合的 key 一致
        List<Long> sortedIds = kbIds.stream().sorted().toList();
        return CACHE_PREFIX + toMd5(question + ":" + sortedIds);
    }

    private String toMd5(String text) {
        try {
            var md = java.security.MessageDigest.getInstance("MD5");
            byte[] hash = md.digest(text.getBytes(java.nio.charset.StandardCharsets.UTF_8));
            StringBuilder sb = new StringBuilder();
            for (byte b : hash) sb.append(String.format("%02x", b));
            return sb.toString();
        } catch (Exception e) {
            return String.valueOf(text.hashCode());
        }
    }
}
```

在 `StreamingRagService.syncQuery()` 加入缓存逻辑：

```java
public RagResponse syncQuery(String question, List<Long> kbIds, String sessionId) {
    // 先查缓存
    RagResponse cached = queryCacheService.getFromCache(question, kbIds);
    if (cached != null) {
        sessionService.saveMessage(sessionId, question, cached.getAnswer(),
                sourceBuilder.sourcesToJson(cached.getSources()), 0);
        return cached;
    }

    // 缓存未命中，走完整 RAG 管道
    long start = System.currentTimeMillis();

    var candidates = enhancedRetriever.retrieveWithHyde(question, kbIds, 20);
    var reranked = rerankerService.rerank(question, candidates, 5);
    var filtered = confidenceFilter.filter(reranked);

    if (filtered.isEmpty()) return RagResponse.notFound();

    var trimmed = contextTrimmer.trim(filtered);
    String context = buildContext(trimmed);
    String systemPrompt = RagPromptTemplate.buildSystemPrompt(context, trimmed.size());

    String answer = chatClient.prompt()
            .system(systemPrompt)
            .user(question)
            .call()
            .content();

    tokenMetrics.recordGenerationTokens(contextTrimmer.countTokens(answer));

    List<RagResponse.Source> sources = sourceBuilder.buildSources(answer, trimmed);
    String sourcesJson = sourceBuilder.sourcesToJson(sources);
    int latencyMs = (int) (System.currentTimeMillis() - start);

    sessionService.saveMessage(sessionId, question, answer, sourcesJson, latencyMs);

    RagResponse response = RagResponse.builder()
            .answer(answer)
            .sources(sources)
            .latencyMs(latencyMs)
            .build();

    // 写入缓存
    queryCacheService.putToCache(question, kbIds, response);
    return response;
}
```

流式接口也要加，`streamQuery` 改动后的完整代码：

```java
public void streamQuery(String question, List<Long> kbIds, String sessionId, SseEmitter emitter) {
    long start = System.currentTimeMillis();

    try {
        // 先查缓存——命中则直接把完整答案一次性推给前端，跳过检索和生成
        RagResponse cached = queryCacheService.getFromCache(question, kbIds);
        if (cached != null) {
            emitter.send(SseEmitter.event().name("token").data(cached.getAnswer()));
            String doneData = objectMapper.writeValueAsString(
                    new DonePayload(cached.getSources(), 0));
            emitter.send(SseEmitter.event().name("done").data(doneData));
            emitter.complete();
            sessionService.saveMessage(sessionId, question, cached.getAnswer(),
                    sourceBuilder.sourcesToJson(cached.getSources()), 0);
            return;
        }

        // 缓存未命中，走完整流式管道
        emitter.send(SseEmitter.event()
                .name("status")
                .data("{\"type\":\"RETRIEVING\",\"message\":\"正在检索知识库...\"}"));

        var candidates = enhancedRetriever.retrieveWithHyde(question, kbIds, 20);
        var reranked = rerankerService.rerank(question, candidates, 5);
        var filtered = confidenceFilter.filter(reranked);

        if (filtered.isEmpty()) {
            sendNotFound(emitter);
            return;
        }

        var trimmed = contextTrimmer.trim(filtered);

        emitter.send(SseEmitter.event()
                .name("status")
                .data("{\"type\":\"GENERATING\",\"message\":\"已找到相关内容，正在生成回答...\"}"));

        String context = buildContext(trimmed);
        String systemPrompt = RagPromptTemplate.buildSystemPrompt(context, trimmed.size());

        StringBuilder fullAnswer = new StringBuilder();

        chatClient.prompt()
                .system(systemPrompt)
                .user(question)
                .stream()
                .content()
                .doOnNext(token -> {
                    try {
                        fullAnswer.append(token);
                        emitter.send(SseEmitter.event().name("token").data(token));
                    } catch (IOException e) {
                        log.warn("[StreamRAG] SSE 推送 Token 失败，客户端可能已断开");
                        throw new RuntimeException("SSE 连接断开");
                    }
                })
                .blockLast();

        String answer = fullAnswer.toString();
        tokenMetrics.recordGenerationTokens(contextTrimmer.countTokens(answer));

        List<RagResponse.Source> sources = sourceBuilder.buildSources(answer, trimmed);
        String sourcesJson = sourceBuilder.sourcesToJson(sources);
        int latencyMs = (int) (System.currentTimeMillis() - start);

        sessionService.saveMessage(sessionId, question, answer, sourcesJson, latencyMs);

        // 写入缓存
        RagResponse response = RagResponse.builder()
                .answer(answer).sources(sources).latencyMs(latencyMs).build();
        queryCacheService.putToCache(question, kbIds, response);

        String doneData = objectMapper.writeValueAsString(new DonePayload(sources, latencyMs));
        emitter.send(SseEmitter.event().name("done").data(doneData));
        emitter.complete();

    } catch (Exception e) {
        log.error("[StreamRAG] 流式查询异常：{}", e.getMessage(), e);
        try {
            emitter.send(SseEmitter.event().name("error")
                    .data("{\"message\":\"生成过程中出现异常\"}"));
            emitter.complete();
        } catch (IOException ignored) {}
    }
}
```

缓存命中时直接把完整答案当一个 `token` 事件推出去，再发 `done`，前端不需要任何改动就能兼容。缓存未命中走完整流式管道后，在 `done` 之前把结果写入缓存，下次同样的问题就不用再调大模型了。

<img src="https://cdn.nlark.com/yuque/0/2026/png/29413969/1776761711332-9adfe14b-163a-4a3d-9eba-e091d866d2ab.png" width="1114" title="" crop="0,0,1,1" id="u24ace2ed" class="ne-image">

---

## 三、成本统计 API
<img src="https://cdn.nlark.com/yuque/0/2026/png/29413969/1776761732389-85872aec-1b81-4bd3-a3aa-43b5056c9992.png" width="1083" title="" crop="0,0,1,1" id="ue8c186b1" class="ne-image">

```java
package com.jichi.ragkb.controller;

import com.jichi.ragkb.dto.ApiResponse;
import com.jichi.ragkb.security.UserContext;
import com.jichi.ragkb.service.TokenMetrics;
import lombok.RequiredArgsConstructor;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import java.util.Map;

@RestController
@RequestMapping("/api/v1/stats")
@RequiredArgsConstructor
public class StatsController {

    private final TokenMetrics tokenMetrics;

    /** 查看当前用户的 Token 消耗统计（数据持久化在 Redis，重启不丢失） */
    @GetMapping("/tokens")
    public ApiResponse<Map<String, Object>> getTokenStats() {
        Long userId = UserContext.getUserId();

        long embeddingTokens = tokenMetrics.getUserTokens(userId, "embeddingTokens");
        long contextTokens = tokenMetrics.getUserTokens(userId, "contextTokens");
        long generationTokens = tokenMetrics.getUserTokens(userId, "generationTokens");
        long totalTokens = embeddingTokens + contextTokens + generationTokens;

        // 简单成本估算（DashScope 定价，仅供参考）
        // text-embedding-v3: ¥0.0007 / 1000 Token
        // qwen-plus 输入: ¥0.0008 / 1000 Token
        // qwen-plus 输出: ¥0.002 / 1000 Token
        double estimatedCostCny =
                embeddingTokens / 1000.0 * 0.0007
                + contextTokens / 1000.0 * 0.0008
                + generationTokens / 1000.0 * 0.002;

        return ApiResponse.ok(Map.of(
                "embeddingTokens", embeddingTokens,
                "contextTokens", contextTokens,
                "generationTokens", generationTokens,
                "totalTokens", totalTokens,
                "estimatedCostCny", Math.round(estimatedCostCny * 10000) / 10000.0
        ));
    }
}
```

管理员看一眼当前 Token 消耗和成本估算：

```bash
# 先登录拿 token
curl -X POST http://localhost:8080/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"demo123"}'

curl -H "Authorization: Bearer <token>" \
  http://localhost:8080/api/v1/stats/tokens
# 返回示例：
# {
#   "code": 200,
#   "data": {
#     "embeddingTokens": 125000,
#     "contextTokens": 890000,
#     "generationTokens": 210000,
#     "totalTokens": 1225000,
#     "estimatedCostCny": 0.63
#   }
# }
```

---

<img src="https://cdn.nlark.com/yuque/0/2026/png/29413969/1776761750512-7c61b4e5-238c-4fe9-af22-b05df00ef793.png" width="1077" title="" crop="0,0,1,1" id="uf00ed5e1" class="ne-image">
