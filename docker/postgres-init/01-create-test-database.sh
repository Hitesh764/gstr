#!/bin/sh
# Creates a second, empty database for the test suite.
#
# The suite empties every table before each test. Pointing it at the
# development database would erase real work, so it gets its own, and the
# test harness additionally refuses to run against any database whose name
# does not contain "test".
set -e
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-SQL
    CREATE DATABASE ${POSTGRES_DB}_test OWNER ${POSTGRES_USER};
SQL
echo "Created database ${POSTGRES_DB}_test for the test suite."
