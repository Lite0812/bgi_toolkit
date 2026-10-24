"""BSI conversion in the shared file conversion workspace."""
from PyQt6.QtWidgets import QMessageBox

from ..bsi_tool import default_output, run_bsi_task
from ..i18n import tr
from .database import DatabaseToolWidget, DatabaseWorker


class BsiWorker(DatabaseWorker):
    task_runner = staticmethod(run_bsi_task)


class BsiToolWidget(DatabaseToolWidget):
    settings_filename = 'bsi.ini'
    worker_class = BsiWorker
    default_output = staticmethod(default_output)
    kind_choices = ()
    task_titles = ('导出 JSON', '重建 BSI', '检查与验证')
    input_placeholder = '拖入 BSI、JSON 或所在目录'
    descriptions = {
        'export': '读取 BP2 的 ._bsi / .bsi 字符串表，导出 UTF-8 JSON，保留块名、key、顺序和空字符串。',
        'build': '从 JSON 重建 BSI。目录模式处理 ._bsi.json / .bsi.json，保留相对目录及原文件名。',
        'check': '检查 BSI 结构，再执行“二进制 → JSON → 二进制”，确认重建结果与原文件逐字节一致。'}

    def _show_help(self):
        QMessageBox.information(self, tr('BSI 转换说明'), tr(
            '导出：选择 ._bsi / .bsi 文件或所在目录，生成“原文件名.json”。JSON 结构为“块名 → key → value”，全部使用 UTF-8，不生成 meta。\n\n'
            '重建：选择 JSON 或所在目录。单文件可使用任意 JSON 文件名；目录模式只处理 ._bsi.json / .bsi.json，恢复原文件名和相对目录。\n\n'
            '修改 value 即可替换文本，请保留块名和 key。空字符串原样保留；非法类型、重复名称、NUL 字符、损坏结构及尾部多余数据会报错。\n\n'
            '检查：只验证并显示块数和条目数，不写出文件。默认不覆盖已有输出；若要写回原资源，请先选择输出路径并勾选允许覆盖已有输出。'))
