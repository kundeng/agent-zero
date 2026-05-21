-- 001_initial.sql — spec 04 thread mapping (preserved as v1).
--
-- thread_map persists the (channel_type, chat_id) -> AgentContext mapping
-- across daemon restarts. Only the durable mapping lives here; the full
-- chat log stays in the agent's normal persistence layer.
CREATE TABLE IF NOT EXISTS thread_map (
    channel_type TEXT NOT NULL,
    chat_id      TEXT NOT NULL,
    context_id   TEXT NOT NULL,
    project_name TEXT,
    last_active  REAL NOT NULL,
    PRIMARY KEY (channel_type, chat_id)
);

CREATE INDEX IF NOT EXISTS idx_thread_context
    ON thread_map(context_id);
