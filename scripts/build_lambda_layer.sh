#!/usr/bin/env bash
# Builds the Lambda dependency layer zip (build/layer.zip) and the app-code
# zip is handled directly by Terraform's `archive_file` over `src/relay`, so
# this script only needs to produce the layer.
#
# Targets manylinux2014_x86_64 / Python 3.12 explicitly so the layer is
# correct for the Lambda runtime regardless of the machine running this
# script (Apple Silicon, etc.) -- every dependency here ships pure-Python or
# manylinux wheels, so no local compilation is needed.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_DIR="${ROOT_DIR}/build"
LAYER_PYTHON_DIR="${BUILD_DIR}/layer/python"

rm -rf "${LAYER_PYTHON_DIR}"
mkdir -p "${LAYER_PYTHON_DIR}"

python3 -m pip install \
    --platform manylinux2014_x86_64 \
    --implementation cp \
    --python-version 3.12 \
    --only-binary=:all: \
    --target "${LAYER_PYTHON_DIR}" \
    -r "${ROOT_DIR}/requirements-lambda.txt"

echo "Layer dependencies installed to ${LAYER_PYTHON_DIR}"
echo "(terraform's archive_file data source zips this directory itself --"
echo " no manual zip step needed before 'terraform plan'/'terraform apply')"
