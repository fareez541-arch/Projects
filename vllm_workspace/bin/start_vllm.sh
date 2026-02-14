#!/bin/bash

# Force reload of environment variables for this session
unset HSA_ENABLE_SDMA  # Clear the incorrect value
export HSA_ENABLE_SDMA=1
export HSA_FORCE_FINE_GRAIN_PCIE=1
export RCCL_ENABLE_DIRECT_GPU_PEER=1
export RCCL_SOCKET_IFNAME=lo
export NCCL_DEBUG=INFO
export RCCL_DEBUG=INFO

# Source the P2P configuration file
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/../config/p2p_config.env"

# Activate Environment
source "$HOME/miniforge3/etc/profile.d/conda.sh"
conda activate "$CONDA_ENV_NAME"

echo "Environment configured for P2P:"
env | grep -E "(HSA|RCCL|NCCL)" | sort

# GPU Power/Cleanup
sudo rocm-smi --setpoweroverdrive 350 -d 0
sudo rocm-smi --setpoweroverdrive 350 -d 1
sudo rocm-smi --setperflevel high
fuser -k 8000/tcp > /dev/null 2>&1

# Start vLLM with tensor parallelism
# Optimized for DeepSeek R1 Distill 32B AWQ
exec python3 -m vllm.entrypoints.openai.api_server \
    --model "$VLLM_MODEL" \
    --served-model-name DeepSeek-R1-Distill-32B-AWQ \
    --tensor-parallel-size 2 \
    --gpu-memory-utilization "$VLLM_GPU_MEMORY_UTILIZATION" \
    --max-model-len 32768 \
    --dtype float16 \
    --quantization awq \
    --device cuda \
    --port 8000 \
    --host 0.0.0.0 \
    --distributed-executor-backend mp \
    --enforce-eager \
    "$@"
