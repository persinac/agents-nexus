-- 002_embedding_768.sql
-- Resize embedding column to match Ollama nomic-embed-text (768 dims).
-- Previous: vector(1536) for OpenAI text-embedding-3-small.
-- Idempotent: safe to re-run.
--
-- Run with:
--   psql $DATABASE_URL -f migrations/002_embedding_768.sql

ALTER TABLE agents.memory_nodes
    ALTER COLUMN embedding TYPE vector(768);
