# Phase 6 AI resource budget

Track per model/provider/hardware: decoded FPS, decoded megapixels/s, preprocess/inference/postprocess p50/p95/p99, RAM, VRAM, accelerator utilization, queue delay and skipped frames.

Keep >=30% headroom before placement. Queue growth causes frame skipping according to policy, never recording backpressure.

Example structural load only: 100,000 cameras, 10% AI enabled, 2 FPS, 640x360 = 10,000 active AI cameras, 20,000 sampled frames/s, 4,608 decoded MP/s. Hardware count remains unknown until measured.
