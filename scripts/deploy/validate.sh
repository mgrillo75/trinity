#!/bin/bash

set -e

cd "$(dirname "$0")/../.."

echo "====================================="
echo "Trinity Platform Validation"
echo "====================================="
echo ""

echo "1. Checking directory structure..."
required_dirs=("docker" "src" "config" "scripts")
for dir in "${required_dirs[@]}"; do
    if [ -d "$dir" ]; then
        echo "   ✅ $dir/"
    else
        echo "   ❌ $dir/ missing"
        exit 1
    fi
done

echo ""
echo "2. Checking Docker Compose configuration..."
if docker compose config --quiet 2>&1 | grep -q "error\|Error"; then
    echo "   ❌ Docker Compose configuration has errors"
    exit 1
else
    echo "   ✅ Docker Compose configuration valid"
fi

echo ""
echo "3. Checking required files..."
required_files=(
    "docker-compose.yml"
    ".env.example"
    "src/backend/main.py"
    "docker/base-image/Dockerfile"
    "scripts/deploy/start.sh"
    "scripts/deploy/build-base-image.sh"
)

for file in "${required_files[@]}"; do
    if [ -f "$file" ]; then
        echo "   ✅ $file"
    else
        echo "   ❌ $file missing"
        exit 1
    fi
done

echo ""
echo "4. Checking security configurations..."
security_count=$(grep -c "security_opt\|cap_drop\|cap_add" docker-compose.yml)
if [ $security_count -gt 10 ]; then
    echo "   ✅ Security configurations present ($security_count entries)"
else
    echo "   ❌ Security configurations missing"
    exit 1
fi

echo ""
echo "5. Checking executable permissions..."
for script in scripts/deploy/*.sh scripts/management/*.sh; do
    if [ -x "$script" ]; then
        echo "   ✅ $script"
    else
        echo "   ⚠️  $script not executable (fixing...)"
        chmod +x "$script"
    fi
done

echo ""
echo "====================================="
echo "Trinity Platform Validation: PASSED ✅"
echo "====================================="
echo ""
echo "Platform is ready for deployment!"
echo ""
echo "Next steps:"
echo "  1. Copy .env.example to .env and configure"
echo "  2. Run ./scripts/deploy/build-base-image.sh"
echo "  3. Run ./scripts/deploy/start.sh"
echo "  4. Access web UI at http://localhost"
echo ""

