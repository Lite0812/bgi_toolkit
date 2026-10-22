"""Canonical encodings for game text; legacy Shift-JIS labels mean CP932."""


def normalize_encoding(encoding):
    if not encoding:
        return encoding
    name = str(encoding).strip().lower()
    if name.replace('-', '_') in ('sjis', 'shift_jis', 'shiftjis', 's_jis', 'cp932', 'ms932', 'ms_kanji', 'windows_31j', '932'):
        return 'cp932'
    if name in ('utf8', 'utf_8'):
        return 'utf-8'
    if name == 'big-5':
        return 'big5'
    return name


def encoding_candidates(primary=None, fallback=None):
    result = []
    for value in (primary or 'cp932', fallback, 'cp932', 'gbk', 'utf-8'):
        name = normalize_encoding(value)
        if name and name not in result:
            result.append(name)
    return result
