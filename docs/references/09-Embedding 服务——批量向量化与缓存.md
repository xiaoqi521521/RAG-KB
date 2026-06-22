<img src="https://cdn.nlark.com/yuque/0/2026/png/29413969/1776758452943-3f5c205c-2605-4f2d-94e7-dfec6ae2f385.png" width="1113" title="" crop="0,0,1,1" id="ue8fdb39e" class="ne-image">

Embedding 是把文本转成向量的过程，是 RAG 向量检索的基础。这节重点解决两个生产问题：

1. **批量化**：几百个 chunk 不能一个个调 API，要批量提交降低延迟
2. **缓存**：相同文本不重复计算，节省 Token 成本

鸡哥先说一下整体设计思路。很多教程讲 Embedding，就是调一下 `embeddingModel.call()`，拿到向量，完事。Demo 没问题，但一到生产就崩——一个 100 页的文档分完块有几百个 chunk，你一个个调 API 慢得离谱，而且同一份文档改个标题重建索引，所有 chunk 又要重新算一遍向量，Token 白花花地烧。

<img src="https://cdn.nlark.com/yuque/0/2026/png/29413969/1776758700318-92bbfc15-edee-4e2a-bcad-e940f886e40d.png" width="1115" title="" crop="0,0,1,1" id="u0c60b111" class="ne-image">

所以这节的核心设计目标就两个字：**快**和**省**。快靠批量，省靠缓存。整个 EmbeddingService 的架构分三层：

<img src="https://cdn.nlark.com/yuque/0/2026/png/29413969/1776758736352-abaff457-d4e6-49e6-a91f-1f817c585e2a.png" width="1119" title="" crop="0,0,1,1" id="u66ce05a5" class="ne-image">

+ **缓存层**：Redis 存向量结果，命中直接返回，不调 API
+ **批量层**：缓存没命中的文本，攒一批统一调 API，减少网络往返
+ **容错层**：API 调用加重试 + 兜底，不因为一次网络抖动让整个索引失败

为什么不用本地缓存（比如 Caffeine）而用 Redis？两个核心原因：

+ **多实例部署需要共享缓存**：服务一般跑多个 Pod，Caffeine 是堆内缓存，每个 Pod 一份，相同 chunk 会被重复算多次
+ **数据量大不适合放堆内**：一个 1536 维的 float 数组在堆内约 6KB（`1536 × 4 byte`），Redis 用 CSV 字符串序列化后约 12~16 KB；几万个 chunk 就是 GB 级数据，全放堆内会让 GC 暂停时间显著拉长

Redis 天然适合这个场景：容量大、可跨实例共享、支持 TTL 自动过期。

<img src="https://cdn.nlark.com/yuque/0/2026/png/29413969/1776758747384-ec635741-635a-4ee3-867f-8f17e78d317d.png" width="1124" title="" crop="0,0,1,1" id="u0f2297a8" class="ne-image">



---

## 一、为什么要做批量缓存
一个 50 页的 PDF，分成约 100 个 chunk，每个 chunk 调一次 Embedding API：

+ 顺序调用：100 次 × 平均 300ms = **30 秒**
+ 批量调用（每批 20 个）：5 次 × 300ms = **1.5 秒**

差了 **20 倍**。而且 Embedding API 按 Token 计费，同一个 chunk 内容不变，下次重建索引时不需要重新向量化，直接读缓存即可。鸡哥遇到过真实场景——一个知识库有 200 份文档，每次全量重建索引如果不做缓存，光 Embedding 就要烧好几块钱。**假设每次只有 10% 的文档发生新增或修改**，加上缓存之后只有这部分会真的调 API，成本就降到原来的十分之一左右——实际节省比例取决于文档变更频率，**变更越少、缓存越省**。

---

## 二、EmbeddingService
<img src="https://cdn.nlark.com/yuque/0/2026/png/29413969/1776758767631-eb7145a5-5fc5-4ee9-9cb9-91417e115a36.png" width="1122" title="" crop="0,0,1,1" id="uda6e06ae" class="ne-image">

鸡哥说一下这个 Service 里几个关键设计决策，大家写的时候可能会遇到同样的纠结：

**1）缓存 Key 为什么用 MD5 而不是原文？**

chunk 内容少则几十字，多则上千字，直接当 Redis Key 太长了（Redis Key 建议不超过 512 字节）。用 MD5 固定 32 字符，既短又唯一。有人可能说 MD5 有碰撞风险——理论上有，但在这个场景下概率小到可以忽略，而且碰撞最多导致拿到错误缓存，重建索引一次就修复了，不是安全敏感场景，不需要用 SHA-256。

**2）缓存 Key 带 **`v1`** 前缀是什么意思？**

`emb:v1:` 里的 `v1` 代表 Embedding 模型版本。换了模型（比如从 `text-embedding-v2` 升级到 `text-embedding-v3`），向量维度和空间都会变，旧缓存直接作废。这时候把前缀改成 `v2`，旧缓存自然就不会命中了，等 TTL 到期自动清理，不需要手动去 Redis 删 Key。

<img src="https://cdn.nlark.com/yuque/0/2026/png/29413969/1776758774792-ef050eae-9638-4faa-863a-b5ef12d0c656.png" width="1126" title="" crop="0,0,1,1" id="u756d66a4" class="ne-image">

**3）BATCH_SIZE 为什么是 20？**

DashScope 的 Embedding API 单次最多接受 25 个文本，鸡哥设成 20 留一些余量。设太大容易触发限流或超时，设太小批量效果不明显。20 是鸡哥实测下来比较稳的值，大家可以根据自己用的模型 API 限制来调。

**4）**`embedBatch`** 方法为什么分三步走？**

先查缓存 → 缓存未命中的统一调 API → 按原始顺序组装结果。这么设计是因为调用方传进来的 texts 列表是有顺序的（第 i 个文本对应第 i 个 chunk），最终返回的向量必须和输入顺序一一对应。如果不维护这个顺序映射，分块和向量就对不上了，后续写库就全乱了。

**5）为什么要 **`@Lazy`** 注入一个 **`self`** 字段？**

这是和后面 `IndexTaskLauncher` 一样的"AOP 自调用坑"——`@Retryable` 和 `@Recover` 都基于 Spring AOP 代理实现，**Bean 内部直接 **`this.embedFromApi(...)`** 会绕过代理，重试和兜底全失效**。

解决方法是注入自身：构造器参数加 `@Lazy EmbeddingService self`（不加 `@Lazy` 会构造期循环依赖——自己构造时拿不到自己）。`embedBatch` 里改成 `self.embedFromApi(...)`，调用会先经过代理 → 触发 `@Retryable` → 失败走 `@Recover`。

鸡哥这里特别强调一下——**很多教程里写 **`embedBatch`** 直接 **`embedFromApi(...)`**，看上去能跑、其实重试根本不生效**——只有真出问题时才会暴露，是个超隐蔽的 Bug。**Bean 内部调用带注解的方法，一定要么走 **`self`** 走代理、要么把目标方法抽到独立 Bean**。

**6）**`@Retryable(retryFor = ...)`** 为什么不直接写 **`Exception.class`**？**

写 `Exception.class` 会把**所有异常**都重试 3 次——包括 4xx 类的客户端错（API Key 错、参数错、单条文本超 token 上限）。这类错误"重试 100 次结果也一样"，纯纯浪费时间和 Token 费。

正确做法是**只对值得重试的异常重试**：

+ `ResourceAccessException`：网络 IO 异常（连接超时、读超时、Connection reset）
+ `HttpServerErrorException`：5xx 服务端错（502/503/504，下游临时故障）
+ `TimeoutException`：超时

同时用 `noRetryFor = HttpClientErrorException.class` 显式排除 4xx——**4xx 直接冒泡给调用方处理**，不重试。

**7）反序列化时为什么要 try-catch？**

`deserializeVector` 里调 `Float.parseFloat`——如果 Redis 缓存里塞进了脏数据（旧版本残留、人工写入、网络截断），会抛 `NumberFormatException`。

`embedBatch` 里对这个异常**捕获后当缓存 miss 处理**——清掉脏 key、走 API 重新算。**一份 100 chunk 的文档不能因为 1 个 key 损坏就全批失败**。



```java
package com.jichi.ragkb.service;

import lombok.extern.slf4j.Slf4j;
import org.springframework.ai.embedding.EmbeddingModel;
import org.springframework.ai.embedding.EmbeddingRequest;
import org.springframework.ai.embedding.EmbeddingResponse;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.annotation.Lazy;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.retry.annotation.Backoff;
import org.springframework.retry.annotation.Recover;
import org.springframework.retry.annotation.Retryable;
import org.springframework.stereotype.Service;
import org.springframework.web.client.HttpClientErrorException;
import org.springframework.web.client.HttpServerErrorException;
import org.springframework.web.client.ResourceAccessException;

import java.time.Duration;
import java.util.*;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.stream.IntStream;

@Service
@Slf4j
public class EmbeddingService {

    private final EmbeddingModel embeddingModel;
    private final StringRedisTemplate redisTemplate;
    private final TokenMetrics tokenMetrics;

    /**
     * 自身代理引用——用于内部调用走 AOP 代理（@Retryable / @Recover 才会生效）。
     * 必须用 @Lazy，否则会形成构造期循环依赖（自己构造时拿不到自己）。
     */
    private final EmbeddingService self;

    public EmbeddingService(EmbeddingModel embeddingModel,
                            StringRedisTemplate redisTemplate,
                            TokenMetrics tokenMetrics,
                            @Lazy EmbeddingService self) {
        this.embeddingModel = embeddingModel;
        this.redisTemplate = redisTemplate;
        this.tokenMetrics = tokenMetrics;
        this.self = self;
    }

    private static final String CACHE_PREFIX = "emb:v1:";

    @Value("${rag.cache.embedding-ttl:7d}")
    private Duration embeddingTtl;

    private static final int BATCH_SIZE = 20;

    /**
     * 批量向量化，带 Redis 缓存。
     * 先查缓存，缓存未命中的批量调 API，结果写入缓存。
     *
     * @param texts 待向量化的文本列表
     * @return 与输入顺序对应的向量列表
     */
    public List<float[]> embedBatch(List<String> texts) {
        if (texts == null || texts.isEmpty()) return List.of();

        Map<Integer, float[]> cached = new HashMap<>();
        List<Integer> missedIndices = new ArrayList<>();
        List<String> missedTexts = new ArrayList<>();

        for (int i = 0; i < texts.size(); i++) {
            String cacheKey = buildCacheKey(texts.get(i));
            String cachedStr = redisTemplate.opsForValue().get(cacheKey);
            if (cachedStr != null) {
                try {
                    cached.put(i, deserializeVector(cachedStr));
                } catch (NumberFormatException ex) {
                    // 缓存数据被污染（旧版本残留 / 人工写入 / 截断等）
                    // 当作缓存 miss 处理：清掉脏 key、走 API 重新算
                    log.warn("[Embedding] 缓存数据损坏，回退到 API：key={}, err={}",
                            cacheKey, ex.getMessage());
                    redisTemplate.delete(cacheKey);
                    missedIndices.add(i);
                    missedTexts.add(texts.get(i));
                }
            } else {
                missedIndices.add(i);
                missedTexts.add(texts.get(i));
            }
        }

        log.debug("[Embedding] 总数={}，缓存命中={}，需要调API={}",
                texts.size(), cached.size(), missedTexts.size());

        if (!missedTexts.isEmpty()) {
            // ★ 关键：必须用 self.embedFromApi(...) 走代理，否则 @Retryable / @Recover 不生效
            //   直接 this.embedFromApi(...) 会绕过 Spring AOP 代理 —— 重试/兜底全失效
            List<float[]> newVectors = self.embedFromApi(missedTexts);

            for (int j = 0; j < missedIndices.size(); j++) {
                int originalIndex = missedIndices.get(j);
                float[] vector = newVectors.get(j);
                cached.put(originalIndex, vector);

                String cacheKey = buildCacheKey(texts.get(originalIndex));
                redisTemplate.opsForValue().set(cacheKey, serializeVector(vector), embeddingTtl);
            }
        }

        return IntStream.range(0, texts.size())
                .mapToObj(cached::get)
                .toList();
    }

    /**
     * 调 Embedding API，按批次处理，避免单次请求过大。
     *
     * 带重试：网络抖动 / 5xx / 限流 时自动重试 3 次，指数退避。
     * 不重试：4xx 类客户端错（API Key 错、参数错、文本超长）—— 重试 100 次结果一样，纯浪费时间和 Token。
     */
    @Retryable(
        retryFor = {
            ResourceAccessException.class,        // 网络 IO 异常（连接超时、读超时、Connection reset）
            HttpServerErrorException.class,        // 5xx 服务端错误（502/503/504）
            java.util.concurrent.TimeoutException.class
        },
        noRetryFor = HttpClientErrorException.class,   // 4xx 不重试（401/400/413）
        maxAttempts = 3,
        backoff = @Backoff(delay = 1000, multiplier = 2)
    )
    public List<float[]> embedFromApi(List<String> texts) {
        List<float[]> result = new ArrayList<>();
        AtomicInteger totalTokens = new AtomicInteger(0);

        // 分批提交
        for (int start = 0; start < texts.size(); start += BATCH_SIZE) {
            int end = Math.min(start + BATCH_SIZE, texts.size());
            List<String> batch = texts.subList(start, end);

            long batchStart = System.currentTimeMillis();
            EmbeddingResponse response = embeddingModel.call(
                    new EmbeddingRequest(batch, null));
            long elapsed = System.currentTimeMillis() - batchStart;

            // 统计 Token 消耗（用于成本监控）
            if (response.getMetadata() != null && response.getMetadata().getUsage() != null) {
                long tokens = response.getMetadata().getUsage().getTotalTokens();
                totalTokens.addAndGet((int) tokens);
            }

            // 按顺序提取向量
            // Spring AI 1.1.x：Embedding.getOutput() 返回 float[]
            response.getResults().stream()
                    .sorted(Comparator.comparingInt(r -> r.getIndex()))
                    .forEach(r -> result.add(r.getOutput()));

            log.debug("[Embedding] 批次{}/{}，size={}，耗时={}ms",
                    start / BATCH_SIZE + 1,
                    (texts.size() + BATCH_SIZE - 1) / BATCH_SIZE,
                    batch.size(), elapsed);
        }

        log.info("[Embedding] API调用完成，共{}条，消耗Token={}",
                texts.size(), totalTokens.get());

        if (totalTokens.get() > 0) {
            tokenMetrics.recordEmbeddingTokens(totalTokens.get());
        }

        return result;
    }

    /**
     * 兜底方法 —— 重试 3 次后仍失败时进入这里（仅对 retryFor 中的异常生效）。
     *
     * 注意：4xx 客户端错（HttpClientErrorException）已通过 noRetryFor 排除——
     * 这类异常不会进入重试链路、也不会触发 @Recover，会直接冒泡给调用方。
     */
    @Recover
    public List<float[]> embedFromApiFallback(Exception e, List<String> texts) {
        log.error("[Embedding] 重试3次后仍失败，texts.size={}，error={}",
                texts.size(), e.getMessage());
        throw new RuntimeException("Embedding API 调用失败，已重试3次：" + e.getMessage(), e);
    }

    /** 单条向量化（查询时使用） */
    public float[] embed(String text) {
        List<float[]> result = embedBatch(List.of(text));
        return result.isEmpty() ? new float[0] : result.get(0);
    }

    private String buildCacheKey(String text) {
        // 用内容的 MD5 作为缓存 Key，避免 Key 过长
        return CACHE_PREFIX + toMd5(text);
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

    /**
     * float[] 转逗号分隔字符串存入 Redis。
     * 不用 JSON 序列化器，避免 GenericJackson2JsonRedisSerializer
     * 把浮点数当类名解析导致反序列化失败。
     */
    private String serializeVector(float[] vector) {
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < vector.length; i++) {
            if (i > 0) sb.append(',');
            sb.append(vector[i]);
        }
        return sb.toString();
    }

    private float[] deserializeVector(String str) {
        str = str.replace("[", "").replace("]", "").replace(" ", "");
        String[] parts = str.split(",");
        float[] vector = new float[parts.length];
        for (int i = 0; i < parts.length; i++) {
            vector[i] = Float.parseFloat(parts[i]);
        }
        return vector;
    }

}
```

鸡哥展开说两个点：

<img src="https://cdn.nlark.com/yuque/0/2026/png/29413969/1776758799748-9ec1ea75-ac82-4153-ba18-dc81b86b5fe6.png" width="1121" title="" crop="0,0,1,1" id="u8cc1ab9d" class="ne-image">

**为什么重试用指数退避（1s → 2s → 4s）而不是固定间隔？**

Embedding API 报错通常是两个原因：网络抖动和限流。如果是限流，固定间隔 1 秒连续重试 3 次，大概率 3 次全失败，因为限流窗口还没过。指数退避给了服务端喘息的时间——第一次等 1 秒，第二次等 2 秒，第三次等 4 秒，命中率高很多。

**为什么兜底方法 **`embedFromApiFallback`** 选择抛异常而不是返回空向量？**

有些同学可能觉得返回一组零向量也行，起码不报错。但鸡哥告诉大家，这样做后果更严重——零向量写进数据库后，用户查询时这些 chunk 会以随机的相似度被召回，返回一堆毫不相关的内容，用户体验直接炸。**宁可索引失败让管理员看到报错去处理，也不能让脏数据悄悄混进去。**这是生产系统设计的一个原则：**fail loud > fail silent**。

---

## 三、IndexService 依赖准备
<img src="https://cdn.nlark.com/yuque/0/2026/png/29413969/1776758811080-55386c17-22ae-4e5d-b55b-1abb021e19ff.png" width="1105" title="" crop="0,0,1,1" id="u3391ba69" class="ne-image">

`IndexService` 是索引管道的核心，它依赖几个目前还没定义的类。在写 `IndexService` 之前，先把这些依赖补齐。

### KbDocumentRepository
```java
package com.jichi.ragkb.repository;

import com.jichi.ragkb.entity.KbDocument;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Query;

import java.util.List;

public interface KbDocumentRepository extends JpaRepository<KbDocument, Long> {

    List<KbDocument> findByKbIdAndIsDeletedFalse(Long kbId);

    @Query("SELECT COUNT(d) FROM KbDocument d WHERE d.status = :status")
    long countByStatus(KbDocument.DocumentStatus status);
}
```

### IndexTaskRepository
```java
package com.jichi.ragkb.repository;

import com.jichi.ragkb.entity.IndexTask;
import org.springframework.data.jpa.repository.JpaRepository;

import java.util.Optional;

public interface IndexTaskRepository extends JpaRepository<IndexTask, Long> {

    Optional<IndexTask> findTopByDocIdOrderByCreatedAtDesc(Long docId);
}
```

### MinioStorageService（简化版占位）
`MinioStorageService` 的完整实现在后面。这里先给出接口签名，让 `IndexService` 能编译通过。

```java
package com.jichi.ragkb.service;

import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.stereotype.Service;

@Service
@RequiredArgsConstructor
@Slf4j
public class MinioStorageService {

    public byte[] download(String objectPath) {
        throw new UnsupportedOperationException("MinIO 完整实现见下一节");
    }

    public void delete(String objectPath) {
        throw new UnsupportedOperationException("MinIO 完整实现见下一节");
    }
}
```

---

## 四、IndexService——索引管道入口
把文档解析、分块、向量化、写库串起来。

鸡哥先说一下这里的整体设计思路。索引管道看起来就是一个流水线——解析 → 分块 → Embedding → 写库，但实际要考虑几个问题：

1. **索引耗时长**（一份大文档可能要十几秒），不能让用户同步等，必须异步
2. **需要状态追踪**——用户上传文档后要能查到"正在索引中"还是"索引完成"或"索引失败"
3. **失败要能重试**——不能因为一次网络问题就丢了用户的文档
4. **重建索引时不能让用户查询"断档"**——旧版本数据要等新版本写完才能删

所以鸡哥设计了 `IndexTask` 这个任务表来追踪每次索引的状态（PENDING → RUNNING → DONE/FAILED），用户可以随时查询进度，管理员也能看到哪些任务失败了需要手动处理。

**为重建索引时的可用性**——`KbDocument` 维护 `version` 字段、`DocChunk` 维护 `docVersion` 字段——重建时**先把版本号 + 1、写入新版本数据、新版本全部写入完成后再删旧版本**。即使新数据写入中途 JVM 挂了，旧版本数据还在，用户查询不受影响，下次重试再走一遍即可，幂等。**这是真正的"先写后删"**——别写成"先删再写"，那是给生产挖坑。

**为重试的稳定性**——失败任务的退避不能用 `Thread.sleep` 占着业务线程池的 slot（多个失败任务同时 sleep 会把整个 `indexTaskExecutor` 阻塞），而是用独立的 `ScheduledExecutorService` 延迟后**通过 taskLauncher 重新投递**，让任务回到 `indexTaskExecutor` 重新跑。同时 `taskType` 字段区分入口——MinIO 任务可以重试、内存里的文本任务无法重试（文本不在持久层、重启就丢）。

先看 `IndexService` 本身，它是整个索引管道的核心，后面再说异步启动的问题。

```java
package com.jichi.ragkb.service;

import com.jichi.ragkb.entity.*;
import com.jichi.ragkb.repository.*;
import com.jichi.ragkb.security.UserContext;
import com.jichi.ragkb.service.loader.ParseResult;
import com.jichi.ragkb.service.splitter.ChunkResult;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import jakarta.annotation.PreDestroy;
import java.io.ByteArrayInputStream;
import java.time.LocalDateTime;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;

@Service
@RequiredArgsConstructor
@Slf4j
public class IndexService {

    /** taskType 字段值——MinIO 下载入口 */
    public static final String TASK_TYPE_FROM_MINIO = "INDEX_FROM_MINIO";
    /** taskType 字段值——直接传文本入口（测试场景，不可自动重试） */
    public static final String TASK_TYPE_FROM_TEXT = "INDEX_FROM_TEXT";

    private final KbDocumentRepository documentRepository;
    private final DocChunkRepository chunkRepository;
    private final IndexTaskRepository taskRepository;
    private final DocumentLoaderService loaderService;
    private final ChunkService chunkService;
    private final EmbeddingService embeddingService;
    private final MinioStorageService minioStorageService;
    private final IndexTaskLauncher taskLauncher;   // 后面会讲为什么要抽出这个类

    /**
     * 重试调度器：单线程、daemon——专用于"延迟到时间后把任务重新投递到 indexTaskExecutor"。
     * 不直接 Thread.sleep 在业务线程里——避免占用 indexTaskExecutor 的 slot，
     * 高并发失败时 sleep 会让索引线程池整体阻塞。
     */
    private final ScheduledExecutorService retryScheduler =
            Executors.newSingleThreadScheduledExecutor(r -> {
                Thread t = new Thread(r, "index-retry-scheduler");
                t.setDaemon(true);
                return t;
            });

    @PreDestroy
    void shutdownRetryScheduler() {
        retryScheduler.shutdown();
    }

    /**
     * 提交索引任务（支持直接传入文本，测试时跳过 MinIO）。
     */
    public void submitIndexTask(Long docId, String textContent) {
        IndexTask task = new IndexTask();
        task.setDocId(docId);
        task.setTaskType(TASK_TYPE_FROM_TEXT);   // ★ 区分入口——scheduleRetry 据此跳过文本任务
        taskRepository.save(task);

        // 捕获当前用户上下文，传递给异步线程（ThreadLocal 不能跨线程）
        taskLauncher.launchWithText(task.getId(), docId, textContent,
                UserContext.getUserId(), UserContext.getDepartmentId(), UserContext.getRole());
    }

    /**
     * 提交索引任务（生产模式，从 MinIO 读取文件）。
     */
    public void submitIndexTask(Long docId) {
        IndexTask task = new IndexTask();
        task.setDocId(docId);
        task.setTaskType(TASK_TYPE_FROM_MINIO);  // ★ 区分入口——scheduleRetry 才能正确地走 MinIO 路径
        taskRepository.save(task);

        taskLauncher.launchFromMinio(task.getId(), docId,
                UserContext.getUserId(), UserContext.getDepartmentId(), UserContext.getRole());
    }

    /**
     * 从 MinIO 读取文件并执行索引（由 IndexTaskLauncher 异步调用）。
     *
     * 分阶段 try-catch 的理由：
     *   → 文档不存在：是数据被并发删除的脏请求，重试也没用，直接 markFailed 终止
     *   → MinIO 下载失败：通常是网络抖动 / 对象不存在，值得重试 → retryIfPossible
     *   → 解析/索引失败：可能是文件本身格式坏、也可能是依赖服务抖动，先重试，重试上限到了再放弃
     * 旧版本统一 catch + 写死 "从MinIO读取文件失败"——错误归因错乱、且文档不存在时会变孤儿任务。
     */
    public void executeFromMinio(Long taskId, Long docId) {
        KbDocument doc;
        // 阶段 1：取文档元数据——失败说明 docId 已被删，不重试
        try {
            doc = documentRepository.findById(docId)
                    .orElseThrow(() -> new RuntimeException("文档不存在：docId=" + docId));
        } catch (Exception e) {
            markFailed(taskId, docId, e.getMessage());
            return;   // ★ 不进入 retryIfPossible——重试也找不到这条记录
        }

        // 阶段 2：从 MinIO 下载文件——网络/IO 失败，值得重试
        byte[] fileBytes;
        try {
            fileBytes = minioStorageService.download(doc.getMinioPath());
        } catch (Exception e) {
            markFailed(taskId, docId, "从 MinIO 读取文件失败：" + e.getMessage());
            retryIfPossible(taskId, docId);
            return;
        }

        // 阶段 3：解析 + 索引——失败可能是文件格式坏，也可能是 Embedding/DB 抖动，先重试
        try {
            ParseResult parseResult = loaderService.load(
                    new ByteArrayInputStream(fileBytes), doc.getFileName());
            doIndex(taskId, docId, doc, parseResult);
            // doIndex 内部自带 try-catch，失败时已 markFailed + retry，这里 catch 兜底极端情况
        } catch (Exception e) {
            markFailed(taskId, docId, "文档解析或索引失败：" + e.getMessage());
            retryIfPossible(taskId, docId);
        }
    }

    /**
     * 执行索引（直接使用文本内容，由 IndexTaskLauncher 异步调用）。
     * 注意：文本任务失败后**不能自动重试**——文本只在内存里，重启就丢，scheduleRetry 会跳过这种 task。
     */
    public void executeWithText(Long taskId, Long docId, String textContent) {
        KbDocument doc;
        try {
            doc = documentRepository.findById(docId)
                    .orElseThrow(() -> new RuntimeException("文档不存在：docId=" + docId));
        } catch (Exception e) {
            markFailed(taskId, docId, e.getMessage());
            return;
        }
        ParseResult parseResult = ParseResult.builder()
                .success(true)
                .pages(List.of(ParseResult.PageContent.builder()
                        .pageNum(1)
                        .text(textContent)
                        .build()))
                .totalPages(1)
                .build();
        doIndex(taskId, docId, doc, parseResult);
    }

    /**
     * 核心索引逻辑：解析 → 分块 → Embedding → 写新版本 → 删旧版本 → 更新状态。
     *
     * 设计思路（关键）：**新版本写入完成后，才删除旧版本**——真正的"先写后删"。
     *   每次 doIndex 都把 doc.version + 1，新写入的 chunk 用新版本号；
     *   只有 batchInsertChunks 全部完成，才会 deleteByDocIdAndDocVersionLessThan(newVersion)。
     *   即使新数据写入中途 JVM 挂掉，旧版本数据还在，用户查询不受影响，
     *   下一次重试又会再把 version+1 重新走一遍，幂等。
     *
     * 历史版本曾经"先删旧再写新"——一旦写新挂掉，旧的也没了，用户查询直接空——已废弃。
     */
    private void doIndex(Long taskId, Long docId, KbDocument doc, ParseResult parseResult) {
        updateTaskStatus(taskId, IndexTask.TaskStatus.RUNNING);
        updateDocStatus(docId, KbDocument.DocumentStatus.PROCESSING);

        try {
            if (!parseResult.isSuccess()) {
                throw new RuntimeException("文档解析失败：" + parseResult.getErrorMsg());
            }

            // 第一步：分块
            List<ChunkResult> chunks = chunkService.chunk(parseResult);
            if (chunks.isEmpty()) {
                throw new RuntimeException("分块结果为空，文档可能无有效文本内容");
            }
            log.info("[IndexService] docId={}，分块完成，共{}块", docId, chunks.size());

            // 第二步：批量 Embedding
            List<String> texts = chunks.stream().map(ChunkResult::getContent).toList();
            List<float[]> embeddings = embeddingService.embedBatch(texts);

            // ★ 第三步：递增版本号——下面写入用新版本号；旧 chunk 保持旧版本号不动
            //   先 save 让"读 doc.version 的并发查询"能看到新版本，
            //   但旧版本 chunk 还在，查询时 ORDER BY docVersion DESC 取最新即可。
            //
            //   ⚠️ 这里 save 之前必须显式 setStatus(PROCESSING)——doc 参数对象是更早 findById 拿的，
            //     内存里的 status 还是 PENDING；而 updateDocStatus 是用另一个对象 save 的。
            //     如果不显式 set，这次 save 会把 doc.status=PENDING 写回 DB，覆盖掉 PROCESSING。
            int newVersion = (doc.getVersion() == null ? 1 : doc.getVersion() + 1);
            doc.setVersion(newVersion);
            doc.setStatus(KbDocument.DocumentStatus.PROCESSING);  // ★ 保持 PROCESSING，不被覆盖
            documentRepository.save(doc);

            // 第四步：批量写入新版本数据
            List<DocChunk> docChunks = new ArrayList<>();
            int totalTokens = 0;
            for (int i = 0; i < chunks.size(); i++) {
                ChunkResult chunk = chunks.get(i);
                DocChunk docChunk = new DocChunk();
                docChunk.setDocId(docId);
                docChunk.setKbId(doc.getKbId());
                docChunk.setChunkIndex(chunk.getChunkIndex());
                docChunk.setContent(chunk.getContent());
                docChunk.setEmbedding(embeddings.get(i));
                docChunk.setPageNum(chunk.getPageNum());
                docChunk.setSectionTitle(chunk.getSectionTitle());
                docChunk.setTokenCount(chunk.getEstimatedTokens());
                docChunk.setDocVersion(newVersion);   // ★ 用新版本号
                docChunks.add(docChunk);
                totalTokens += chunk.getEstimatedTokens();
            }

            batchInsertChunks(docChunks);

            // ★ 第五步：新数据写入完成后，才删除旧版本——真正的"先写后删"
            //   即使这一步挂了，旧版本 chunk 也只是没删干净，下次重建会再清，
            //   不会出现"旧的没了、新的没全"的窗口。
            chunkRepository.deleteByDocIdAndDocVersionLessThan(docId, newVersion);

            // 第六步：更新文档状态
            doc.setStatus(KbDocument.DocumentStatus.DONE);
            doc.setChunkCount(chunks.size());
            doc.setTokenCount(totalTokens);
            doc.setIndexedAt(LocalDateTime.now());
            documentRepository.save(doc);

            updateTaskStatus(taskId, IndexTask.TaskStatus.DONE);

            log.info("[IndexService] 索引完成：docId={}，version={}，chunks={}，tokens={}",
                    docId, newVersion, chunks.size(), totalTokens);

        } catch (Exception e) {
            log.error("[IndexService] 索引失败：docId={}，error={}", docId, e.getMessage(), e);
            markFailed(taskId, docId, e.getMessage());
            retryIfPossible(taskId, docId);
        }
    }

    /**
     * 分批写入，每批 50 条。
     * 为什么不直接 saveAll 一把梭？因为一份大文档可能有几百个 chunk，
     * 单次 INSERT 几百行对数据库的压力很大（长事务 + 大量 WAL 日志），
     * 分批写可以减少单次事务大小，也方便观察写入进度。
     */
    private void batchInsertChunks(List<DocChunk> chunks) {
        int batchSize = 50;
        for (int i = 0; i < chunks.size(); i += batchSize) {
            List<DocChunk> batch = chunks.subList(i, Math.min(i + batchSize, chunks.size()));
            chunkRepository.saveAll(batch);
            log.debug("[IndexService] 写入批次 {}/{}",
                    i / batchSize + 1, (chunks.size() + batchSize - 1) / batchSize);
        }
    }

    private void markFailed(Long taskId, Long docId, String errorMsg) {
        IndexTask task = taskRepository.findById(taskId).orElseThrow();
        task.setStatus(IndexTask.TaskStatus.FAILED);
        task.setErrorMsg(errorMsg);
        task.setFinishedAt(LocalDateTime.now());
        taskRepository.save(task);

        documentRepository.findById(docId).ifPresent(doc -> {
            doc.setStatus(KbDocument.DocumentStatus.FAILED);
            doc.setErrorMsg(errorMsg);
            documentRepository.save(doc);
        });
    }

    private void retryIfPossible(Long taskId, Long docId) {
        IndexTask task = taskRepository.findById(taskId).orElseThrow();
        if (task.canRetry()) {
            task.setRetryCount(task.getRetryCount() + 1);
            task.setStatus(IndexTask.TaskStatus.PENDING);
            taskRepository.save(task);
            log.info("[IndexService] 任务将重试：taskId={}，retryCount={}", taskId, task.getRetryCount());
            // 延迟重试（指数退避：1s, 2s, 4s）
            scheduleRetry(taskId, docId, task.getRetryCount());
        }
    }

    /**
     * 延迟重试——指数退避（1s → 2s → 4s …）。
     *
     * 关键设计：
     *   1. 用独立的 retryScheduler 延迟、**不在 indexTaskExecutor 线程里 Thread.sleep**——
     *      否则多个失败任务并发 sleep 会把索引线程池整体阻塞。
     *   2. 时间到了通过 taskLauncher 把任务**重新投递到 indexTaskExecutor**——
     *      走 @Async 代理，UserContext 在新线程里重新 set。
     *   3. 根据 task.taskType **正确选择重投入口**——
     *      MinIO 任务走 launchFromMinio，文本任务（文本只在内存里）直接放弃自动重试。
     */
    protected void scheduleRetry(Long taskId, Long docId, int retryCount) {
        IndexTask task = taskRepository.findById(taskId).orElse(null);
        if (task == null) {
            log.warn("[IndexService] 重试时找不到 task：taskId={}", taskId);
            return;
        }

        // ★ 文本任务无法自动重试——文本只在内存里，无法持久化
        if (TASK_TYPE_FROM_TEXT.equals(task.getTaskType())) {
            log.warn("[IndexService] 文本任务（taskType={}）不支持自动重试：taskId={}",
                    task.getTaskType(), taskId);
            return;
        }

        // 在业务线程里**先捕获** UserContext——延迟回调时 ThreadLocal 已被清空
        Long userId = UserContext.getUserId();
        String departmentId = UserContext.getDepartmentId();
        String role = UserContext.getRole();
        long delaySeconds = (long) Math.pow(2, retryCount - 1);

        retryScheduler.schedule(() -> {
            try {
                // ★ 通过 taskLauncher 重新投递——走 @Async 代理 → indexTaskExecutor 起新线程
                //   不要直接 executeFromMinio()，否则会跑在 retryScheduler 的单线程上
                taskLauncher.launchFromMinio(taskId, docId, userId, departmentId, role);
            } catch (Exception e) {
                log.error("[IndexService] 重投递任务失败：taskId={}, err={}", taskId, e.getMessage(), e);
            }
        }, delaySeconds, TimeUnit.SECONDS);
    }

    private void updateTaskStatus(Long taskId, IndexTask.TaskStatus status) {
        taskRepository.findById(taskId).ifPresent(t -> {
            t.setStatus(status);
            if (status == IndexTask.TaskStatus.RUNNING) t.setStartedAt(LocalDateTime.now());
            if (status == IndexTask.TaskStatus.DONE)    t.setFinishedAt(LocalDateTime.now());
            taskRepository.save(t);
        });
    }

    private void updateDocStatus(Long docId, KbDocument.DocumentStatus status) {
        documentRepository.findById(docId).ifPresent(d -> {
            d.setStatus(status);
            documentRepository.save(d);
        });
    }
}
```

### IndexTaskLauncher——解决 @Async 自调问题
<img src="https://cdn.nlark.com/yuque/0/2026/png/29413969/1776758832018-99139066-ae12-4137-a935-d103227bc011.png" width="1120" title="" crop="0,0,1,1" id="u998c9bdf" class="ne-image">

看完 `IndexService`，大家可能注意到了它里面注入了一个 `IndexTaskLauncher`，`submitIndexTask` 方法不是自己直接调 `executeFromMinio`，而是通过 `taskLauncher` 来调。为什么要绕这一层？

这是 Spring 的一个经典坑：`@Async`** 基于 AOP 代理实现，Bean 内部自调不走代理，异步就不生效。** 也就是说如果你在 `IndexService` 里直接写 `this.executeFromMinio(...)`，这个方法会同步执行，用户上传文档就要卡在那里等十几秒。鸡哥面试的时候也经常问这个问题，能答上来的不多。

解决方案有几种：

<img src="https://cdn.nlark.com/yuque/0/2026/png/29413969/1776758840903-37378440-673e-4017-877e-50278dd592b0.png" width="1113" title="" crop="0,0,1,1" id="uc6a8e21e" class="ne-image">

+ 注入自身（`@Lazy private IndexService self`）→ 能用但有循环依赖风险，代码也别扭
+ 用 `ApplicationContext.getBean()` 拿代理对象 → 太丑了
+ **抽出独立的 Launcher Bean** → 职责最清晰，Launcher 只管"在异步线程里启动"，IndexService 只管"怎么执行"

鸡哥选第三种：

```java
package com.jichi.ragkb.service;

import com.jichi.ragkb.security.UserContext;
import org.springframework.context.annotation.Lazy;
import org.springframework.scheduling.annotation.Async;
import org.springframework.stereotype.Component;

@Component
public class IndexTaskLauncher {

    private final IndexService indexService;

    public IndexTaskLauncher(@Lazy IndexService indexService) {
        this.indexService = indexService;
    }

    @Async("indexTaskExecutor")
    public void launchFromMinio(Long taskId, Long docId,
                                Long userId, String departmentId, String role) {
        UserContext.set(userId, departmentId, role);
        try {
            indexService.executeFromMinio(taskId, docId);
        } finally {
            UserContext.clear();
        }
    }

    @Async("indexTaskExecutor")
    public void launchWithText(Long taskId, Long docId, String textContent,
                               Long userId, String departmentId, String role) {
        UserContext.set(userId, departmentId, role);
        try {
            indexService.executeWithText(taskId, docId, textContent);
        } finally {
            UserContext.clear();
        }
    }
}
```

注意这里用了 `@Lazy` 注入 `IndexService`。因为 `IndexService` 注入了 `IndexTaskLauncher`，`IndexTaskLauncher` 又注入了 `IndexService`，形成循环依赖。`@Lazy` 让 Spring 在构造 `IndexTaskLauncher` 时先注入一个代理对象，等到真正调用 `indexService` 的方法时才去拿真实 Bean，打破了启动阶段的循环。这里不能用 `@RequiredArgsConstructor`，因为 `@Lazy` 要标注在构造方法参数上，Lombok 生成的构造器无法加这个注解。

代码很简单，就是个"代理人"角色。`@Async("indexTaskExecutor")` 指定了使用自定义的索引线程池（而不是 Spring 默认的 `SimpleAsyncTaskExecutor`，那个每次都 new 线程，生产环境千万别用）。

---

## 五、DocChunkRepository——含向量查询方法
这里鸡哥要重点说一下两个查询方法的设计思路，因为这俩是后面检索环节的核心。

<img src="https://cdn.nlark.com/yuque/0/2026/png/29413969/1776758858467-0d58e02a-7761-4eee-a0cd-d75ecfd4f56f.png" width="1113" title="" crop="0,0,1,1" id="u61947923" class="ne-image">

**为什么要同时做向量检索和全文检索？**

单靠向量检索有个天然弱点——语义相近但表述不同的内容能找到，但精确关键词匹配反而可能漏掉。比如用户问"Java 21 的虚拟线程怎么用"，向量检索可能会召回一堆"并发编程"相关的内容，但不一定能精确命中包含"虚拟线程"这个关键词的 chunk。全文检索刚好补这个短板。后面的检索服务会用 **RRF（Reciprocal Rank Fusion）** 把两路结果融合，取长补短。

**为什么 SELECT 里不返回相似度分数，而是只排序？**

<img src="https://cdn.nlark.com/yuque/0/2026/png/29413969/1776758871843-fb8454e3-3272-4234-b474-950aab58744e.png" width="1107" title="" crop="0,0,1,1" id="u1541d849" class="ne-image">

鸡哥一开始也想在 SQL 里算 `1 - (embedding <=> CAST(:embedding AS vector)) AS score` 然后直接返回分数。但 Hibernate 6.x 对 native query 的结果集映射非常严格——你的 Entity 里没有 `score` 这个字段，它就直接报错。有几种绕法（用 `@SqlResultSetMapping`、用 Tuple 接收），但都很丑。鸡哥最后选择了最简单的方案：**SQL 只负责排序和截断，分数交给上层用 RRF 按排名计算**。排名本身就是相关性的体现，而且 RRF 融合用的也是排名而不是原始分数，所以这里不返回分数完全不影响最终效果。

```java
package com.jichi.ragkb.repository;

import com.jichi.ragkb.entity.DocChunk;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Modifying;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;
import org.springframework.transaction.annotation.Transactional;

import java.util.List;

public interface DocChunkRepository extends JpaRepository<DocChunk, Long> {

    /** 删除文档的旧版本分块（重建索引时使用） */
    @Modifying
    @Transactional
    @Query("DELETE FROM DocChunk c WHERE c.docId = :docId AND c.docVersion < :version")
    void deleteByDocIdAndDocVersionLessThan(@Param("docId") Long docId,
                                             @Param("version") Integer version);

    /**
     * 向量相似度检索（余弦相似度）。
     * 使用 PGVector 的 <=> 操作符（余弦距离），ORDER BY 距离升序即按相似度降序。
     *
     * 关键过滤条件：
     *   1. c.doc_version = d.version —— 只取每个文档的"当前版本" chunk
     *      重建索引时 doIndex 是"先写新版本、再删旧版本"，中间窗口里 DB 同时存在新旧版本 chunk。
     *      用 doc_version = d.version 自动屏蔽旧版本，让用户查询不会命中"半新半旧"的混合结果。
     *   2. d.is_deleted = FALSE —— 软删除的文档不参与召回
     *      避免"已删除文档"的 chunk 成为幽灵数据。
     *
     * 注意：不能在 SELECT 中直接计算 score 并返回，Hibernate 6.x 严格映射会因
     * 结果集多出 score 列而报错。排序放在 ORDER BY，分数由调用方用 RRF 融合处理。
     */
    @Query(value = """
            SELECT c.*
            FROM kb_doc_chunk c
            JOIN kb_document  d ON c.doc_id = d.id
            WHERE c.kb_id = :kbId
              AND c.doc_version = d.version
              AND d.is_deleted = FALSE
            ORDER BY c.embedding <=> CAST(:embedding AS vector)
            LIMIT :topK
            """, nativeQuery = true)
    List<DocChunk> findByVectorSimilarity(
            @Param("kbId") Long kbId,
            @Param("embedding") String embedding,   // PGVector 格式字符串：[0.1,0.2,...]
            @Param("topK") int topK);

    /**
     * 全文检索（PostgreSQL 全文搜索）。
     * 使用 to_tsquery('simple', :query) 匹配 content_tsv。
     * simple 配置不进行词干化，适合中文分词后的关键词检索。
     *
     * 过滤条件同 findByVectorSimilarity：
     *   1. c.doc_version = d.version —— 只召回当前版本 chunk
     *   2. d.is_deleted = FALSE —— 过滤软删除文档
     *
     * 注意：不能在 SELECT 中包含 ts_rank(...) AS score，原因同上。
     * 全文检索的排序意义在于确保最相关结果在前，实际分数在 RRF 融合阶段按排名计算。
     */
    @Query(value = """
            SELECT c.*
            FROM kb_doc_chunk c
            JOIN kb_document  d ON c.doc_id = d.id
            WHERE c.kb_id = :kbId
              AND c.doc_version = d.version
              AND d.is_deleted = FALSE
              AND c.content_tsv @@ to_tsquery('simple', :tsQuery)
            ORDER BY ts_rank(c.content_tsv, to_tsquery('simple', :tsQuery)) DESC
            LIMIT :topK
            """, nativeQuery = true)
    List<DocChunk> findByFullTextSearch(
            @Param("kbId") Long kbId,
            @Param("tsQuery") String tsQuery,       // 例如："技术 & 规范 & 接口"
            @Param("topK") int topK);

    /** 按文档 ID 查询所有分块 */
    List<DocChunk> findByDocId(Long docId);

    /** 按文档 ID 删除所有分块（删除文档时清理） */
    @Modifying
    @Transactional
    @Query("DELETE FROM DocChunk c WHERE c.docId = :docId")
    void deleteByDocId(@Param("docId") Long docId);

    /** 按 ID 列表批量查询（引用溯源时使用） */
    @Query("SELECT c FROM DocChunk c WHERE c.id IN :ids")
    List<DocChunk> findByIds(@Param("ids") List<Long> ids);
}
```

---

## 六、把测试数据跑起来
<img src="https://cdn.nlark.com/yuque/0/2026/png/29413969/1776758881410-743f6b2b-4790-4ff1-afd7-97d77e269023.png" width="1115" title="" crop="0,0,1,1" id="u9e46af91" class="ne-image">

到这里 `IndexService`、`EmbeddingService`、`DocChunkRepository` 都有了，可以把之前准备的测试文档自动插入并索引了。

### DataInitializer——开发环境自动初始化
鸡哥说一下为什么要写这个初始化器，以及里面几个设计考量：

**为什么不用 **`data.sql`** 或 Flyway 来初始化测试数据？**

因为索引流程不只是往数据库插几行数据，它涉及文档解析、分块、调 Embedding API、写入向量——这些逻辑用纯 SQL 搞不定。所以必须用代码走一遍完整的索引管道。

**为什么加 **`@Profile("dev")`**？**

生产环境绝对不能自动插入测试数据，这个 `@Profile` 就是保险丝。只有启动时指定 `--spring.profiles.active=dev` 才会执行。

**为什么检查 **`documentRepository.count() > 0`** 就跳过？**

`ApplicationRunner` 每次启动都会跑。如果不做这个判断，每次重启都会重复插入测试文档。用 count 做幂等检查，跑过一次就不再跑了。

```java
package com.jichi.ragkb.config;

import com.jichi.ragkb.entity.KbDocument;
import com.jichi.ragkb.repository.KbDocumentRepository;
import com.jichi.ragkb.service.IndexService;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.boot.ApplicationArguments;
import org.springframework.boot.ApplicationRunner;
import org.springframework.context.annotation.Profile;
import org.springframework.core.io.ClassPathResource;
import org.springframework.stereotype.Component;

import java.io.IOException;
import java.nio.charset.StandardCharsets;

/**
 * 开发环境数据初始化器，仅在 dev profile 下执行。
 * 把放在 src/test/resources/test-docs/ 下的样本文档插入数据库并触发索引。
 */
@Component
@Profile("dev")
@RequiredArgsConstructor
@Slf4j
public class DataInitializer implements ApplicationRunner {

    private final KbDocumentRepository documentRepository;
    private final IndexService indexService;

    @Override
    public void run(ApplicationArguments args) throws Exception {
        if (documentRepository.count() > 0) {
            log.info("[DataInit] 已有文档数据，跳过初始化");
            return;
        }

        log.info("[DataInit] 开始初始化测试文档...");

        initDocument(1L, "hr-handbook.txt", "employee-handbook.txt",
                "TXT", 1L, "test-docs/hr-handbook.txt");
        initDocument(2L, "tech-spec.txt", "tech-specification.txt",
                "TXT", 2L, "test-docs/tech-spec.txt");
        initDocument(3L, "product-faq.txt", "product-faq.txt",
                "TXT", 3L, "test-docs/product-faq.txt");

        log.info("[DataInit] 测试文档初始化完成，等待异步索引...");
    }

    private void initDocument(Long kbId, String minioPath, String fileName,
                               String fileType, Long uploadedBy,
                               String classpath) throws IOException {
        ClassPathResource resource = new ClassPathResource(classpath);
        byte[] content = resource.getInputStream().readAllBytes();

        KbDocument doc = new KbDocument();
        doc.setKbId(kbId);
        doc.setFileName(fileName);
        doc.setFileType(fileType);
        doc.setFileSize((long) content.length);
        doc.setMinioPath(minioPath);
        doc.setUploadedBy(uploadedBy);
        KbDocument saved = documentRepository.save(doc);

        String text = new String(content, StandardCharsets.UTF_8);
        indexService.submitIndexTask(saved.getId(), text);

        log.info("[DataInit] 文档已提交索引：id={}, fileName={}", saved.getId(), fileName);
    }
}
```

启动时加上 `--spring.profiles.active=dev`，控制台会打印索引日志。索引是异步的，所以会看到应用启动完成后，后台线程还在打印 Embedding 和写库的日志，这是正常的。

### 验证测试数据
```java
import org.awaitility.Awaitility;
import org.springframework.test.context.ActiveProfiles;

import java.time.Duration;

@SpringBootTest
@ActiveProfiles("dev")     // ★ 坑 1：必须激活 dev 才会跑 DataInitializer
class DataInitTest {

    @Autowired
    private KbDocumentRepository documentRepository;

    @Autowired
    private DocChunkRepository chunkRepository;

    @Test
    void verifyTestDataLoaded() {
        // ★ 坑 2：异步任务可能没跑完——用 Awaitility 轮询等待
        //   最多等 60s（要给 Embedding API + DB 写入留出时间）；
        //   每 2s 查一次，命中目标就立即返回。
        Awaitility.await()
                .atMost(Duration.ofSeconds(60))
                .pollInterval(Duration.ofSeconds(2))
                .untilAsserted(() -> {
                    long docCount = documentRepository
                            .countByStatus(KbDocument.DocumentStatus.DONE);
                    assertThat(docCount).isGreaterThanOrEqualTo(3);
                });

        long docCount = documentRepository.countByStatus(KbDocument.DocumentStatus.DONE);
        long chunkCount = chunkRepository.count();

        assertThat(chunkCount).isGreaterThan(10);

        System.out.printf("已索引文档数：%d，分块总数：%d%n", docCount, chunkCount);
    }
}
```

<img src="https://cdn.nlark.com/yuque/0/2026/png/29413969/1776758893576-179120c4-70d4-4f29-ae31-957a1ed60a58.png" width="1111" title="" crop="0,0,1,1" id="u4f22a17a" class="ne-image">
