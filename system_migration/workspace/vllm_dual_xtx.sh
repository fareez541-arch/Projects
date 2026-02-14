#!/bin/bash
# VLLM Launcher for Dual 7900 XTX (GFX1100)
# Usage: ./vllm_dual_xtx.sh [model_name]

MODEL=${1:-"meta-llama/Llama-2-7b-hf"}

# Source environment
source ../hardware_env.sh

# RCCL Debug (disable in production)
# export RCCL_DEBUG=INFO
# export HSAKMT_DEBUG_LEVEL=3

echo "Launching VLLM with RCCL dual-GPU support..."
echo "Model: $MODEL"
echo "Tensor Parallel: 2 (dual GPU)"

python -m vllm.entrypoints.openai.api_server \
  --model $MODEL \
  --tensor-parallel-size 2 \
  --device cuda \
  --dtype float16 \
  --max-model-len 4096 \
  --gpu-memory-utilization 0.85
