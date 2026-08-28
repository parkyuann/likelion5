-- READ ONLY. This file contains no DDL or DML.
SELECT table_schema, table_name, column_name, data_type, is_nullable
FROM information_schema.columns
WHERE table_schema = 'public'
  AND table_name IN ('users', 'auth_accounts', 'conversations', 'messages', 'favorites')
ORDER BY table_name, ordinal_position;

SELECT grantee, table_name, privilege_type
FROM information_schema.role_table_grants
WHERE table_schema = 'public'
  AND grantee IN ('application_runtime')
ORDER BY table_name, privilege_type;
