-- Runs before 01_mnemon.sql (alphabetical order in initdb.d).
-- Creates the minions schema so the mnemon migration can use it.
-- The vector extension is also created here and in 01_mnemon.sql (idempotent).
CREATE EXTENSION IF NOT EXISTS vector;
CREATE SCHEMA IF NOT EXISTS minions;
