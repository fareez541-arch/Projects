#!/bin/bash                                                                                
                                                                                           
#===============================================================================           
# vLLM Startup Script for Dual AMD Navi 31 (gfx1100) - ROCm 6.2                            
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
                                                                                           
# Check Python version - MUST be 3.11 for vLLM 0.6.3 compatibility                         
PYTHON_VER=$(python -c 'import sys;                                                        
print(f"{sys.version_info.major}.{sys.version_info.minor}")')                              
if [[ "$PYTHON_VER" == "3.12" ]]; then                                                     
    echo "ERROR: Python 3.12 detected. vLLM 0.6.3 requires Python 3.11 due to 'list[int]'  
schema errors."                                                                            
    echo "Please downgrade with: conda install python=3.11"                                
    exit 1                                                                                 
fi                                                                                         
                                                                                           
# Safety Check: Detect CUDA packages in ROCm environment                                   
if pip list 2>/dev/null | grep -q nvidia; then                                             
    echo "ERROR: NVIDIA/CUDA packages detected in environment!"                            
    echo "These are incompatible with AMD GPUs."                                           
    exit 1                                                                                 
fi                                                                                         
                                                                                           
# Verify ROCm PyTorch is installed                                                         
python -c "                                                                                
import torch                                                                               
if not hasattr(torch.version, 'hip') or torch.version.hip is None:                         
    print('ERROR: PyTorch CUDA version detected. Need ROCm version.')                      
    exit(1)                                                                                
" || exit 1                                                                                
                                                                                           
# Check if vLLM is installed                                                               
if ! python -c "import vllm" 2>/dev/null; then                                             
    echo "ERROR: vLLM is not installed in the current environment."                        
    exit 1                                                                                 
fi                                                                                         
                                                                                           
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

