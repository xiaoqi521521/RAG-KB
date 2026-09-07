-- 全量导入后，将用户消息表的创建时间字段调整到最后。
\i /tmp/reorder_chat_message_columns.sql
