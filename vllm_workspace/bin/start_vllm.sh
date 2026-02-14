#!/bin/bash
#===============================================================================
# vLLM Startup Script for Dual AMD Navi 31 (gfx1100) - ROCm 6.2
# REQUIRES Python 3.11 (Python 3.12 is NOT supported due to type annotation issues)
# CRITICAL: Builds vLLM from source for ROCm (PyPI wheels are CUDA-only!)
#===============================================================================

# Source the P2P configuration file first (for CONDA_ENV_NAME and other vars)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/../config/p2p_config.env"

# Activate Environment first (all subsequent checks must run in the correct env)
source "$HOME/miniforge3/etc/profile.d/conda.sh"
conda activate "$CONDA_ENV_NAME" || { 
    echo "ERROR: Failed to activate conda environment: $CONDA_ENV_NAME"
    exit 1 
}

# Check Python version - MUST BE 3.11
PYTHON_VER=$(python -c 'import sys; print("{}.{}".format(sys.version_info.major, sys.version_info.minor))')

if [[ "$PYTHON_VER" != "3.11" ]]; then
    echo "ERROR: Python $PYTHON_VER detected. vLLM 0.6.3 requires Python 3.11."
    echo ""
    echo "Fix with:"
    echo "  conda install python=3.11"
    echo ""
    echo "Then restart this script."
    exit 1
fi

echo "✓ Python 3.11 confirmed"

# Safety Check: Detect CUDA packages in ROCm environment
if pip list 2>/dev/null | grep -q nvidia; then
    echo "ERROR: NVIDIA/CUDA packages detected in environment!"
    echo "These are incompatible with AMD GPUs."
    echo "Fix with: pip install torch==2.5.1 torchvision==0.20.1 torchaudio==2.5.1 --index-url https://download.pytorch.org/whl/rocm6.2 --force-reinstall"
    exit 1
fi

# Safety Check: Detect xformers (pulls CUDA torch)
if pip list 2>/dev/null | grep -iq xformers; then
    echo "ERROR: xformers detected in environment!"
    echo "xformers is NOT supported on ROCm and will pull CUDA dependencies."
    echo "Remove with: pip uninstall xformers -y"
    echo "Then reinstall ROCm torch: pip install torch==2.5.1 torchvision==0.20.1 torchaudio==2.5.1 --index-url https://download.pytorch.org/whl/rocm6.2 --force-reinstall"
    exit 1
fi

# Verify ROCm PyTorch is installed
python -c "
import torch
if not hasattr(torch.version, 'hip') or torch.version.hip is None:
    print('ERROR: PyTorch CUDA version detected. Need ROCm version.')
    print('Install with: pip install torch==2.5.1 torchvision==0.20.1 torchaudio==2.5.1 --index-url https://download.pytorch.org/whl/rocm6.2 --force-reinstall')
    exit(1)
" || exit 1

echo "✓ ROCm PyTorch confirmed"

# Function to check if vLLM is installed and built for ROCm (not CUDA)
check_vllm_rocm() {
    python -c "
import vllm
import torch
# Check if vLLM is importable and PyTorch is ROCm version
try:
    # Try to import vLLM C extensions - if built for CUDA, this will fail on ROCm
    from vllm import _core_C
    # Check if we can get vLLM version
    print(f'vLLM version: {vllm.__version__}')
    exit(0)
except ImportError as e:
    if 'libc10_cuda' in str(e) or 'CUDA' in str(e):
        print('ERROR: vLLM is compiled for CUDA, not ROCm')
        exit(1)
    else:
        # Other import error
        print(f'ERROR: vLLM import failed: {e}')
        exit(2)
" 2>&1
    return $?
}

# Function to build vLLM from source for ROCm
build_vllm_from_source() {
    echo "=========================================="
    echo "Building vLLM from source for ROCm 6.2..."
    echo "This will take 10-15 minutes."
    echo "=========================================="
    
    # Install build dependencies
    pip install cmake ninja packaging wheel setuptools-scm -q
    
    # Clean up any previous build attempts
    rm -rf /tmp/vllm_build
    
    # Clone vLLM 0.6.3
    git clone --branch v0.6.3 --depth 1 https://github.com/vllm-project/vllm.git /tmp/vllm_build
    
    cd /tmp/vllm_build
    
    # Set ROCm build flags
    export VLLM_TARGET_DEVICE=rocm
    export PYTORCH_ROCM_ARCH=gfx1100
    export ROCM_HOME=/opt/rocm-6.2
    
    echo "Starting build process..."
    python setup.py install 2>&1 | tee /tmp/vllm_build.log
    
    if [ $? -ne 0 ]; then
        echo "ERROR: vLLM build failed. Check /tmp/vllm_build.log"
        cd ~
        rm -rf /tmp/vllm_build
        exit 1
    fi
    
    echo "Build completed successfully!"
    cd ~
    rm -rf /tmp/vllm_build
}

# Check if vLLM is installed and built for ROCm
echo "Checking vLLM installation..."
VLLM_CHECK=$(check_vllm_rocm)
VLLM_STATUS=$?

if [ $VLLM_STATUS -eq 1 ]; then
    echo "$VLLM_CHECK"
    echo "vLLM is compiled for CUDA. Rebuilding from source for ROCm..."
    build_vllm_from_source
elif [ $VLLM_STATUS -ne 0 ]; then
    echo "vLLM not found or import error. Building from source..."
    build_vllm_from_source
else
    echo "$VLLM_CHECK"
fi

# Final verification
echo "Verifying vLLM installation..."
python -c "import vllm; print(f'vLLM {vllm.__version__} loaded successfully')" || { 
    echo "ERROR: vLLM import failed after build."
    exit 1 
}

# Force reload of environment variables for this session
unset HSA_ENABLE_SDMA
export HSA_ENABLE_SDMA=1
export HSA_FORCE_FINE_GRAIN_PCIE=1
export RCCL_ENABLE_DIRECT_GPU_PEER=1
export RCCL_SOCKET_IFNAME=lo
export NCCL_DEBUG=INFO
export RCCL_DEBUG=INFO

echo "Environment configured for P2P:"
env | grep -E "(HSA|RCCL|NCCL)" | sort

# GPU Power/Cleanup
sudo rocm-smi --setpoweroverdrive 350 -d 0
sudo rocm-smi --setpoweroverdrive 350 -d 1
sudo rocm-smi --setperflevel high
fuser -k 8000/tcp > /dev/null 2>&1

# Start vLLM with tensor parallelism
echo "Firing up vLLM... let's rock!"
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
