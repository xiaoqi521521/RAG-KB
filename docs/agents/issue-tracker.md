# Issue Tracker：本地 Markdown

本仓库的 Issues 和 Specs（也可称为 PRD）使用 Markdown 文件管理，统一存放在 `.scratch/` 下。

## 约定

- 每个功能使用一个独立目录：`.scratch/<feature-slug>/`
- Spec 文件固定为：`.scratch/<feature-slug>/spec.md`
- 实施 Tickets 按一个 Ticket 一个文件存放：`.scratch/<feature-slug>/issues/<NN>-<slug>.md`
- Ticket 从 `01` 开始编号，禁止把全部 Tickets 合并到单个文件
- 每个 Issue 文件顶部附近使用 `Status:` 行记录 Triage 状态，具体状态值参见 `triage-labels.md`
- 评论和讨论历史追加到文件底部的 `## Comments` 标题下

## 当技能要求“发布到 Issue Tracker”时

在 `.scratch/<feature-slug>/` 下创建新文件；目标目录不存在时一并创建。

## 当技能要求“读取相关 Ticket”时

读取用户指定路径或编号对应的文件。通常用户会直接提供文件路径或 Issue 编号。

## Wayfinder 操作约定

以下约定供 `/wayfinder` 使用。一个 Map 对应多个子 Ticket 文件。

- **Map**：`.scratch/<effort>/map.md`，用于记录 Notes、Decisions-so-far 和 Fog
- **子 Ticket**：`.scratch/<effort>/issues/NN-<slug>.md`，从 `01` 开始编号，正文记录待解决问题
- **Ticket 类型**：文件顶部使用 `Type:` 记录，允许值为 `research`、`prototype`、`grilling`、`task`
- **Ticket 状态**：文件顶部使用 `Status:` 记录，允许值为 `claimed`、`resolved`
- **阻塞关系**：文件顶部使用 `Blocked by: NN, NN` 记录；列出的 Ticket 全部为 `resolved` 后，当前 Ticket 才解除阻塞
- **Frontier**：扫描 `.scratch/<effort>/issues/`，选择状态开放、未阻塞且未被领取的文件，编号较小者优先
- **领取**：开始工作前把 `Status:` 更新为 `claimed` 并保存
- **解决**：在 `## Answer` 标题下追加答案，把 `Status:` 更新为 `resolved`，然后在 `map.md` 的 Decisions-so-far 中追加结论摘要和链接
