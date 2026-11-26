-- Runs once, on first container start (empty data volume).
-- Creates a separate database for automated tests so tests never touch dev data.
CREATE DATABASE documind_test;
\connect documind_test
CREATE EXTENSION IF NOT EXISTS vector;
\connect documind
CREATE EXTENSION IF NOT EXISTS vector;
