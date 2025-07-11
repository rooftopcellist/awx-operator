#!/bin/bash

# PostgreSQL SCRAM-SHA-256 Migration Script
# This script ensures PostgreSQL uses SCRAM-SHA-256 password encryption
# and migrates existing MD5 passwords to SCRAM-SHA-256

set -e

# Environment variables expected:
# POSTGRES_USER - Database username
# POSTGRES_PASSWORD - Database password  
# POSTGRES_DB - Database name
# PGDATA - PostgreSQL data directory

echo "Starting PostgreSQL SCRAM-SHA-256 initialization..."

# Function to wait for PostgreSQL to be ready
wait_for_postgres() {
    local max_attempts=30
    local attempt=1
    
    echo "Waiting for PostgreSQL to be ready..."
    while [ $attempt -le $max_attempts ]; do
        if pg_isready -h localhost -p 5432 -U "$POSTGRES_USER" -d "$POSTGRES_DB" >/dev/null 2>&1; then
            echo "PostgreSQL is ready"
            return 0
        fi
        echo "Attempt $attempt/$max_attempts: PostgreSQL not ready, waiting..."
        sleep 2
        attempt=$((attempt + 1))
    done
    
    echo "ERROR: PostgreSQL failed to become ready after $max_attempts attempts"
    return 1
}

# Function to check current password encryption method
check_password_encryption() {
    local current_method
    current_method=$(PGPASSWORD="$POSTGRES_PASSWORD" psql -h localhost -U "$POSTGRES_USER" -d "$POSTGRES_DB" -t -c "SHOW password_encryption;" 2>/dev/null | xargs)
    echo "Current password_encryption setting: $current_method"
    echo "$current_method"
}

# Function to check if user passwords are using MD5
check_md5_passwords() {
    echo "Checking for MD5 password hashes..."
    local md5_users
    md5_users=$(PGPASSWORD="$POSTGRES_PASSWORD" psql -h localhost -U "$POSTGRES_USER" -d "$POSTGRES_DB" -t -c "
        SELECT rolname 
        FROM pg_authid 
        WHERE rolpassword LIKE 'md5%'
        AND rolname IN ('$POSTGRES_USER');" 2>/dev/null | xargs)
    
    if [ -n "$md5_users" ]; then
        echo "Found users with MD5 passwords: $md5_users"
        return 0
    else
        echo "No MD5 passwords found"
        return 1
    fi
}

# Function to migrate passwords to SCRAM-SHA-256
migrate_passwords() {
    echo "Migrating passwords to SCRAM-SHA-256..."
    
    # Set password encryption to scram-sha-256
    PGPASSWORD="$POSTGRES_PASSWORD" psql -h localhost -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "ALTER SYSTEM SET password_encryption = 'scram-sha-256';"
    
    # Reload configuration
    PGPASSWORD="$POSTGRES_PASSWORD" psql -h localhost -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT pg_reload_conf();"
    
    # Update the main user password to use SCRAM-SHA-256
    echo "Updating password for user: $POSTGRES_USER"
    PGPASSWORD="$POSTGRES_PASSWORD" psql -h localhost -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "ALTER USER \"$POSTGRES_USER\" WITH PASSWORD '$POSTGRES_PASSWORD';"
    
    echo "Password migration completed"
}

# Main execution
main() {
    echo "PostgreSQL SCRAM-SHA-256 Migration Script"
    echo "=========================================="
    echo "User: $POSTGRES_USER"
    echo "Database: $POSTGRES_DB"
    echo "Data Directory: $PGDATA"
    
    # Check if PostgreSQL data directory exists and is initialized
    if [ ! -f "$PGDATA/PG_VERSION" ]; then
        echo "PostgreSQL data directory not initialized, skipping SCRAM migration"
        exit 0
    fi
    
    # Start PostgreSQL temporarily for migration
    echo "Starting PostgreSQL for SCRAM migration..."
    pg_ctl -D "$PGDATA" start
    
    # Wait for PostgreSQL to be ready
    if ! wait_for_postgres; then
        echo "Failed to start PostgreSQL for migration"
        exit 1
    fi
    
    # Check current password encryption method
    current_method=$(check_password_encryption)
    
    # Check if migration is needed
    if [ "$current_method" != "scram-sha-256" ] || check_md5_passwords; then
        echo "Migration needed: current method is $current_method or MD5 passwords found"
        migrate_passwords
    else
        echo "No migration needed: already using scram-sha-256"
    fi
    
    # Stop PostgreSQL
    echo "Stopping PostgreSQL after migration..."
    pg_ctl -D "$PGDATA" stop
    
    echo "SCRAM-SHA-256 migration completed successfully"
}

# Execute main function
main "$@"
