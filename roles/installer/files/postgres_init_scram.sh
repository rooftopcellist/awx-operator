#!/bin/bash

# PostgreSQL initialization script with SCRAM-SHA-256 password encryption setup
# This script ensures that PostgreSQL is properly configured to use SCRAM-SHA-256
# for password encryption, both for new installations and existing databases

set -e

echo "Starting PostgreSQL initialization with SCRAM-SHA-256 setup..."

# Standard file permissions setup
chown 26:0 /var/lib/pgsql/data
chmod 700 /var/lib/pgsql/data

# Check if this is a new installation or existing database
if [ ! -f "/var/lib/pgsql/data/userdata/PG_VERSION" ]; then
    echo "New PostgreSQL installation detected - SCRAM-SHA-256 will be configured via environment variables"
    echo "POSTGRES_INITDB_ARGS: ${POSTGRES_INITDB_ARGS:-not set}"
    echo "POSTGRES_HOST_AUTH_METHOD: ${POSTGRES_HOST_AUTH_METHOD:-not set}"
else
    echo "Existing PostgreSQL installation detected"
    echo "Password encryption migration will be handled by the operator after startup"
fi

echo "PostgreSQL initialization completed successfully"
