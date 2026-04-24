#!/bin/bash
# build.sh — Build y tag de la imagen apiquickserve/extdb
# Uso: ./build.sh [version]  (default: 1.0.0)

set -e

VERSION=${1:-1.2.0}
IMAGE="apiquickserve/extdb"

echo ">>> Building $IMAGE:$VERSION"
docker build -t "$IMAGE:$VERSION" -t "$IMAGE:latest" .

echo ""
echo ">>> Build exitoso:"
echo "    $IMAGE:$VERSION"
echo "    $IMAGE:latest"
echo ""
echo ">>> Para publicar:"
echo "    docker push $IMAGE:$VERSION"
echo "    docker push $IMAGE:latest"
