-- 为已有数据库中的 hr002 演示账号授予 HR 知识库管理员权限。
INSERT INTO kb_permission (kb_id, subject_type, subject_id, permission, granted_by)
VALUES (1, 'USER', '4', 'ADMIN', 3)
ON CONFLICT (kb_id, subject_type, subject_id)
DO UPDATE SET permission = EXCLUDED.permission, granted_by = EXCLUDED.granted_by;
