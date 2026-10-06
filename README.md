# Magika benchmark: quick reproduction

This project benchmarks the Magika Python API against a small Govdocs1 subset. The goal is a repeatable throughput/memory check, not label accuracy validation.

## Setup

This project is packaged as a minimal Python project via `pyproject.toml`.

```zsh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
```

This installs the benchmark entry point (`magika-benchmark`) and the pinned dependency `magika==1.0.3`.

## Download the corpus

```zsh
mkdir -p dataset
curl -fL --retry 3 \
  -o dataset/thread0.zip \
  https://downloads.digitalcorpora.org/corpora/files/govdocs1/threads/thread0.zip
mkdir -p dataset/thread0
unzip -q dataset/thread0.zip -d dataset/thread0
find dataset/thread0 -type f | wc -l
```

This subset is ~991 files in the original run, with the extracted data occupying a few hundred MB.

## Run locally

```zsh
magika-benchmark dataset/thread0 --progress-every 0
```

Useful flags:

- `--mode per-file` or `--mode batch`
- `--progress-every N` to print progress; use `0` to disable
- `--compare-extensions` to compare predicted labels with file extensions after the scan
- `--mismatches-csv FILE` to write those mismatches to a CSV file (with `--compare-extensions`)

## Offline Linux ARM64 wheelhouse for Docker

This avoids a TLS/certificate problem while installing Magika from PyPI inside the container. Instead of fetching dependencies at build time, the required Linux ARM64 wheels are downloaded on the host and installed offline from the local wheelhouse.

```zsh
mkdir -p wheelhouse
.venv/bin/python -m pip download \
  --dest wheelhouse \
  --only-binary=:all: \
  --platform manylinux_2_28_aarch64 \
  --python-version 3.14 \
  --implementation cp \
  --abi cp314 \
  --abi abi3 \
  --abi none \
  magika==1.0.3
```

Dockerfile used in the project:

```dockerfile
FROM python:3.14-slim
WORKDIR /work
COPY . /wheelhouse
RUN python -m pip install --no-index --find-links=/wheelhouse magika==1.0.3
ENTRYPOINT ["python"]
```

Build and verify:

```zsh
docker build --platform linux/arm64 -f Dockerfile.bench -t magika-bench ./wheelhouse
docker run --rm --platform linux/arm64 magika-bench -c 'import magika; print(magika.__file__)'
```

## Benchmark in Docker with resource limits

Use a Docker-managed volume for a fairer Linux-local comparison:

```zsh
docker volume create magika-corpus-bench
docker run --rm \
  -v magika-corpus-bench:/data \
  -v "$PWD/dataset/thread0:/source:ro" \
  alpine:3.21 \
  sh -c 'cp -a /source/. /data/'
```

Then run the benchmark:

```zsh
docker run --rm \
  --platform linux/arm64 \
  --cpus=1 \
  --memory=1g \
  --memory-swap=1g \
  -v "$PWD/benchmark_magika.py:/work/benchmark_magika.py:ro" \
  -v magika-corpus-bench:/data:ro \
  magika-bench \
  /work/benchmark_magika.py /data --progress-every 100
```

For a Fargate-shaped task, use `--memory=2g --memory-swap=2g` (1 vCPU requires at least 2 GiB there).

### Match visible CPUs to the CPU limit

`--cpus` limits CPU time, not the CPUs the container can see. onnxruntime sizes its thread pool from the visible CPUs, and Magika 1.0.3 has no option to change it. With 15 visible CPUs (the original Docker Desktop setting on the test machine) and `--cpus=1`, onnxruntime starts 14 worker threads that compete for one CPU's quota, and the container is throttled in every scheduling period.

Before benchmarking, set Docker Desktop → Settings → Resources → Advanced → CPU limit to the same value as `--cpus`, then check:

```zsh
docker run --rm alpine:3.21 nproc
```

In the benchmark output, `CPUs visible to process (os.cpu_count)` should match `Container CPU limit (cgroup)`.

- `--cpuset-cpus` does not help: onnxruntime ignores CPU affinity when sizing its pool and logs `pthread_setaffinity_np failed` errors.
- The Docker Desktop CPU limit applies to all containers until you change it back.
- To check throttling, add `--name magika-run` to the run and, while it runs, compare `nr_throttled` with `nr_periods` in `docker exec magika-run cat /sys/fs/cgroup/cpu.stat`.

## Repeated runs with hyperfine

[hyperfine](https://github.com/sharkdp/hyperfine) runs a command several times and reports mean, standard deviation and range. [vmtouch](https://github.com/hoytech/vmtouch) evicts specific files from the macOS file cache without sudo.

```zsh
brew install hyperfine vmtouch
```

macOS, warm and cold file cache:

```zsh
hyperfine --warmup 1 --runs 5 \
  -n warm --prepare 'true' 'magika-benchmark dataset/thread0 --progress-every 0' \
  -n cold --prepare 'vmtouch -qe dataset/thread0 .venv' 'magika-benchmark dataset/thread0 --progress-every 0'
```

macOS, cold start of a single file (whole process):

```zsh
hyperfine --warmup 3 --runs 20 'magika-benchmark sample.py --progress-every 0'
```

Docker, cold file cache in the Docker VM before each run:

```zsh
hyperfine --runs 3 \
  --prepare "docker run --rm --privileged alpine:3.21 sh -c 'sync; echo 3 > /proc/sys/vm/drop_caches'" \
  'docker run --rm --platform linux/arm64 --cpus=1 --memory=1g --memory-swap=1g -v "$PWD/benchmark_magika.py:/work/benchmark_magika.py:ro" -v magika-corpus-bench:/data:ro magika-bench /work/benchmark_magika.py /data --progress-every 0'
```

To compare CPU limits, set the Docker Desktop CPU limit to each value and rerun with a matching `--cpus`. A single run with `-L cpus 1,2,4` needs a VM with at least 4 CPUs, so the lower limits would measure throttling rather than Magika (see [Match visible CPUs to the CPU limit](#match-visible-cpus-to-the-cpu-limit)). Docker also rejects `--cpus` values above the VM's CPU count.

- hyperfine swallows the command's output by default, so all you get is the timing. `--show-output` prints the script's breakdown (import, init, scan, memory) for every run. Handy for a peek, but the extra printing can nudge the timings, so leave it off for the final numbers.
- For Docker commands, hyperfine's `User`/`System` values and its memory numbers (`memory_usage_byte` in the hyperfine 1.20 JSON export, ~32 MiB) belong to the `docker` CLI on the host, not to Magika in the container. Take `Time` from hyperfine and RSS from the script's own output.
- Keep the command in single quotes so `$PWD` is expanded when each run starts.
- `vmtouch -qe` evicts only the listed paths; the base Python interpreter and system libraries stay cached. Check residency with `vmtouch dataset/thread0`.
- For bind-mount Docker runs, evict on both sides: prefix the `--prepare` command with `vmtouch -qe dataset/thread0; `.
- Use `--export-json FILE` or `--export-markdown FILE` to save results.

To keep the timings and every run's script output together, point both at a folder:

```zsh
mkdir -p benchmark-results
hyperfine --warmup 1 --runs 10 \
  --export-json benchmark-results/cpu-2-cold.json \
  --prepare "docker run --rm --privileged alpine:3.21 sh -c 'sync; echo 3 > /proc/sys/vm/drop_caches'" \
  'docker run --rm --platform linux/arm64 --cpus=2 --memory=1g --memory-swap=1g -v "$PWD/benchmark_magika.py:/work/benchmark_magika.py:ro" -v magika-corpus-bench:/data:ro magika-bench /work/benchmark_magika.py /data --progress-every 0 > "benchmark-results/cpu-2-cold-run-${HYPERFINE_ITERATION}.log" 2>&1'
```

You end up with `cpu-2-cold.json`, `cpu-2-cold-run-0.log` to `cpu-2-cold-run-9.log`, and `cpu-2-cold-run-warmup-0.log` from the warmup. For a warm run, drop the `--prepare` line and swap `cold` for `warm` in the names. Same names on the next run means they get overwritten, so rename (or pick a new folder) if you want to keep the old ones.

## What was observed

- Host run on Apple Silicon: a full scan of the 991-file subset took ~2.3 s end to end, the same with a warm or cold (vmtouch-evicted) file cache; peak RSS stayed under ~80 MiB.
- Magika reads only small parts of each file: one cold scan loaded ~79 MB of the 573 MB corpus into the cache, so scan time is dominated by CPU, not disk.
- The very first runs were slower (~6.5 s). The file cache does not explain this; likely first-run effects (Python bytecode compilation, antivirus scanning of newly extracted files), not confirmed.
- Background load matters: with Microsoft Defender real-time scanning busy, the same host scan took 10–11 s.

Docker, corpus on a volume, 1 GiB memory limit, Docker VM CPU count matched to `--cpus` each time, `--warmup 1 --runs 10`. "Cold" means `drop_caches` before every run; "warm" means no clearing. hyperfine is the whole `docker run`; "scan" and "cores busy" come from the script's own output, averaged over the 10 runs. Raw JSON and logs are in `benchmark-results/cpu-N-{cold,warm}*`.

| `--cpus` | Cache | hyperfine (mean ± SD) | Scan | Scan CPU time | Cores busy |
|---:|---|---:|---:|---:|---:|
| 1 | cold | 8.17 s ± 0.17 s | ~7.9 s | ~7.8 s | 1.0 |
| 1 | warm | 7.96 s ± 0.10 s | ~7.7 s | ~7.7 s | 1.0 |
| 2 | cold | 9.56 s ± 1.32 s | ~8.7 s | ~10.5 s | 1.2 |
| 2 | warm | 4.86 s ± 0.08 s | ~4.6 s | ~9.2 s | 2.0 |
| 4 | cold | 6.88 s ± 0.08 s | ~6.5 s | ~19.8 s | 3.0 |
| 4 | warm | 3.45 s ± 0.12 s | ~3.1 s | ~12.5 s | 4.0 |

- Warm cache scales OK-ish: the scan goes 7.7 → 4.6 → 3.1 s, so ~1.7x at 2 CPUs and ~2.5x at 4. Not linear, because total CPU time climbs too (7.7 → 9.2 → 12.5 s); extra cores pay some thread overhead.
- Cold cache barely matters at 1 CPU (+0.2 s) but roughly doubles the scan at 2 and 4 CPUs. Cores sit partly idle (1.2 of 2, 3.0 of 4) **and** CPU time goes up (12.5 → 19.8 s at 4 CPUs), so it isn't just waiting on disk. A guess: onnxruntime's worker threads spin while the main thread waits for a file read. Not verified.
- 2 CPUs cold is slower than 1 CPU in all three 10-run sets we have (10.49, 10.37 and 9.56 s) and much noisier. One run in the last set did a 7.0 s scan with 1.4 cores busy, the rest were 8.5–9.1 s at 1.2. Still unexplained.
- hyperfine minus scan is ~0.3 s every time: container start, Python import, Magika init.
- Changing the Docker VM CPU count restarts the VM, so the cache starts empty. The warmup runs of the warm sets looked cold (9.2 s at 2 CPUs, 6.6 s at 4); without `--warmup 1` the first "warm" run would have been a cold one.
- RSS sat at 74–77 MiB whatever the CPU count or cache state.

- Earlier Docker runs with 15 visible CPUs and `--cpus=1` took ~158–254 s (~200 ms/file). onnxruntime started 14 worker threads, and the container was throttled in every 100 ms period (197 of 197). Forcing one onnxruntime thread in the same setup gave ~7.8 ms/file.
- A macOS bind mount took ~276–301 s with the same limits. This was measured before the CPU fix and has not been re-measured.
- The container's cgroup memory (`memory.current`/`peak`) includes the Linux file cache: ~282 MiB after a cold scan and ~43 MiB after a warm one, with the same ~75 MiB RSS. Readahead cached ~160 KiB per file even though Magika reads at most 4 KiB from each end. Use RSS for sizing.
- The workload was CPU-bound, not memory-bound.

## Notes

- The benchmark measures throughput and memory; it does not verify prediction correctness.
- The script includes a separate one-file probe before the main scan, so the reported full-scan timing is after model setup.
- Magika is content-based; file extension is not the deciding factor.
- For repeatable results, keep the same image, corpus, CPU limit, Docker VM CPU count, and storage path across runs.
- Where a container sees more CPUs than its CPU limit (possible on ECS/Fargate or Kubernetes), onnxruntime oversubscribes the same way. Compare the two CPU lines in the benchmark output. The fix there is setting onnxruntime's `intra_op_num_threads` in code, which Magika 1.0.3 does not expose.
- Check machine load before benchmarking (`uptime`, `ps -Ao pcpu,comm -r | head -n 4`).

## References

- https://github.com/google/magika
- https://github.com/sharkdp/hyperfine
- https://github.com/hoytech/vmtouch
- https://securityresearch.google/magika/cli-and-bindings/python/
- https://downloads.digitalcorpora.org/corpora/files/govdocs1/threads/
