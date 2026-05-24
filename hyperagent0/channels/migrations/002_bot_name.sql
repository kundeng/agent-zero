-- 002_bot_name.sql — spec 09 D3: extend thread_map composite key with bot_name.
--
-- Two bots on the same platform (e.g. two Slack workspaces or two bots in
-- the same workspace) can share a chat_id, so the (channel_type, chat_id)
-- pair is no longer globally unique. We add bot_name and rebuild the PK.
--
-- SQLite can't ALTER TABLE to change a PRIMARY KEY, so the migration
-- creates a new table, copies rows in, drops the old table, and renames.
-- Existing rows get bot_name = '_legacy'; the router maps those onto
-- whichever bot the operator names '_legacy' in channels.json, or the
-- first bot in the platform's list as a fallback (see ChannelRouter).
--
-- The migrator wraps the whole script in BEGIN/COMMIT so a crash partway
-- through leaves the prior state intact.

CREATE TABLE thread_map_v2 (
    channel_type TEXT NOT NULL,
    bot_name     TEXT NOT NULL DEFAULT '_legacy',
    chat_id      TEXT NOT NULL,
    context_id   TEXT NOT NULL,
    project_name TEXT,
    last_active  REAL NOT NULL,
    PRIMARY KEY (channel_type, bot_name, chat_id)
);

INSERT INTO thread_map_v2(channel_type, bot_name, chat_id, context_id, project_name, last_active)
SELECT channel_type, '_legacy', chat_id, context_id, project_name, last_active
FROM thread_map;

DROP TABLE thread_map;
ALTER TABLE thread_map_v2 RENAME TO thread_map;

CREATE INDEX IF NOT EXISTS idx_thread_context
    ON thread_map(context_id);
