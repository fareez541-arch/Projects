#!/bin/bash
# ROCm/GFX1100 Dual GPU Environment for VLLM
# Usage: source ./hardware_env.sh

# --- GFX1100 Dual 7900 XTX Configuration ---
export HSA_OVERRIDE_GFX_VERSION=11.0.0
export HIP_VISIBLE_DEVICES=0,1
export ROCM_MAX_VRAM_PER_GPU=28000

# --- RCCL (AMD NCCL equivalent) Multi-GPU ---
export RCCL_ENABLE_CLIQUE=1                    # Enable clique-based P2P for dual GPU
export RCCL_MNNCL=1                          # Enable multi-node/multi-GPU optimizations
export NCCL_P2P_LEVEL=SYS                    # Force P2P over PCIe (required for 7900 XTX dGPU setup)
export NCCL_P2P_DISABLE=0                    # Ensure P2P is enabled (VLLM uses NCCL env vars for RCCL)

# --- PyTorch/ROCm Memory Management ---
export PYTORCH_HIP_ALLOC_CONF=expandable_segments:True,max_split_size_mb:512
export HSA_ENABLE_SDMA=0                       # Disable SDMA for stability on dual GFX1100
export AMD_SERIALIZE_KERNEL=3                  # Serialization for dual GPU sync stability
export TORCH_BLAS_PREFER_HIPBLASLT=1           # Use hipBLASLt for performance

# --- VLLM Specific ---
export VLLM_WORKER_MULTIPROC_METHOD=spawn    # Required for ROCm multiprocessing
export CUDA_VISIBLE_DEVICES=${HIP_VISIBLE_DEVICES}  # VLLM compatibility layer mapping

# --- Verification ---
echo "[GFX1100_DUAL_XTX_READY]"
echo "HSA_OVERRIDE_GFX_VERSION: $HSA_OVERRIDE_GFX_VERSION"
echo "HIP_VISIBLE_DEVICES: $HIP_VISIBLE_DEVICES"
echo "RCCL_P2P: ENABLED (PCIe)"
echo "VLLM_ROCM_COMPAT: ACTIVE"
