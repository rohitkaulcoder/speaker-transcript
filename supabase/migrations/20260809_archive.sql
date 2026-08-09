-- Speaker Transcript archive: one row per transcribed source.

create table if not exists transcripts (
  id              uuid primary key default gen_random_uuid(),
  -- Stable dedupe key: youtube -> video id, upload -> file hash
  external_key    text not null unique,
  source_type     text not null check (source_type in ('youtube', 'upload')),
  source_url      text,
  title           text,
  creator         text,
  duration_seconds integer,
  language        text,
  utterances      jsonb not null default '[]',
  speaker_mapping jsonb,
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now()
);

-- Allow upsert by external_key (re-transcribe refreshes the row).
create index if not exists transcripts_created_at_idx
  on transcripts (created_at desc);

-- RLS: the app writes via the service_role key (bypasses RLS). Policies are
-- added explicitly below so future non-service access is controlled.
alter table transcripts enable row level security;

create policy "service_role_all"
  on transcripts for all
  using (true)
  with check (true);
