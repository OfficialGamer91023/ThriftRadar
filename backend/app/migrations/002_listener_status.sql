-- Build step 11 (DESIGN §4.3 listener heartbeat): the listener reports 'awaiting_qr' while waiting to be
-- paired, and sends its drop/accept counters, which the status page shows.
ALTER TABLE listener_status DROP CONSTRAINT listener_status_state_check;
ALTER TABLE listener_status ADD CONSTRAINT listener_status_state_check
    CHECK (state IN ('connecting', 'awaiting_qr', 'open', 'reconnecting', 'logged_out',
                     'replaced', 'bad_session', 'forbidden', 'stopped'));
ALTER TABLE listener_status ADD COLUMN counts jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE listener_status ADD COLUMN version text NULL;
