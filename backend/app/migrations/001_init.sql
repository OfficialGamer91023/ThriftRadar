-- ThriftRadar schema v1. Spec: docs/DESIGN.md §2.4 and Appendix A.
-- The "Writer" column there names the only function allowed to write each field.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE db_meta (
    key   text PRIMARY KEY,
    value text NOT NULL
);
INSERT INTO db_meta (key, value) VALUES ('role', 'local');

CREATE FUNCTION set_updated_at() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END $$;

-- posts: the queue
CREATE TABLE posts (
    id               bigserial PRIMARY KEY,
    source           text NOT NULL
                     CHECK (source IN ('whatsapp', 'chat_export', 'demo_seed', 'demo_upload')),
    idempotency_key  text NOT NULL UNIQUE,
    chat_ref         text NOT NULL,
    sender_ref       text NOT NULL,
    sender_jid       text NULL,
    first_msg_at     timestamptz NOT NULL,
    last_msg_at      timestamptz NOT NULL,
    priority         smallint NOT NULL DEFAULT 0,
    vlm_policy       text NOT NULL DEFAULT 'auto' CHECK (vlm_policy IN ('auto', 'never')),
    status           text NOT NULL DEFAULT 'received'
                     CHECK (status IN ('received', 'processing', 'awaiting_vlm', 'done', 'failed')),
    stage            text NULL CHECK (stage IN ('local', 'vlm')),
    outcome          text NULL CHECK (outcome IN ('listed', 'repost', 'no_shoe', 'mixed')),
    claim_token      uuid NULL,
    claimed_at       timestamptz NULL,
    lease_expires_at timestamptz NULL,
    attempts         int NOT NULL DEFAULT 0,
    vlm_attempts     int NOT NULL DEFAULT 0,
    next_attempt_at  timestamptz NOT NULL DEFAULT now(),
    last_error       text NULL CHECK (char_length(last_error) <= 500),
    owner_session    text NULL,
    seed_attrs       jsonb NULL,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    CHECK (last_msg_at >= first_msg_at),
    CHECK (status <> 'processing' OR (claim_token IS NOT NULL AND lease_expires_at IS NOT NULL AND stage IS NOT NULL))
);
CREATE TRIGGER posts_updated_at BEFORE UPDATE ON posts
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE INDEX posts_ready_idx ON posts (priority, created_at)
    WHERE status IN ('received', 'awaiting_vlm');
CREATE INDEX posts_lease_idx ON posts (lease_expires_at) WHERE status = 'processing';
CREATE INDEX posts_sender_idx ON posts (sender_ref, last_msg_at DESC);
CREATE INDEX posts_owner_idx ON posts (owner_session) WHERE owner_session IS NOT NULL;

CREATE TABLE images (
    id          bigserial PRIMARY KEY,
    post_id     bigint NOT NULL REFERENCES posts (id) ON DELETE CASCADE,
    seq         int NOT NULL,
    sha256      bytea NOT NULL CHECK (length(sha256) = 32),
    phash       bigint NOT NULL,
    width       int NOT NULL,
    height      int NOT NULL,
    bytes       int NOT NULL,
    segment_idx int NULL,
    detections  jsonb NULL,
    primary_box int[] NULL,
    embedding   vector(768) NULL,
    ocr_text    text NULL,
    ocr_ran     boolean NOT NULL DEFAULT false,
    UNIQUE (post_id, seq)
);
CREATE INDEX images_sha_idx ON images (sha256);
CREATE INDEX images_phash_idx ON images (phash);

CREATE TABLE post_messages (
    id            bigserial PRIMARY KEY,
    post_id       bigint NOT NULL REFERENCES posts (id) ON DELETE CASCADE,
    chat_ref      text NOT NULL,
    msg_key       text NOT NULL,
    seq           int NOT NULL,
    kind          text NOT NULL CHECK (kind IN ('image', 'text')),
    sent_at       timestamptz NOT NULL,
    caption       text NULL CHECK (char_length(caption) <= 4096),
    image_id      bigint NULL REFERENCES images (id) ON DELETE SET NULL,
    missing_media boolean NOT NULL DEFAULT false,
    reject_reason text NULL
                  CHECK (reject_reason IN ('corrupt', 'too_large', 'bad_type', 'download_failed')),
    UNIQUE (chat_ref, msg_key),
    UNIQUE (post_id, seq)
);
CREATE INDEX post_messages_post_idx ON post_messages (post_id);

CREATE TABLE listings (
    id               bigserial PRIMARY KEY,
    post_id          bigint NOT NULL REFERENCES posts (id) ON DELETE CASCADE,
    segment_idx      int NOT NULL,
    item_idx         int NOT NULL,
    source           text NOT NULL,
    owner_session    text NULL,
    sender_ref       text NOT NULL,
    brand            text NULL,
    model            text NULL,
    colour           text NULL,
    condition        text NULL,
    gender           text NULL,
    size_label       text NULL,
    size_eu          numeric(4, 1) NULL,
    size_approx      boolean NOT NULL DEFAULT false,
    price_amount     int NULL,
    currency         text NULL,
    price_on_request boolean NOT NULL DEFAULT false,
    attr_sources     jsonb NOT NULL DEFAULT '{}',
    extraction       text NOT NULL CHECK (extraction IN ('local', 'vlm', 'vlm_failed', 'seed')),
    vlm_missing      text[] NULL,
    status           text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'sold', 'withdrawn')),
    cover_image_id   bigint NULL REFERENCES images (id) ON DELETE SET NULL,
    embedding        vector(768) NOT NULL,
    first_seen_at    timestamptz NOT NULL,
    last_seen_at     timestamptz NOT NULL,
    repost_count     int NOT NULL DEFAULT 0,
    prompt_version   text NULL,
    match_checked_at timestamptz NULL,
    UNIQUE (post_id, segment_idx, item_idx)
);
CREATE INDEX listings_emb_hnsw ON listings USING hnsw (embedding vector_cosine_ops);
CREATE INDEX listings_size_idx ON listings (size_eu);
CREATE INDEX listings_price_idx ON listings (price_amount);
CREATE INDEX listings_brand_idx ON listings (lower(brand));
CREATE INDEX listings_last_seen_idx ON listings (last_seen_at DESC);
CREATE INDEX listings_sender_idx ON listings (sender_ref, last_seen_at DESC);
CREATE INDEX listings_owner_idx ON listings (owner_session) WHERE owner_session IS NOT NULL;

CREATE TABLE listing_sightings (
    id           bigserial PRIMARY KEY,
    listing_id   bigint NOT NULL REFERENCES listings (id) ON DELETE CASCADE,
    post_id      bigint NOT NULL REFERENCES posts (id) ON DELETE CASCADE,
    segment_idx  int NOT NULL,
    seen_at      timestamptz NOT NULL,
    price_amount int NULL,
    match_kind   text NOT NULL CHECK (match_kind IN ('origin', 'phash', 'embedding')),
    UNIQUE (post_id, segment_idx)
);
CREATE INDEX listing_sightings_listing_idx ON listing_sightings (listing_id);

CREATE TABLE vlm_calls (
    id             bigserial PRIMARY KEY,
    post_id        bigint NOT NULL REFERENCES posts (id) ON DELETE CASCADE,
    segment_idx    int NOT NULL,
    attempt        int NOT NULL,
    provider       text NOT NULL,
    model          text NOT NULL,
    prompt_version text NOT NULL,
    day            date NOT NULL,
    status         text NOT NULL DEFAULT 'reserved'
                   CHECK (status IN ('reserved', 'ok', 'bad_json', 'http_error', 'timeout', 'net_error')),
    http_status    int NULL,
    input_tokens   int NULL,
    output_tokens  int NULL,
    cost_usd       numeric(10, 6) NULL,
    latency_ms     int NULL,
    response       jsonb NULL,
    raw_excerpt    text NULL CHECK (char_length(raw_excerpt) <= 2048),
    created_at     timestamptz NOT NULL DEFAULT now(),
    finished_at    timestamptz NULL,
    UNIQUE (post_id, segment_idx, prompt_version, attempt)
);
CREATE INDEX vlm_calls_day_idx ON vlm_calls (day);
CREATE INDEX vlm_calls_post_idx ON vlm_calls (post_id);

CREATE TABLE wishlists (
    id              bigserial PRIMARY KEY,
    owner           text NOT NULL,
    query_text      text NULL,
    ref_image_sha   bytea NULL,
    query_embedding vector(768) NOT NULL,
    size_eu_min     numeric(4, 1) NULL,
    size_eu_max     numeric(4, 1) NULL,
    max_price       int NULL,
    brand           text NULL,
    min_score       real NOT NULL,
    active          boolean NOT NULL DEFAULT true,
    created_at      timestamptz NOT NULL DEFAULT now(),
    CHECK (query_text IS NOT NULL OR ref_image_sha IS NOT NULL)
);
CREATE INDEX wishlists_owner_idx ON wishlists (owner) WHERE active;

CREATE TABLE matches (
    id              bigserial PRIMARY KEY,
    wishlist_id     bigint NOT NULL REFERENCES wishlists (id) ON DELETE CASCADE,
    listing_id      bigint NOT NULL REFERENCES listings (id) ON DELETE CASCADE,
    score           real NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    notified_at     timestamptz NULL,
    notify_attempts int NOT NULL DEFAULT 0,
    UNIQUE (wishlist_id, listing_id)
);

CREATE TABLE listener_status (
    id                int PRIMARY KEY CHECK (id = 1),
    state             text NOT NULL
                      CHECK (state IN ('connecting', 'open', 'reconnecting', 'logged_out',
                                       'replaced', 'bad_session', 'forbidden', 'stopped')),
    detail            text NULL,
    last_heartbeat_at timestamptz NULL,
    last_message_at   timestamptz NULL,
    spool_pending     int NOT NULL DEFAULT 0,
    notified_state    text NULL
);

CREATE TABLE media_blobs (
    sha256     bytea PRIMARY KEY CHECK (length(sha256) = 32),
    data       bytea NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
