#!/bin/bash

#===============================================================================
# Emergency Fix: Remove CUDA packages and reinstall ROCm-compatible versions
#===============================================================================

set -e

echo "=========================================="
echo "EMERGENCY: Fixing CUDA/ROCm Mix-up"
echo "=========================================="

# Check if we're in the right conda environment
if [[ "$CONDA_DEFAULT_ENV" != "ccrn_agent" ]]; then
    echo "ERROR: Not in ccrn_agent environment!"
    echo "Run: conda activate ccrn_agent"
    exit 1
fi

# Detect CUDA packages
echo "Checking for NVIDIA/CUDA packages..."
CUDA_PKGS=$(pip list 2>/dev/null | grep -i nvidia || true)

if [ -n "$CUDA_PKGS" ]; then
    echo "FOUND CUDA PACKAGES (These will break AMD GPUs):"
    echo "$CUDA_PKGS"
    echo ""
    echo "Uninstalling CUDA packages..."
    pip uninstall -y nvidia-cublas-cu12 nvidia-cuda-cupti-cu12 nvidia-cuda-nvrtc-cu12 \
        nvidia-cuda-runtime-cu12 nvidia-cudnn-cu12 nvidia-cufft-cu12 nvidia-curand-cu12 \
        nvidia-cusolver-cu12 nvidia-cusparse-cu12 nvidia-nccl-cu12 nvidia-nvjitlink-cu12 \
        nvidia-nvtx-cu12 2>/dev/null || true
else
    echo "No CUDA packages found - good!"
fi

# Clear pip cache to prevent re-downloading CUDA versions
echo "Purging pip cache..."
pip cache purge

# Check Python version (3.12 has compatibility issues with vLLM 0.6.3)
PYTHON_VERSION=$(python --version 2>&1 | cut -d' ' -f2 | cut -d'.' -f1,2)
if [[ "$PYTHON_VERSION" == "3.12" ]]; then
    echo "WARNING: Python 3.12 detected. vLLM 0.6.3 may have compatibility issues."
    echo "Consider downgrading to Python 3.11:"
    echo "  conda install python=3.11"
fi

# Reinstall PyTorch for ROCm 6.2
echo ""
echo "Reinstalling PyTorch for ROCm 6.2..."
pip install torch==2.5.1 torchvision==0.20.1 torchaudio==2.5.1 \
    --index-url https://download.pytorch.org/whl/rocm6.2 \
    --force-reinstall --no-cache-dir

# Reinstall AutoAWQ without CUDA dependencies
echo ""
echo "Reinstalling AutoAWQ (ROCm compatible)..."
pip uninstall -y autoawq 2>/dev/null || true
pip install git+https://github.com/casper-hansen/AutoAWQ.git@v0.2.5 --no-deps --no-cache-dir

# Reinstall vLLM for ROCm
echo ""
echo "Reinstalling vLLM 0.6.3 for ROCm..."
pip uninstall -y vllm 2>/dev/null || true
pip install vllm==0.6.3 --extra-index-url https://download.pytorch.org/whl/rocm6.2 --no-cache-dir

# Verify installation
echo ""
echo "Verifying installation..."
python -c "
import torch
print(f'PyTorch version: {torch.__version__}')
print(f'ROCm available: {torch.cuda.is_available()}')
if torch.cuda.is_available():
    print(f'GPU: {torch.cuda.get_device_name(0)}')
    print(f'GPU Count: {torch.cuda.device_count()}')
" || echo "WARNING: PyTorch verification failed!"

echo ""
echo "=========================================="
echo "Fix complete! Try running start_vllm.sh again."
echo "=========================================="
