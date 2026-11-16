#!/usr/bin/env bash
# Runs only for an empty PGDATA. Explicit manual execution also repairs passwords.
# A subshell keeps options and password variables out of the image entrypoint.
(
    set +x
    set -euo pipefail

    fail() {
        printf '%s\n' 'FinanceIQ database initialization failed.' >&2
        exit 1
    }

    psql -X --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
        --file /opt/financeiq/bootstrap.sql >/dev/null 2>&1 || fail

    for role in migrator runtime; do
        password=$(cat "/run/secrets/${role}_password") || fail
        [[ -n "$password" && "$password" != *$'\n'* && "$password" != *$'\r'* ]] || fail
        # psql encrypts the password before sending ALTER ROLE. Passwords go
        # through stdin, never argv, generated SQL, shell tracing or logs.
        printf '%s\n%s\n' "$password" "$password" | \
            psql -X --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
                --set ON_ERROR_STOP=1 --command "\password financeiq_${role}" \
                >/dev/null 2>&1 || fail
        unset password
    done
)
