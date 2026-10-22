"""Exercise the shipped runtime without relying on an installed Python."""
from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import traceback
import wave


def _square(value):
    return value * value


def run(report_path=None):
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    checks = {}

    def check(name, function):
        started = time.monotonic()
        try:
            detail = function()
            checks[name] = {'ok': True, 'seconds': round(time.monotonic() - started, 3), 'detail': detail}
        except Exception:
            checks[name] = {'ok': False, 'error': traceback.format_exc()}

    import bgi_toolkit
    package = Path(bgi_toolkit.__file__).parent

    def mapping():
        from bgi_toolkit.function_mapping import load_mapping
        names = load_mapping(package / 'asdis/resources/bss_mapping.json')
        assert len(names) == 56, len(names)
        assert 0x140 in names and 0x080 not in names
        assert (package / 'asdis/resources/bss_mapping_v0.json').is_file()
        if getattr(sys, 'frozen', False):
            assert package.is_relative_to(Path(sys._MEIPASS))
        return {'entries': len(names), 'package': str(package)}

    def native():
        from bgi_toolkit.native_codec import transform
        from bgi_toolkit.bm_codec import _load_decoder_lib
        for bpp in (24, 32):
            raw = bytes((i * 13) % 256 for i in range(8 * 8 * (bpp // 8)))
            encoded = transform(1, raw, width=8, height=8, bpp=bpp, header=bytes(48))
            assert transform(0, encoded) == raw
        assert _load_decoder_lib() is not None
        return 'CBG 24/32-bit round trips; both native DLLs loaded'

    def scripts():
        from bgi_toolkit.asdis.service import AsdisOptions, assemble_story, disassemble_story, detect_bsd_version
        from bgi_toolkit.asdis.common.text_scan import scan_file
        from bgi_toolkit.asdis.common.bgi_dialog_json import extract_dialog_json_from_script, import_dialog_json_to_script
        source = ('push_dword(1);\npush_dword(1);\npush_dword(0);\n'
                  'push_string("アリス");\npush_string("本文");\nmsg_::f_140();\nret();\n')
        with tempfile.TemporaryDirectory(prefix='bgi_release_脚本_') as folder:
            root = Path(folder)
            original, binary, assembly = root / 'input.bsd', root / 'compiled', root / 'output.bsd'
            original.write_text(source, encoding='utf-8')
            assert detect_bsd_version(original) == 'v1'
            options = AsdisOptions(script_version='v1')
            assemble_story(original, binary, options)
            disassemble_story(binary, assembly, options, use_function_names=False)
            result = scan_file(0, binary, options, '')
            assert result['error'] is None, result
            assert any(group['function'] == 'f_140' for group in result['groups'])
            assert '本文' in assembly.read_text(encoding='utf-8')
            assert options.encoding == 'cp932'
            translation = root / 'dialog.json'
            extract_dialog_json_from_script(binary, translation)
            metadata = json.loads(Path(str(translation)+'.meta.json').read_text(encoding='utf-8'))
            assert metadata == {'format': 'bgi-dialog-metadata', 'version': 2,
                                'rules': [], 'entry_count': 1}, metadata
            original.write_text(source.replace('本文', '別の本文'), encoding='utf-8')
            assemble_story(original, binary, options)
            # Old sidecars must also ignore their former source fingerprint.
            metadata['source_sha256'] = 'obsolete'
            Path(str(translation)+'.meta.json').write_text(json.dumps(metadata), encoding='utf-8')
            import_dialog_json_to_script(binary, translation, root/'rebuilt')
            disassemble_story(root/'rebuilt', assembly, options, use_function_names=False)
            assert 'push_string("本文");' in assembly.read_text(encoding='utf-8')
        from bgi_toolkit.bsd2c import pipeline as bsd_pipeline
        from bgi_toolkit.bpd2c import pipeline as bpd_pipeline
        from bgi_toolkit.bsd2c.asm_parser import parse_assembly_text
        from bgi_toolkit.bsd2c.lifter import lift_module
        from bgi_toolkit.bsd2c.emitter import emit_module
        internal = ('line("main.bss", 1);\npush_string("章の名前");\npush_dword(10);\n'
                    'push_offset(L4bc4c);\ncall();\nret();\nL4bc4c:\n'
                    'load_base();\npush_dword(12);\nadd();\nstore_base();\n'
                    'push_base_offset(4);\nmove_arg(2);\npush_base_offset(8);\nmove_arg(2);\n'
                    'push_base_offset(8);\nload(2);\nf_SetSceneName();\nret();\n')
        recovered = lift_module(parse_assembly_text(internal))
        rendered = emit_module(recovered)
        assert len(recovered.functions) == 2
        assert 'L4bc4c("章の名前", 10);' in rendered and 'f_SetSceneName(arg0);' in rendered
        return 'v1 assemble/disassemble, dialog scan, BSD internal functions and argument binding; BPD pipeline imported'

    def compression():
        from bgi_toolkit.gdb_tool import _encode_sdc
        from bgi_toolkit.resource_codec import encode_dsc
        from bgi_toolkit.sdc_tool import run_sdc_task
        with tempfile.TemporaryDirectory(prefix='bgi_release_compression_') as folder:
            root = Path(folder)
            payload = b'known script bytes' * 30
            for kind in ('sdc', 'dsc'):
                packed, plain, rebuilt = root/kind, root/(kind+'.plain'), root/(kind+'.rebuilt')
                packed.write_bytes(_encode_sdc(payload, 123, b'') if kind == 'sdc' else encode_dsc(payload))
                result = run_sdc_task('decompress', packed, plain, format_filter=kind, log=lambda _:None)
                assert result.succeeded == 1 and not result.errors, result
                assert plain.read_bytes() == payload
                result = run_sdc_task('compress', plain, rebuilt, output_format=kind, log=lambda _:None)
                assert result.succeeded == 1 and not result.errors, result
                assert rebuilt.read_bytes().startswith(kind.upper().encode()+b' FORMAT 1.00\0')
        return 'separate SDC / DSC decompression and recompression'

    def databases():
        from bgi_toolkit.db_text_tool import decode_db, encode_db, infer_kind, run_db_task
        labels = ['タユタマ_製品版', '日本語のラベル' * 3]
        compact = struct.pack('<I', len(labels)) + b''.join(
            text.encode('cp932').ljust(64, b'\0') for text in labels)
        assert infer_kind(compact) == 'ScenarioDB'
        document = decode_db(compact, 'ScenarioDB')
        assert document['record_size'] == 64 and document['records'] == [{'label': label} for label in labels]
        assert encode_db(document) == compact
        with tempfile.TemporaryDirectory(prefix='bgi_release_databases_') as folder:
            root = Path(folder)
            source = root/'input'
            source.mkdir()
            (source/'ScenarioDB').write_bytes(compact)
            modern = {'format': 'ScenarioDB', 'version': 1, 'encoding': 'cp932',
                      'records': [{'resource': 'scene.bs', 'label': '本文'}]}
            strings = {'format': 'StringsDB', 'version': 1, 'encoding': 'cp932',
                       'records': [{'key': [1, 0, 0, 0], 'text': 'bitmap'}]}
            (source/'ScenarioDB__2').write_bytes(encode_db(modern))
            (source/'StringsDB').write_bytes(encode_db(strings))
            exported = run_db_task('export', source, root/'json', log=lambda _: None)
            assert exported.succeeded == 3 and not exported.errors, exported
            rebuilt = run_db_task('build', root/'json', root/'rebuilt', log=lambda _: None)
            assert rebuilt.succeeded == 3 and not rebuilt.errors, rebuilt
            for path in source.iterdir():
                assert (root/'rebuilt'/path.name).read_bytes() == path.read_bytes()
        return '64/128-byte ScenarioDB and StringsDB export/rebuild byte-for-byte round trips'

    def acceleration():
        import cv2
        import numpy as np
        from bgi_toolkit import bm_codec
        assert bm_codec.prepare_numba() is not None, bm_codec._numba_error
        assert bm_codec.float_to_byte_jit(270.0) == 255
        assert bm_codec.float_to_byte_jit(-5.0) == 0
        assert bm_codec.float_to_short_jit(8.0) == 129.0
        image = np.zeros((8, 8, 3), dtype=np.uint8)
        assert cv2.cvtColor(image, cv2.COLOR_BGR2GRAY).shape == (8, 8)
        return 'Numba JIT and OpenCV executed'

    def processes():
        with ProcessPoolExecutor(max_workers=2) as executor:
            assert list(executor.map(_square, [2, 3, 4])) == [4, 9, 16]
        return 'spawned process pool returned results'

    def audio():
        from bgi_toolkit.audio_tool import AudioOptions, run_audio_task, audio_info
        executable = shutil.which('ffmpeg')
        assert executable
        if getattr(sys, 'frozen', False):
            assert Path(executable).is_relative_to(Path(sys._MEIPASS))
        with tempfile.TemporaryDirectory(prefix='bgi_release_audio_') as folder:
            root = Path(folder)
            source, output = root / 'tone.wav', root / 'tone.ogg'
            with wave.open(str(source), 'wb') as stream:
                stream.setparams((1, 2, 44100, 0, 'NONE', 'not compressed'))
                stream.writeframes(struct.pack('<4410h', *([0] * 4410)))
            result = run_audio_task(source, output, options=AudioOptions(format='ogg', bitrate=96))
            assert not result.errors and result.succeeded == 1, result
            assert output.read_bytes().startswith(b'OggS')
        return {'ffmpeg': executable, 'conversion': 'WAV to OGG'}

    def gui():
        from PyQt6.QtCore import QPoint, QStandardPaths, QTimer
        from PyQt6.QtWidgets import QApplication, QScrollArea
        from bgi_toolkit.gui.app import MainWindow
        from bgi_toolkit.i18n import load_catalog
        QStandardPaths.setTestModeEnabled(True)
        app = QApplication.instance() or QApplication([])
        app.setApplicationName('BGI Toolkit Release Check')
        app.setOrganizationName('BGI Toolkit')
        app.setStyle('Fusion')
        window = MainWindow()
        assert len(window.tool_windows) == 13
        assert not window.bm_window.remove_black_bg_checkbox.isChecked()
        assert not window.bm_window.remove_black_bg_32_checkbox.isChecked()
        assert len(load_catalog('en')) > 100
        window.show()
        for index in range(len(window.tool_windows)):
            window.tool_tabs.setCurrentIndex(index)
            app.processEvents()
            tool = window.tool_windows[index]
            assert window.tool_stack.currentWidget() is tool
            tabs = getattr(tool, 'task_tabs', None) or tool.content_tabs
            page = tabs.currentWidget()
            scroll = page if isinstance(page, QScrollArea) else page.findChild(QScrollArea)
            assert scroll is not None, index
            position = tabs.tabBar().mapTo(window, QPoint(0, 0))
            scroll.verticalScrollBar().setValue(scroll.verticalScrollBar().maximum())
            app.processEvents()
            assert tabs.tabBar().mapTo(window, QPoint(0, 0)) == position
            assert tabs.tabBar().isVisibleTo(window)
        QTimer.singleShot(150, app.quit)
        assert app.exec() == 0
        window.close()
        app.processEvents()
        return 'all 13 tools selected with fixed tab bars; Qt event loop and English catalog loaded'

    for name, function in [('mapping', mapping), ('native', native), ('scripts', scripts), ('compression', compression), ('databases', databases),
                           ('acceleration', acceleration), ('process_pool', processes),
                           ('audio', audio), ('gui', gui)]:
        check(name, function)
    report = {'ok': all(row['ok'] for row in checks.values()), 'frozen': getattr(sys, 'frozen', False),
              'executable': sys.executable, 'checks': checks}
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if report_path:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(rendered + '\n', encoding='utf-8')
    print(rendered)
    return 0 if report['ok'] else 1
