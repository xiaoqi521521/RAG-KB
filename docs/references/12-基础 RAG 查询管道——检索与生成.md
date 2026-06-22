<img src="https://cdn.nlark.com/yuque/0/2026/png/29413969/1776759769300-192eb7f5-e695-482d-8f42-08750e710620.png" width="1120" title="" crop="0,0,1,1" id="u16b380b7" class="ne-image">

在线查询管道从这节开跑。鸡哥先带大家把最朴素的流程走通：向量检索 → 组装 Prompt → 模型生成答案；后面再往上叠混合检索、Reranker、引用溯源这些增强。

<img src="https://cdn.nlark.com/yuque/0/2026/png/29413969/1776759768014-f8638402-c661-4a6c-bee1-766117426099.png" width="1108" title="" crop="0,0,1,1" id="u6411a0ab" class="ne-image">

---

## 一、基础 RAG 流程
```plain
用户提问："API 限流策略是什么？"

① 向量化问题
   → embed("API 限流策略是什么？") → float[1536]

② 向量检索
   → SELECT * FROM kb_doc_chunk WHERE kb_id = 2
     ORDER BY embedding <=> query_vector LIMIT 5

③ 组装 Prompt
   → System: "你是企业知识库助手，根据以下内容回答问题..."
   → Context: [top5 chunk 内容]
   → User: "API 限流策略是什么？"

④ 模型生成
   → ChatClient 调用 → 答案（含引用标注）
```

---

## 二、SpringAiConfig——ChatClient 配置
<img src="https://cdn.nlark.com/yuque/0/2026/png/29413969/1776759780802-a45f2423-3f2b-4984-b421-10af071953cc.png" width="1085" title="" crop="0,0,1,1" id="ud0c05454" class="ne-image">

`RagQueryService` 需要注入 `ChatClient` 来调用大模型，Spring AI 默认只注册 `ChatClient.Builder`，不会自动创建 `ChatClient` Bean。我们通过一个配置类统一构建，后续所有需要 `ChatClient` 的 Service 直接注入即可。

```java
package com.jichi.ragkb.config;

import org.springframework.ai.chat.client.ChatClient;
import org.springframework.ai.chat.model.ChatModel;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

@Configuration
public class SpringAiConfig {

    @Bean
    public ChatClient chatClient(ChatModel chatModel) {
        return ChatClient.builder(chatModel)
                .build();
    }
}
```

---

## 三、RagQueryService——基础版
<img src="https://cdn.nlark.com/yuque/0/2026/png/29413969/1776759787868-48a17d7c-3728-451f-bb60-3a30bb62f039.png" width="1101" title="" crop="0,0,1,1" id="u35df60fb" class="ne-image">

```java
package com.jichi.ragkb.service;

import com.jichi.ragkb.entity.DocChunk;
import com.jichi.ragkb.repository.DocChunkRepository;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.ai.chat.client.ChatClient;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;

import java.util.List;
import java.util.stream.Collectors;

@Service
@Slf4j
@RequiredArgsConstructor
public class RagQueryService {

    private final EmbeddingService embeddingService;
    private final DocChunkRepository chunkRepository;
    private final ChatClient chatClient;

    @Value("${rag.retrieval.vector-top-k:5}")
    private int vectorTopK;

    @Value("${rag.retrieval.min-score:0.5}")
    private double minScore;

    /**
     * 基础 RAG 查询：向量检索 + 生成回答。
     *
     * @param question 用户问题
     * @param kbIds    要查询的知识库 ID 列表
     * @return 生成的答案
     */
    public String query(String question, List<Long> kbIds) {
        // Step 1：向量化问题
        float[] queryEmbedding = embeddingService.embed(question);

        // Step 2：向量检索（从多个知识库）
        List<DocChunk> retrievedChunks = retrieveChunks(queryEmbedding, kbIds, vectorTopK);

        if (retrievedChunks.isEmpty()) {
            return "在您选择的知识库中未找到与该问题相关的内容。请确认问题是否与知识库的主题相关，或尝试用不同的表达方式提问。";
        }

        // Step 3：组装 Prompt 并生成回答
        return generateAnswer(question, retrievedChunks);
    }

    /**
     * 从多个知识库执行向量检索，合并结果并按相似度排序。
     */
    protected List<DocChunk> retrieveChunks(float[] queryEmbedding, List<Long> kbIds, int topK) {
        // PGVector 格式：[0.1,0.2,...] 字符串
        String embeddingStr = toVectorString(queryEmbedding);

        List<DocChunk> allChunks = kbIds.stream()
                .flatMap(kbId -> chunkRepository.findByVectorSimilarity(kbId, embeddingStr, topK).stream())
                .collect(Collectors.toList());

        // 如果查多个知识库，需要合并后重新排序（近似处理：按 embedding 相似度重排）
        // 注意：此处 chunk 里没有 score 字段，因为 JPA 映射复杂，简化处理：
        // 直接保留 topK 个（后续 Reranker 节会处理精确排序）
        if (allChunks.size() > topK) {
            allChunks = allChunks.subList(0, topK);
        }

        log.debug("[RAG] 向量检索完成：kbIds={}，召回{}条", kbIds, allChunks.size());
        return allChunks;
    }

    /**
     * 组装 System Prompt + Context，调用模型生成答案。
     */
    protected String generateAnswer(String question, List<DocChunk> chunks) {
        String context = buildContext(chunks);

        String systemPrompt = """
                你是企业内部知识库的智能助手。你的工作是根据提供的参考文档内容，准确回答员工的问题。
                
                重要规则：
                1. 只根据提供的【参考内容】回答问题，不要使用自己的知识进行推测或补充
                2. 如果参考内容不足以回答问题，明确告诉用户"在知识库中未找到相关信息"，并建议用户联系相关部门
                3. 回答要准确、简洁，用中文回答
                4. 如果参考内容涉及多个文档，综合各文档回答
                5. 禁止编造不在参考内容中的信息
                
                参考内容如下：
                ---
                %s
                ---
                """.formatted(context);

        log.debug("[RAG] 开始生成回答，context长度={}", context.length());
        long start = System.currentTimeMillis();

        String answer = chatClient.prompt()
                .system(systemPrompt)
                .user(question)
                .call()
                .content();

        log.info("[RAG] 生成完成，耗时={}ms，answer长度={}", System.currentTimeMillis() - start, answer.length());
        return answer;
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

    protected String toVectorString(float[] embedding) {
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

## 四、RagQueryRequest 与 RagQueryController
`RagQueryService` 不能光躺在容器里，得有个 HTTP 入口方便大家用 cURL 或前端调通。DTO 和 Controller 如下（`ApiResponse` 沿用知识库管理接口里的统一包装）。

<img src="https://cdn.nlark.com/yuque/0/2026/png/29413969/1776759799052-9e4ad588-c675-4437-8df7-ae6688b5d5e6.png" width="1102" title="" crop="0,0,1,1" id="u22c8c837" class="ne-image">

```java
package com.jichi.ragkb.dto;

import lombok.Data;

import java.util.List;

@Data
public class RagQueryRequest {
    private String question;
    private List<Long> kbIds;
}
```

```java
package com.jichi.ragkb.controller;

import com.jichi.ragkb.dto.ApiResponse;
import com.jichi.ragkb.dto.RagQueryRequest;
import com.jichi.ragkb.service.RagQueryService;
import lombok.RequiredArgsConstructor;
import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/api/v1/rag")
@RequiredArgsConstructor
public class RagQueryController {

    private final RagQueryService ragQueryService;

    @PostMapping("/query")
    public ApiResponse<String> query(@RequestBody RagQueryRequest req) {
        return ApiResponse.ok(ragQueryService.query(req.getQuestion(), req.getKbIds()));
    }
}
```

### curl 测试
<img src="https://cdn.nlark.com/yuque/0/2026/png/29413969/1776759805603-c226bfef-7551-4736-878c-6ed4f9a288c3.png" width="1117" title="" crop="0,0,1,1" id="u354c7b6c" class="ne-image">

```bash
curl -X POST http://localhost:8080/api/v1/rag/query \
  -H "Content-Type: application/json" \
  -d '{"question":"代码提交需要遵守什么规范？","kbIds":[2]}'
```

查一个知识库里不存在的内容，验证模型不会瞎编：

```bash
curl -X POST http://localhost:8080/api/v1/rag/query \
  -H "Content-Type: application/json" \
  -d '{"question":"量子计算原理是什么？","kbIds":[1]}'
```

预期返回应包含"未找到"之类的提示，而不是模型自己编一段量子计算的内容。

基础 RAG 能跑通之后，检索层最值得加的一刀就是混合检索：向量 + 全文 + RRF 融合，召回率通常能明显抬一截，下一节鸡哥展开写。
