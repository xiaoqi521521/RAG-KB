# 收货地址管理 Spec

## 背景与目标
用户可以维护多个收货地址，支持设置默认地址，下单时自动使用默认地址。

## In Scope
- 地址 CRUD（新增、修改、删除、查询列表）
- 设置默认地址
- 获取用户默认地址（供下单模块调用）

## Out of Scope
- 地址验证（不调用第三方地址 API 验证真实性）
- 地址导入/导出
- 管理员操作用户地址

---

## 接口定义

### 1. 获取地址列表
GET /api/user/addresses

响应 200：
[
  {
    "id": 1,
    "name": "张三",
    "phone": "13800138000",
    "province": "广东省",
    "city": "深圳市",
    "district": "南山区",
    "detail": "科技园南路XX号",
    "isDefault": true
  }
]
// 按 isDefault DESC, createdAt DESC 排序
// 不分页，一次返回全部（最多10条）

### 2. 新增地址
POST /api/user/addresses

请求体：
{
  "name": "张三",        // 必填，1-20字符
  "phone": "13800138000", // 必填，11位手机号
  "province": "广东省",   // 必填，1-20字符
  "city": "深圳市",       // 必填，1-20字符
  "district": "南山区",   // 必填，1-20字符
  "detail": "科技园...",  // 必填，1-100字符
  "isDefault": false      // 选填，默认 false
}

响应 201：新增的地址完整信息

错误：
- 400：参数校验失败
- 422 ADDRESS_LIMIT_EXCEEDED：地址数量已达上限（10个）

业务规则：
- isDefault=true 时，自动将同一用户其他地址设为 isDefault=false
- 用户第一个地址自动设为默认地址（无论 isDefault 传 true 还是 false）

### 3. 修改地址
PUT /api/user/addresses/{id}

请求体：同新增（所有字段选填）
响应 200：更新后的完整地址信息

错误：
- 400：参数校验失败
- 404：地址不存在
- 403：地址不属于当前用户

规则：isDefault=true 时，其他地址同步设为 false

### 4. 删除地址
DELETE /api/user/addresses/{id}

响应 204 No Content

错误：
- 404：地址不存在
- 403：地址不属于当前用户

规则：
- 硬删除（直接从数据库删除）
- 删除的是默认地址时：如果还有其他地址，自动将最新一条设为默认

### 5. 设置默认地址
PUT /api/user/addresses/{id}/default

请求体：无
响应 200：{ "id": 1, "isDefault": true }

错误：
- 404：地址不存在
- 403：地址不属于当前用户
- 422 ALREADY_DEFAULT：已经是默认地址（幂等处理，返回 200 即可）

### 6. 获取默认地址（内部接口，供下单使用）
GET /api/user/addresses/default

响应 200：默认地址完整信息
响应 204 No Content：用户没有地址

---

## 数据模型

### 表：user_addresses

| 字段 | 类型 | 约束 | 默认值 | 说明 |
|------|------|------|--------|------|
| id | BIGINT | PK AUTO | - | |
| user_id | BIGINT | NOT NULL | - | 用户 ID |
| name | VARCHAR(20) | NOT NULL | - | 收件人姓名 |
| phone | VARCHAR(11) | NOT NULL | - | 手机号 |
| province | VARCHAR(20) | NOT NULL | - | 省 |
| city | VARCHAR(20) | NOT NULL | - | 市 |
| district | VARCHAR(20) | NOT NULL | - | 区/县 |
| detail | VARCHAR(100) | NOT NULL | - | 详细地址 |
| is_default | TINYINT(1) | NOT NULL | 0 | 是否默认地址 |
| created_at | DATETIME | NOT NULL | NOW() | |
| updated_at | DATETIME | NOT NULL | NOW() ON UPDATE | |

索引：
- INDEX: user_id
- INDEX: (user_id, is_default)（查默认地址）

---

## 验收标准

### 场景一：新增第一个地址
GIVEN 用户无任何地址，isDefault=false
WHEN POST /api/user/addresses
THEN 返回 201，is_default=true（第一个地址自动设为默认）

### 场景二：新增超上限
GIVEN 用户已有 10 个地址
WHEN POST /api/user/addresses
THEN 返回 422，code=ADDRESS_LIMIT_EXCEEDED

### 场景三：设置新默认地址
GIVEN 用户有 3 个地址，id=1 是默认地址
WHEN PUT /api/user/addresses/2/default
THEN 返回 200，id=2 的 is_default=true，id=1 的 is_default=false

### 场景四：删除默认地址
GIVEN 用户有 2 个地址，id=1 是默认地址（createdAt 更早），id=2 非默认
WHEN DELETE /api/user/addresses/1
THEN 返回 204，id=2 的 is_default 自动变为 true

### 场景五：删除别人的地址
GIVEN 地址 id=5 属于用户 A
WHEN 用户 B 发 DELETE /api/user/addresses/5
THEN 返回 403

---

## 技术约束
- 用户身份通过 SecurityContextHolder 获取（不传 userId 参数）
- 不引入新依赖
- 事务注解用 @Transactional，默认隔离级别