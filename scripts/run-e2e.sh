#!/bin/bash
# 跑生产 e2e（容器内）
cd /opt/shared-memory-mcp
PYTHONPATH=/opt/shared-memory-mcp/src timeout 120 ./venv/bin/python e2e.py 2>&1 | grep -vE "INFO:|^\s*$"
