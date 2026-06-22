<img src="https://cdn.nlark.com/yuque/0/2026/png/29413969/1776761323991-97b22070-e07d-4fb7-b59a-3ea297640a82.png" width="1113" title="" crop="0,0,1,1" id="ub71bb753" class="ne-image">

鸡哥说句实话，很多团队 RAG 做到上一节那个程度就直接上线了，效果好不好全靠"自己试几个问题感觉还行"。这在 Demo 阶段没问题，但企业项目不能这么干——老板问"上了 RAG 之后效果提升了多少"，总不能回答"感觉还不错"吧？

<img src="https://cdn.nlark.com/yuque/0/2026/png/29413969/1776761333405-b4a69631-0176-47d6-bf06-65c96340fc46.png" width="1118" title="" crop="0,0,1,1" id="u3aa853cc" class="ne-image">

所以这节鸡哥带大家搭一套评估流水线，用数据说话。

这节实现 RAG 评估流水线：定义标准问题集 → 自动运行检索 → 计算 Hit Rate / MRR → 跑 RAGAS 评估 → 保存结果对比不同版本。

<img src="https://cdn.nlark.com/yuque/0/2026/png/29413969/1776761340618-4509b1b1-0c0a-46af-9c3a-de9af804cfd6.png" width="1119" title="" crop="0,0,1,1" id="u89278d0c" class="ne-image">

---

## 一、关键评估指标
### 检索指标（衡量召回质量）
<img src="https://cdn.nlark.com/yuque/0/2026/png/29413969/1776761359746-ef004be1-415d-4509-a5cb-b552dc69d913.png" width="1129" title="" crop="0,0,1,1" id="u13804f43" class="ne-image">

| 指标 | 含义 | 计算方法 |
| --- | --- | --- |
| Hit Rate | 命中率 | 期望 chunk 出现在 Top K 结果中的比例 |
| MRR | 平均倒数排名 | 命中 chunk 的排名倒数的均值，越高越好 |
| Precision@K | 精准率 | Top K 中相关 chunk 的比例 |


### 生成指标（衡量回答质量）
<img src="https://cdn.nlark.com/yuque/0/2026/png/29413969/1776761367590-577da652-1717-4372-a16e-1ef4b7f58abb.png" width="1112" title="" crop="0,0,1,1" id="u0a4490a8" class="ne-image">

| 指标 | 含义 | 说明 |
| --- | --- | --- |
| Faithfulness | 忠实性 | 答案中的事实是否都有 context 支撑 |
| Answer Relevancy | 答案相关性 | 答案是否回答了问题 |
| Context Relevancy | 上下文相关性 | 检索到的内容是否和问题相关 |


---

## 二、Entity 与 Repository
数据库设计那节定义了 `kb_eval_dataset`、`kb_eval_result`、`kb_answer_feedback` 三张表，这里把对应的 Entity 和 Repository 写出来。

<img src="https://cdn.nlark.com/yuque/0/2026/png/29413969/1776761390954-c992e566-19bb-4aaf-bf51-a367ed4b9197.png" width="1118" title="" crop="0,0,1,1" id="u1bfd510f" class="ne-image">

### EvalDataset
```java
package com.jichi.ragkb.entity;

import jakarta.persistence.*;
import lombok.Data;
import org.hibernate.annotations.CreationTimestamp;

import java.time.LocalDateTime;

@Entity
@Table(name = "kb_eval_dataset")
@Data
public class EvalDataset {

    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;

    @Column(nullable = false)
    private Long kbId;

    @Column(nullable = false, columnDefinition = "TEXT")
    private String question;

    @Column(columnDefinition = "TEXT")
    private String expectedAnswer;

    @Column(columnDefinition = "BIGINT[]")
    private Long[] expectedChunkIds;

    @Column(nullable = false)
    private Long createdBy;

    @CreationTimestamp
    private LocalDateTime createdAt;
}
```

### EvalResult
```java
package com.jichi.ragkb.entity;

import jakarta.persistence.*;
import lombok.Data;

import java.time.LocalDateTime;

@Entity
@Table(name = "kb_eval_result")
@Data
public class EvalResult {

    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;

    @Column(nullable = false)
    private Long datasetId;

    @Column(nullable = false, length = 50)
    private String evalVersion;

    @Column(nullable = false)
    private Boolean hit;

    private Integer rank;

    @Column(columnDefinition = "TEXT")
    private String actualAnswer;

    private Double faithfulness;

    private Double answerRelevancy;

    @Column(nullable = false)
    private LocalDateTime evalAt = LocalDateTime.now();
}
```

### AnswerFeedback
```java
package com.jichi.ragkb.entity;

import jakarta.persistence.*;
import lombok.Data;
import org.hibernate.annotations.CreationTimestamp;

import java.time.LocalDateTime;

@Entity
@Table(name = "kb_answer_feedback")
@Data
public class AnswerFeedback {

    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;

    @Column(nullable = false)
    private Long messageId;

    @Column(nullable = false)
    private Long userId;

    @Column(nullable = false)
    private Short feedback;

    private String comment;

    @CreationTimestamp
    private LocalDateTime createdAt;
}
```

### EvalReport DTO
Repository 的聚合查询会返回 `EvalReport`，所以先把这个 DTO 定义好：

```java
package com.jichi.ragkb.dto;

import lombok.AllArgsConstructor;
import lombok.Builder;
import lombok.Data;
import lombok.NoArgsConstructor;

import java.time.LocalDateTime;

@Data
@Builder
@AllArgsConstructor
@NoArgsConstructor
public class EvalReport {
    private Long kbId;
    private String evalVersion;
    private long totalQuestions;
    private long hitCount;
    private double hitRate;
    private double mrr;
    private double avgFaithfulness;
    private LocalDateTime evalAt;

    public String summary() {
        return String.format(
                "评估版本：%s | 问题数：%d | Hit Rate：%.1f%% | MRR：%.4f | Faithfulness：%.4f",
                evalVersion, totalQuestions,
                hitRate * 100, mrr, avgFaithfulness);
    }
}
```

JPQL 的 `new` 语法要求目标类有匹配的全参构造器，`@AllArgsConstructor` 就是干这个的。`totalQuestions` 和 `hitCount` 改成 `long` 类型，因为 JPQL 的 `COUNT` 和 `SUM` 返回的是 `Long`。

### Repository
```java
package com.jichi.ragkb.repository;

import com.jichi.ragkb.entity.EvalDataset;
import org.springframework.data.jpa.repository.JpaRepository;

import java.util.List;

public interface EvalDatasetRepository extends JpaRepository<EvalDataset, Long> {

    List<EvalDataset> findByKbId(Long kbId);
}
```

```java
package com.jichi.ragkb.repository;

import com.jichi.ragkb.dto.EvalReport;
import com.jichi.ragkb.entity.EvalResult;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Query;

import java.util.List;

public interface EvalResultRepository extends JpaRepository<EvalResult, Long> {

    @Query("""
            SELECT new com.jichi.ragkb.dto.EvalReport(
                d.kbId, r.evalVersion, COUNT(r), 
                SUM(CASE WHEN r.hit = true THEN 1 ELSE 0 END),
                AVG(CASE WHEN r.hit = true THEN 1.0 ELSE 0.0 END),
                AVG(CASE WHEN r.rank > 0 THEN 1.0 / r.rank ELSE 0.0 END),
                AVG(COALESCE(r.faithfulness, 0)),
                MAX(r.evalAt))
            FROM EvalResult r JOIN EvalDataset d ON r.datasetId = d.id
            WHERE d.kbId = :kbId
            GROUP BY d.kbId, r.evalVersion
            ORDER BY MAX(r.evalAt) DESC
            """)
    List<EvalReport> aggregateByVersion(Long kbId);
}
```

```java
package com.jichi.ragkb.repository;

import com.jichi.ragkb.entity.AnswerFeedback;
import org.springframework.data.jpa.repository.JpaRepository;

public interface AnswerFeedbackRepository extends JpaRepository<AnswerFeedback, Long> {
    Optional<AnswerFeedback> findByMessageIdAndUserId(Long messageId, Long userId);
}
```

---

## 三、EvalService——评估流水线
```java
package com.jichi.ragkb.service;

import com.jichi.ragkb.entity.*;
import com.jichi.ragkb.repository.*;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.stereotype.Service;

import java.time.LocalDateTime;
import java.util.*;
import java.util.stream.Collectors;

@Service
@RequiredArgsConstructor
@Slf4j
public class EvalService {

    private final EvalDatasetRepository datasetRepository;
    private final EvalResultRepository resultRepository;
    private final EnhancedRetrieverService retriever;
    private final RerankerService rerankerService;
    private final ConfidenceFilter confidenceFilter;
    private final HallucinationChecker hallucinationChecker;
    private final StreamingRagService ragService;

    /**
     * 运行完整评估流水线。
     *
     * @param kbId        知识库 ID
     * @param evalVersion 评估版本标识（如 v1_chunk512_hybrid_reranker）
     * @return 评估摘要报告
     */
    public EvalReport runEvaluation(Long kbId, String evalVersion) {
        List<EvalDataset> questions = datasetRepository.findByKbId(kbId);
        if (questions.isEmpty()) {
            throw new RuntimeException("知识库 " + kbId + " 没有评估数据集，请先录入标准问题");
        }

        log.info("[Eval] 开始评估：kbId={}，version={}，问题数={}", kbId, evalVersion, questions.size());

        List<EvalResult> results = new ArrayList<>();
        int hits = 0;
        double mrr = 0.0;
        double totalFaithfulness = 0.0;
        int evalCount = 0;

        for (EvalDataset question : questions) {
            try {
                EvalResult result = evaluateOne(question, kbId, evalVersion);
                results.add(result);

                if (result.getHit()) hits++;
                if (result.getRank() != null && result.getRank() > 0) {
                    mrr += 1.0 / result.getRank();
                }
                if (result.getFaithfulness() != null) {
                    totalFaithfulness += result.getFaithfulness();
                    evalCount++;
                }

            } catch (Exception e) {
                log.error("[Eval] 问题评估失败：questionId={}，error={}", question.getId(), e.getMessage());
            }
        }

        // 批量保存评估结果
        resultRepository.saveAll(results);

        double hitRate = questions.isEmpty() ? 0 : (double) hits / questions.size();
        double mrrScore = questions.isEmpty() ? 0 : mrr / questions.size();
        double avgFaithfulness = evalCount == 0 ? 0 : totalFaithfulness / evalCount;

        EvalReport report = EvalReport.builder()
                .kbId(kbId)
                .evalVersion(evalVersion)
                .totalQuestions(questions.size())
                .hitCount(hits)
                .hitRate(hitRate)
                .mrr(mrrScore)
                .avgFaithfulness(avgFaithfulness)
                .evalAt(LocalDateTime.now())
                .build();

        log.info("[Eval] 评估完成：hitRate={}%，MRR={}，faithfulness={}",
                String.format("%.2f", hitRate * 100),
                String.format("%.4f", mrrScore),
                String.format("%.4f", avgFaithfulness));

        return report;
    }

    private EvalResult evaluateOne(EvalDataset question, Long kbId, String evalVersion) {
        // 执行检索
        List<HybridRetrieverService.ScoredChunk> candidates =
                retriever.retrieveWithHyde(question.getQuestion(), List.of(kbId), 20);
        List<HybridRetrieverService.ScoredChunk> reranked =
                rerankerService.rerank(question.getQuestion(), candidates, 10);

        // 计算 Hit Rate 和 MRR
        Long[] expectedChunkIds = question.getExpectedChunkIds();
        boolean hit = false;
        int rank = 0;

        if (expectedChunkIds != null && expectedChunkIds.length > 0) {
            Set<Long> expected = Set.of(expectedChunkIds);
            for (int i = 0; i < reranked.size(); i++) {
                if (expected.contains(reranked.get(i).id())) {
                    hit = true;
                    rank = i + 1;  // 1-based
                    break;
                }
            }
        }

        // 生成回答并评估忠实性（抽样评估，降低成本）
        String actualAnswer = null;
        Double faithfulness = null;

        if (question.getExpectedAnswer() != null) {
            RagResponse response = ragService.syncQuery(
                    question.getQuestion(), List.of(kbId), "eval-session");
            actualAnswer = response.getAnswer();

            // 忠实性检测（每次评估都跑，但仅限标准问题集）
            String context = candidates.stream()
                    .limit(5)
                    .map(HybridRetrieverService.ScoredChunk::content)
                    .collect(Collectors.joining("\n\n"));

            HallucinationChecker.FaithfulnessResult faithResult =
                    hallucinationChecker.check(question.getQuestion(), actualAnswer, context);
            faithfulness = faithResult.score();
        }

        EvalResult result = new EvalResult();
        result.setDatasetId(question.getId());
        result.setEvalVersion(evalVersion);
        result.setHit(hit);
        result.setRank(rank > 0 ? rank : null);
        result.setActualAnswer(actualAnswer);
        result.setFaithfulness(faithfulness);
        result.setEvalAt(LocalDateTime.now());

        return result;
    }

    /**
     * 对比不同评估版本的指标，生成对比报告。
     */
    public List<EvalReport> compareVersions(Long kbId) {
        // 从数据库查历史评估结果，按版本聚合
        return resultRepository.aggregateByVersion(kbId);
    }
}
```

---

## 四、EvalController
```java
package com.jichi.ragkb.controller;

import com.jichi.ragkb.dto.ApiResponse;
import com.jichi.ragkb.dto.EvalReport;
import com.jichi.ragkb.entity.DocChunk;
import com.jichi.ragkb.entity.EvalDataset;
import com.jichi.ragkb.repository.DocChunkRepository;
import com.jichi.ragkb.repository.EvalDatasetRepository;
import com.jichi.ragkb.security.UserContext;
import com.jichi.ragkb.service.EvalService;
import lombok.Data;
import lombok.RequiredArgsConstructor;
import org.springframework.web.bind.annotation.*;

import java.util.List;

@RestController
@RequestMapping("/api/v1/eval")
@RequiredArgsConstructor
public class EvalController {

    private final EvalService evalService;
    private final EvalDatasetRepository datasetRepository;
    private final DocChunkRepository chunkRepository;

    /** 触发评估（管理员专用） */
    @PostMapping("/{kbId}/run")
    public ApiResponse<EvalReport> runEval(
            @PathVariable Long kbId,
            @RequestParam(defaultValue = "latest") String version) {
        EvalReport report = evalService.runEvaluation(kbId, version);
        return ApiResponse.ok(report);
    }

    /** 查看历史评估对比 */
    @GetMapping("/{kbId}/history")
    public ApiResponse<List<EvalReport>> getHistory(@PathVariable Long kbId) {
        return ApiResponse.ok(evalService.compareVersions(kbId));
    }

    // ==================== 评估数据集管理 ====================

    /** 查询知识库的评估数据集 */
    @GetMapping("/{kbId}/dataset")
    public ApiResponse<List<EvalDataset>> listDataset(@PathVariable Long kbId) {
        return ApiResponse.ok(datasetRepository.findByKbId(kbId));
    }

    /** 新增评估问题 */
    @PostMapping("/{kbId}/dataset")
    public ApiResponse<EvalDataset> addQuestion(
            @PathVariable Long kbId,
            @RequestBody EvalDatasetRequest req) {
        EvalDataset item = new EvalDataset();
        item.setKbId(kbId);
        item.setQuestion(req.getQuestion());
        item.setExpectedAnswer(req.getExpectedAnswer());
        item.setExpectedChunkIds(req.getExpectedChunkIds());
        item.setCreatedBy(UserContext.getUserId());
        return ApiResponse.ok(datasetRepository.save(item));
    }

    /** 更新评估问题（含回填 expectedChunkIds） */
    @PutMapping("/{kbId}/dataset/{id}")
    public ApiResponse<EvalDataset> updateQuestion(
            @PathVariable Long kbId,
            @PathVariable Long id,
            @RequestBody EvalDatasetRequest req) {
        EvalDataset item = datasetRepository.findById(id)
                .orElseThrow(() -> new RuntimeException("评估数据不存在"));
        if (req.getQuestion() != null) item.setQuestion(req.getQuestion());
        if (req.getExpectedAnswer() != null) item.setExpectedAnswer(req.getExpectedAnswer());
        if (req.getExpectedChunkIds() != null) item.setExpectedChunkIds(req.getExpectedChunkIds());
        return ApiResponse.ok(datasetRepository.save(item));
    }

    /** 删除评估问题 */
    @DeleteMapping("/{kbId}/dataset/{id}")
    public ApiResponse<Void> deleteQuestion(
            @PathVariable Long kbId,
            @PathVariable Long id) {
        datasetRepository.deleteById(id);
        return ApiResponse.ok(null);
    }

    // ==================== Chunk 查询（用于标注 expectedChunkIds） ====================

    /** 查询知识库下的所有 Chunk（只返回 id、docId、chunkIndex 和内容摘要） */
    @GetMapping("/{kbId}/chunks")
    public ApiResponse<List<ChunkSummary>> listChunks(@PathVariable Long kbId) {
        List<DocChunk> chunks = chunkRepository.findByKbId(kbId);
        List<ChunkSummary> summaries = chunks.stream().map(c -> {
            ChunkSummary s = new ChunkSummary();
            s.setId(c.getId());
            s.setDocId(c.getDocId());
            s.setChunkIndex(c.getChunkIndex());
            s.setContent(c.getContent().length() > 200
                    ? c.getContent().substring(0, 200) + "..."
                    : c.getContent());
            s.setTokenCount(c.getTokenCount());
            return s;
        }).toList();
        return ApiResponse.ok(summaries);
    }

    @Data
    public static class EvalDatasetRequest {
        private String question;
        private String expectedAnswer;
        private Long[] expectedChunkIds;
    }

    @Data
    public static class ChunkSummary {
        private Long id;
        private Long docId;
        private Integer chunkIndex;
        private String content;
        private Integer tokenCount;
    }
}
```

跑评估之前有两个前提条件：

**1）先上传测试文档**

`kb_doc_chunk` 表里的 chunk 是文档上传后自动切分生成的，不是手动插入的。如果还没上传过测试文档，先通过上传接口把 `test-docs/` 下的文件传进去：

```bash
# 上传 HR 手册到知识库 1
curl -X POST http://localhost:8080/api/v1/kb/1/documents \
  -H "Authorization: Bearer <token>" \
  -F "file=@src/test/resources/test-docs/hr-handbook.txt"

# 等索引状态变为 DONE
curl -H "Authorization: Bearer <token>" \
  http://localhost:8080/api/v1/kb/1/documents/1/status
```

**2）回填 **`expected_chunk_ids`

文档上传切分完成后，查一下实际生成的 chunk ID，再回填到评估数据集：

```sql
-- 查看知识库 1 的 chunk ID
SELECT id, LEFT(content, 50) FROM kb_doc_chunk WHERE kb_id = 1 ORDER BY id;

-- 根据查询结果回填（这里的 ID 以你的实际数据为准）
UPDATE kb_eval_dataset SET expected_chunk_ids = ARRAY[1, 2] WHERE id = 1;
UPDATE kb_eval_dataset SET expected_chunk_ids = ARRAY[3]    WHERE id = 2;
```

两步都做完，再跑评估，Hit Rate 和 MRR 才有意义：

```bash
# 先登录拿 token
curl -X POST http://localhost:8080/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"demo123"}'

# 触发知识库 1 的评估（version 标识本次评估配置，方便对比）
curl -X POST "http://localhost:8080/api/v1/eval/1/run?version=v1_hybrid_reranker" \
  -H "Authorization: Bearer <token>"

# 查看知识库 1 的历史评估记录（对比不同版本的效果）
curl -H "Authorization: Bearer <token>" \
  http://localhost:8080/api/v1/eval/1/history
```

---

## 五、用户反馈——扩充评估数据集
<img src="https://cdn.nlark.com/yuque/0/2026/png/29413969/1776761428126-809eb2f2-a369-4a74-b8b6-7348ec3f246b.png" width="1115" title="" crop="0,0,1,1" id="u6a8c4cc9" class="ne-image">

用户在使用过程中的点赞/点踩，是扩充评估数据集的黄金来源：

```java
package com.jichi.ragkb.service;

import com.jichi.ragkb.entity.*;
import com.jichi.ragkb.repository.*;
import com.jichi.ragkb.security.UserContext;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

@Service
@RequiredArgsConstructor
@Slf4j
public class FeedbackService {

    private final AnswerFeedbackRepository feedbackRepository;
    private final ChatMessageRepository messageRepository;
    private final EvalDatasetRepository datasetRepository;

    /**
     * 提交用户反馈（点赞/点踩）。
     * 对于差评，自动提取问题加入候选评估数据集（人工审核后正式纳入）。
     */
    @Transactional
    public void submitFeedback(Long messageId, int feedback, String comment) {
        ChatMessage message = messageRepository.findById(messageId)
                .orElseThrow(() -> new RuntimeException("消息不存在"));

        // 同一用户对同一消息只保留一条反馈，重复提交则覆盖
        Long userId = UserContext.getUserId();
        AnswerFeedback fb = feedbackRepository.findByMessageIdAndUserId(messageId, userId)
                .orElseGet(() -> {
                    AnswerFeedback newFb = new AnswerFeedback();
                    newFb.setMessageId(messageId);
                    newFb.setUserId(userId);
                    return newFb;
                });
        fb.setFeedback((short) feedback);
        fb.setComment(comment);
        feedbackRepository.save(fb);

        // 更新消息的 feedback 字段
        message.setFeedback((short) feedback);
        messageRepository.save(message);

        // 差评：把这个问题加入评估候选（人工审核后加入正式评估集）
        if (feedback == -1) {
            log.info("[Feedback] 差评记录，候选加入评估集：messageId={}", messageId);
            // 这里只记录日志，实际需要一个管理后台人工审核后才加入 eval_dataset
        }

        log.info("[Feedback] 反馈已记录：messageId={}，feedback={}，userId={}",
                messageId, feedback, UserContext.getUserId());
    }
}
```

Service 有了，还得给它配个 Controller，不然前端没法调：

```java
package com.jichi.ragkb.controller;

import com.jichi.ragkb.dto.ApiResponse;
import com.jichi.ragkb.service.FeedbackService;
import lombok.RequiredArgsConstructor;
import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/api/v1/feedback")
@RequiredArgsConstructor
public class FeedbackController {

    private final FeedbackService feedbackService;

    @PostMapping("/{messageId}")
    public ApiResponse<Void> submitFeedback(
            @PathVariable Long messageId,
            @RequestParam int feedback,
            @RequestParam(required = false) String comment) {
        feedbackService.submitFeedback(messageId, feedback, comment);
        return ApiResponse.ok(null);
    }
}
```

用户反馈接口就这么简单，大家可以直接试：

```bash
# 点赞（feedback=1），messageId 换成实际的消息 ID
curl -X POST "http://localhost:8080/api/v1/feedback/1?feedback=1" \
  -H "Authorization: Bearer <token>"

# 点踩 + 评论（feedback=-1）
curl -X POST "http://localhost:8080/api/v1/feedback/1?feedback=-1&comment=答案不准确" \
  -H "Authorization: Bearer <token>"
```

---

## 六、链路追踪日志
<img src="https://cdn.nlark.com/yuque/0/2026/png/29413969/1776761474870-7330bddf-1aba-4b53-8aa6-fd4715bac76f.png" width="1101" title="" crop="0,0,1,1" id="ua598ad17" class="ne-image">

为每次 RAG 查询加入 `traceId`，方便排查问题：

```java
package com.jichi.ragkb.security;

import jakarta.servlet.Filter;
import jakarta.servlet.FilterChain;
import jakarta.servlet.ServletException;
import jakarta.servlet.ServletRequest;
import jakarta.servlet.ServletResponse;
import jakarta.servlet.http.HttpServletRequest;
import org.slf4j.MDC;
import org.springframework.stereotype.Component;

import java.io.IOException;
import java.util.UUID;

/**
 * 请求追踪过滤器：为每个请求生成 traceId，注入 MDC 供日志使用。
 * application.yml 中日志格式已配置 [%X{traceId}]，自动打印。
 */
@Component
public class TraceFilter implements Filter {

    private static final String TRACE_ID_HEADER = "X-Trace-Id";

    @Override
    public void doFilter(ServletRequest request, ServletResponse response,
                          FilterChain chain) throws IOException, ServletException {
        HttpServletRequest httpReq = (HttpServletRequest) request;

        // 优先使用客户端传入的 traceId（方便分布式追踪）
        String traceId = httpReq.getHeader(TRACE_ID_HEADER);
        if (traceId == null || traceId.isBlank()) {
            traceId = UUID.randomUUID().toString().substring(0, 8);
        }

        MDC.put("traceId", traceId);
        try {
            chain.doFilter(request, response);
        } finally {
            MDC.remove("traceId");
        }
    }
}
```

到这里，评估、反馈、链路追踪三件套就齐了。系统效果有数据衡量，用户的吐槽也能收集回来持续改进。

---
