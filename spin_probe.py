"""Run benchmark_magika with onnxruntime spinning/threads overridden via ORT_SPIN and ORT_THREADS."""

import os
import resource
import sys

import onnxruntime as rt

import benchmark_magika

spin = os.environ.get("ORT_SPIN", "1")
threads = int(os.environ.get("ORT_THREADS", "0"))
_original_session = rt.InferenceSession


def patched_session(path, sess_options=None, **kwargs):
    options = sess_options or rt.SessionOptions()
    options.add_session_config_entry("session.intra_op.allow_spinning", spin)
    options.intra_op_num_threads = threads
    return _original_session(path, options, **kwargs)


# Magika looks up rt.InferenceSession at call time, so patching the module attribute is enough.
rt.InferenceSession = patched_session

exit_code = benchmark_magika.main()
usage = resource.getrusage(resource.RUSAGE_SELF)
print(f"ORT_SPIN={spin} ORT_THREADS={threads or 'default'}")
print(f"Process user CPU: {usage.ru_utime:.3f} s, system CPU: {usage.ru_stime:.3f} s")
try:
    with open("/sys/fs/cgroup/cpu.stat") as cpu_stat:
        print("cgroup cpu.stat:", " ".join(line.strip() for line in cpu_stat if "throttled" in line or "periods" in line))
except OSError:
    pass
sys.exit(exit_code)
