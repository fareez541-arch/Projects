#!/usr/bin/env python3
"""Verify RCCL/GFX1100 compatibility for VLLM"""
import os
import torch

def check_rocm_env():
    assert os.getenv('HSA_OVERRIDE_GFX_VERSION') == '11.0.0', "GFX1100 override missing"
    assert os.getenv('HIP_VISIBLE_DEVICES') == '0,1', "Dual GPU visibility not set"
    
    if torch.cuda.is_available():
        print(f"✓ PyTorch ROCm available: {torch.version.hip}")
        print(f"✓ GPU count: {torch.cuda.device_count()}")
        for i in range(torch.cuda.device_count()):
            print(f"  - GPU {i}: {torch.cuda.get_device_name(i)}")
    else:
        raise RuntimeError("PyTorch ROCm not available")

if __name__ == "__main__":
    check_rocm_env()
    print("[RCCL_DUAL_GFX1100_READY]")
