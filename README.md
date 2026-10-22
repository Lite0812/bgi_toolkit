# BGI Toolkit

**简体中文** | [English](README_EN.md)

用于提取和编辑 BGI 游戏的脚本、文本、图片、封包、动画、音频和存档。

本包适用于 64 位 Windows，已包含运行环境和 FFmpeg，不用另装 Python。

## 打开程序

解压后双击 `BGI_Toolkit.exe`，也可以双击 `run_gui.bat`。

请保留整个文件夹。移动或分享时，把 `_internal` 文件夹一起带上，单独复制 EXE 无法运行。

## 主要功能

| 内容 | 支持的操作 |
| --- | --- |
| 剧情和系统脚本 | 提取、构建，导出 JSON / TXT 文本，修改后导回 |
| BSD / BPD 反编译 | 查看脚本逻辑，输出 C；BSD 还支持 Ren’Py 输出 |
| 图片 | CBG / BGI / DSC 图片转 PNG，修改后重新生成，保留图片基点等信息 |
| 封包 | ARC 解包、封包和文件列表查看 |
| SDC 工具 | SDC 解压与压缩，支持单文件和目录 |
| DSC 工具 | DSC 解压与压缩，支持单文件和目录 |
| BM 动画 | 导出 PNG 帧或视频，用图片序列或视频生成 BM |
| 音频 | BGI BW 与 WAV、OGG、MP3、FLAC 等格式互转 |
| 数据与定义 | GDB 存档、ScenarioDB / StringsDB 与 JSON 互转，动画定义与 CSV 互转 |

在左侧选择工具，填好输入文件和输出位置，再执行对应操作。处理进度和结果会显示在下方日志里。

如果要修改剧本文本，先导出 JSON 或 TXT，改好后再用导回功能生成脚本。导回需要对应的原剧本，译文旁的小型 `.meta.json` 请一起保留。它只保存提取规则和条目数量；导回时按这些规则重新定位并核对数量，不校验原文件的 SHA-256。

日文剧本默认使用 CP932 编码。

数据库工具会自动识别 ScenarioDB 的 64 字节和 128 字节格式，并按原格式重建。64 字节格式按条目顺序对应 `Scenario0000`、`Scenario0001` 等资源，编辑时请保留条目顺序。

界面左下角可以切换简体中文和 English，也可以选择明亮、深色或跟随系统的主题。

BM 去黑底默认关闭，需要时可在“透明与画质”中手动打开。

部分系统函数会显示为 `f_XXX` 编号。文本提取和导回按原始编号识别，不依赖显示名称。

## 命令行

需要批量处理时，可以使用 `BGI_Toolkit_cli.exe`。在终端中运行下面的命令查看用法：

```powershell
.\BGI_Toolkit_cli.exe --help
```

## 源码与许可

项目源码放在 `source` 文件夹。两个原生 DLL 的 C / C++ 源码和编译脚本都在 `source/bgi_toolkit/native`。

第三方组件的许可说明见 `licenses` 文件夹和 `THIRD_PARTY_NOTICES.md`。
