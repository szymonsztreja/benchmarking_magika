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

Docker, cold file cache in the Docker VM before each run, compared across CPU limits:

```zsh
hyperfine --runs 3 \
  --prepare "docker run --rm --privileged alpine:3.21 sh -c 'sync; echo 3 > /proc/sys/vm/drop_caches'" \
  -L cpus 1,2,4 \
  'docker run --rm --platform linux/arm64 --cpus={cpus} --memory=1g --memory-swap=1g -v "$PWD/benchmark_magika.py:/work/benchmark_magika.py:ro" -v magika-corpus-bench:/data:ro magika-bench /work/benchmark_magika.py /data --progress-every 0'
```

- hyperfine times the whole process; the script's breakdown (import, init, scan, memory) is hidden unless you add `--show-output`.
- For Docker commands, hyperfine's `User`/`System` values belong to the `docker` CLI, not the container; use `Time` only.
- Keep the command in single quotes so `$PWD` is expanded when each run starts.
- `vmtouch -qe` evicts only the listed paths; the base Python interpreter and system libraries stay cached. Check residency with `vmtouch dataset/thread0`.
- For bind-mount Docker runs, evict on both sides: prefix the `--prepare` command with `vmtouch -qe dataset/thread0; `.
- Use `--export-json FILE` or `--export-markdown FILE` to save results.

## What was observed

- Host run on Apple Silicon: a full scan of the 991-file subset took ~2.3 s end to end, the same with a warm or cold (vmtouch-evicted) file cache; peak RSS stayed under ~80 MiB.
- Magika reads only small parts of each file: one cold scan loaded ~79 MB of the 573 MB corpus into the cache, so scan time is dominated by CPU, not disk.
- The very first runs were slower (~6.5 s). The file cache does not explain this; likely first-run effects (Python bytecode compilation, antivirus scanning of newly extracted files), not confirmed.
- Background load matters: with Microsoft Defender real-time scanning busy, the same host scan took 10–11 s.
- Docker, 1 vCPU and 1 GiB limit, corpus on a volume: ~254 s on the first run, ~158 s on later runs.
- Same limits with a macOS bind mount: ~276–301 s.
- The workload was CPU-bound, not memory-bound.

## Notes

- The benchmark measures throughput and memory; it does not verify prediction correctness.
- The script includes a separate one-file probe before the main scan, so the reported full-scan timing is after model setup.
- Magika is content-based; file extension is not the deciding factor.
- For repeatable results, keep the same image, corpus, CPU limit, and storage path across runs.
- Check machine load before benchmarking (`uptime`, `ps -Ao pcpu,comm -r | head -n 4`).

## References

- https://github.com/google/magika
- https://github.com/sharkdp/hyperfine
- https://github.com/hoytech/vmtouch
- https://securityresearch.google/magika/cli-and-bindings/python/
- https://downloads.digitalcorpora.org/corpora/files/govdocs1/threads/
