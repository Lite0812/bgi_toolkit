#!/usr/bin/env python3
"""Standalone compatibility entry for the mapper integrated into BPD tools."""
import sys
from pathlib import Path

# Allow direct execution from any working directory without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bgi_toolkit.function_mapping import *
from bgi_toolkit.function_mapping import write_mapping as _write_mapping


def write_mapping(mapping, runtime, output_path=None):
    return _write_mapping(mapping, runtime, output_path or Path(__file__).resolve().parent / OUTPUT_FILE)


def process_directory(directory, mapping):
    directory = Path(directory)
    output_directory = directory if directory.is_dir() else directory.parent
    return generate_function_mapping(directory, output_directory / OUTPUT_FILE, mapping=mapping)


def select_directory():
    try:
        import tkinter
        from tkinter import filedialog

        root = tkinter.Tk()
        root.withdraw()
        selected = filedialog.askdirectory(title="Select decompiled C directory")
        root.destroy()
        return Path(selected) if selected else None
    except Exception:
        return None


def main():
    script_directory = Path(__file__).resolve().parent
    custom_mapping = script_directory / MAPPING_FILE
    mapping = load_mapping(custom_mapping if custom_mapping.is_file() else None)

    arguments = [Path(argument.strip('"')) for argument in sys.argv[1:]]
    if not arguments:
        selected = select_directory()
        if selected is None:
            print("Drag a decompiled C directory onto this Python script.")
            return 1
        arguments = [selected]

    failed = False
    for index, directory in enumerate(arguments):
        if index:
            print()
        try:
            process_directory(directory, mapping)
        except Exception as error:
            failed = True
            print(f"ERROR: {error}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
