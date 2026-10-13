# 仅 CPU 的 Ollama：去掉官方镜像中约 4 GB 的 CUDA / MLX / Vulkan 库，
# 演示设备（国产 OS、无 NVIDIA GPU）只需 CPU 推理。
FROM ollama/ollama:0.32.1 AS src

FROM --platform=$BUILDPLATFORM busybox:1.37 AS prune
COPY --from=src /usr/lib/ollama /out/lib/ollama
COPY --from=src /usr/bin/ollama /out/bin/ollama
RUN rm -rf /out/lib/ollama/cuda_* /out/lib/ollama/mlx_* /out/lib/ollama/rocm* \
           /out/lib/ollama/vulkan /out/lib/ollama/include \
    && ls /out/lib/ollama

FROM ubuntu:24.04
COPY --from=prune /out/lib/ollama /usr/lib/ollama
COPY --from=prune /out/bin/ollama /usr/bin/ollama
ENV OLLAMA_HOST=0.0.0.0:11434
EXPOSE 11434
ENTRYPOINT ["/usr/bin/ollama"]
CMD ["serve"]
