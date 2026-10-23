"""Seed an output directory's bsd2c_defs.json from a target's bgiscn command table."""
from pathlib import Path
import argparse
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bgi_toolkit.bsd2c.command_table import import_command_table


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("table", type=Path, help="UTF-8 output of bgiscn --scan sysprg.arc")
    parser.add_argument("-o", "--output", required=True, type=Path, help="destination bsd2c_defs.json (existing user definitions are retained)")
    args = parser.parse_args()
    destination, count = import_command_table(args.table, args.output)
    print(f"Imported {count} target command signatures: {destination}")


if __name__ == "__main__":
    main()
