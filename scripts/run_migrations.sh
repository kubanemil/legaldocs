#!/usr/bin/env bash
set -euo pipefail

# Run Alembic database migrations
echo "Running database migrations..."
alembic upgrade head
echo "Migrations complete."
