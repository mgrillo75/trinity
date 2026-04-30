#!/bin/bash

set -e

cd "$(dirname "$0")/../.."

echo "====================================="
echo "Trinity Agent Platform - Starting"
echo "====================================="
echo ""

if [ ! -f .env ]; then
    echo "⚠️  No .env file found. Creating from template..."
    cp .env.example .env
    echo "✅ Created .env file. Please update with your configuration."
    echo ""
fi

# Auto-generate CREDENTIAL_ENCRYPTION_KEY if not set
if grep -qE '^CREDENTIAL_ENCRYPTION_KEY=$' .env 2>/dev/null || ! grep -q 'CREDENTIAL_ENCRYPTION_KEY' .env 2>/dev/null; then
    NEW_KEY=$(openssl rand -hex 32)
    if grep -q 'CREDENTIAL_ENCRYPTION_KEY' .env; then
        sed -i.bak "s/^CREDENTIAL_ENCRYPTION_KEY=$/CREDENTIAL_ENCRYPTION_KEY=${NEW_KEY}/" .env && rm -f .env.bak
    else
        echo "CREDENTIAL_ENCRYPTION_KEY=${NEW_KEY}" >> .env
    fi
    echo "Auto-generated CREDENTIAL_ENCRYPTION_KEY"
fi

# Check base image before starting — without it, agent creation will silently fail
if ! docker images --format "{{.Repository}}:{{.Tag}}" | grep -q "trinity-agent-base:latest"; then
    echo "⚠️  trinity-agent-base:latest not found."
    echo "   Building base agent image first (required for agent creation)..."
    echo ""
    ./scripts/deploy/build-base-image.sh
    echo ""
fi

echo "Starting services..."
docker compose up -d

echo ""
echo "Waiting for services to be ready..."
sleep 5

echo ""
echo "====================================="
echo "Trinity Agent Platform - Ready!"
echo "====================================="
echo ""
# Read FRONTEND_PORT from .env or use default
FRONTEND_PORT=${FRONTEND_PORT:-$(grep -E '^FRONTEND_PORT=' .env 2>/dev/null | cut -d'=' -f2 || echo "80")}
FRONTEND_PORT=${FRONTEND_PORT:-80}

echo "Access points:"
if [ "$FRONTEND_PORT" = "80" ]; then
    echo "  - Web UI:       http://localhost (login: admin/password)"
else
    echo "  - Web UI:       http://localhost:$FRONTEND_PORT (login: admin/password)"
fi
echo "  - Backend API:  http://localhost:8000/docs"
echo "  - MCP Server:   http://localhost:8080/mcp"
echo ""
echo "To view logs:"
echo "  docker compose logs -f"
echo ""
echo "To stop services:"
echo "  docker compose stop"
echo ""
echo "NOTE: Use 'stop' not 'down' — 'down' destroys agent containers."
echo ""

