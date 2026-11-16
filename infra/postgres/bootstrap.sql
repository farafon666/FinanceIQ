-- Run with psql as the administrator, connected to the dedicated financeiq DB.
-- Roles are cluster-wide. This script never changes existing passwords.
-- Assign new passwords separately with psql's interactive \password command.
\set ON_ERROR_STOP on

BEGIN;

DO $$
DECLARE
    role_name text;
BEGIN
    IF current_database() <> 'financeiq' THEN
        RAISE EXCEPTION 'Bootstrap requires the dedicated financeiq database';
    END IF;
    IF current_user IN ('financeiq_migrator', 'financeiq_runtime') THEN
        RAISE EXCEPTION 'Bootstrap requires a separate administrator';
    END IF;
    IF (SELECT pg_get_userbyid(datdba) FROM pg_database
        WHERE datname = current_database()) <> current_user THEN
        RAISE EXCEPTION 'Bootstrap requires the database owner';
    END IF;
    FOREACH role_name IN ARRAY ARRAY['financeiq_migrator', 'financeiq_runtime']
    LOOP
        IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = role_name) THEN
            EXECUTE format(
                'CREATE ROLE %I LOGIN NOINHERIT NOSUPERUSER NOCREATEDB '
                'NOCREATEROLE NOREPLICATION NOBYPASSRLS', role_name
            );
        END IF;
        IF EXISTS (
            SELECT FROM pg_roles WHERE rolname = role_name AND (
                NOT rolcanlogin OR rolinherit OR rolsuper OR rolcreatedb
                OR rolcreaterole OR rolreplication OR rolbypassrls
            )
        ) OR EXISTS (
            SELECT FROM pg_auth_members
            WHERE member = (SELECT oid FROM pg_roles WHERE rolname = role_name)
        ) THEN
            RAISE EXCEPTION 'Existing application role has incompatible privileges';
        END IF;
    END LOOP;
    IF EXISTS (
        SELECT FROM pg_namespace
        WHERE nspname NOT LIKE 'pg_%' AND nspname <> 'information_schema'
        AND nspowner = (SELECT oid FROM pg_roles WHERE rolname = 'financeiq_runtime')
    ) OR EXISTS (
        SELECT FROM pg_class
        WHERE relowner = (SELECT oid FROM pg_roles WHERE rolname = 'financeiq_runtime')
    ) OR EXISTS (
        SELECT FROM pg_proc
        WHERE proowner = (SELECT oid FROM pg_roles WHERE rolname = 'financeiq_runtime')
    ) OR EXISTS (
        SELECT FROM pg_type
        WHERE typowner = (SELECT oid FROM pg_roles WHERE rolname = 'financeiq_runtime')
    ) THEN
        RAISE EXCEPTION 'Runtime role must not own database objects';
    END IF;
    IF has_parameter_privilege('financeiq_runtime', 'session_replication_role', 'SET')
    OR EXISTS (
        SELECT FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
        AND has_table_privilege('financeiq_runtime', c.oid, 'TRUNCATE,TRIGGER,REFERENCES')
    ) OR EXISTS (
        SELECT FROM pg_default_acl d, aclexplode(d.defaclacl) a
        WHERE d.defaclrole = (SELECT oid FROM pg_roles WHERE rolname='financeiq_migrator')
        AND d.defaclobjtype = 'r'
        AND a.grantee IN (0, (SELECT oid FROM pg_roles WHERE rolname='financeiq_runtime'))
    ) THEN
        RAISE EXCEPTION 'Existing runtime grants or default table privileges are incompatible';
    END IF;
    IF NOT EXISTS (
        SELECT FROM pg_namespace n JOIN pg_roles r ON r.oid = n.nspowner
        WHERE n.nspname = 'public'
        AND r.rolname IN ('pg_database_owner', current_user, 'financeiq_migrator')
    ) THEN
        RAISE EXCEPTION 'Existing public schema has an incompatible owner';
    END IF;
END
$$;

REVOKE ALL ON DATABASE financeiq FROM PUBLIC;
REVOKE ALL ON DATABASE financeiq FROM financeiq_runtime, financeiq_migrator;
GRANT CONNECT ON DATABASE financeiq TO financeiq_runtime, financeiq_migrator;

ALTER SCHEMA public OWNER TO financeiq_migrator;
REVOKE ALL ON SCHEMA public FROM PUBLIC, financeiq_runtime;
GRANT USAGE ON SCHEMA public TO financeiq_runtime;
ALTER ROLE financeiq_runtime IN DATABASE financeiq SET search_path = public;
ALTER ROLE financeiq_migrator IN DATABASE financeiq SET search_path = public;

-- Global within this database: a per-schema REVOKE cannot undo PUBLIC EXECUTE.
ALTER DEFAULT PRIVILEGES FOR ROLE financeiq_migrator
    REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;

COMMIT;
