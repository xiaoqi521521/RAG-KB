<img src="https://cdn.nlark.com/yuque/0/2026/png/29413969/1776760398548-7e6be742-3096-406b-8e6c-f73f527550d0.png" width="1115" title="" crop="0,0,1,1" id="uaccae1cd" class="ne-image">

上一节做完 Reranker 精排，检索质量已经很不错了，但还有一个问题没解决——**钱**。Reranker 返回 Top 5，这 5 个 chunk 加起来可能有几千个 Token，如果直接全塞给模型，加上问题本身、System Prompt、对话历史，很可能超出模型的上下文窗口限制，或者成本失控。鸡哥之前有个项目就是这么翻车的，一天的 Token 费用比服务器都贵。

<img src="https://cdn.nlark.com/yuque/0/2026/png/29413969/1776760430165-879da35f-1f5c-42c8-b0de-bad5d091125e.png" width="1126" title="" crop="0,0,1,1" id="uc27fd4ec" class="ne-image">

所以这节要做的事情很明确：**在固定的 Token 预算内，尽可能多地保留最相关的内容**。

<img src="https://cdn.nlark.com/yuque/0/2026/png/29413969/1776760419311-6c904e73-984e-404c-8197-be714cb4268d.png" width="1119" title="" crop="0,0,1,1" id="u68f7a7a6" class="ne-image">

若大家在 Reranker 精排那节贴过**占位的** `ContextTrimmerService`，这里用下面的代码**整类替换**即可。

---

## 一、Token 预算规划
<img src="https://cdn.nlark.com/yuque/0/2026/png/29413969/1776760417118-b53cdb1f-b5d6-415f-a136-ce4c530adde3.png" width="1115" title="" crop="0,0,1,1" id="u829137cc" class="ne-image">

```plain
一次 RAG 请求的 Token 消耗：
  System Prompt        ≈ 200 Token
  对话历史（最近 5 轮）≈ 1000 Token
  Context（参考内容）  ≤ 3000 Token  ← 这里控制
  用户问题             ≈ 50 Token
  生成回答             ≈ 500 Token
  ─────────────────────────────
  总计                 ≈ 4750 Token

qwen-plus 上下文窗口：128K Token，远远够用。
但 Context 控制在 3000 Token 以内有两个好处：
  1. 降低每次请求成本（按 Token 计费）
  2. 避免"迷失在中间"问题（模型对超长上下文中间的内容注意力下降）
```

---

## 二、ContextTrimmerService
<img src="https://cdn.nlark.com/yuque/0/2026/png/29413969/1776760510303-30dadad3-f242-43b0-abba-c211fa369235.png" width="1099" title="" crop="0,0,1,1" id="u5d76272b" class="ne-image">

```java
package com.jichi.ragkb.service;

import com.jichi.ragkb.entity.DocChunk;
import com.knuddels.jtokkit.Encodings;
import com.knuddels.jtokkit.api.Encoding;
import com.knuddels.jtokkit.api.EncodingRegistry;
import com.knuddels.jtokkit.api.EncodingType;
import jakarta.annotation.PostConstruct;
import lombok.extern.slf4j.Slf4j;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;

import java.util.ArrayList;
import java.util.List;

/**
 * 上下文裁剪器：在 Token 预算内尽量多保留高相关性 chunk。
 *
 * 策略：按 Reranker 分数从高到低贪心添加 chunk，
 * 直到 Token 预算耗尽或所有 chunk 已添加完毕。
 */
@Service
@Slf4j
public class ContextTrimmerService {

    @Value("${rag.context.max-tokens:3000}")
    private int maxContextTokens;

    private final TokenMetrics tokenMetrics;
    private Encoding tokenizer;

    public ContextTrimmerService(TokenMetrics tokenMetrics) {
        this.tokenMetrics = tokenMetrics;
    }

    @PostConstruct
    public void init() {
        // 使用 cl100k_base tokenizer（GPT-4 / qwen-plus 兼容）
        EncodingRegistry registry = Encodings.newDefaultEncodingRegistry();
        this.tokenizer = registry.getEncoding(EncodingType.CL100K_BASE);
    }

    /**
     * 裁剪 chunk 列表，确保总 Token 不超过预算。
     * 候选 chunk 已经按相关性排序（Reranker 分数），贪心选取。
     *
     * @param candidates 已排序的 chunk 列表（相关性高的在前）
     * @return 裁剪后的 chunk 列表（顺序不变）
     */
    public List<HybridRetrieverService.ScoredChunk> trim(
            List<HybridRetrieverService.ScoredChunk> candidates) {

        List<HybridRetrieverService.ScoredChunk> selected = new ArrayList<>();
        int usedTokens = 0;

        for (HybridRetrieverService.ScoredChunk sc : candidates) {
            int chunkTokens = countTokens(sc.content());

            if (usedTokens + chunkTokens <= maxContextTokens) {
                selected.add(sc);
                usedTokens += chunkTokens;
            } else if (selected.isEmpty()) {
                // 第一个 chunk 就超了，截断后加入（至少要有一些内容）
                String truncated = truncateToTokens(sc.content(),
                        maxContextTokens - usedTokens);
                if (!truncated.isBlank()) {
                    // 构建截断后的 DocChunk 副本，替换 content 为截断版本
                    DocChunk truncatedChunk = new DocChunk();
                    truncatedChunk.setId(sc.chunk().getId());
                    truncatedChunk.setDocId(sc.chunk().getDocId());
                    truncatedChunk.setKbId(sc.chunk().getKbId());
                    truncatedChunk.setChunkIndex(sc.chunk().getChunkIndex());
                    truncatedChunk.setContent(truncated);
                    truncatedChunk.setPageNum(sc.chunk().getPageNum());
                    truncatedChunk.setSectionTitle(sc.chunk().getSectionTitle());
                    truncatedChunk.setTokenCount(countTokens(truncated));
                    truncatedChunk.setDocVersion(sc.chunk().getDocVersion());

                    selected.add(new HybridRetrieverService.ScoredChunk(
                            truncatedChunk, sc.score()));
                    usedTokens += countTokens(truncated);
                }
                break;
            } else {
                // 已有内容，后面的 chunk 放不下了
                break;
            }
        }

        log.info("[ContextTrimmer] 候选={}，选取={}，usedTokens={}/{}",
                candidates.size(), selected.size(), usedTokens, maxContextTokens);

        tokenMetrics.recordContextTokens(usedTokens);

        return selected;
    }

    /**
     * 统计文本的 Token 数。
     * 对于中文，jtokkit 使用 cl100k 编码，1个汉字约 1-2 Token。
     */
    public int countTokens(String text) {
        if (text == null || text.isBlank()) return 0;
        return tokenizer.encode(text).size();
    }

    /**
     * 截断文本到不超过指定 Token 数，在句子边界处截断。
     */
    private String truncateToTokens(String text, int maxTokens) {
        if (maxTokens <= 0) return "";
        if (countTokens(text) <= maxTokens) return text;

        // 按句子分割，贪心添加
        String[] sentences = text.split("(?<=[。！？\\n])");
        StringBuilder result = new StringBuilder();
        int tokens = 0;

        for (String sentence : sentences) {
            int sentenceTokens = countTokens(sentence);
            if (tokens + sentenceTokens <= maxTokens) {
                result.append(sentence);
                tokens += sentenceTokens;
            } else {
                break;
            }
        }

        return result.toString();
    }
}
```

---

## 三、Token 消耗监控
<img src="https://cdn.nlark.com/yuque/0/2026/png/29413969/1776760509650-8143b5c7-e853-4cfd-b09f-301362625e70.png" width="1106" title="" crop="0,0,1,1" id="u40516e90" class="ne-image">

在 `ContextTrimmerService` 中加入 Micrometer 指标，方便后续在 Grafana 里看 Token 消耗趋势：

```java
package com.jichi.ragkb.service;

import com.jichi.ragkb.security.UserContext;
import io.micrometer.core.instrument.Counter;
import io.micrometer.core.instrument.MeterRegistry;
import jakarta.annotation.PostConstruct;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.stereotype.Component;

/**
 * RAG 查询的 Token 消耗统计器。
 *
 * 双写策略：
 * 1. Micrometer Counter —— 全局指标，供 Prometheus / Grafana 监控使用，重启归零
 * 2. Redis Hash —— 按用户维度持久化，重启不丢失，供前端监控面板展示
 *
 * 注意：必须用 StringRedisTemplate，不能用自定义的 RedisTemplate<String, Object>。
 * 因为 GenericJackson2JsonRedisSerializer 和 HINCRBY 写入的原生数字格式不兼容：
 * increment() 写入 "150"，但 JSON 反序列化器期望 ["java.lang.Long", 150]，读取时会失败。
 * StringRedisTemplate 全部使用 StringRedisSerializer，读写一致。
 *
 * Redis Key 格式：rag:token-stats:{userId}
 * Hash Fields：embeddingTokens / contextTokens / generationTokens
 */
@Component
@RequiredArgsConstructor
@Slf4j
public class TokenMetrics {

    private final MeterRegistry meterRegistry;
    private final StringRedisTemplate stringRedisTemplate;

    private static final String REDIS_KEY_PREFIX = "rag:token-stats:";

    private Counter embeddingTokenCounter;
    private Counter contextTokenCounter;
    private Counter generationTokenCounter;

    @PostConstruct
    public void init() {
        embeddingTokenCounter = Counter.builder("rag.tokens.embedding")
                .description("Embedding 消耗的 Token 总数")
                .register(meterRegistry);

        contextTokenCounter = Counter.builder("rag.tokens.context")
                .description("传入模型的 Context Token 总数")
                .register(meterRegistry);

        generationTokenCounter = Counter.builder("rag.tokens.generation")
                .description("模型生成消耗的 Token 总数")
                .register(meterRegistry);
    }

    public void recordEmbeddingTokens(int tokens) {
        log.info("[TokenMetrics] recordEmbeddingTokens={}", tokens);
        embeddingTokenCounter.increment(tokens);
        incrementRedis("embeddingTokens", tokens);
    }

    public void recordContextTokens(int tokens) {
        log.info("[TokenMetrics] recordContextTokens={}", tokens);
        contextTokenCounter.increment(tokens);
        incrementRedis("contextTokens", tokens);
    }

    public void recordGenerationTokens(int tokens) {
        log.info("[TokenMetrics] recordGenerationTokens={}", tokens);
        generationTokenCounter.increment(tokens);
        incrementRedis("generationTokens", tokens);
    }

    /** 从 Redis 读取指定用户的 Token 统计 */
    public long getUserTokens(Long userId, String field) {
        String val = (String) stringRedisTemplate.opsForHash()
                .get(REDIS_KEY_PREFIX + userId, field);
        log.info("[TokenMetrics] getUserTokens: userId={}, field={}, val={}", userId, field, val);
        if (val != null) {
            try { return Long.parseLong(val); } catch (NumberFormatException ignored) {}
        }
        return 0;
    }

    private void incrementRedis(String field, int delta) {
        try {
            Long userId = UserContext.getUserId();
            String key = REDIS_KEY_PREFIX + userId;
            log.info("[TokenMetrics] incrementRedis: key={}, field={}, delta={}", key, field, delta);
            stringRedisTemplate.opsForHash().increment(key, field, delta);
        } catch (Exception e) {
            log.error("[TokenMetrics] Redis 写入失败：{}", e.getMessage(), e);
        }
    }
}
```

---

`ContextTrimmerService` 和 `TokenMetrics` 都是内部服务，没有独立的 Controller 端点。大家可以通过之前的 `/api/v1/rag/query` 接口验证裁剪效果——调用后观察日志里的 `[ContextTrimmer] 候选=X，选取=Y，usedTokens=Z/3000`，就能看到裁剪是不是真的在干活了。

<img src="https://cdn.nlark.com/yuque/0/2026/png/29413969/1776760561118-2128f755-0c44-473f-96e3-2715fc60c8c5.png" width="1109" title="" crop="0,0,1,1" id="u7f87a379" class="ne-image">

---

好，Token 预算控制搞定了，成本这块算是兜住了。但现在还有个问题：模型给出了回答，用户怎么知道这个答案从哪来的？万一模型"发挥"了呢？下一节鸡哥带大家做引用溯源和防幻觉校验，让答案有据可查。

---
