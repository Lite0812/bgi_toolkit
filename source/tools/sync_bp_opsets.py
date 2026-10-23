"""Update factual BP availability masks from a local OpenBGI source checkout."""
from pathlib import Path
import argparse
import hashlib
import json
import re


def read_survey(checkout):
    checkout = Path(checkout)
    header = (checkout / 'inc/bgi/version.h').read_text(encoding='utf8')
    enums = re.findall(r'^\s*(GEN_1_\w+),', header, re.M)
    generations = []
    for enum in enums:
        numbers = enum.removeprefix('GEN_').split('_')
        generations.append('.'.join(numbers[:2]) + ('/' + numbers[2] if len(numbers) == 3 else ''))
    source = checkout / 'src/vm/opset.c'
    text = source.read_text(encoding='utf8')
    families = {}
    for match in re.finditer(r'// (main table|family ([0-9A-Fa-f]+))[^\r\n]*\n\s*\{([^}]+)\}', text):
        key = 'main' if match[1] == 'main table' else match[2].upper()
        values = re.findall(r'0x([0-9a-fA-F]+)', match[3])
        if len(values) != 256:
            raise ValueError(f'{key}: expected 256 entries, got {len(values)}')
        families[key] = [f'{int(value, 16):06x}' for value in values]
    if not generations or 'main' not in families:
        raise ValueError('Incomplete OpenBGI opcode survey')
    return enums, generations, families


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('checkout', type=Path)
    parser.add_argument('-o', '--output', type=Path, required=True)
    args = parser.parse_args()
    _, generations, families = read_survey(args.checkout)
    # EF is OpenBGI's private extension family, not an original BGI opcode.
    families.pop('EF', None)
    families['main'][0xEF] = '000000'
    result = {
        'source': 'OpenBGI executable survey, docs/versions.md and src/vm/opset.c',
        'source_sha256': hashlib.sha256((args.checkout / 'src/vm/opset.c').read_bytes()).hexdigest(),
        'generations': generations,
        'families': families,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf8')
    print(f'Wrote {len(generations)} generations, {len(families)} opcode families: {args.output}')


if __name__ == '__main__':
    main()
