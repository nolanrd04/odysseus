#!/bin/zsh
# Odysseus Stop Script
# Run this to stop Odysseus

set -e

echo "🛑 Stopping Odysseus..."

# Stop all containers
docker compose down

echo "✅ Odysseus has been stopped."