"""CPU-parallel text scans with isolated decoders and deterministic merging."""
from __future__ import annotations
from ...i18n import tr

import contextlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import io
import json
import os
from pathlib import Path
from queue import Empty, Queue
import subprocess
import sys
import tempfile
from threading import Event, Lock

from .dialog_analysis import analyze_calls, describe_calls
from ..service import AsdisOptions, disassemble_story


def _file_size(path):
    try:
        return path.stat().st_size
    except OSError:
        # A file removed during scanning is reported by its worker, not the scheduler.
        return 0


def scan_worker_count(files):
    cpu_count = getattr(os, "process_cpu_count", os.cpu_count)() or 1
    if getattr(sys, "frozen", False) or cpu_count < 2 or len(files) < 4:
        return 1
    if sum(_file_size(path) for path in files) < 1_000_000:
        return 1
    return min(4, len(files), max(1, cpu_count - 1))


def scan_file(index, path, options, rules, *, analyzer=None, temporary_root=None):
    path = Path(path)
    result = {"index": index, "path": str(path), "groups": [], "error": None, "log": ""}
    output = io.StringIO()
    try:
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            if path.suffix.lower() == ".bsd" or path.name.lower().endswith(".bsd.txt"):
                assembly = path
                lines = assembly.read_text(encoding="utf-8").splitlines(keepends=True)
            else:
                with contextlib.ExitStack() as cleanup:
                    folder = temporary_root or cleanup.enter_context(tempfile.TemporaryDirectory(prefix="bgi_scan_file_"))
                    assembly = Path(folder) / "scan.bsd"
                    disassemble_story(path, assembly, options, use_function_names=False)
                    lines = assembly.read_text(encoding="utf-8").splitlines(keepends=True)
            result["groups"] = describe_calls((analyzer or analyze_calls)(lines), rules, include_all=True)
    except Exception as error:
        result["error"] = str(error)
    result["log"] = output.getvalue().strip()
    return result


def merge_scan_groups(merged, groups, source):
    for group in groups:
        group["source"] = source
        previous = merged.get(group["function"])
        if previous is None:
            merged[group["function"]] = group
            continue
        previous["calls"] += group["calls"]
        previous["versions"] = list(dict.fromkeys(previous.get("versions", []) + group.get("versions", [])))
        rows = {row["position"]: row for row in previous["arguments"]}
        for row in group["arguments"]:
            old = rows.get(row["position"])
            if old is None:
                previous["arguments"].append(row)
            else:
                if old["auto_role"] != row["auto_role"]:
                    old["uncertain"] = True
                old["samples"] = list(dict.fromkeys(old["samples"] + row["samples"]))[:3]


def _worker_main():
    payload = json.load(sys.stdin)
    options = AsdisOptions(**payload["options"])
    for index, path in payload["files"]:
        result = scan_file(index, path, options, payload["rules"], temporary_root=payload["temporary_root"])
        print(json.dumps(result, ensure_ascii=True), flush=True)


def _scan_batch(batch, options, rules, queue, stop, processes, lock):
    seen = set()
    process = None
    failure = tr('分析进程未返回结果')
    try:
        if stop.is_set():
            return
        with tempfile.TemporaryDirectory(prefix="bgi_scan_batch_") as folder:
            package_root = str(Path(__file__).resolve().parents[3])
            bootstrap = "import sys; sys.path.insert(0, sys.argv[1]); from bgi_toolkit.asdis.common.text_scan import _worker_main; _worker_main()"
            process = subprocess.Popen([sys.executable, "-I", "-c", bootstrap, package_root],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            with lock:
                processes.add(process)
                if stop.is_set():
                    process.terminate()
            try:
                json.dump({"files": [(index, str(path)) for index, path in batch],
                    "options": asdict(options), "rules": rules, "temporary_root": folder}, process.stdin)
                process.stdin.close()
                expected = {index for index, _ in batch}
                for line in process.stdout:
                    if stop.is_set():
                        break
                    try:
                        result = json.loads(line)
                    except ValueError:
                        queue.put({"diagnostic": line.strip()})
                        continue
                    if not isinstance(result, dict) or result.get("index") not in expected:
                        queue.put({"diagnostic": line.strip()})
                        continue
                    if result["index"] not in seen:
                        seen.add(result["index"])
                        queue.put(result)
                if stop.is_set() and process.poll() is None:
                    process.terminate()
                return_code = process.wait()
                if return_code:
                    failure = tr('分析进程退出（{0}）', return_code)
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait()
                process.stdout.close()
                if not process.stdin.closed:
                    process.stdin.close()
                with lock:
                    processes.discard(process)
    except Exception as error:
        failure = str(error)
    finally:
        if not stop.is_set():
            for index, path in batch:
                if index not in seen:
                    queue.put({"index": index, "path": str(path), "groups": [],
                        "error": failure, "log": "", "retry_serial": True})
        queue.put(None)


def parallel_scan(files, options, rules, workers, cancelled=lambda: False):
    """Yield completed files; cancellation terminates only this scan's children."""
    buckets = [[] for _ in range(workers)]
    weights = [0] * workers
    for index, path in sorted(enumerate(files), key=lambda item: _file_size(item[1]), reverse=True):
        target = min(range(workers), key=weights.__getitem__)
        buckets[target].append((index, path))
        weights[target] += max(1, _file_size(path))
    buckets = [bucket for bucket in buckets if bucket]
    queue, stop, processes, lock = Queue(), Event(), set(), Lock()
    executor = ThreadPoolExecutor(max_workers=len(buckets), thread_name_prefix="bgi_scan_process")
    remaining = len(buckets)
    try:
        for bucket in buckets:
            executor.submit(_scan_batch, bucket, options, rules, queue, stop, processes, lock)
        while remaining:
            if cancelled():
                return
            try:
                result = queue.get(timeout=0.1)
            except Empty:
                continue
            if result is None:
                remaining -= 1
            else:
                yield result
    finally:
        stop.set()
        with lock:
            for process in processes:
                if process.poll() is None:
                    process.terminate()
        executor.shutdown(wait=True, cancel_futures=True)
