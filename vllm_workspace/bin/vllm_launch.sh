#!/bin/bash

#===============================================================================
# vLLM Launch Script for Dual Navi 31 (gfx1100)
# ROCm 6.2 Compatibility Configuration - PHASE B FIXES APPLIED
#===============================================================================

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
export PYTORCH_HIP_ALLOC_CONF=expandable_segments:True,max_split_size_mb:512

# Performance Tuning - CRITICAL FIX: Disabled fine-grain due to RDNA3 perf regression
# export HSA_FORCE_FINE_GRAIN_PCIE=1  # DISABLED: Causes perf regression on RDNA3

# RCCL P2P Configuration for PCIe Switch Topology - CRITICAL FIX: RCCL overrides for ROCm 6.2
export RCCL_ENABLE_DIRECT_GPU_COMMUNICATION=1
export RCCL_CROSS_P2P=1
export RCCL_P2P_LEVEL=SYS

# Model Configuration
MODEL_PATH="/mnt/models/llm-awq"
TP_SIZE=2

# Launch vLLM Server
python -m vllm.entrypoints.openai.api_server \
  --model $MODEL_PATH \
  --tensor-parallel-size $TP_SIZE \
  --distributed-executor-backend mp \
  --quantization awq \
  --enforce-eager \  # Required for ROCm 6.2 AWQ + Tensor Parallel
  --port 8000
