# BGI Toolkit

[简体中文](README.md) | **English**

A toolkit for extracting and editing scripts, text, images, archives, animations, audio, and save files from BGI games.

This package is for 64-bit Windows. The runtime and FFmpeg are included; no separate Python installation is needed.

## Getting started

Extract the package and double-click `BGI_Toolkit.exe` or `run_gui.bat`.

Keep the whole folder together when moving or sharing it. The EXE needs the `_internal` folder to run.

## Features

| Content | Supported operations |
| --- | --- |
| Story and system scripts | Extract and rebuild scripts; export text as JSON / TXT and import edited text |
| BSD / BPD decompilation | Inspect script logic and export C code; BSD also supports Ren’Py output |
| Images | Convert CBG / BGI / DSC images to PNG and rebuild them after editing, preserving image origins and other metadata |
| Archives | Extract, repack, and list ARC archives |
| SDC Tools | Decompress and compress SDC files or folders |
| DSC Tools | Decompress and compress DSC files or folders |
| BM animations | Export PNG frames or video; create BM files from image sequences or video |
| Audio | Convert between BGI BW, WAV, OGG, MP3, FLAC, and other audio formats |
| Data and definitions | Convert GDB saves and ScenarioDB / StringsDB files to and from JSON; convert animation definitions to and from CSV |

Choose a tool from the left sidebar, set the input and output paths, then run the operation. Progress and results appear in the log below.

To edit script text, export it as JSON or TXT first. After editing, use the import function to rebuild the script. Keep the small `.meta.json` file beside the translation. Importing edits also requires the matching original script. The sidecar stores only the extraction rules and entry count. Import uses those rules to locate the text again and checks the count; it does not check the original file's SHA-256.

Japanese scripts use CP932 by default.

The database tool automatically detects 64-byte and 128-byte ScenarioDB records and preserves the original format when rebuilding. In the 64-byte format, entries correspond to `Scenario0000`, `Scenario0001`, and so on by their position. Keep the entry order when editing.

The controls at the bottom left let you switch between Simplified Chinese and English and choose a light, dark, or system theme.

Black-background removal for BM animations is off by default. Enable it in the transparency and quality settings when needed.

Some system functions appear as `f_XXX` identifiers. Text extraction and import use the original function IDs and do not depend on their display names.

## Command line

For batch processing, use `BGI_Toolkit_cli.exe`. Run this command in a terminal to see the available commands:

```powershell
.\BGI_Toolkit_cli.exe --help
```

## Source code and licenses

Project source code is included in the `source` folder. C / C++ source files and build scripts for both native DLLs are in `source/bgi_toolkit/native`.

Third-party license information is included in the `licenses` folder and `THIRD_PARTY_NOTICES.md`.
