#!/bin/bash                                                                                
                                                                                           
#===============================================================================           
# vLLM Startup Script for Dual AMD Navi 31 (gfx1100) - ROCm 6.2                            
# Supports Python 3.11 (native) and Python 3.12 (with compatibility patches)               
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
                                                                                           
# Check Python version                                                         
PYTHON_VER=$(python -c 'import sys; print("{}.{}".format(sys.version_info.major, sys.version_info.minor))')
                                                                                           
# Python 3.12 Compatibility: Patch vLLM type annotations                         
if [[ "$PYTHON_VER" == "3.12" ]]; then                                                     
    echo "WARNING: Python 3.12 detected. vLLM 0.6.3 requires Python 3.11."                 
    echo "Attempting to apply compatibility patches for Python 3.12..."                  
    
    # Get vLLM installation path                                               
    VLLM_PATH=$(python -c "import vllm; import os; print(os.path.dirname(vllm.__file__))" 2>/dev/null)
    
    if [[ -n "$VLLM_PATH" && -d "$VLLM_PATH" ]]; then
        # Patch parallel_state.py for list[int] -> List[int]                   
        PARALLEL_STATE_FILE="$VLLM_PATH/distributed/parallel_state.py"
        if [[ -f "$PARALLEL_STATE_FILE" ]]; then
            # Check if file contains the problematic annotation               
            if grep -q "output_shape: list\\[int\\]" "$PARALLEL_STATE_FILE"; then
                echo "Patching parallel_state.py for Python 3.12 compatibility..."
                
                # Create backup if not exists                                
                [[ -f "$PARALLEL_STATE_FILE.bak" ]] || cp "$PARALLEL_STATE_FILE" "$PARALLEL_STATE_FILE.bak"
                
                # Fix the type annotation: list[int] -> List[int]            
                sed -i 's/output_shape: list\\[int\\]/output_shape: List[int]/g' "$PARALLEL_STATE_FILE"
                
                # Add typing import if missing                               
                if ! grep -q "^from typing import.*List" "$PARALLEL_STATE_FILE"; then
                    # Insert after existing imports or at top                    
                    sed -i '/^from __future__/a from typing import List' "$PARALLEL_STATE_FILE" 2>/dev/null || \
                    sed -i '1i from typing import List' "$PARALLEL_STATE_FILE"
                fi
                
                echo "Parallel state patched successfully."
            fi
        fi
        
        # Patch any other files with list[int] annotations that might cause issues
        find "$VLLM_PATH" -name "*.py" -type f -exec grep -l "list\\[int\\]" {} \; | while read -r file; do
            # Skip if already backed up (already patched)                      
            [[ -f "$file.bak" ]] && continue
            
            # Only patch files that are likely to be registered as custom ops
            if grep -q "direct_register_custom_op\|torch.library\|@custom_op" "$file" 2>/dev/null; then
                echo "Patching $(basename "$file") for Python 3.12 compatibility..."
                cp "$file" "$file.bak"
                sed -i 's/\\blist\\[int\\]\\b/List[int]/g' "$file"
                # Ensure typing.List is imported                                 
                if ! grep -q "^from typing import.*List" "$file"; then
                    sed -i '1i from typing import List' "$file"
                fi
            fi
        done
        
        echo "Python 3.12 compatibility patches applied."
        echo "NOTE: For permanent fix, downgrade to Python 3.11: conda install python=3.11"
        echo ""
    fi
fi                                                                                         
                                                                                           
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
                                                                                           
# Check if vLLM is installed                                                               
if ! python -c "import vllm" 2>/dev/null; then                                             
    echo "ERROR: vLLM is not installed in the current environment."                        
    echo "Install with: pip install vllm==0.6.3 --extra-index-url https://download.pytorch.org/whl/rocm6.2"
    exit 1                                                                                 
fi                                                                                         
                                                                                           
# Verify vLLM imports work (catches schema errors after patching)                            
python -c "import vllm; print(f'vLLM version: {vllm.__version__}')" || {
    echo "ERROR: vLLM import failed even after patching."
    echo "Python 3.12 may require manual patching or downgrade to 3.11."
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
