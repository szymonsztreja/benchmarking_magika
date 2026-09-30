import argparse
import importlib
import os
import resource
import subprocess
import sys
import time
from collections import Counter
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path
from statistics import quantiles


@dataclass
class MemorySnapshot:
    phase: str
    process_rss_bytes: int | None
    process_peak_rss_bytes: int
    container_current_bytes: int | None
    container_peak_bytes: int | None
    container_limit_bytes: int | str | None


def parse_args():
    parser = argparse.ArgumentParser(
        description="Benchmark Magika's Python API on one file or a directory of files."
    )
    parser.add_argument("path", type=Path, help="File or directory to scan")
    parser.add_argument(
        "--mode",
        choices=("per-file", "batch"),
        default="per-file",
        help="Use identify_path for each file, or identify_paths for the whole list",
    )
    parser.add_argument(
        "--progress-every",
        type=int,
        default=100,
        help="Print progress every N files; use 0 to disable (default: 100)",
    )
    args = parser.parse_args()
    if args.progress_every < 0:
        parser.error("--progress-every must be zero or greater")
    return args


def discover_files(path):
    if path.is_file():
        return [path]
    if path.is_dir():
        return sorted(file_path for file_path in path.rglob("*") if file_path.is_file())
    raise ValueError(f"path does not exist or is not a regular file/directory: {path}")


def read_process_rss_bytes():
    if sys.platform.startswith("linux"):
        try:
            for line in Path("/proc/self/status").read_text().splitlines():
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024
        except (OSError, ValueError):
            return None
    elif sys.platform == "darwin":
        try:
            result = subprocess.run(
                ["ps", "-o", "rss=", "-p", str(os.getpid())],
                check=True,
                capture_output=True,
                text=True,
            )
            return int(result.stdout.strip()) * 1024
        except (OSError, subprocess.CalledProcessError, ValueError):
            return None
    return None


def read_memory_value(path):
    try:
        value = path.read_text().strip()
    except OSError:
        return None
    if value == "max":
        return value
    try:
        return int(value)
    except ValueError:
        return None


def read_container_memory():
    cgroup_v2 = Path("/sys/fs/cgroup")
    if (cgroup_v2 / "cgroup.controllers").exists():
        values = (
            read_memory_value(cgroup_v2 / "memory.current"),
            read_memory_value(cgroup_v2 / "memory.peak"),
            read_memory_value(cgroup_v2 / "memory.max"),
        )
        if any(value is not None for value in values):
            return values

    cgroup_v1 = Path("/sys/fs/cgroup/memory")
    values = (
        read_memory_value(cgroup_v1 / "memory.usage_in_bytes"),
        read_memory_value(cgroup_v1 / "memory.max_usage_in_bytes"),
        read_memory_value(cgroup_v1 / "memory.limit_in_bytes"),
    )
    if any(value is not None for value in values):
        return values
    return None, None, None


def read_container_cpu_limit():
    try:
        quota, period = Path("/sys/fs/cgroup/cpu.max").read_text().split()
    except (OSError, ValueError):
        try:
            quota = Path("/sys/fs/cgroup/cpu/cpu.cfs_quota_us").read_text().strip()
            period = Path("/sys/fs/cgroup/cpu/cpu.cfs_period_us").read_text().strip()
        except OSError:
            return "n/a"
    if quota in ("max", "-1"):
        return "unlimited"
    try:
        return f"{int(quota) / int(period):.2f} CPUs"
    except (ValueError, ZeroDivisionError):
        return "n/a"


def process_peak_rss_bytes():
    peak_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform == "darwin":
        return peak_rss
    peak_rss_bytes = peak_rss * 1024
    if sys.platform.startswith("linux"):
        try:
            for line in Path("/proc/self/status").read_text().splitlines():
                if line.startswith("VmHWM:"):
                    high_water_bytes = int(line.split()[1]) * 1024
                    return max(peak_rss_bytes, high_water_bytes)
        except (OSError, ValueError):
            pass
    return peak_rss_bytes


def capture_memory(phase):
    container_current, container_peak, container_limit = read_container_memory()
    process_rss = read_process_rss_bytes()
    process_peak_rss = process_peak_rss_bytes()
    if process_rss is not None:
        process_peak_rss = max(process_peak_rss, process_rss)
    return MemorySnapshot(
        phase=phase,
        process_rss_bytes=process_rss,
        process_peak_rss_bytes=process_peak_rss,
        container_current_bytes=container_current,
        container_peak_bytes=container_peak,
        container_limit_bytes=container_limit,
    )


def format_mib(value):
    if value is None:
        return "n/a"
    if value == "max":
        return "unlimited"
    return f"{value / (1024 * 1024):.1f}"


def print_aligned_table(title, headers, rows):
    widths = [
        max(len(headers[index]), *(len(row[index]) for row in rows))
        for index in range(len(headers))
    ]

    print(title)
    print(" | ".join(header.ljust(widths[index]) for index, header in enumerate(headers)))
    print("-+-".join("-" * width for width in widths))
    for row in rows:
        cells = [row[0].ljust(widths[0])]
        cells.extend(
            row[index].rjust(widths[index]) for index in range(1, len(row))
        )
        print(" | ".join(cells))


def print_memory_snapshots(snapshots):
    process_rows = [
        (
            snapshot.phase,
            format_mib(snapshot.process_rss_bytes),
            format_mib(snapshot.process_peak_rss_bytes),
        )
        for snapshot in snapshots
    ]
    container_rows = [
        (
            snapshot.phase,
            format_mib(snapshot.container_current_bytes),
            format_mib(snapshot.container_peak_bytes),
            format_mib(snapshot.container_limit_bytes),
        )
        for snapshot in snapshots
    ]
    print_aligned_table(
        "Process memory checkpoints (MiB):",
        ("Phase", "RSS", "Peak RSS"),
        process_rows,
    )
    print_aligned_table(
        "Container cgroup memory checkpoints (MiB):",
        ("Phase", "Current", "Peak", "Limit"),
        container_rows,
    )


def scan_files(magika, files, mode, progress_every, scan_label):
    if mode == "batch":
        print(f"{scan_label}: processing batch of {len(files)} files", flush=True)
        return magika.identify_paths(files), []

    results = []
    latencies = []
    for index, file_path in enumerate(files, start=1):
        started = time.perf_counter()
        results.append(magika.identify_path(file_path))
        latencies.append(time.perf_counter() - started)
        if progress_every and (
            index % progress_every == 0 or index == len(files)
        ):
            print(f"{scan_label}: {index}/{len(files)} files", flush=True)
    return results, latencies


def validate_results(results, expected_count, scan_label):
    if len(results) != expected_count:
        raise RuntimeError(
            f"{scan_label}: expected {expected_count} results, received {len(results)}"
        )
    return sum(not result.ok for result in results)


def main():
    args = parse_args()
    try:
        discovery_started = time.perf_counter()
        files = discover_files(args.path)
        discovery_seconds = time.perf_counter() - discovery_started
    except ValueError as error:
        raise SystemExit(str(error)) from error

    if not files:
        raise SystemExit(f"no files found under: {args.path}")

    memory_snapshots = [capture_memory("before Magika import")]

    import_started = time.perf_counter()
    magika_module = importlib.import_module("magika")
    import_seconds = time.perf_counter() - import_started
    memory_snapshots.append(capture_memory("after Magika import"))

    initialization_started = time.perf_counter()
    magika = magika_module.Magika()
    initialization_seconds = time.perf_counter() - initialization_started
    memory_snapshots.append(capture_memory("after Magika initialization"))

    first_prediction_started = time.perf_counter()
    first_prediction = magika.identify_path(files[0])
    first_prediction_seconds = time.perf_counter() - first_prediction_started
    memory_snapshots.append(capture_memory("after first prediction"))

    cpu_before_scan = time.process_time()
    scan_started = time.perf_counter()
    first_results, latencies = scan_files(
        magika, files, args.mode, args.progress_every, "Full scan"
    )
    first_scan_seconds = time.perf_counter() - scan_started
    scan_cpu_seconds = time.process_time() - cpu_before_scan
    first_scan_failures = validate_results(first_results, len(files), "Full scan")
    memory_snapshots.append(capture_memory("after full scan"))

    label_counts = Counter(
        result.output.label for result in first_results if result.ok
    )
    print(f"Magika version: {version('magika')}")
    print(f"Magika model: {magika.get_model_name()}")
    print(f"Python API mode: {args.mode}")
    print(f"CPUs visible to process (os.cpu_count): {os.cpu_count()}")
    print(f"Container CPU limit (cgroup): {read_container_cpu_limit()}")
    print(f"Files found: {len(files)}")
    print(f"Results returned: {len(first_results)}")
    print(f"Failed identifications: {first_scan_failures}")
    print(f"First prediction successful: {first_prediction.ok}")
    print(f"File discovery: {discovery_seconds * 1000:.2f} ms")
    print(f"Magika import: {import_seconds * 1000:.2f} ms")
    print(f"Magika initialization: {initialization_seconds * 1000:.2f} ms")
    print(f"First prediction: {first_prediction_seconds * 1000:.2f} ms")
    cold_start_seconds = import_seconds + initialization_seconds + first_prediction_seconds
    print(f"Cold start (import + init + first prediction): {cold_start_seconds * 1000:.2f} ms")
    print(f"Full scan after first-prediction probe: {first_scan_seconds:.3f} s")
    print(f"Scan throughput: {len(files) / first_scan_seconds:.1f} files/s")
    print(f"Scan average: {first_scan_seconds * 1000 / len(files):.3f} ms/file")
    print(
        f"Scan CPU time: {scan_cpu_seconds:.3f} s "
        f"(avg {scan_cpu_seconds / first_scan_seconds:.1f} cores busy)"
    )
    if len(latencies) >= 2:
        cuts = quantiles(latencies, n=100, method="inclusive")
        print(
            f"Per-file latency: p50 {cuts[49] * 1000:.2f} ms, "
            f"p95 {cuts[94] * 1000:.2f} ms, p99 {cuts[98] * 1000:.2f} ms, "
            f"max {max(latencies) * 1000:.2f} ms"
        )
        slowest = sorted(
            zip(latencies, files, first_results), key=lambda item: item[0], reverse=True
        )[:5]
        print("Slowest files:")
        for seconds, file_path, result in slowest:
            label = result.output.label if result.ok else "error"
            print(
                f"  {seconds * 1000:8.2f} ms  {file_path.stat().st_size / 1024:10.1f} KiB  "
                f"{label:10s} {file_path}"
            )
    print(f"Most common labels: {label_counts.most_common(10)}")
    print(f"Files detected as TSV (by content): {label_counts.get('tsv', 0)}")
    print(f"Files with .tsv extension: {sum(p.suffix.lower() == '.tsv' for p in files)}")
    print(f"All file types: {len(label_counts)}")

    usage = resource.getrusage(resource.RUSAGE_SELF)
    print(f"Process CPU time (whole run): {usage.ru_utime + usage.ru_stime:.3f} s")
    print_memory_snapshots(memory_snapshots)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())