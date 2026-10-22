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

The controls at the bottom left let you switch between Simplified Chinese and English and choose a light, dark, or system theme.

## Command line

For batch processing, use `BGI_Toolkit_cli.exe`. Run this command in a terminal to see the available commands:

```powershell
.\BGI_Toolkit_cli.exe --help
```

## Acknowledgements

Thanks to the following open-source projects for their references and assistance:

- [GARbro](https://github.com/morkt/GARbro)
- [openbgi](https://github.com/Cytlan/openbgi)
- [Bgi_asdis](https://github.com/KlparetlR/Bgi_asdis)

## Source code and licenses

Project source code is included in the `source` folder. C / C++ source files and build scripts for both native DLLs are in `source/bgi_toolkit/native`.

Third-party license information is included in the `licenses` folder and `THIRD_PARTY_NOTICES.md`.
