-- Bootstrap script: Run this ONCE manually to create the database and user
-- This cannot be run via dbmate as it creates the database dbmate connects to
--
-- Connect to postgres as superuser first, then run:

CREATE USER nexus_admin WITH PASSWORD '<doppler>';
CREATE DATABASE nexus;
ALTER DATABASE nexus OWNER TO nexus_admin;

GRANT ALL PRIVILEGES ON DATABASE nexus TO nexus_admin;

-- Connect to the nexus database, then:
-- \c nexus
CREATE EXTENSION IF NOT EXISTS vector;
