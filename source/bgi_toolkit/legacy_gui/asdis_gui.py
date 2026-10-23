from ..gui.localized_ui import ui_call, ui_widget, ui_slot, source_text
from ..i18n import tr
import sys
import os
import glob
import io
import struct
import configparser
import contextlib
import tempfile
import traceback
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
                             QPushButton, QLabel, QComboBox, QFrame, QMessageBox,
                             QCheckBox, QLineEdit, QTabWidget, QTextEdit, QFileDialog, QStackedWidget,
                             QButtonGroup, QRadioButton, QSplitter, QSizePolicy)
from PyQt6.QtCore import QStandardPaths, Qt, QThread, pyqtSignal
from PyQt6.QtGui import QTextCursor

if sys.platform.startswith('win'):
    try:
        import winreg
    except ImportError:
        winreg = None
else:
    winreg = None

try:
    import darkdetect
    HAS_DARKDETECT = True
except ImportError:
    HAS_DARKDETECT = False

from ..gui.paths import PathLineEdit
from pathlib import Path
from ..detector import iter_inputs, non_story_notice, detect_input, NON_STORY_RESOURCES
from ..models import InputKind
from ..asdis.bp import bpas, bpdis, bpop
from ..asdis.common.bgi_dialog_json import (
    extract_dialog_json_from_bsd,
    extract_push_string_json_from_bpd,
    import_dialog_json_to_bsd,
    import_push_string_json_to_bpd,
)
from ..asdis.common.bgi_dialog_txt import (
    extract_dialog_txt_from_bsd,
    extract_push_string_txt_from_bpd,
    import_dialog_txt_to_bsd,
    import_push_string_txt_to_bpd,
)
from ..asdis.v0 import bgias_v0, bgidis_v0, bgiop_v0
from ..asdis.v1 import bgias, bgidis, bgiop

class StreamRedirector(object):
    def __init__(self, signal=None):
        self.signal = signal
        self._original_stdout = sys.__stdout__
        base_stdout = sys.__stdout__ if sys.__stdout__ else sys.stdout
        self._console_encoding = getattr(base_stdout, "encoding", None) or 'utf-8'
        self.encoding = self._console_encoding

    def write(self, text):
        if self.signal:
            try:
                msg = str(text)
                if msg:
                    self.signal.emit(msg)
            except:
                pass

        if self._original_stdout:
            try:
                if hasattr(self._original_stdout, 'buffer'):
                    pass
                self._original_stdout.write(text)
                self._original_stdout.flush()
            except Exception:
                pass

    def flush(self):
        if self._original_stdout:
            try:
                self._original_stdout.flush()
            except:
                pass

class CardFrame(QFrame):
    def __init__(self):
        super().__init__()
        self.setObjectName("CardFrame")
        self.setFrameShape(QFrame.Shape.NoFrame)

class ModernButton(QPushButton):
    def __init__(self, text, is_primary=False):
        super().__init__(text)
        ui_call(self, "setText", text)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setObjectName("PrimaryButton" if is_primary else "SecondaryButton")
        self.setMinimumHeight(30)

class DragDropLineEdit(PathLineEdit):
    def __init__(self, parent=None, is_folder=False):
        super().__init__(parent)
        self.is_folder = is_folder
        self.setAcceptDrops(True)
        ui_call(self, 'setPlaceholderText', tr('可直接拖入文件或文件夹...') if is_folder else tr('可直接拖入文件...'))

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        urls = event.mimeData().urls()
        if urls:
            path = urls[0].toLocalFile()
            if path:
                ui_call(self, 'setText', os.path.normpath(path))
            event.acceptProposedAction()

class WorkerThread(QThread):
    log_signal = pyqtSignal(str)
    finished_signal = pyqtSignal(bool, str)

    def __init__(self, mode, input_path, output_path, encoding, dialog_path='', source_encoding='', script_version='auto', user_function_names='', bp_engine='auto'):
        super().__init__()
        self.mode = mode
        self.input_path = input_path
        self.output_path = output_path
        self.encoding = encoding
        self.dialog_path = dialog_path
        self.source_encoding = source_encoding or encoding
        self.script_version = (script_version or 'auto').strip().lower()
        self.user_function_names = user_function_names or ''
        self.bp_engine = bp_engine
        if self.script_version not in ('auto', 'v0', 'v1'):
            self.script_version = 'auto'
        self.is_running = True
        self._batch_stats = {}
        self._reset_batch_stats()

    def _v0_mapping_path(self):
        return os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "asdis",
            "resources",
            "bss_mapping_v0.json",
        )

    def _detect_bsd_version(self, bsd_path):
        with open(bsd_path, "r", encoding="utf-8", errors="replace") as f:
            for raw in f:
                line = raw.strip()
                if not line:
                    continue
                if line.startswith("//") or line.startswith(";"):
                    continue
                if line.startswith("#v0"):
                    return "v0"
                if line.startswith("#header") or line.startswith("#import") or line.startswith("#extra_import") or line.startswith("#define"):
                    return "v1"
                if line.startswith("f_"):
                    return "v0"
        return "v1"

    def _resolve_script_version(self, script_path):
        if str(script_path).lower().endswith(('.bsd', '.bsd.txt')):
            return self._resolve_bsd_version(script_path)
        if self.script_version in ('v0', 'v1'):
            return self.script_version
        return self._detect_script_version(script_path)

    def _resolve_bsd_version(self, bsd_path):
        if self.script_version in ('v0', 'v1'):
            return self.script_version
        return self._detect_bsd_version(bsd_path)

    def _reset_batch_stats(self):
        self._batch_stats = {
            "processed": 0,
            "success": 0,
            "failed": 0,
            "skipped": 0,
            "skipped_types": {},
            "v0": 0,
            "v1": 0,
            "unknown_ops": {}
        }

    def _record_skipped_resource(self, path, kind=None):
        kind = kind or detect_input(path)
        label = tr(NON_STORY_RESOURCES.get(kind, (kind.value, ''))[0])
        self._batch_stats['skipped'] += 1
        groups = self._batch_stats['skipped_types']
        groups[label] = groups.get(label, 0) + 1

    def _skipped_resource_description(self):
        return '、'.join(tr('{0} {1} 个', name, count) for name, count in sorted(self._batch_stats['skipped_types'].items()))

    def _record_unknown_ops(self, file_path, unknown_lines):
        if not unknown_lines:
            return
        norm = os.path.normpath(file_path)
        bucket = self._batch_stats["unknown_ops"].setdefault(norm, set())
        for line in unknown_lines:
            if line:
                bucket.add(line.strip())

    def _extract_unknown_op_lines(self, text):
        found = []
        for raw in (text or "").splitlines():
            line = raw.strip()
            if not line:
                continue
            low = line.lower()
            if "unknown op" in low or "unknown v0 opcode" in low or "未知 v0 opcode" in low:
                found.append(line)
        return found

    def _run_with_capture(self, func, *args, **kwargs):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            result = func(*args, **kwargs)
        output = buf.getvalue()
        if output:
            print(output, end="" if output.endswith("\n") else "\n")
        return result, output

    def _disassemble_script_to_bsd(self, infile, out_bsd, version, encoding_for_dis=None):
        dis_enc = encoding_for_dis or self.encoding
        use_names = self.mode not in ('json_extract', 'txt_extract', 'json_import', 'txt_import')
        if not use_names and str(infile).lower().endswith(('.bsd', '.bsd.txt')):
            with open(infile, 'r', encoding='utf-8') as source:
                text = source.read()
            with open(out_bsd, 'w', encoding='utf-8') as destination:
                destination.write(text)
            return
        if version == "v0":
            try:
                bgidis_v0.disassemble_file(
                    infile,
                    output_path=out_bsd,
                    encoding=dis_enc,
                    mapping_path=self._v0_mapping_path(),
                    use_function_names=use_names,
                    exact_mode=False
                )
            except Exception as e:
                message = str(e)
                unknown = []
                if any(marker in message.casefold() for marker in ('unknown v0 opcode', '未知 v0 opcode')):
                    unknown.append(message)
                self._record_unknown_ops(infile, unknown)
                raise
            return
        _, output = self._run_with_capture(
            bgidis.dis,
            infile,
            encoding=dis_enc,
            output_path=out_bsd,
            use_function_names=use_names,
        )
        unknown = self._extract_unknown_op_lines(output)
        if unknown:
            self._record_unknown_ops(infile, unknown)
            raise Exception(tr('检测到 Unknown op，已标记为失败: {0}', unknown[0]))

    def _disassemble_bp_to_bpd(self, infile, out_bpd, encoding_for_dis=None, exact_mode=False):
        dis_enc = encoding_for_dis or self.encoding
        detection, _ = self._run_with_capture(
            bpdis.dis,
            infile,
            debug=False,
            exact_mode=exact_mode,
            encoding=dis_enc,
            output_path=out_bpd,
            engine=self.bp_engine,
        )
        self.log_signal.emit(detection.describe())

    def _assemble_bpd_to_bp(self, in_bpd, out_bp, encoding_for_asm=None):
        asm_enc = encoding_for_asm or self.encoding
        self._run_with_capture(
            bpas.asm,
            in_bpd,
            encoding=asm_enc,
            output_path=out_bp,
            engine=self.bp_engine,
        )

    def _assemble_bsd_to_script(self, in_bsd, out_script, version):
        if version == "v0":
            bgias_v0.assemble(
                in_bsd,
                output_path=out_script,
                mapping_path=self._v0_mapping_path(),
                encoding_override=self.encoding
            )
            return
        bgias.asm(in_bsd, encoding=self.encoding, output_path=out_script)

    def _print_batch_summary(self):
        stats = self._batch_stats
        print(tr('处理汇总:'))
        print(tr('  文件总数: {0}', stats['processed']))
        print(tr('  解析成功: {0}', stats['success']))
        print(tr('  解析失败: {0}', stats['failed']))
        print(tr('  跳过非剧本资源: {0}', stats['skipped']))
        for name, count in sorted(stats['skipped_types'].items()):
            print(tr('    - {0}: {1} 个', name, count))
        if not self.mode.startswith('bp_'):
            print(tr('  v0 脚本数: {0}', stats['v0']))
            print(tr('  v1 脚本数: {0}', stats['v1']))
        unknown_files = stats["unknown_ops"]
        total_unknown = sum(len(v) for v in unknown_files.values())
        if total_unknown or not self.mode.startswith('bp_'):
            print(tr('  Unknown op 条目数: {0}', total_unknown))
        if unknown_files:
            print(tr('Unknown op 明细:'))
            for file_path in sorted(unknown_files.keys()):
                for line in sorted(unknown_files[file_path]):
                    print(f"  - {file_path}: {line}")

    def _try_parse_v1_headerless(self, data):
        try:
            encoding = bgiop.normalize_encoding(self.encoding)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                inst, _, _, _, _, _, _, _, _, size, _ = bgidis.parse(data, b"", encoding, "gbk")
            if self._extract_unknown_op_lines(buf.getvalue()):
                return False, 0, 0.0
            if not inst:
                return False, 0, 0.0
            if size <= 0 or size > len(data):
                return False, 0, 0.0
            coverage = float(size) / float(max(1, len(data)))
            if len(inst) < 3 and coverage < 0.05:
                return False, 0, 0.0
            return True, len(inst), coverage
        except Exception:
            return False, 0, 0.0

    def _try_parse_v0(self, data):
        try:
            code_end = bgidis_v0.estimate_code_end(data)
            if code_end <= 0 or code_end > len(data):
                return False, 0, 0.0
            pos = 0
            insn_count = 0
            template_hints = {}
            while pos < code_end:
                try:
                    _, _, _, next_pos, _, _ = bgidis_v0.parse_instruction(data, pos, set(), -1, template_hints=template_hints)
                except Exception:
                    break
                if next_pos <= pos:
                    break
                pos = next_pos
                insn_count += 1
                if insn_count > 1000000:
                    break
            if insn_count == 0:
                return False, 0, 0.0
            coverage = float(code_end) / float(max(1, len(data)))
            if insn_count < 3 and coverage < 0.05:
                return False, 0, 0.0
            return True, insn_count, coverage
        except Exception:
            return False, 0, 0.0

    def _detect_script_version(self, script_path):
        with open(script_path, "rb") as f:
            data = f.read()
        if len(data) >= 0x20 and data.startswith(b"BurikoCompiledScriptVer1.00\x00"):
            return "v1"
        if len(data) >= 4:
            op32 = struct.unpack_from("<I", data, 0)[0]
        else:
            op32 = None
        if len(data) >= 2:
            op16 = struct.unpack_from("<H", data, 0)[0]
        else:
            op16 = None
        v1_hint = op32 in bgiop.ops if op32 is not None else False
        v0_hint = ((op16 in bgiop_v0.OPERAND_TEMPLATES) or (op16 in bgiop_v0.SPECIAL_OPS)) if op16 is not None else False
        v1_ok, v1_insn, v1_cov = self._try_parse_v1_headerless(data)
        v0_ok, v0_insn, v0_cov = self._try_parse_v0(data)
        if v1_ok and not v0_ok:
            return "v1"
        if v0_ok and not v1_ok:
            return "v0"
        if v1_ok and v0_ok:
            v1_weak = v1_insn < 8 or v1_cov < 0.10
            v0_weak = v0_insn < 8 or v0_cov < 0.10
            if v1_weak and not v0_weak:
                return "v0"
            if v0_weak and not v1_weak:
                return "v1"
            if v0_cov > v1_cov + 0.08:
                return "v0"
            if v1_cov > v0_cov + 0.08:
                return "v1"
            if v0_insn >= v1_insn:
                return "v0"
            return "v1"
        if v0_hint and not v1_hint:
            return "v0"
        if v1_hint and not v0_hint:
            return "v1"
        return "v1"

    def run(self):
        original_stdout = sys.stdout
        original_stderr = sys.stderr
        redirector = StreamRedirector(self.log_signal)
        sys.stdout = redirector
        sys.stderr = redirector

        try:
            self.reset_decode_fallback_stats()
            self._reset_batch_stats()
            if self._is_script_input_mode() and not os.path.isdir(self.input_path):
                notice = non_story_notice(self.input_path)
                if notice:
                    self._record_skipped_resource(self.input_path)
                    self.log_signal.emit(notice)
                    self.finished_signal.emit(False, notice)
                    return
            if os.path.isdir(self.input_path):
                if not self.output_path:
                    self.output_path = os.path.normpath(self.input_path) + self._get_directory_suffix()
                self.process_directory()
            else:
                out = self.output_path
                if not out:
                    out = self._default_single_output(self.input_path)

                if out and os.path.isdir(out):
                    fname = self._mode_output_name(self.input_path)
                    out = os.path.join(out, fname)

                if out:
                    out_dir = os.path.dirname(out)
                    if out_dir:
                        os.makedirs(out_dir, exist_ok=True)

                if self.mode in ('json_import', 'txt_import', 'bp_json_import', 'bp_txt_import'):
                    dialog_file = self.dialog_path
                    if not dialog_file:
                        need_type = "JSON" if self.mode in ('json_import', 'bp_json_import') else "TXT"
                        raise Exception(tr('请先选择 {0} 输入路径', need_type))
                    self._batch_stats["processed"] += 1
                    self.process_single_file(self.input_path, out, dialog_file)
                    self._batch_stats["success"] += 1
                else:
                    self._batch_stats["processed"] += 1
                    self.process_single_file(self.input_path, out)
                    self._batch_stats["success"] += 1

            if self._batch_stats['skipped']:
                success = bool(self._batch_stats['success'])
                message = tr('完成 {0} 个剧本，跳过 {1} 个非剧本资源（{2}）；详情见日志。', self._batch_stats['success'], self._batch_stats['skipped'], self._skipped_resource_description())
                self.finished_signal.emit(success, message)
            else:
                self.finished_signal.emit(True, tr('任务完成'))
        except Exception as e:
            self._batch_stats["failed"] += 1
            self.log_signal.emit(f"\nError: {traceback.format_exc()}")
            self.finished_signal.emit(False, str(e))
        finally:
            self._print_batch_summary()
            self.print_decode_fallback_summary()
            sys.stdout = original_stdout
            sys.stderr = original_stderr

    def _get_directory_suffix(self):
        if self.mode in ('disassemble', 'json_extract', 'txt_extract', 'bp_disassemble', 'bp_json_extract', 'bp_txt_extract'):
            return "_out"
        if self.mode in ('assemble', 'json_import', 'txt_import', 'bp_assemble', 'bp_json_import', 'bp_txt_import'):
            return "_build"
        return "_out"

    def _is_script_input_mode(self):
        return self.mode in ('disassemble', 'json_import', 'json_extract', 'txt_import', 'txt_extract')

    def _is_bp_input_mode(self):
        return self.mode in ('bp_disassemble', 'bp_json_extract', 'bp_txt_extract', 'bp_json_import', 'bp_txt_import')

    def _mode_output_name(self, file_path):
        base = os.path.splitext(os.path.basename(file_path))[0]
        if self.mode == 'disassemble':
            return base + ".bsd"
        if self.mode == 'json_extract':
            return base + ".json"
        if self.mode == 'txt_extract':
            return base + ".txt"
        if self.mode == 'bp_disassemble':
            return base + ".bpd"
        if self.mode == 'bp_json_extract':
            return base + ".json"
        if self.mode == 'bp_txt_extract':
            return base + ".txt"
        if self.mode == 'bp_assemble':
            return base + "._bp"
        if self.mode in ('json_import', 'txt_import', 'bp_json_import', 'bp_txt_import'):
            return os.path.basename(self._default_single_output(file_path))
        return base

    def _default_single_output(self, file_path):
        base_path = os.path.splitext(file_path)[0]
        if self.mode == 'disassemble':
            return base_path + ".bsd"
        if self.mode == 'json_extract':
            return base_path + ".json"
        if self.mode == 'txt_extract':
            return base_path + ".txt"
        if self.mode == 'json_import':
            return base_path + "_jsonimp"
        if self.mode == 'txt_import':
            return base_path + "_txtimp"
        if self.mode == 'bp_disassemble':
            return base_path + ".bpd"
        if self.mode == 'bp_json_extract':
            return base_path + ".json"
        if self.mode == 'bp_txt_extract':
            return base_path + ".txt"
        if self.mode == 'bp_assemble':
            return base_path + "._bp"
        if self.mode == 'bp_json_import':
            return base_path + "._bp"
        if self.mode == 'bp_txt_import':
            return base_path + "._bp"
        return base_path

    def _collect_files(self):
        if self._is_script_input_mode():
            accepted = {InputKind.STORY_BINARY}
            if self.mode != 'disassemble':
                accepted.add(InputKind.BSD)
            def skipped(path, kind):
                self._record_skipped_resource(path, kind)
                self.log_signal.emit(non_story_notice(path, kind))
            return [str(path) for path in iter_inputs(self.input_path, accepted, on_skip=skipped)]
        files = []
        for root, _, filenames in os.walk(self.input_path):
            for name in filenames:
                _, ext = os.path.splitext(name)
                ext_lower = ext.lower()
                if self.mode == 'assemble':
                    if ext_lower == '.bsd':
                        files.append(os.path.join(root, name))
                    continue
                if self.mode == 'bp_assemble':
                    if ext_lower == '.bpd':
                        files.append(os.path.join(root, name))
                    continue
                if self.mode in ('bp_disassemble', 'bp_json_extract', 'bp_txt_extract', 'bp_json_import', 'bp_txt_import'):
                    if ext_lower == '._bp':
                        files.append(os.path.join(root, name))
                    continue
                text_mode = self.mode in ('json_extract', 'txt_extract', 'json_import', 'txt_import')
                if self._is_script_input_mode() and (ext_lower in ('', '._bs') or (text_mode and ext_lower == '.bsd')):
                    files.append(os.path.join(root, name))
        return files

    def reset_decode_fallback_stats(self):
        if hasattr(bgidis, 'reset_decode_fallback_stats'):
            bgidis.reset_decode_fallback_stats()
        if hasattr(bgiop, 'reset_decode_fallback_stats'):
            bgiop.reset_decode_fallback_stats()

    def _merge_decode_fallback_stats(self):
        merged = {
            'fallback_count': 0,
            'fallback_by_encoding': {},
            'replace_primary_count': 0,
            'replace_utf8_count': 0
        }
        providers = []
        if hasattr(bgidis, 'get_decode_fallback_stats'):
            providers.append(bgidis.get_decode_fallback_stats)
        if hasattr(bgiop, 'get_decode_fallback_stats'):
            providers.append(bgiop.get_decode_fallback_stats)
        for getter in providers:
            stats = getter() or {}
            merged['fallback_count'] += int(stats.get('fallback_count', 0) or 0)
            merged['replace_primary_count'] += int(stats.get('replace_primary_count', 0) or 0)
            merged['replace_utf8_count'] += int(stats.get('replace_utf8_count', 0) or 0)
            by_enc = stats.get('fallback_by_encoding', {}) or {}
            for enc, cnt in by_enc.items():
                merged['fallback_by_encoding'][enc] = merged['fallback_by_encoding'].get(enc, 0) + int(cnt or 0)
        return merged

    def print_decode_fallback_summary(self):
        stats = self._merge_decode_fallback_stats()
        has_fallback = (
            stats['fallback_count'] > 0
            or stats['replace_primary_count'] > 0
            or stats['replace_utf8_count'] > 0
        )
        if not has_fallback:
            return
        if stats['fallback_by_encoding']:
            sorted_items = sorted(stats['fallback_by_encoding'].items(), key=lambda x: x[1], reverse=True)
            enc_text = '，'.join([f"{enc}({cnt})" for enc, cnt in sorted_items])
        else:
            enc_text = tr('未知')
        print(tr('[编码提示] 本次批量处理中检测到字符串解码回退 {0} 次，可能编码: {1}', stats['fallback_count'], enc_text))
        if stats['replace_primary_count'] > 0:
            print(tr('[编码提示] 严格解码全部失败后，使用主编码 replace {0} 次', stats['replace_primary_count']))
        if stats['replace_utf8_count'] > 0:
            print(tr('[编码提示] 主编码 replace 失败后，使用 utf-8 replace {0} 次', stats['replace_utf8_count']))

    def process_directory(self):
        files = self._collect_files()
        total = len(files)

        print(tr('共发现 {0} 个文件待处理。', total))

        for i, fpath in enumerate(files):
            if not self.is_running:
                break

            try:
                if self.output_path:
                    rel_path = os.path.relpath(fpath, self.input_path)
                    base_rel = os.path.splitext(rel_path)[0]
                    suffix_map = {
                        'disassemble': '.bsd',
                        'json_extract': '.json',
                        'txt_extract': '.txt',
                        'bp_disassemble': '.bpd',
                        'bp_json_extract': '.json',
                        'bp_txt_extract': '.txt',
                        'bp_json_import': '._bp',
                        'bp_txt_import': '._bp',
                        'bp_assemble': '._bp'
                    }
                    rel_path = base_rel + suffix_map.get(self.mode, '')
                    out = os.path.join(self.output_path, rel_path)
                    os.makedirs(os.path.dirname(out), exist_ok=True)
                else:
                    out = self._default_single_output(fpath)

                self._batch_stats["processed"] += 1
                print(tr('[{0}/{1}] 正在处理: {2}', i + 1, total, os.path.basename(fpath)))
                if self.mode in ('json_import', 'txt_import', 'bp_json_import', 'bp_txt_import'):
                    if not self.dialog_path:
                        need_type = "JSON" if self.mode in ('json_import', 'bp_json_import') else "TXT"
                        raise Exception(tr('请先选择 {0} 输入路径', need_type))
                    if os.path.isdir(self.dialog_path):
                        rel_path = os.path.relpath(fpath, self.input_path)
                        base_rel = os.path.splitext(rel_path)[0]
                        ext = ".json" if self.mode in ('json_import', 'bp_json_import') else ".txt"
                        dialog_file = os.path.join(self.dialog_path, base_rel + ext)
                    else:
                        if total > 1:
                            raise Exception(tr('目录批处理导回时，文本输入路径必须是目录'))
                        dialog_file = self.dialog_path
                    self.process_single_file(fpath, out, dialog_file)
                else:
                    self.process_single_file(fpath, out)
                self._batch_stats["success"] += 1
            except Exception as e:
                self._batch_stats["failed"] += 1
                print(tr('处理失败 {0}: {1}', os.path.basename(fpath), e))
        print(tr('批量处理完成。'))

    def process_single_file(self, infile, outfile, dialog_file=None):
        if self._is_script_input_mode():
            notice = non_story_notice(infile)
            if notice:
                raise ValueError(notice)
        if self.mode == 'disassemble':
            if os.path.splitext(infile)[1].lower() == '.bsd':
                print(tr('跳过 {0}: .bsd 已是反汇编文本', os.path.basename(infile)))
                return
            version = self._resolve_script_version(infile)
            self._batch_stats[version] += 1
            if version == "v0":
                self._disassemble_script_to_bsd(infile, outfile, version, encoding_for_dis=self.encoding)
            else:
                self._disassemble_script_to_bsd(infile, outfile, version, encoding_for_dis=self.encoding)
            print(tr('已自动识别脚本版本: {0}', version.upper()))
        elif self.mode == 'assemble':
            version = self._resolve_bsd_version(infile)
            self._batch_stats[version] += 1
            self._assemble_bsd_to_script(infile, outfile, version)
            print(tr('已自动识别 BSD 版本: {0}', version.upper()))
        elif self.mode == 'bp_disassemble':
            if os.path.splitext(infile)[1].lower() == '.bpd':
                print(tr('跳过 {0}: .bpd 已是 BP 反汇编文本', os.path.basename(infile)))
                return
            self._disassemble_bp_to_bpd(infile, outfile)
            print(tr('BP 反汇编完成 -> {0}', outfile))
        elif self.mode == 'bp_json_extract':
            with tempfile.TemporaryDirectory(prefix='bgi_bp_json_extract_gui_') as td:
                temp_bpd = os.path.join(td, os.path.basename(infile) + '.bpd')
                self._disassemble_bp_to_bpd(infile, temp_bpd)
                count = extract_push_string_json_from_bpd(temp_bpd, outfile)
            print(tr('已提取 BP push_string {0} 条 -> {1}', count, outfile))
        elif self.mode == 'bp_txt_extract':
            with tempfile.TemporaryDirectory(prefix='bgi_bp_txt_extract_gui_') as td:
                temp_bpd = os.path.join(td, os.path.basename(infile) + '.bpd')
                self._disassemble_bp_to_bpd(infile, temp_bpd)
                count, units = extract_push_string_txt_from_bpd(temp_bpd, outfile)
            print(tr('已提取 BP push_string {0} 条，文本单元 {1} 条 -> {2}', count, units, outfile))
        elif self.mode == 'bp_assemble':
            self._assemble_bpd_to_bp(infile, outfile)
            print(tr('BP 构建完成 -> {0}', outfile))
        elif self.mode == 'json_extract':
            version = self._resolve_script_version(infile)
            self._batch_stats[version] += 1
            with tempfile.TemporaryDirectory(prefix='bgi_json_extract_gui_') as td:
                temp_bsd = os.path.join(td, os.path.basename(infile) + '.bsd')
                self._disassemble_script_to_bsd(infile, temp_bsd, version, encoding_for_dis=self.encoding)
                count = extract_dialog_json_from_bsd(
                    temp_bsd,
                    outfile,
                    user_function_names=self.user_function_names
                )
            print(tr('已提取对话 {0} 条 -> {1}', count, outfile))
            print(tr('文本定位元数据 -> {0}.meta.json', outfile))
        elif self.mode == 'txt_extract':
            version = self._resolve_script_version(infile)
            self._batch_stats[version] += 1
            with tempfile.TemporaryDirectory(prefix='bgi_txt_extract_gui_') as td:
                temp_bsd = os.path.join(td, os.path.basename(infile) + '.bsd')
                self._disassemble_script_to_bsd(infile, temp_bsd, version, encoding_for_dis=self.encoding)
                count, units = extract_dialog_txt_from_bsd(
                    temp_bsd,
                    outfile,
                    user_function_names=self.user_function_names
                )
            print(tr('已提取对话 {0} 条，文本单元 {1} 条 -> {2}', count, units, outfile))
            print(tr('文本定位元数据 -> {0}.meta.json', outfile))
        elif self.mode == 'json_import':
            if not dialog_file or not os.path.isfile(dialog_file):
                raise Exception(tr('JSON 文件不存在: {0}', dialog_file))
            version = self._resolve_script_version(infile)
            self._batch_stats[version] += 1
            with tempfile.TemporaryDirectory(prefix='bgi_json_import_gui_') as td:
                base = os.path.basename(infile)
                temp_src_bsd = os.path.join(td, base + '.src.bsd')
                temp_out_bsd = os.path.join(td, base + '.out.bsd')
                self._disassemble_script_to_bsd(infile, temp_src_bsd, version, encoding_for_dis=self.source_encoding)
                count, applied = import_dialog_json_to_bsd(
                    temp_src_bsd,
                    dialog_file,
                    temp_out_bsd,
                    user_function_names=self.user_function_names
                )
                self._assemble_bsd_to_script(temp_out_bsd, outfile, version)
            print(tr('已导回对话 {0} 项（共 {1} 条）-> {2}', applied, count, outfile))
        elif self.mode == 'txt_import':
            if not dialog_file or not os.path.isfile(dialog_file):
                raise Exception(tr('TXT 文件不存在: {0}', dialog_file))
            version = self._resolve_script_version(infile)
            self._batch_stats[version] += 1
            with tempfile.TemporaryDirectory(prefix='bgi_txt_import_gui_') as td:
                base = os.path.basename(infile)
                temp_src_bsd = os.path.join(td, base + '.src.bsd')
                temp_out_bsd = os.path.join(td, base + '.out.bsd')
                self._disassemble_script_to_bsd(infile, temp_src_bsd, version, encoding_for_dis=self.source_encoding)
                count, applied, units = import_dialog_txt_to_bsd(
                    temp_src_bsd,
                    dialog_file,
                    temp_out_bsd,
                    user_function_names=self.user_function_names
                )
                self._assemble_bsd_to_script(temp_out_bsd, outfile, version)
            print(tr('已导回文本 {0} 项（对话 {1} 条 / 文本单元 {2} 条）-> {3}', applied, count, units, outfile))
        elif self.mode == 'bp_json_import':
            if not dialog_file or not os.path.isfile(dialog_file):
                raise Exception(tr('JSON 文件不存在: {0}', dialog_file))
            with tempfile.TemporaryDirectory(prefix='bgi_bp_json_import_gui_') as td:
                base = os.path.basename(infile)
                temp_src_bpd = os.path.join(td, base + '.src.bpd')
                temp_out_bpd = os.path.join(td, base + '.out.bpd')
                self._disassemble_bp_to_bpd(infile, temp_src_bpd, encoding_for_dis=self.source_encoding)
                count, applied = import_push_string_json_to_bpd(temp_src_bpd, dialog_file, temp_out_bpd)
                self._assemble_bpd_to_bp(temp_out_bpd, outfile, encoding_for_asm=self.encoding)
            print(tr('已导回 BP push_string {0} 项（共 {1} 条）-> {2}', applied, count, outfile))
        elif self.mode == 'bp_txt_import':
            if not dialog_file or not os.path.isfile(dialog_file):
                raise Exception(tr('TXT 文件不存在: {0}', dialog_file))
            with tempfile.TemporaryDirectory(prefix='bgi_bp_txt_import_gui_') as td:
                base = os.path.basename(infile)
                temp_src_bpd = os.path.join(td, base + '.src.bpd')
                temp_out_bpd = os.path.join(td, base + '.out.bpd')
                self._disassemble_bp_to_bpd(infile, temp_src_bpd, encoding_for_dis=self.source_encoding)
                count, applied, units = import_push_string_txt_to_bpd(temp_src_bpd, dialog_file, temp_out_bpd)
                self._assemble_bpd_to_bp(temp_out_bpd, outfile, encoding_for_asm=self.encoding)
            print(tr('已导回 BP 文本 {0} 项（push_string {1} 条 / 文本单元 {2} 条）-> {3}', applied, count, units, outfile))

    def stop(self):
        self.is_running = False

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        ui_call(self, 'setWindowTitle', 'BGI Tools GUI')
        self.resize(750, 700)
        self.setObjectName("MainBackground")

        self.worker = None
        self._syncing_user_function_names = False

        self.init_ui()
        self.setup_user_function_sync()
        self.load_persistent_settings()
        self.detect_system_theme()

    def init_ui(self):
        central_widget = QWidget()
        central_widget.setObjectName("MainBackground")
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(10, 10, 10, 10)
        main_layout.setSpacing(5)

        header_layout = QHBoxLayout()
        title_label = ui_widget(QLabel, 'BGI Tools')
        title_label.setObjectName("AppTitle")
        header_layout.addWidget(title_label)

        header_layout.addStretch()

        self.theme_combo = QComboBox()
        ui_call(self.theme_combo, 'addItems', ['跟随系统', '现代浅色', '现代深色', '赛博朋克'])
        self.theme_combo.currentTextChanged.connect(self.apply_theme)
        self.theme_combo.setFixedWidth(120)
        header_layout.addWidget(self.theme_combo)

        main_layout.addLayout(header_layout)

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.setHandleWidth(2)

        self.tabs = QTabWidget()
        splitter.addWidget(self.tabs)

        self.setup_bgi_tab()
        self.setup_bp_tab()

        log_card = CardFrame()
        log_layout = QVBoxLayout(log_card)
        log_layout.setContentsMargins(0, 0, 0, 0)
        log_layout.setSpacing(0)

        log_header = QWidget()
        log_header.setObjectName("LogHeader")
        lh_layout = QHBoxLayout(log_header)
        lh_layout.setContentsMargins(10, 5, 10, 5)
        lh_layout.addWidget(ui_widget(QLabel, tr('📝 运行日志 (Log)')))
        lh_layout.addStretch()
        clear_btn = ui_widget(QPushButton, tr('清除'))
        clear_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        clear_btn.setStyleSheet("border:none; font-weight:bold; color: #888;")
        clear_btn.clicked.connect(lambda: ui_call(self.log_view, 'clear'))
        lh_layout.addWidget(clear_btn)
        log_layout.addWidget(log_header)

        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setObjectName("LogConsole")
        log_layout.addWidget(self.log_view)

        splitter.addWidget(log_card)
        # 设置初始分割比例，给日志更多空间
        splitter.setSizes([400, 500])
        splitter.setCollapsible(0, False)
        splitter.setCollapsible(1, False)
        
        main_layout.addWidget(splitter)

    def setup_bgi_tab(self):
        extract_tab = QWidget()
        extract_layout = QVBoxLayout(extract_tab)
        extract_layout.setContentsMargins(5, 5, 5, 5)
        extract_layout.setSpacing(5)

        extract_card = CardFrame()
        ec_layout = QVBoxLayout(extract_card)
        ec_layout.setContentsMargins(10, 10, 10, 10)
        ec_layout.setSpacing(5)
        ec_layout.addWidget(ui_widget(QLabel, tr('🔓 提取 (Extract)')))

        ec_layout.addWidget(ui_widget(QLabel, tr('提取类型:')))
        self.extract_type_bg = QButtonGroup(self)
        extract_mode_row = QHBoxLayout()
        extract_modes = [
            (tr('BSD 反汇编'), "bsd", tr('脚本 -> BSD 文本')),
            (tr('JSON 对话'), "json", tr('脚本 -> JSON 对话')),
            (tr('TXT 对话'), "txt", tr('脚本 -> ☆/★ 双行文本')),
        ]
        for i, (name, val, tip) in enumerate(extract_modes):
            container = QWidget()
            c_layout = QVBoxLayout(container)
            c_layout.setContentsMargins(0, 0, 0, 0)
            c_layout.setSpacing(2)
            rb = ui_widget(QRadioButton, name)
            ui_call(rb, 'setToolTip', tip)
            rb.setProperty("arg_val", val)
            if i == 0:
                rb.setChecked(True)
            self.extract_type_bg.addButton(rb, i)
            c_layout.addWidget(rb)
            tip_lbl = ui_widget(QLabel, tip)
            tip_lbl.setObjectName("TipLabel")
            tip_lbl.setWordWrap(True)
            c_layout.addWidget(tip_lbl)
            extract_mode_row.addWidget(container)
        ec_layout.addLayout(extract_mode_row)

        self.extract_desc_label = ui_widget(QLabel, '')
        self.extract_desc_label.setWordWrap(True)
        self.extract_desc_label.setObjectName("TipLabel")
        ec_layout.addWidget(self.extract_desc_label)

        self.extract_stack = QStackedWidget()

        extract_bsd_page = QWidget()
        eb_layout = QVBoxLayout(extract_bsd_page)
        eb_layout.setContentsMargins(0, 0, 0, 0)
        eb_layout.setSpacing(8)
        self.dis_input_edit = self.create_path_selector(
            eb_layout, tr('📂 输入脚本:'),
            on_change=lambda: self.auto_fill_output('ext_bsd'),
            add_file_button=True,
            add_dir_button=True
        )
        self.dis_output_edit = self.create_path_selector(
            eb_layout, tr('💾 输出BSD(可选):'),
            add_file_button=True,
            add_dir_button=True,
            is_output=True
        )
        self.dis_enc_combo, self.dis_ver_combo = self.create_encoding_version_row(
            eb_layout, tr('🔤 字符串编码:'), ["cp932", "utf-8", "gbk", "big5"], True
        )
        self.btn_dis = ModernButton(tr('执行 BSD 提取'), is_primary=True)
        self.btn_dis.clicked.connect(self.run_disassemble)
        eb_layout.addWidget(self.btn_dis)
        self.extract_stack.addWidget(extract_bsd_page)

        extract_json_page = QWidget()
        ej_layout = QVBoxLayout(extract_json_page)
        ej_layout.setContentsMargins(0, 0, 0, 0)
        ej_layout.setSpacing(8)
        self.ext_json_input_edit = self.create_path_selector(
            ej_layout, tr('📂 输入脚本:'),
            on_change=lambda: self.auto_fill_output('ext_json'),
            add_file_button=True,
            add_dir_button=True
        )
        self.ext_json_output_edit = self.create_path_selector(
            ej_layout, tr('💾 输出JSON(可选):'),
            add_file_button=True,
            add_dir_button=True,
            is_output=True
        )
        self.ext_json_userfunc_edit = self.create_text_row(
            ej_layout,
            tr('🧩 文本函数规则:'),
            tr('如 _Selection，多个可用逗号/分号/换行分隔，输入后提取这个用户函数的所有字符串')
        )
        self.ext_json_enc_combo, self.ext_json_ver_combo = self.create_encoding_version_row(
            ej_layout, tr('🔤 字符串编码:'), ["cp932", "utf-8", "gbk", "big5"], True
        )
        self.btn_json_extract = ModernButton(tr('执行 JSON 提取'), is_primary=True)
        self.btn_json_extract.clicked.connect(self.run_json_extract)
        ej_layout.addWidget(self.btn_json_extract)
        self.extract_stack.addWidget(extract_json_page)

        extract_txt_page = QWidget()
        et_layout = QVBoxLayout(extract_txt_page)
        et_layout.setContentsMargins(0, 0, 0, 0)
        et_layout.setSpacing(8)
        self.ext_txt_input_edit = self.create_path_selector(
            et_layout, tr('📂 输入脚本:'),
            on_change=lambda: self.auto_fill_output('ext_txt'),
            add_file_button=True,
            add_dir_button=True
        )
        self.ext_txt_output_edit = self.create_path_selector(
            et_layout, tr('💾 输出TXT(可选):'),
            add_file_button=True,
            add_dir_button=True,
            is_output=True
        )
        self.ext_txt_userfunc_edit = self.create_text_row(
            et_layout,
            tr('🧩 文本函数规则:'),
            tr('如 _Selection，多个可用逗号/分号/换行分隔')
        )
        self.ext_txt_enc_combo, self.ext_txt_ver_combo = self.create_encoding_version_row(
            et_layout, tr('🔤 字符串编码:'), ["cp932", "utf-8", "gbk", "big5"], True
        )
        self.btn_txt_extract = ModernButton(tr('执行 TXT 提取'), is_primary=True)
        self.btn_txt_extract.clicked.connect(self.run_txt_extract)
        et_layout.addWidget(self.btn_txt_extract)
        self.extract_stack.addWidget(extract_txt_page)

        ec_layout.addWidget(self.extract_stack)
        ec_layout.addStretch()
        extract_layout.addWidget(extract_card)
        ui_call(self.tabs, 'addTab', extract_tab, tr('提取（Extract）'))

        build_tab = QWidget()
        build_tab_layout = QVBoxLayout(build_tab)
        build_tab_layout.setContentsMargins(5, 5, 5, 5)
        build_tab_layout.setSpacing(5)

        build_card = CardFrame()
        bc_layout = QVBoxLayout(build_card)
        bc_layout.setContentsMargins(10, 10, 10, 10)
        bc_layout.setSpacing(5)
        bc_layout.addWidget(ui_widget(QLabel, tr('🔒 构建 (Build)')))

        bc_layout.addWidget(ui_widget(QLabel, tr('构建类型:')))
        self.build_type_bg = QButtonGroup(self)
        build_mode_row = QHBoxLayout()
        build_modes = [
            (tr('BSD 构建'), "bsd", tr('BSD 文本 -> 脚本')),
            (tr('JSON 导回'), "json", tr('脚本 + JSON -> 新脚本')),
            (tr('TXT 导回'), "txt", tr('脚本 + TXT -> 新脚本')),
        ]
        for i, (name, val, tip) in enumerate(build_modes):
            container = QWidget()
            c_layout = QVBoxLayout(container)
            c_layout.setContentsMargins(0, 0, 0, 0)
            c_layout.setSpacing(2)
            rb = ui_widget(QRadioButton, name)
            ui_call(rb, 'setToolTip', tip)
            rb.setProperty("arg_val", val)
            if i == 0:
                rb.setChecked(True)
            self.build_type_bg.addButton(rb, i)
            c_layout.addWidget(rb)
            tip_lbl = ui_widget(QLabel, tip)
            tip_lbl.setObjectName("TipLabel")
            tip_lbl.setWordWrap(True)
            c_layout.addWidget(tip_lbl)
            build_mode_row.addWidget(container)
        bc_layout.addLayout(build_mode_row)

        self.build_desc_label = ui_widget(QLabel, '')
        self.build_desc_label.setWordWrap(True)
        self.build_desc_label.setObjectName("TipLabel")
        bc_layout.addWidget(self.build_desc_label)

        self.build_stack = QStackedWidget()

        page_bsd = QWidget()
        page_bsd_layout = QVBoxLayout(page_bsd)
        page_bsd_layout.setContentsMargins(0, 0, 0, 0)
        page_bsd_layout.setSpacing(8)
        self.asm_bsd_input_edit = self.create_path_selector(
            page_bsd_layout, tr('📂 BSD输入:'),
            on_change=lambda: self.auto_fill_output('asm_bsd'),
            add_file_button=True,
            add_dir_button=True
        )
        self.asm_bsd_output_edit = self.create_path_selector(
            page_bsd_layout, tr('💾 输出脚本(可选):'),
            add_file_button=True,
            add_dir_button=True,
            is_output=True
        )
        self.asm_bsd_enc_combo, self.asm_bsd_ver_combo = self.create_encoding_version_row(
            page_bsd_layout, tr('🔤 字符串编码:'), ["cp932", "utf-8", "gbk", "big5"], True
        )
        self.btn_asm = ModernButton(tr('执行 BSD 构建'), is_primary=True)
        self.btn_asm.clicked.connect(self.run_assemble)
        page_bsd_layout.addWidget(self.btn_asm)
        self.build_stack.addWidget(page_bsd)

        page_json = QWidget()
        page_json_layout = QVBoxLayout(page_json)
        page_json_layout.setContentsMargins(0, 0, 0, 0)
        page_json_layout.setSpacing(8)
        self.asm_json_src_edit = self.create_path_selector(
            page_json_layout, tr('📂 原始脚本:'),
            on_change=lambda: self.auto_fill_output('asm_json'),
            add_file_button=True,
            add_dir_button=True
        )
        self.asm_json_edit = self.create_path_selector(
            page_json_layout, tr('🧾 JSON输入:'),
            add_file_button=True,
            add_dir_button=True
        )
        self.asm_json_output_edit = self.create_path_selector(
            page_json_layout, tr('💾 输出脚本(可选):'),
            add_file_button=True,
            add_dir_button=True,
            is_output=True
        )
        self.asm_json_userfunc_edit = self.create_text_row(
            page_json_layout,
            tr('🧩 文本函数规则:'),
            tr('如 _Selection，需与提取时保持一致')
        )
        self.asm_json_src_enc_combo, self.asm_json_enc_combo, self.asm_json_ver_combo = self.create_build_dual_version_row(
            page_json_layout,
            tr('🔤 原始剧本编码:'),
            tr('📤 输出脚本编码:'),
            ["cp932", "utf-8", "gbk", "big5"],
            True
        )
        self.btn_json_import = ModernButton(tr('执行 JSON 导回构建'), is_primary=True)
        self.btn_json_import.clicked.connect(self.run_json_import)
        page_json_layout.addWidget(self.btn_json_import)
        self.build_stack.addWidget(page_json)

        page_txt = QWidget()
        page_txt_layout = QVBoxLayout(page_txt)
        page_txt_layout.setContentsMargins(0, 0, 0, 0)
        page_txt_layout.setSpacing(8)
        self.asm_txt_src_edit = self.create_path_selector(
            page_txt_layout, tr('📂 原始脚本:'),
            on_change=lambda: self.auto_fill_output('asm_txt'),
            add_file_button=True,
            add_dir_button=True
        )
        self.asm_txt_edit = self.create_path_selector(
            page_txt_layout, tr('🧾 TXT输入:'),
            add_file_button=True,
            add_dir_button=True
        )
        self.asm_txt_output_edit = self.create_path_selector(
            page_txt_layout, tr('💾 输出脚本(可选):'),
            add_file_button=True,
            add_dir_button=True,
            is_output=True
        )
        self.asm_txt_userfunc_edit = self.create_text_row(
            page_txt_layout,
            tr('🧩 文本函数规则:'),
            tr('如 _Selection，需与提取时保持一致')
        )
        self.asm_txt_src_enc_combo, self.asm_txt_enc_combo, self.asm_txt_ver_combo = self.create_build_dual_version_row(
            page_txt_layout,
            tr('🔤 原始剧本编码:'),
            tr('📤 输出脚本编码:'),
            ["cp932", "utf-8", "gbk", "big5"],
            True
        )
        self.btn_txt_import = ModernButton(tr('执行 TXT 导回构建'), is_primary=True)
        self.btn_txt_import.clicked.connect(self.run_txt_import)
        page_txt_layout.addWidget(self.btn_txt_import)
        self.build_stack.addWidget(page_txt)

        bc_layout.addWidget(self.build_stack)
        bc_layout.addStretch()
        build_tab_layout.addWidget(build_card)
        ui_call(self.tabs, 'addTab', build_tab, tr('构建（Build）'))

        for btn in self.extract_type_bg.buttons():
            btn.toggled.connect(lambda _: self.on_extract_mode_changed())
        for btn in self.build_type_bg.buttons():
            btn.toggled.connect(lambda _: self.on_build_mode_changed())
        self._extract_shared = {'input': '', 'output': '', 'encoding': '', 'version': tr('自动')}
        self._extract_last_mode = self.get_extract_mode()
        self._build_shared = {'input': '', 'output': '', 'encoding': '', 'source_encoding': 'cp932', 'version': tr('自动')}
        self._build_last_mode = self.get_build_mode()
        self.on_extract_mode_changed()
        self.on_build_mode_changed()

    def setup_bp_tab(self):
        bp_tab = QWidget()
        bp_layout = QVBoxLayout(bp_tab)
        bp_layout.setContentsMargins(5, 5, 5, 5)
        bp_layout.setSpacing(5)

        bp_tabs = QTabWidget()
        bp_layout.addWidget(bp_tabs)

        extract_tab = QWidget()
        extract_layout = QVBoxLayout(extract_tab)
        extract_layout.setContentsMargins(5, 5, 5, 5)
        extract_layout.setSpacing(5)

        extract_card = CardFrame()
        ec_layout = QVBoxLayout(extract_card)
        ec_layout.setContentsMargins(10, 10, 10, 10)
        ec_layout.setSpacing(5)
        ec_layout.addWidget(ui_widget(QLabel, tr('🔓 BP 提取 (Extract)')))

        ec_layout.addWidget(ui_widget(QLabel, tr('提取类型:')))
        self.bp_extract_type_bg = QButtonGroup(self)
        extract_mode_row = QHBoxLayout()
        extract_modes = [
            (tr('BPD 反汇编'), "bpd", tr('._bp -> BPD 文本')),
            (tr('JSON 文本'), "json", tr('._bp -> 全量文本的 JSON')),
            (tr('TXT 文本'), "txt", tr('._bp ->  ☆/★ 全量双行文本')),
        ]
        for i, (name, val, tip) in enumerate(extract_modes):
            container = QWidget()
            c_layout = QVBoxLayout(container)
            c_layout.setContentsMargins(0, 0, 0, 0)
            c_layout.setSpacing(2)
            rb = ui_widget(QRadioButton, name)
            ui_call(rb, 'setToolTip', tip)
            rb.setProperty("arg_val", val)
            if i == 0:
                rb.setChecked(True)
            self.bp_extract_type_bg.addButton(rb, i)
            c_layout.addWidget(rb)
            tip_lbl = ui_widget(QLabel, tip)
            tip_lbl.setObjectName("TipLabel")
            tip_lbl.setWordWrap(True)
            c_layout.addWidget(tip_lbl)
            extract_mode_row.addWidget(container)
        ec_layout.addLayout(extract_mode_row)

        self.bp_extract_desc_label = ui_widget(QLabel, '')
        self.bp_extract_desc_label.setWordWrap(True)
        self.bp_extract_desc_label.setObjectName("TipLabel")
        ec_layout.addWidget(self.bp_extract_desc_label)

        self.bp_extract_stack = QStackedWidget()

        extract_bpd_page = QWidget()
        ebpd_layout = QVBoxLayout(extract_bpd_page)
        ebpd_layout.setContentsMargins(0, 0, 0, 0)
        ebpd_layout.setSpacing(8)
        self.bp_dis_input_edit = self.create_path_selector(
            ebpd_layout, tr('📂 输入BP脚本:'),
            on_change=lambda: self.auto_fill_output('ext_bp'),
            add_file_button=True,
            add_dir_button=True
        )
        self.bp_dis_output_edit = self.create_path_selector(
            ebpd_layout, tr('💾 输出BPD(可选):'),
            add_file_button=True,
            add_dir_button=True,
            is_output=True
        )
        self.bp_dis_enc_combo = self.create_combo_row(
            ebpd_layout, tr('🔤 文本编码:'), ["cp932", "utf-8", "gbk", "big5"], True
        )
        self.btn_bp_dis = ModernButton(tr('执行 BPD 提取'), is_primary=True)
        self.btn_bp_dis.clicked.connect(self.run_bp_disassemble)
        ebpd_layout.addWidget(self.btn_bp_dis)
        self.bp_extract_stack.addWidget(extract_bpd_page)

        extract_json_page = QWidget()
        ej_layout = QVBoxLayout(extract_json_page)
        ej_layout.setContentsMargins(0, 0, 0, 0)
        ej_layout.setSpacing(8)
        self.bp_json_input_edit = self.create_path_selector(
            ej_layout, tr('📂 输入BP脚本:'),
            on_change=lambda: self.auto_fill_output('ext_bp_json'),
            add_file_button=True,
            add_dir_button=True
        )
        self.bp_json_output_edit = self.create_path_selector(
            ej_layout, tr('💾 输出JSON(可选):'),
            add_file_button=True,
            add_dir_button=True,
            is_output=True
        )
        self.bp_json_enc_combo = self.create_combo_row(
            ej_layout, tr('🔤 文本编码:'), ["cp932", "utf-8", "gbk", "big5"], True
        )
        self.btn_bp_json_extract = ModernButton(tr('执行 BP JSON 提取'), is_primary=True)
        self.btn_bp_json_extract.clicked.connect(self.run_bp_json_extract)
        ej_layout.addWidget(self.btn_bp_json_extract)
        self.bp_extract_stack.addWidget(extract_json_page)

        extract_txt_page = QWidget()
        et_layout = QVBoxLayout(extract_txt_page)
        et_layout.setContentsMargins(0, 0, 0, 0)
        et_layout.setSpacing(8)
        self.bp_txt_input_edit = self.create_path_selector(
            et_layout, tr('📂 输入BP脚本:'),
            on_change=lambda: self.auto_fill_output('ext_bp_txt'),
            add_file_button=True,
            add_dir_button=True
        )
        self.bp_txt_output_edit = self.create_path_selector(
            et_layout, tr('💾 输出TXT(可选):'),
            add_file_button=True,
            add_dir_button=True,
            is_output=True
        )
        self.bp_txt_enc_combo = self.create_combo_row(
            et_layout, tr('🔤 文本编码:'), ["cp932", "utf-8", "gbk", "big5"], True
        )
        self.btn_bp_txt_extract = ModernButton(tr('执行 BP TXT 提取'), is_primary=True)
        self.btn_bp_txt_extract.clicked.connect(self.run_bp_txt_extract)
        et_layout.addWidget(self.btn_bp_txt_extract)
        self.bp_extract_stack.addWidget(extract_txt_page)

        ec_layout.addWidget(self.bp_extract_stack)
        ec_layout.addStretch()
        extract_layout.addWidget(extract_card)
        ui_call(bp_tabs, 'addTab', extract_tab, tr('提取'))

        build_tab = QWidget()
        build_tab_layout = QVBoxLayout(build_tab)
        build_tab_layout.setContentsMargins(5, 5, 5, 5)
        build_tab_layout.setSpacing(5)

        build_card = CardFrame()
        bc_layout = QVBoxLayout(build_card)
        bc_layout.setContentsMargins(10, 10, 10, 10)
        bc_layout.setSpacing(5)
        bc_layout.addWidget(ui_widget(QLabel, tr('🔒 BP 构建 (Build)')))

        bc_layout.addWidget(ui_widget(QLabel, tr('构建类型:')))
        self.bp_build_type_bg = QButtonGroup(self)
        build_mode_row = QHBoxLayout()
        build_modes = [
            (tr('BPD 构建'), "bpd", tr('BPD 文本 -> ._bp')),
            (tr('JSON 导回'), "json", tr('原始 ._bp + JSON -> 新 ._bp')),
            (tr('TXT 导回'), "txt", tr('原始 ._bp + TXT -> 新 ._bp')),
        ]
        for i, (name, val, tip) in enumerate(build_modes):
            container = QWidget()
            c_layout = QVBoxLayout(container)
            c_layout.setContentsMargins(0, 0, 0, 0)
            c_layout.setSpacing(2)
            rb = ui_widget(QRadioButton, name)
            ui_call(rb, 'setToolTip', tip)
            rb.setProperty("arg_val", val)
            if i == 0:
                rb.setChecked(True)
            self.bp_build_type_bg.addButton(rb, i)
            c_layout.addWidget(rb)
            tip_lbl = ui_widget(QLabel, tip)
            tip_lbl.setObjectName("TipLabel")
            tip_lbl.setWordWrap(True)
            c_layout.addWidget(tip_lbl)
            build_mode_row.addWidget(container)
        bc_layout.addLayout(build_mode_row)

        self.bp_build_desc_label = ui_widget(QLabel, '')
        self.bp_build_desc_label.setWordWrap(True)
        self.bp_build_desc_label.setObjectName("TipLabel")
        bc_layout.addWidget(self.bp_build_desc_label)

        self.bp_build_stack = QStackedWidget()

        page_bpd = QWidget()
        page_bpd_layout = QVBoxLayout(page_bpd)
        page_bpd_layout.setContentsMargins(0, 0, 0, 0)
        page_bpd_layout.setSpacing(8)
        self.bp_asm_input_edit = self.create_path_selector(
            page_bpd_layout, tr('📂 BPD输入:'),
            on_change=lambda: self.auto_fill_output('asm_bp'),
            add_file_button=True,
            add_dir_button=True
        )
        self.bp_asm_output_edit = self.create_path_selector(
            page_bpd_layout, tr('💾 输出BP脚本(可选):'),
            add_file_button=True,
            add_dir_button=True,
            is_output=True
        )
        self.bp_asm_enc_combo = self.create_combo_row(
            page_bpd_layout, tr('📤 输出BP编码:'), ["cp932", "utf-8", "gbk", "big5"], True
        )
        self.btn_bp_asm = ModernButton(tr('执行 BPD 构建'), is_primary=True)
        self.btn_bp_asm.clicked.connect(self.run_bp_assemble)
        page_bpd_layout.addWidget(self.btn_bp_asm)
        self.bp_build_stack.addWidget(page_bpd)

        page_json = QWidget()
        page_json_layout = QVBoxLayout(page_json)
        page_json_layout.setContentsMargins(0, 0, 0, 0)
        page_json_layout.setSpacing(8)
        self.bp_json_src_edit = self.create_path_selector(
            page_json_layout, tr('📂 原始BP脚本:'),
            on_change=lambda: self.auto_fill_output('asm_bp_json'),
            add_file_button=True,
            add_dir_button=True
        )
        self.bp_json_edit = self.create_path_selector(
            page_json_layout, tr('🧾 JSON输入:'),
            add_file_button=True,
            add_dir_button=True
        )
        self.bp_json_build_output_edit = self.create_path_selector(
            page_json_layout, tr('💾 输出BP脚本(可选):'),
            add_file_button=True,
            add_dir_button=True,
            is_output=True
        )
        self.bp_json_src_enc_combo, self.bp_json_build_enc_combo = self.create_dual_combo_row(
            page_json_layout,
            tr('🔤 原始BP编码:'),
            tr('📤 输出BP编码:'),
            ["cp932", "utf-8", "gbk", "big5"],
            True
        )
        self.btn_bp_json_import = ModernButton(tr('执行 BP JSON 导回'), is_primary=True)
        self.btn_bp_json_import.clicked.connect(self.run_bp_json_import)
        page_json_layout.addWidget(self.btn_bp_json_import)
        self.bp_build_stack.addWidget(page_json)

        page_txt = QWidget()
        page_txt_layout = QVBoxLayout(page_txt)
        page_txt_layout.setContentsMargins(0, 0, 0, 0)
        page_txt_layout.setSpacing(8)
        self.bp_txt_src_edit = self.create_path_selector(
            page_txt_layout, tr('📂 原始BP脚本:'),
            on_change=lambda: self.auto_fill_output('asm_bp_txt'),
            add_file_button=True,
            add_dir_button=True
        )
        self.bp_txt_edit = self.create_path_selector(
            page_txt_layout, tr('🧾 TXT输入:'),
            add_file_button=True,
            add_dir_button=True
        )
        self.bp_txt_build_output_edit = self.create_path_selector(
            page_txt_layout, tr('💾 输出BP脚本(可选):'),
            add_file_button=True,
            add_dir_button=True,
            is_output=True
        )
        self.bp_txt_src_enc_combo, self.bp_txt_build_enc_combo = self.create_dual_combo_row(
            page_txt_layout,
            tr('🔤 原始BP编码:'),
            tr('📤 输出BP编码:'),
            ["cp932", "utf-8", "gbk", "big5"],
            True
        )
        self.btn_bp_txt_import = ModernButton(tr('执行 BP TXT 导回'), is_primary=True)
        self.btn_bp_txt_import.clicked.connect(self.run_bp_txt_import)
        page_txt_layout.addWidget(self.btn_bp_txt_import)
        self.bp_build_stack.addWidget(page_txt)

        bc_layout.addWidget(self.bp_build_stack)
        bc_layout.addStretch()
        build_tab_layout.addWidget(build_card)
        ui_call(bp_tabs, 'addTab', build_tab, tr('构建'))

        for btn in self.bp_extract_type_bg.buttons():
            btn.toggled.connect(lambda _: self.on_bp_extract_mode_changed())
        for btn in self.bp_build_type_bg.buttons():
            btn.toggled.connect(lambda _: self.on_bp_build_mode_changed())
        self._bp_extract_shared = {'input': '', 'output': '', 'encoding': 'cp932'}
        self._bp_extract_last_mode = self.get_bp_extract_mode()
        self._bp_build_shared = {'input': '', 'output': '', 'encoding': 'cp932', 'source_encoding': 'cp932'}
        self._bp_build_last_mode = self.get_bp_build_mode()
        self.on_bp_extract_mode_changed()
        self.on_bp_build_mode_changed()

        ui_call(self.tabs, 'addTab', bp_tab, tr('BP 工具（BP Tool）'))

    def create_path_selector(self, parent_layout, label_text=None, on_change=None, add_file_button=False, add_dir_button=True, is_output=False):
        row = QHBoxLayout()
        row.setSpacing(5)
        row.setContentsMargins(0, 0, 0, 0)

        if label_text:
            lbl = ui_widget(QLabel, label_text)
            # lbl.setFixedWidth(120) 
            row.addWidget(lbl)

        edit = DragDropLineEdit(is_folder=True)
        if on_change:
            edit.textChanged.connect(on_change)
        row.addWidget(edit)
        if add_file_button:
            btn_file = ModernButton(tr('文件'))
            btn_file.setFixedWidth(50)
            if is_output:
                btn_file.clicked.connect(lambda: self.browse_output_file(edit))
            else:
                btn_file.clicked.connect(lambda: self.browse_input_file(edit, on_change))
            row.addWidget(btn_file)
        if add_dir_button:
            btn_dir = ModernButton(tr('目录'))
            btn_dir.setFixedWidth(50)
            btn_dir.clicked.connect(lambda: self.browse_folder(edit, on_change))
            row.addWidget(btn_dir)
        parent_layout.addLayout(row)
        return edit

    def create_text_row(self, parent_layout, label_text, placeholder=''):
        row = QHBoxLayout()
        row.setSpacing(5)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(ui_widget(QLabel, label_text))
        edit = QLineEdit()
        if placeholder:
            ui_call(edit, 'setPlaceholderText', placeholder)
        row.addWidget(edit)
        parent_layout.addLayout(row)
        return edit

    def _settings_ini_path(self):
        root = os.path.join(
            QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppConfigLocation),
            "bgi_toolkit",
        )
        os.makedirs(root, exist_ok=True)
        return os.path.join(root, "bgi_gui.ini")

    def _user_function_name_edits(self):
        return [
            self.ext_json_userfunc_edit,
            self.ext_txt_userfunc_edit,
            self.asm_json_userfunc_edit,
            self.asm_txt_userfunc_edit
        ]

    def _current_user_function_names(self):
        for edit in self._user_function_name_edits():
            text = edit.text().strip()
            if text:
                return text
        return ''

    def _apply_user_function_names(self, text, persist=True):
        normalized = str(text or '')
        if self._syncing_user_function_names:
            return
        self._syncing_user_function_names = True
        try:
            for edit in self._user_function_name_edits():
                if edit.text() != normalized:
                    ui_call(edit, 'setText', normalized)
        finally:
            self._syncing_user_function_names = False
        if persist:
            self.save_persistent_settings()

    def on_user_function_names_changed(self, text):
        self._apply_user_function_names(text, persist=True)

    def setup_user_function_sync(self):
        for edit in self._user_function_name_edits():
            edit.textChanged.connect(self.on_user_function_names_changed)

    def load_persistent_settings(self):
        config = configparser.ConfigParser()
        ini_path = self._settings_ini_path()
        if os.path.isfile(ini_path):
            config.read(ini_path, encoding='utf-8')
        user_function_names = config.get('text_extract', 'user_function_names', fallback='')
        if user_function_names:
            self._apply_user_function_names(user_function_names, persist=False)

    def save_persistent_settings(self):
        config = configparser.ConfigParser()
        ini_path = self._settings_ini_path()
        if os.path.isfile(ini_path):
            config.read(ini_path, encoding='utf-8')
        if not config.has_section('text_extract'):
            config.add_section('text_extract')
        config.set('text_extract', 'user_function_names', self._current_user_function_names())
        with open(ini_path, 'w', encoding='utf-8') as f:
            config.write(f)

    def _prepare_encoding_items(self, items):
        final_items = []
        for enc in (items or []):
            text = str(enc).strip()
            if text and text not in final_items:
                final_items.append(text)
        if 'cp932' in final_items:
            final_items.remove('cp932')
        final_items.insert(0, 'cp932')
        return final_items

    def create_combo_row(self, parent_layout, label_text, items, editable=True):
        row = QHBoxLayout()
        row.setSpacing(3)
        row.setContentsMargins(0, 0, 0, 0)
        lbl = ui_widget(QLabel, label_text)
        row.addWidget(lbl)
        combo = QComboBox()
        combo.setEditable(editable)
        ui_call(combo, 'addItems', self._prepare_encoding_items(items))
        combo.setCurrentText('cp932')
        combo.setMinimumWidth(130)
        combo.setMaximumWidth(180)
        combo.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        row.addWidget(combo)
        row.addStretch(1)
        parent_layout.addLayout(row)
        return combo

    def create_version_combo_row(self, parent_layout):
        row = QHBoxLayout()
        row.setSpacing(3)
        row.setContentsMargins(0, 0, 0, 0)
        lbl = ui_widget(QLabel, tr('🧭 脚本版本:'))
        row.addWidget(lbl)
        combo = QComboBox()
        combo.setEditable(False)
        ui_call(combo, 'addItems', [tr('自动'), 'v0', 'v1'])
        combo.setCurrentText(tr('自动'))
        combo.setMinimumWidth(90)
        combo.setMaximumWidth(120)
        combo.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        row.addWidget(combo)
        row.addStretch(1)
        parent_layout.addLayout(row)
        return combo

    def create_encoding_version_row(self, parent_layout, label_text, items, editable=True):
        row = QHBoxLayout()
        row.setSpacing(12)
        row.setContentsMargins(0, 0, 0, 0)

        left_wrap = QHBoxLayout()
        left_wrap.setSpacing(3)
        left_wrap.setContentsMargins(0, 0, 0, 0)
        lbl = ui_widget(QLabel, label_text)
        left_wrap.addWidget(lbl)
        enc_combo = QComboBox()
        enc_combo.setEditable(editable)
        ui_call(enc_combo, 'addItems', self._prepare_encoding_items(items))
        enc_combo.setCurrentText('cp932')
        enc_combo.setMinimumWidth(130)
        enc_combo.setMaximumWidth(180)
        enc_combo.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        left_wrap.addWidget(enc_combo)
        left_wrap.addStretch(1)

        right_wrap = QHBoxLayout()
        right_wrap.setSpacing(3)
        right_wrap.setContentsMargins(0, 0, 0, 0)
        ver_lbl = ui_widget(QLabel, tr('🧭 脚本版本:'))
        right_wrap.addWidget(ver_lbl)
        ver_combo = QComboBox()
        ver_combo.setEditable(False)
        ui_call(ver_combo, 'addItems', [tr('自动'), 'v0', 'v1'])
        ver_combo.setCurrentText(tr('自动'))
        ver_combo.setMinimumWidth(90)
        ver_combo.setMaximumWidth(120)
        ver_combo.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        right_wrap.addWidget(ver_combo)
        right_wrap.addStretch(1)

        row.addLayout(left_wrap, 1)
        row.addLayout(right_wrap, 1)
        parent_layout.addLayout(row)
        return enc_combo, ver_combo

    def create_build_dual_version_row(self, parent_layout, left_label, middle_label, items, editable=True):
        item_list = self._prepare_encoding_items(items)
        row = QHBoxLayout()
        row.setSpacing(12)
        row.setContentsMargins(0, 0, 0, 0)

        left_wrap = QHBoxLayout()
        left_wrap.setSpacing(3)
        left_wrap.setContentsMargins(0, 0, 0, 0)
        left_lbl = ui_widget(QLabel, left_label)
        left_wrap.addWidget(left_lbl)
        left_combo = QComboBox()
        left_combo.setEditable(editable)
        ui_call(left_combo, 'addItems', item_list)
        left_combo.setCurrentText('cp932')
        left_combo.setMinimumWidth(130)
        left_combo.setMaximumWidth(180)
        left_combo.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        left_wrap.addWidget(left_combo)
        left_wrap.addStretch(1)

        middle_wrap = QHBoxLayout()
        middle_wrap.setSpacing(3)
        middle_wrap.setContentsMargins(0, 0, 0, 0)
        middle_lbl = ui_widget(QLabel, middle_label)
        middle_wrap.addWidget(middle_lbl)
        middle_combo = QComboBox()
        middle_combo.setEditable(editable)
        ui_call(middle_combo, 'addItems', item_list)
        middle_combo.setCurrentText('cp932')
        middle_combo.setMinimumWidth(130)
        middle_combo.setMaximumWidth(180)
        middle_combo.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        middle_wrap.addWidget(middle_combo)
        middle_wrap.addStretch(1)

        right_wrap = QHBoxLayout()
        right_wrap.setSpacing(3)
        right_wrap.setContentsMargins(0, 0, 0, 0)
        right_lbl = ui_widget(QLabel, tr('🧭 脚本版本:'))
        right_wrap.addWidget(right_lbl)
        right_combo = QComboBox()
        right_combo.setEditable(False)
        ui_call(right_combo, 'addItems', [tr('自动'), 'v0', 'v1'])
        right_combo.setCurrentText(tr('自动'))
        right_combo.setMinimumWidth(90)
        right_combo.setMaximumWidth(120)
        right_combo.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        right_wrap.addWidget(right_combo)
        right_wrap.addStretch(1)

        row.addLayout(left_wrap, 1)
        row.addLayout(middle_wrap, 1)
        row.addLayout(right_wrap, 1)
        parent_layout.addLayout(row)
        return left_combo, middle_combo, right_combo

    def create_dual_combo_row(self, parent_layout, left_label, right_label, items, editable=True):
        item_list = self._prepare_encoding_items(items)
        row = QHBoxLayout()
        row.setSpacing(12)
        row.setContentsMargins(0, 0, 0, 0)

        left_wrap = QHBoxLayout()
        left_wrap.setSpacing(3)
        left_wrap.setContentsMargins(0, 0, 0, 0)
        left_lbl = ui_widget(QLabel, left_label)
        left_wrap.addWidget(left_lbl)
        left_combo = QComboBox()
        left_combo.setEditable(editable)
        ui_call(left_combo, 'addItems', item_list)
        left_combo.setCurrentText('cp932')
        left_combo.setMinimumWidth(130)
        left_combo.setMaximumWidth(180)
        left_combo.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        left_wrap.addWidget(left_combo)
        left_wrap.addStretch(1)

        right_wrap = QHBoxLayout()
        right_wrap.setSpacing(3)
        right_wrap.setContentsMargins(0, 0, 0, 0)
        right_lbl = ui_widget(QLabel, right_label)
        right_wrap.addWidget(right_lbl)
        right_combo = QComboBox()
        right_combo.setEditable(editable)
        ui_call(right_combo, 'addItems', item_list)
        right_combo.setCurrentText('cp932')
        right_combo.setMinimumWidth(130)
        right_combo.setMaximumWidth(180)
        right_combo.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        right_wrap.addWidget(right_combo)
        right_wrap.addStretch(1)

        row.addLayout(left_wrap, 1)
        row.addLayout(right_wrap, 1)
        parent_layout.addLayout(row)
        return left_combo, right_combo

    def browse_folder(self, line_edit, on_change=None):
        path = QFileDialog.getExistingDirectory(self, tr('选择目录'))
        if path:
            ui_call(line_edit, 'setText', os.path.normpath(path))
            if on_change:
                on_change()

    def browse_input_file(self, line_edit, on_change=None):
        path, _ = QFileDialog.getOpenFileName(self, tr('选择文件'))
        if path:
            ui_call(line_edit, 'setText', os.path.normpath(path))
            if on_change:
                on_change()

    def browse_output_file(self, line_edit):
        initial_path = line_edit.text().strip()
        path, _ = QFileDialog.getSaveFileName(self, tr('选择输出文件'), initial_path)
        if path:
            ui_call(line_edit, 'setText', os.path.normpath(path))

    def auto_fill_output(self, mode):
        cfg = {
            'ext_bsd': (self.dis_input_edit, self.dis_output_edit, "_out", ".bsd", None, None),
            'ext_json': (self.ext_json_input_edit, self.ext_json_output_edit, "_out", ".json", None, None),
            'ext_txt': (self.ext_txt_input_edit, self.ext_txt_output_edit, "_out", ".txt", None, None),
            'ext_bp': (self.bp_dis_input_edit, self.bp_dis_output_edit, "_out", ".bpd", None, None),
            'ext_bp_json': (self.bp_json_input_edit, self.bp_json_output_edit, "_out", ".json", None, None),
            'ext_bp_txt': (self.bp_txt_input_edit, self.bp_txt_output_edit, "_out", ".txt", None, None),
            'asm_bsd': (self.asm_bsd_input_edit, self.asm_bsd_output_edit, "_build", "", None, None),
            'asm_json': (self.asm_json_src_edit, self.asm_json_output_edit, "_build", "_jsonimp", self.asm_json_edit, ".json"),
            'asm_txt': (self.asm_txt_src_edit, self.asm_txt_output_edit, "_build", "_txtimp", self.asm_txt_edit, ".txt"),
            'asm_bp': (self.bp_asm_input_edit, self.bp_asm_output_edit, "_build", "._bp", None, None),
            'asm_bp_json': (self.bp_json_src_edit, self.bp_json_build_output_edit, "_build", "_jsonimp._bp", self.bp_json_edit, ".json"),
            'asm_bp_txt': (self.bp_txt_src_edit, self.bp_txt_build_output_edit, "_build", "_txtimp._bp", self.bp_txt_edit, ".txt")
        }
        if mode not in cfg:
            return
        input_edit, output_edit, dir_suffix, file_suffix, dialog_edit, dialog_ext = cfg[mode]
        path = input_edit.text().strip()
        if not path:
            return
        if os.path.isdir(path):
            norm_path = os.path.normpath(path)
            ui_call(output_edit, 'setText', norm_path + dir_suffix)
            if dialog_edit is not None:
                ui_call(dialog_edit, 'setText', norm_path + '_out')
            return
        base = os.path.splitext(path)[0]
        ui_call(output_edit, 'setText', base + file_suffix)
        if dialog_edit is not None:
            ui_call(dialog_edit, 'setText', base + dialog_ext)

    def _set_combo_value(self, combo, value):
        if value is None:
            return
        text = str(value).strip()
        if not text:
            return
        if text.lower().replace('-', '_') in ('sjis', 'shift_jis', 'shiftjis', 's_jis', 'ms932', 'ms_kanji', 'windows_31j'):
            text = 'cp932'
        combo.setCurrentText(text)

    def _normalize_version_choice(self, text):
        value = (text or '').strip().lower()
        if value in ('v0', 'v1'):
            return value
        return 'auto'

    def _get_extract_controls(self, mode):
        if mode == 'bsd':
            return self.dis_input_edit, self.dis_output_edit, self.dis_enc_combo, self.dis_ver_combo
        if mode == 'json':
            return self.ext_json_input_edit, self.ext_json_output_edit, self.ext_json_enc_combo, self.ext_json_ver_combo
        if mode == 'txt':
            return self.ext_txt_input_edit, self.ext_txt_output_edit, self.ext_txt_enc_combo, self.ext_txt_ver_combo
        return self.bp_dis_input_edit, self.bp_dis_output_edit, self.bp_dis_enc_combo, None

    def _capture_extract_shared(self, mode):
        in_edit, out_edit, enc_combo, ver_combo = self._get_extract_controls(mode)
        self._extract_shared['input'] = in_edit.text().strip()
        self._extract_shared['output'] = out_edit.text().strip()
        self._extract_shared['encoding'] = enc_combo.currentText().strip()
        if ver_combo is not None:
            self._extract_shared['version'] = ver_combo.currentText().strip() or tr('自动')

    def _apply_extract_shared(self, mode):
        in_edit, out_edit, enc_combo, ver_combo = self._get_extract_controls(mode)
        if self._extract_shared['input']:
            ui_call(in_edit, 'setText', self._extract_shared['input'])
        if self._extract_shared['output']:
            ui_call(out_edit, 'setText', self._extract_shared['output'])
        self._set_combo_value(enc_combo, self._extract_shared['encoding'])
        if ver_combo is not None:
            self._set_combo_value(ver_combo, self._extract_shared['version'] or tr('自动'))

    def _get_build_controls(self, mode):
        if mode == 'bsd':
            return {
                'input': self.asm_bsd_input_edit,
                'output': self.asm_bsd_output_edit,
                'encoding': self.asm_bsd_enc_combo,
                'source_encoding': None,
                'version': self.asm_bsd_ver_combo
            }
        if mode == 'json':
            return {
                'input': self.asm_json_src_edit,
                'output': self.asm_json_output_edit,
                'encoding': self.asm_json_enc_combo,
                'source_encoding': self.asm_json_src_enc_combo,
                'version': self.asm_json_ver_combo
            }
        if mode == 'txt':
            return {
                'input': self.asm_txt_src_edit,
                'output': self.asm_txt_output_edit,
                'encoding': self.asm_txt_enc_combo,
                'source_encoding': self.asm_txt_src_enc_combo,
                'version': self.asm_txt_ver_combo
            }
        return {
            'input': self.bp_asm_input_edit,
            'output': self.bp_asm_output_edit,
            'encoding': self.bp_asm_enc_combo,
            'source_encoding': None,
            'version': None
        }
 
    def _capture_build_shared(self, mode):
        ctrls = self._get_build_controls(mode)
        self._build_shared['input'] = ctrls['input'].text().strip()
        self._build_shared['output'] = ctrls['output'].text().strip()
        self._build_shared['encoding'] = ctrls['encoding'].currentText().strip()
        if ctrls['source_encoding'] is not None:
            self._build_shared['source_encoding'] = ctrls['source_encoding'].currentText().strip()
        if ctrls['version'] is not None:
            self._build_shared['version'] = ctrls['version'].currentText().strip() or tr('自动')

    def _apply_build_shared(self, mode):
        ctrls = self._get_build_controls(mode)
        if self._build_shared['input']:
            ui_call(ctrls['input'], 'setText', self._build_shared['input'])
        if self._build_shared['output']:
            ui_call(ctrls['output'], 'setText', self._build_shared['output'])
        self._set_combo_value(ctrls['encoding'], self._build_shared['encoding'])
        if ctrls['source_encoding'] is not None:
            src_enc = self._build_shared['source_encoding'] or 'cp932'
            self._set_combo_value(ctrls['source_encoding'], src_enc)
        if ctrls['version'] is not None:
            self._set_combo_value(ctrls['version'], self._build_shared['version'] or tr('自动'))

    def _run_bp_extract(self):
        input_path = self.bp_dis_input_edit.text().strip()
        if not input_path:
            return
        output_path = self.bp_dis_output_edit.text().strip()
        if not output_path:
            output_path = self._default_extract_output_path(input_path, ".bpd")
        elif not os.path.isdir(input_path) and not os.path.isdir(output_path) and not output_path.lower().endswith(".bpd"):
            output_path = output_path + ".bpd"
        self.start_worker('bp_disassemble', input_path, output_path, self.bp_dis_enc_combo.currentText().strip())

    def _run_bp_build(self):
        input_path = self.bp_asm_input_edit.text().strip()
        if not input_path:
            return
        output_path = self.bp_asm_output_edit.text().strip()
        if not output_path:
            output_path = self._default_import_output_path(input_path, "._bp")
        elif not os.path.isdir(input_path) and not os.path.isdir(output_path) and not output_path.lower().endswith("._bp"):
            output_path = output_path + "._bp"
        self.start_worker('bp_assemble', input_path, output_path, self.bp_asm_enc_combo.currentText().strip())

    def _normalize_bp_output_path(self, input_path, output_path, suffix):
        if not output_path:
            return self._default_import_output_path(input_path, suffix)
        if not os.path.isdir(input_path) and not os.path.isdir(output_path) and not output_path.lower().endswith(suffix.lower()):
            return output_path + suffix
        return output_path

    def get_bp_extract_mode(self):
        btn = self.bp_extract_type_bg.checkedButton()
        if not btn:
            return 'bpd'
        return btn.property("arg_val")

    def get_bp_build_mode(self):
        btn = self.bp_build_type_bg.checkedButton()
        if not btn:
            return 'bpd'
        return btn.property("arg_val")

    def _get_bp_extract_controls(self, mode):
        if mode == 'json':
            return self.bp_json_input_edit, self.bp_json_output_edit, self.bp_json_enc_combo
        if mode == 'txt':
            return self.bp_txt_input_edit, self.bp_txt_output_edit, self.bp_txt_enc_combo
        return self.bp_dis_input_edit, self.bp_dis_output_edit, self.bp_dis_enc_combo

    def _capture_bp_extract_shared(self, mode):
        in_edit, out_edit, enc_combo = self._get_bp_extract_controls(mode)
        self._bp_extract_shared['input'] = in_edit.text().strip()
        self._bp_extract_shared['output'] = out_edit.text().strip()
        self._bp_extract_shared['encoding'] = enc_combo.currentText().strip()

    def _apply_bp_extract_shared(self, mode):
        in_edit, out_edit, enc_combo = self._get_bp_extract_controls(mode)
        if self._bp_extract_shared['input']:
            ui_call(in_edit, 'setText', self._bp_extract_shared['input'])
        if self._bp_extract_shared['output']:
            ui_call(out_edit, 'setText', self._bp_extract_shared['output'])
        self._set_combo_value(enc_combo, self._bp_extract_shared['encoding'])

    def _get_bp_build_controls(self, mode):
        if mode == 'json':
            return {
                'input': self.bp_json_src_edit,
                'dialog': self.bp_json_edit,
                'output': self.bp_json_build_output_edit,
                'encoding': self.bp_json_build_enc_combo,
                'source_encoding': self.bp_json_src_enc_combo
            }
        if mode == 'txt':
            return {
                'input': self.bp_txt_src_edit,
                'dialog': self.bp_txt_edit,
                'output': self.bp_txt_build_output_edit,
                'encoding': self.bp_txt_build_enc_combo,
                'source_encoding': self.bp_txt_src_enc_combo
            }
        return {
            'input': self.bp_asm_input_edit,
            'dialog': None,
            'output': self.bp_asm_output_edit,
            'encoding': self.bp_asm_enc_combo,
            'source_encoding': None
        }

    def _capture_bp_build_shared(self, mode):
        ctrls = self._get_bp_build_controls(mode)
        self._bp_build_shared['input'] = ctrls['input'].text().strip()
        self._bp_build_shared['output'] = ctrls['output'].text().strip()
        self._bp_build_shared['encoding'] = ctrls['encoding'].currentText().strip()
        if ctrls['source_encoding'] is not None:
            self._bp_build_shared['source_encoding'] = ctrls['source_encoding'].currentText().strip()

    def _apply_bp_build_shared(self, mode):
        ctrls = self._get_bp_build_controls(mode)
        if self._bp_build_shared['input']:
            ui_call(ctrls['input'], 'setText', self._bp_build_shared['input'])
        if self._bp_build_shared['output']:
            ui_call(ctrls['output'], 'setText', self._bp_build_shared['output'])
        self._set_combo_value(ctrls['encoding'], self._bp_build_shared['encoding'])
        if ctrls['source_encoding'] is not None:
            src_enc = self._bp_build_shared['source_encoding'] or 'cp932'
            self._set_combo_value(ctrls['source_encoding'], src_enc)

    def on_bp_extract_mode_changed(self):
        mode = self.get_bp_extract_mode()
        if self._bp_extract_last_mode:
            self._capture_bp_extract_shared(self._bp_extract_last_mode)
        index_map = {'bpd': 0, 'json': 1, 'txt': 2}
        self.bp_extract_stack.setCurrentIndex(index_map.get(mode, 0))
        desc_map = {
            'bpd': tr('说明: 将 ._bp 反汇编为 BPD 文本。'),
            'json': tr('说明: 提取所有 push_string 到 JSON，并在导回时同步更新 BPD 字符串区。'),
            'txt': tr('说明: 提取所有 push_string 到 ☆/★ TXT，并在导回时同步更新 BPD 字符串区。')
        }
        ui_call(self.bp_extract_desc_label, 'setText', desc_map.get(mode, ''))
        self._apply_bp_extract_shared(mode)
        self._bp_extract_last_mode = mode

    def on_bp_build_mode_changed(self):
        mode = self.get_bp_build_mode()
        if self._bp_build_last_mode:
            self._capture_bp_build_shared(self._bp_build_last_mode)
        index_map = {'bpd': 0, 'json': 1, 'txt': 2}
        self.bp_build_stack.setCurrentIndex(index_map.get(mode, 0))
        desc_map = {
            'bpd': tr('说明: 将 BPD 文本构建回 ._bp 文件。'),
            'json': tr('说明: 用原始 ._bp + JSON 文本导回并重建 ._bp。'),
            'txt': tr('说明: 用原始 ._bp + TXT 文本导回并重建 ._bp。')
        }
        ui_call(self.bp_build_desc_label, 'setText', desc_map.get(mode, ''))
        self._apply_bp_build_shared(mode)
        self._bp_build_last_mode = mode

    def on_extract_mode_changed(self):
        mode = self.get_extract_mode()
        if self._extract_last_mode:
            self._capture_extract_shared(self._extract_last_mode)
        index_map = {'bsd': 0, 'json': 1, 'txt': 2, 'bp': 3}
        self.extract_stack.setCurrentIndex(index_map.get(mode, 0))
        desc_map = {
            'bsd': tr('说明: 将脚本反汇编为 BSD 文本，适合底层脚本分析。'),
            'json': tr('说明: 提取 name/message 对话到 JSON；支持指定系统函数或用户函数，并自动判断参数角色。'),
            'txt': tr('说明: 提取为 ☆/★ 双行 TXT，导回时只读取 ★ 行；姓名 / 正文 / 选项按参数角色生成 N / T / S。'),
            'bp': tr('说明: 将 ._bp 反汇编为 BPD 文本，可按所选编码读写字符串。')
        }
        ui_call(self.extract_desc_label, 'setText', desc_map.get(mode, ''))
        self._apply_extract_shared(mode)
        self._extract_last_mode = mode

    def on_build_mode_changed(self):
        mode = self.get_build_mode()
        if self._build_last_mode:
            self._capture_build_shared(self._build_last_mode)
        index_map = {'bsd': 0, 'json': 1, 'txt': 2, 'bp': 3}
        self.build_stack.setCurrentIndex(index_map.get(mode, 0))
        desc_map = {
            'bsd': tr('说明: 直接将 BSD 文本构建为脚本文件。'),
            'json': tr('说明: 用原始脚本 + JSON 对话导回构建新脚本；可同步导回指定用户函数文本。'),
            'txt': tr('说明: 用原始脚本 + TXT 对话导回构建新脚本；可同步导回指定用户函数文本。'),
            'bp': tr('说明: 将 BPD 文本按所选编码构建回 ._bp 文件。')
        }
        ui_call(self.build_desc_label, 'setText', desc_map.get(mode, ''))
        self._apply_build_shared(mode)
        self._build_last_mode = mode

    def _default_import_output_path(self, input_path, suffix):
        if os.path.isdir(input_path):
            return os.path.normpath(input_path) + "_build"
        return os.path.splitext(input_path)[0] + suffix

    def _default_extract_output_path(self, input_path, suffix):
        if os.path.isdir(input_path):
            return os.path.normpath(input_path) + "_out"
        return os.path.splitext(input_path)[0] + suffix

    def _run_dialog_extract(self, mode, input_edit, output_edit, enc_combo, ver_combo, ext, user_func_edit=None):
        input_path = input_edit.text().strip()
        if not input_path:
            return
        is_input_dir = os.path.isdir(input_path)
        output_path = output_edit.text().strip()
        if not is_input_dir and output_path and not os.path.isdir(output_path) and not output_path.lower().endswith(ext):
            output_path = output_path + ext
        if not output_path:
            output_path = self._default_extract_output_path(input_path, ext)
        user_function_names = user_func_edit.text().strip() if user_func_edit is not None else ''
        self.start_worker(
            mode,
            input_path,
            output_path,
            enc_combo.currentText().strip(),
            script_version=self._normalize_version_choice(ver_combo.currentText()),
            user_function_names=user_function_names
        )

    def _run_dialog_import(self, mode, input_edit, dialog_edit, output_edit, enc_combo, src_enc_combo, ver_combo, suffix, dialog_type, user_func_edit=None):
        input_path = input_edit.text().strip()
        if not input_path:
            return
        dialog_path = dialog_edit.text().strip()
        if not dialog_path:
            QMessageBox.warning(self, tr('提示'), tr('请先选择 {0} 输入路径', dialog_type))
            return
        output_path = output_edit.text().strip()
        if not output_path:
            output_path = self._default_import_output_path(input_path, suffix)
        user_function_names = user_func_edit.text().strip() if user_func_edit is not None else ''
        self.start_worker(
            mode,
            input_path,
            output_path,
            enc_combo.currentText().strip(),
            dialog_path,
            src_enc_combo.currentText().strip(),
            self._normalize_version_choice(ver_combo.currentText()),
            user_function_names
        )

    def get_extract_mode(self):
        btn = self.extract_type_bg.checkedButton()
        if not btn:
            return 'bsd'
        return btn.property("arg_val")

    def get_build_mode(self):
        btn = self.build_type_bg.checkedButton()
        if not btn:
            return 'bsd'
        return btn.property("arg_val")

    def run_disassemble(self):
        input_path = self.dis_input_edit.text().strip()
        if not input_path:
            return
        output_path = self.dis_output_edit.text().strip()
        if not output_path and os.path.isdir(input_path):
            output_path = os.path.normpath(input_path) + "_out"
        encoding = self.dis_enc_combo.currentText().strip()
        self.start_worker(
            'disassemble',
            input_path,
            output_path,
            encoding,
            script_version=self._normalize_version_choice(self.dis_ver_combo.currentText())
        )

    def run_assemble(self):
        input_path = self.asm_bsd_input_edit.text().strip()
        if not input_path:
            return
        output_path = self.asm_bsd_output_edit.text().strip()
        if not output_path and os.path.isdir(input_path):
            output_path = os.path.normpath(input_path) + "_build"
        encoding = self.asm_bsd_enc_combo.currentText().strip()
        self.start_worker(
            'assemble',
            input_path,
            output_path,
            encoding,
            script_version=self._normalize_version_choice(self.asm_bsd_ver_combo.currentText())
        )

    def run_json_extract(self):
        self._run_dialog_extract(
            'json_extract',
            self.ext_json_input_edit,
            self.ext_json_output_edit,
            self.ext_json_enc_combo,
            self.ext_json_ver_combo,
            ".json",
            self.ext_json_userfunc_edit
        )

    def run_txt_extract(self):
        self._run_dialog_extract(
            'txt_extract',
            self.ext_txt_input_edit,
            self.ext_txt_output_edit,
            self.ext_txt_enc_combo,
            self.ext_txt_ver_combo,
            ".txt",
            self.ext_txt_userfunc_edit
        )

    def run_bp_disassemble(self):
        self._run_bp_extract()

    def run_bp_json_extract(self):
        input_path = self.bp_json_input_edit.text().strip()
        if not input_path:
            return
        output_path = self.bp_json_output_edit.text().strip()
        if not output_path:
            output_path = self._default_extract_output_path(input_path, ".json")
        elif not os.path.isdir(input_path) and not os.path.isdir(output_path) and not output_path.lower().endswith(".json"):
            output_path = output_path + ".json"
        self.start_worker('bp_json_extract', input_path, output_path, self.bp_json_enc_combo.currentText().strip())

    def run_bp_txt_extract(self):
        input_path = self.bp_txt_input_edit.text().strip()
        if not input_path:
            return
        output_path = self.bp_txt_output_edit.text().strip()
        if not output_path:
            output_path = self._default_extract_output_path(input_path, ".txt")
        elif not os.path.isdir(input_path) and not os.path.isdir(output_path) and not output_path.lower().endswith(".txt"):
            output_path = output_path + ".txt"
        self.start_worker('bp_txt_extract', input_path, output_path, self.bp_txt_enc_combo.currentText().strip())

    def run_json_import(self):
        self._run_dialog_import(
            'json_import',
            self.asm_json_src_edit,
            self.asm_json_edit,
            self.asm_json_output_edit,
            self.asm_json_enc_combo,
            self.asm_json_src_enc_combo,
            self.asm_json_ver_combo,
            "_jsonimp",
            "JSON",
            self.asm_json_userfunc_edit
        )

    def run_txt_import(self):
        self._run_dialog_import(
            'txt_import',
            self.asm_txt_src_edit,
            self.asm_txt_edit,
            self.asm_txt_output_edit,
            self.asm_txt_enc_combo,
            self.asm_txt_src_enc_combo,
            self.asm_txt_ver_combo,
            "_txtimp",
            "TXT",
            self.asm_txt_userfunc_edit
        )

    def run_bp_assemble(self):
        self._run_bp_build()

    def run_bp_json_import(self):
        input_path = self.bp_json_src_edit.text().strip()
        if not input_path:
            return
        dialog_path = self.bp_json_edit.text().strip()
        if not dialog_path:
            QMessageBox.warning(self, tr('提示'), tr('请先选择 JSON 输入路径'))
            return
        output_path = self._normalize_bp_output_path(
            input_path,
            self.bp_json_build_output_edit.text().strip(),
            "._bp"
        )
        self.start_worker(
            'bp_json_import',
            input_path,
            output_path,
            self.bp_json_build_enc_combo.currentText().strip(),
            dialog_path,
            self.bp_json_src_enc_combo.currentText().strip()
        )

    def run_bp_txt_import(self):
        input_path = self.bp_txt_src_edit.text().strip()
        if not input_path:
            return
        dialog_path = self.bp_txt_edit.text().strip()
        if not dialog_path:
            QMessageBox.warning(self, tr('提示'), tr('请先选择 TXT 输入路径'))
            return
        output_path = self._normalize_bp_output_path(
            input_path,
            self.bp_txt_build_output_edit.text().strip(),
            "._bp"
        )
        self.start_worker(
            'bp_txt_import',
            input_path,
            output_path,
            self.bp_txt_build_enc_combo.currentText().strip(),
            dialog_path,
            self.bp_txt_src_enc_combo.currentText().strip()
        )

    def start_worker(self, mode, input_path, output_path, encoding, dialog_path='', source_encoding='', script_version='auto', user_function_names='', bp_engine='auto'):
        if mode in ('json_extract', 'txt_extract', 'json_import', 'txt_import'):
            self._apply_user_function_names(user_function_names, persist=True)
        self.toggle_ui(False)
        ui_call(self.log_view, 'clear')
        mode_name_map = {
            'disassemble': tr('反汇编'),
            'assemble': tr('汇编'),
            'json_extract': tr('提取对话JSON'),
            'txt_extract': tr('提取对话TXT'),
            'json_import': tr('JSON导回'),
            'txt_import': tr('TXT导回'),
            'bp_disassemble': tr('BP反汇编'),
            'bp_json_extract': tr('BP JSON 提取'),
            'bp_txt_extract': tr('BP TXT 提取'),
            'bp_assemble': tr('BP构建'),
            'bp_json_import': tr('BP JSON 导回'),
            'bp_txt_import': tr('BP TXT 导回')
        }
        mode_cn = mode_name_map.get(mode, mode)
        self.log_view.append(tr('--- 开始任务: {0} ({1}) ---', mode_cn, mode))
        self.log_view.append(tr('输入路径: {0}', input_path))
        self.log_view.append(tr('输出路径: {0}', output_path))
        if dialog_path:
            self.log_view.append(tr('文本路径: {0}', dialog_path))
        self.log_view.append(tr('输出编码: {0}', encoding))
        if user_function_names and mode in ('json_extract', 'txt_extract', 'json_import', 'txt_import'):
            self.log_view.append(tr('文本函数规则: {0}', user_function_names))
        if mode in ('json_import', 'txt_import'):
            self.log_view.append(tr('原始剧本编码: {0}', source_encoding or encoding))
        if mode in ('bp_json_import', 'bp_txt_import'):
            self.log_view.append(tr('原始BP编码: {0}', source_encoding or encoding))
        if mode not in ('bp_disassemble', 'bp_json_extract', 'bp_txt_extract', 'bp_assemble', 'bp_json_import', 'bp_txt_import'):
            self.log_view.append(tr('脚本版本: {0}', script_version))
        self.worker = WorkerThread(
            mode,
            input_path,
            output_path,
            encoding,
            dialog_path,
            source_encoding,
            script_version,
            user_function_names,
            bp_engine=bp_engine,
        )
        self.worker.log_signal.connect(self.log_message)
        self.worker.finished_signal.connect(self.on_finished)
        self.worker.start()

    def log_message(self, msg):
        self.log_view.moveCursor(QTextCursor.MoveOperation.End)
        if msg.endswith('\n'):
            self.log_view.insertPlainText(msg)
        else:
            self.log_view.insertPlainText(msg + "\n")
        self.log_view.moveCursor(QTextCursor.MoveOperation.End)

    def on_finished(self, success, msg):
        self.toggle_ui(True)
        if success:
            QMessageBox.information(self, "Success", msg)
        else:
            QMessageBox.warning(self, "Error", msg)

    def toggle_ui(self, enabled):
        self.btn_dis.setEnabled(enabled)
        self.btn_json_extract.setEnabled(enabled)
        self.btn_txt_extract.setEnabled(enabled)
        self.btn_bp_dis.setEnabled(enabled)
        self.btn_bp_json_extract.setEnabled(enabled)
        self.btn_bp_txt_extract.setEnabled(enabled)
        self.btn_asm.setEnabled(enabled)
        self.btn_json_import.setEnabled(enabled)
        self.btn_txt_import.setEnabled(enabled)
        self.btn_bp_asm.setEnabled(enabled)
        self.btn_bp_json_import.setEnabled(enabled)
        self.btn_bp_txt_import.setEnabled(enabled)
        self.tabs.setEnabled(enabled)

    def detect_system_theme(self):
        self.theme_combo.blockSignals(True)
        self.theme_combo.setCurrentText("跟随系统")
        self.theme_combo.blockSignals(False)
        self.apply_theme("跟随系统")

    def _is_system_dark_theme(self):
        if HAS_DARKDETECT:
            try:
                detected = darkdetect.isDark()
                if detected is not None:
                    return bool(detected)
            except Exception:
                pass
        if winreg is not None:
            try:
                with winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER,
                    r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
                ) as key:
                    value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
                return int(value) == 0
            except Exception:
                pass
        return self.palette().window().color().lightness() < 128

    def _resolve_theme_name(self, theme_name):
        if theme_name == "跟随系统":
            return "现代深色" if self._is_system_dark_theme() else "现代浅色"
        return theme_name

    def apply_theme(self, theme_name):
        real_theme = self._resolve_theme_name(theme_name)

        light_qss = """
        QWidget { font-family: 'Segoe UI', 'Microsoft YaHei', sans-serif; font-size: 10pt; color: #333333; }
        QWidget#MainBackground { background-color: #f5f7fa; }
        QFrame#CardFrame { background-color: #ffffff; border: 1px solid #e1e4e8; border-radius: 8px; }
        QLabel#AppTitle { font-size: 18pt; font-weight: bold; color: #2c3e50; }
        QLineEdit { padding: 8px; border: 1px solid #ced4da; border-radius: 4px; background: #ffffff; color: #333; }
        QLineEdit:focus { border: 1px solid #3498db; }
        QPushButton#SecondaryButton { background-color: #ffffff; border: 1px solid #dcdfe6; border-radius: 4px; color: #606266; padding: 6px 12px; }
        QPushButton#SecondaryButton:hover { border-color: #c6e2ff; color: #409eff; background-color: #ecf5ff; }
        QPushButton#PrimaryButton { background-color: #3498db; border: 1px solid #3498db; border-radius: 4px; color: #ffffff; font-weight: bold; padding: 8px 16px; }
        QPushButton#PrimaryButton:hover { background-color: #5dade2; border-color: #5dade2; }
        QTabWidget::pane { border: 1px solid #e1e4e8; background: #fff; border-radius: 5px; }
        QTabBar::tab { background: #e8ebf0; color: #666; padding: 10px 20px; margin-right: 2px; border-top-left-radius: 4px; border-top-right-radius: 4px; }
        QTabBar::tab:selected { background: #ffffff; color: #3498db; font-weight: bold; }
        QTextEdit#LogConsole { background-color: #fcfcfc; color: #333333; border: 1px solid #e1e4e8; font-family: 'Consolas', monospace; font-size: 9pt; }
        QWidget#LogHeader { background-color: #f1f1f1; border-bottom: 1px solid #ddd; }
        QComboBox { padding: 4px; color: #333; background: #fff; border: 1px solid #ced4da; border-radius: 4px; }
        QComboBox QAbstractItemView { background-color: #ffffff; color: #333333; selection-background-color: #e6f7ff; selection-color: #333333; }
        QLabel#TipLabel { font-size: 9pt; color: #888888; }
        """

        dark_qss = """
        QWidget { font-family: 'Segoe UI', 'Microsoft YaHei', sans-serif; font-size: 10pt; color: #e0e0e0; }
        QWidget#MainBackground { background-color: #1e1e1e; }
        QFrame#CardFrame { background-color: #2d2d2d; border: 1px solid #444; border-radius: 8px; }
        QLabel { color: #e0e0e0; }
        QLabel#AppTitle { color: #ffffff; font-size: 18pt; font-weight: bold; }
        QLineEdit { background: #1a1a1a; border: 1px solid #555; border-radius: 4px; color: #ffffff; padding: 8px; }
        QLineEdit:focus { border: 1px solid #bb86fc; }
        QPushButton#SecondaryButton { background: #333; border: 1px solid #555; color: #ddd; border-radius: 4px; }
        QPushButton#SecondaryButton:hover { background: #444; border-color: #777; }
        QPushButton#PrimaryButton { background: #bb86fc; border: 1px solid #bb86fc; color: #121212; border-radius: 4px; font-weight:bold; }
        QPushButton#PrimaryButton:hover { background: #d0aaff; }
        QTabWidget::pane { border: 1px solid #444; background: #2d2d2d; }
        QTabBar::tab { background: #1e1e1e; color: #999; padding: 10px 20px; border-top-left-radius: 4px; border-top-right-radius: 4px; margin-right:2px;}
        QTabBar::tab:selected { background: #2d2d2d; color: #bb86fc; font-weight:bold; }
        QTextEdit#LogConsole { background-color: #1a1a1a; color: #e0e0e0; border: 1px solid #444; font-family: 'Consolas', monospace; font-size: 9pt; }
        QWidget#LogHeader { background-color: #252525; border-bottom: 1px solid #444; }
        QComboBox { padding: 4px; color: #e0e0e0; background: #333; border: 1px solid #555; border-radius: 4px; }
        QComboBox QAbstractItemView { background-color: #2d2d2d; color: #e0e0e0; selection-background-color: #bb86fc; selection-color: #121212; }
        QLabel#TipLabel { color: #999999; font-size: 9pt; }
        """

        cyber_qss = """
        QWidget { font-family: 'Segoe UI', 'Microsoft YaHei', sans-serif; font-size: 10pt; color: #00ffcc; }
        QWidget#MainBackground { background-color: #0d0d15; }
        QFrame#CardFrame { background-color: #1a1a2e; border: 1px solid #00ffcc; border-radius: 8px; }
        QLabel { color: #00ffcc; }
        QLabel#AppTitle { color: #ff00ff; font-size: 18pt; font-weight: bold; text-shadow: 0 0 5px #ff00ff; }
        QLineEdit { background: #0f0f1a; border: 1px solid #ff00ff; border-radius: 4px; color: #00ffcc; padding: 8px; }
        QPushButton#SecondaryButton { background: #0b0b19; border: 1px solid #00ffcc; color: #00ffcc; }
        QPushButton#PrimaryButton { background: #ff0055; border: 1px solid #ff0055; color: #ffffff; font-weight: bold; }
        QTabWidget::pane { border: 1px solid #00ffcc; background: #0d0d15; }
        QTabBar::tab { background: #0d0d15; color: #008888; border: 1px solid #004444; padding: 10px; }
        QTabBar::tab:selected { color: #00ffcc; border: 1px solid #00ffcc; }
        QTextEdit#LogConsole { background-color: #0f0f1f; color: #00ffcc; border: 1px solid #00ffcc; }
        QWidget#LogHeader { background-color: #121225; border-bottom: 1px solid #00ffcc; }
        QComboBox { background: #0d0d15; color: #00ffcc; border: 1px solid #00ffcc; }
        QComboBox QAbstractItemView { background-color: #0d0d15; color: #00ffcc; selection-background-color: #ff0055; }
        QLabel#TipLabel { color: #0088aa; font-size: 9pt; }
        """

        if real_theme == "现代深色":
            self.setStyleSheet(dark_qss)
        elif real_theme == "赛博朋克":
            self.setStyleSheet(cyber_qss)
        else:
            self.setStyleSheet(light_qss)

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
