# Standard library imports
import os
import math
import random
import array
import ctypes
import torch

# Third-party imports
import pydra
from pydra import REQUIRED, Config


# Write results to JSON file
import json
import datetime
from hip import hip, hiprtc

# Local/project imports
from utils.check import hip_check, compare
from utils.types import DataType, KernelType
from src import run_pytorch, run_triton, run_hip, run_tk

"""
Evaluate the performance of HiP implementations of various kernels
"""

REPO_TOP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KERNEL_DIR = os.path.join(REPO_TOP_DIR, "kernels")

class EvalConfig(Config):
    def __init__(self):
        self.kernel = "" # name of the matmul kernel to evaluate
        self.block_size = 16

        # matrix size dimensions
        # A matrix is M x K 
        # B matrix is K x N
        # C matrix is M x N
        self.M = 1024
        self.K = 1024
        self.N = 1024
        self.alpha = 1.0
        self.beta = 0.0

        self.AB_type = DataType.FP32

        # which kernel backend to run
        self.kernel_type = KernelType.HIP

        # timing
        self.num_warmup = 3
        self.num_iterations = 10

        self.debug = False
        self.results_dir = os.path.join(REPO_TOP_DIR, "results")

    def correctness(self):
        self.num_warmup = 0
        self.num_iterations = 1
        self.M = 8192
        self.K = 8192
        self.N = 8192
        self.debug = True

    def matmul_shape(self):
        # Standard GEMM shape for benchmarking

        self.M = 8192
        self.K = 8192
        self.N = 8192   

    # Below are shapes of operations used in Llama 70B model
    def qkv_proj_shape(self):
        # Fused QKV Projection GEMM shape

        self.M = 16384
        self.K = 8192
        self.N = 1280

    def attn_output_shape(self):
        # Attention Output Projection shape
        self.M = 16384
        self.K = 1024
        self.N = 8192

    def ffn_gemm_shape(self):
        # FFN GEMM shape
        self.M = 16384
        self.K = 3584
        self.N = 8192


    def __repr__(self):
        return f"EvalConfig({self.to_dict()})"

def test_kernel_harness(config: EvalConfig):
    """
    Driver code to test kernels
    """

    # Convert string to KernelType if needed
    if not isinstance(config.kernel_type, KernelType):
        kernel_type = KernelType.from_string(str(config.kernel_type))
    else:
        kernel_type = config.kernel_type

    # Convert string to DataType if needed
    if not isinstance(config.AB_type, DataType):
        config.AB_type = DataType.from_string(str(config.AB_type))

    # Extract AB data type
    if config.AB_type == DataType.FP32:
        ab_type = torch.float32
    elif config.AB_type == DataType.FP16:
        ab_type = torch.float16
    elif config.AB_type == DataType.BF16:
        ab_type = torch.bfloat16

    # Define matrix dimensions
    M = config.M
    K = config.K
    N = config.N

    alpha = config.alpha
    beta = config.beta
 
    device = torch.device("cuda")
    A_h = torch.randn(M, K).to(ab_type).to(device)
    B_h = torch.randn(K, N).to(ab_type).to(device)
    C_h = torch.randn(M, N).to(torch.float32).to(device)

    # Compute expected result using NumPy as Golden Reference
    C_expected = alpha * A_h @ B_h + beta * C_h
    C_expected = C_expected.cpu()

    if kernel_type in [KernelType.HIP]:

        # Scalars
        alpha = ctypes.c_float(config.alpha)
        beta = ctypes.c_float(config.beta)

        # Allocate device memory
        num_bytes_A = A_h.numel() * A_h.element_size()
        num_bytes_B = B_h.numel() * B_h.element_size()
        num_bytes_C = C_h.numel() * C_h.element_size()

        A_d = hip_check(hip.hipMalloc(num_bytes_A))
        B_d = hip_check(hip.hipMalloc(num_bytes_B))
        C_d = hip_check(hip.hipMalloc(num_bytes_C))

        # Copy input data to device
        hip_check(hip.hipMemcpy(A_d, A_h.data_ptr(), num_bytes_A, hip.hipMemcpyKind.hipMemcpyHostToDevice))
        hip_check(hip.hipMemcpy(B_d, B_h.data_ptr(), num_bytes_B, hip.hipMemcpyKind.hipMemcpyHostToDevice))
        hip_check(hip.hipMemcpy(C_d, C_h.data_ptr(), num_bytes_C, hip.hipMemcpyKind.hipMemcpyHostToDevice))

    # setup kernel
    match kernel_type:
        case KernelType.HIP: # hand-written kernel
            assert len(config.kernel) > 0, "Kernel name must be provided"
            C_output = run_hip.test_hip_matmul(config, M, N, K, A_d, B_d, C_d, alpha, beta, C_expected)
        
        case KernelType.TRITON: # Triton
            C_output = run_triton.test_triton_matmul(config, M, N, K, A_h, B_h, C_h, alpha, beta, C_expected)

        case KernelType.THUNDERKITTEN: # Thunderkitten
            assert len(config.kernel) > 0, "Kernel name must be provided"
            C_output = run_tk.test_tk_matmul(config, M, N, K, A_h, B_h, C_h, alpha, beta, C_expected)

        case KernelType.PYTORCH: # PyTorch
            # pass the numpy arrays to pytorch
            C_output = run_pytorch.test_pytorch_matmul(config, M, N, K, A_h, B_h, C_h, alpha, beta, C_expected)
        
        case _:
            raise ValueError(f"Not implemented for kernel type: {config.kernel_type}")
    
    if kernel_type in [KernelType.HIP]:
        # Clean up device memory
        hip_check(hip.hipFree(A_d))
        hip_check(hip.hipFree(B_d))
        hip_check(hip.hipFree(C_d))

    return C_output


@pydra.main(base=EvalConfig)
def main(config: EvalConfig):

    print(f"Running {config.kernel_type} kernel")

    M = config.M
    N = config.N
    K = config.K
    alpha = config.alpha
    beta = config.beta

    print(f"Testing Kernel with size: M {M} x K {K} x N {N} Alpha: {alpha}, Beta: {beta}")
    print(f"Using Kernel Backend: {config.kernel_type} Kernel Name: {config.kernel}")

    # Warmup
    for _ in range(config.num_warmup):
        test_kernel_harness(config)

    num_iterations = config.num_iterations

    kernel_times = []
    for _ in range(config.num_iterations):
        kernel_times.append(test_kernel_harness(config))
    

    # Compute execution test stats
    times_array = torch.tensor(kernel_times)

    stats = {
        "mean": float(f"{times_array.mean():.2f}"),
        "std": float(f"{times_array.std():.2f}"), 
        "min": float(f"{times_array.min():.2f}"),
        "max": float(f"{times_array.max():.2f}"),
        "median": float(f"{times_array.median():.2f}"),
        "total_time": float(f"{times_array.sum():.2f}")
    }
    
    
    # total_time = sum(kernel_times)
    # avg_time = total_time / num_iterations
    print(f"\n✅ Average Kernel Execution Time: {stats['mean']:.4f} ms over {num_iterations} runs")
    print(f"Stats: {stats}")
    flops = 2. * 1e-9 * num_iterations * M * N * K / stats['total_time']
    print(f"\nPerformance FLOPS: ({flops:.2f}) TFLOPS. size: ({M} x {K}) * ({K} x {N}).\n")
    

    results = {
        "kernel_type": str(config.kernel_type),
        "kernel_name": config.kernel,
        "matrix_dims": {
            "M": M,
            "N": N, 
            "K": K,
            "alpha": alpha,
            "beta": beta
        },
        "parameters": {
            "num_warmup": config.num_warmup,
            "num_iterations": num_iterations
        },
        "timing_stats": stats,
        "performance": {
            "tflops": float(f"{flops:.2f}")
        },
        "timestamp": datetime.datetime.now().isoformat(),
        "precision": "fp32"
    }

    # Generate filename with timestamp
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"results_{config.kernel_type}_config_{config.kernel}_{M}x{K}x{N}_{timestamp}.json"
    filepath = os.path.join(config.results_dir, filename)

    # Write results to JSON file
    with open(filepath, "w") as f:
        json.dump(results, f, indent=4)

    print(f"\nResults written to: {filepath}")

if __name__ == "__main__":
    main()