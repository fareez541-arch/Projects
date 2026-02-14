#!/bin/bash

#===============================================================================
# vLLM Launch Script for Dual Navi 31 (gfx1100)
# ROCm 6.2 Compatibility Configuration - PHASE B FIXES APPLIED
#===============================================================================

# Check Python version before proceeding
PYTHON_VER=$(python -c 'import sys; print("{}.{}".format(sys.version_info.major, sys.version_info.minor))')
if [[ "$PYTHON_VER" == "3.12" ]]; then
    echo "ERROR: Python 3.12 detected. vLLM 0.6.3 requires Python 3.11."
    echo "Please run: conda install python=3.11"
    echo ""
    echo "Then reinstall PyTorch ROCm with:"
    echo "pip install torch==2.5.1 torchvision==0.20.1 torchaudio==2.5.1 --index-url https://download.pytorch.org/whl/rocm6.2 --force-reinstall"
    exit 1
fi

# Verify ROCm PyTorch is installed before checking vLLM
python -c "
import torch
if not hasattr(torch.version, 'hip') or torch.version.hip is None:
    print('ERROR: PyTorch is not the ROCm version.')
    print('Install with: pip install torch==2.5.1 torchvision==0.20.1 torchaudio==2.5.1 --index-url https://download.pytorch.org/whl/rocm6.2 --force-reinstall')
    exit(1)
" || exit 1

# Check if vLLM is installed
if ! python -c "import vllm" 2>/dev/null; then
    echo "ERROR: vLLM is not installed."
    echo "Install with: pip install vllm==0.6.3 --extra-index-url https://download.pytorch.org/whl/rocm6.2"
    exit 1
fi

# ROCm Environment
export ROCM_HOME=/opt/rocm-6.2
export PATH=$ROCM_HOME/bin:$PATH
export LD_LIBRARY_PATH=$ROCM_HOME/lib:$LD_LIBRARY_PATH

# GPU Topology
export HIP_VISIBLE_DEVICES=0,1
export CUDA_VISIBLE_DEVICES=0,1
export GPU_NUM_DEVICES=2

# vLLM Core Settings
export VLLM_USE_TRITON_AWQ=1
export VLLM_TARGET_DEVICE=rocm

# Distributed Execution - CRITICAL FIX: spawn required for ROCm 6.2 fork-safety
export VLLM_WORKER_MULTIPROC_METHOD=spawn

# Memory Allocator Configuration - CRITICAL FIX: expandable segments for 48GB VRAM
export PYTORCH_HIP_ALLOC_CONF=expandable_segments:True,max_split_size_mb=512

# Performance Tuning - CRITICAL FIX: Fine-grain required for P2P on RDNA3
export HSA_FORCE_FINE_GRAIN_PCIE=1

# RCCL P2P Configuration for PCIe Switch Topology - CRITICAL FIX: RCCL overrides for ROCm 6.2
export RCCL_ENABLE_DIRECT_GPU_COMMUNICATION=1
export RCCL_CROSS_P2P=1
export RCCL_P2P_LEVEL=SYS

# Model Configuration - FIXED: Updated to correct model path
MODEL_PATH="/home/fareez541/vllm_workspace/models/DeepSeek-R1-Distill-32B-AWQ"
TP_SIZE=2

# Launch vLLM Server
python -m vllm.entrypoints.openai.api_server \
  --model $MODEL_PATH \
  --tensor-parallel-size $TP_SIZE \
  --distributed-executor-backend mp \
  --quantization awq \
  --enforce-eager \
  --port 8000
