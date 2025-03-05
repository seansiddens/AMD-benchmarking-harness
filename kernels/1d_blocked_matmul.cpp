extern "C" __global__ void matmul_kernel(int M, int N, int K, float *A, float *B, float *C, float alpha, float beta) {

    const int BM = 64;
    const int BN = 64;
    const int BK = 8;

    __shared__ float As[BM * BK];
    __shared__ float Bs[BK * BN];

    const int C_row = blockIdx.y * BM;
    const int C_col = blockIdx.x * BN;

    const int steps = (K + BK - 1) / BK;

    float thread_result[BK] = {0.0f};

    // Each thread maps to a single element in the input matrix blocks
    assert(BN * K == BM * K == blockDim.x);

    for (int i = 0; i < steps; i++) {

        // Load A and B tiles to shared memory
        const int A_row = C_row + threadIdx.x / BK;
        const int A_col = i * BK + threadIdx.x % BK;
        As[threadIdx.x] = (A_row < M && A_col < K) ? A[A_row * K + A_col] : 0.0f;

        const int B_row = i * BK + threadIdx.x / BN;
        const int B_col = C_col + threadIdx.x % BN;
        Bs[threadIdx.x] = (B_row < K && B_col < N) ? B[B_row * N + B_col] : 0.0f;

        __syncthreads();

        // Multiply the tiles and accumulate the result
        for (int j = 0; j < BK; j++) {
            float b = Bs[j * BK + threadIdx.x % BN];

            for (int k = 0; k < BK; k++) {
                float a = As[threadIdx.x / BN * BK + k * BK + j]
                thread_result[k] += a * b;
            }
        }

        __syncthreads();
    }

    // Store the result in the output matrix
    for (int i = 0; i < BK; i++) {
        if (C_row + i < M && C_col + threadIdx.x % BN < N) {
            C[(C_row + i) * N + C_col + threadIdx.x % BN] = alpha * thread_result[i] + beta * C[(C_row + i) * N + C_col + threadIdx.x % BN];
        }
    }
}