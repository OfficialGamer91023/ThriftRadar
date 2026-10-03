-- Applied only to the demo DB (python -m app.migrate --demo). Spec: DESIGN.md §2.4.
ALTER TABLE posts ADD CONSTRAINT demo_sources_only CHECK (source IN ('demo_seed', 'demo_upload'));
ALTER TABLE posts ADD CONSTRAINT demo_no_jid CHECK (sender_jid IS NULL);
UPDATE db_meta SET value = 'demo' WHERE key = 'role';
