#!/usr/bin/env python3

# BGI ._bp file disassembler

import glob
import io
import os
import struct
import sys
import json
from dataclasses import asdict

from ..common import asdis
from . import bpop
from ...bp_engine import PROFILES, detect_engine, engine_scope, get_profile

last_string_refs = {}
last_string_ref_order = []
last_debug_string_refs = {}
last_str_data_offset = 0
_OP_STRUCTS = {
	fmt: struct.Struct(fmt)
	for fmt, _, _ in bpop.ops.values()
	if fmt
}

def get_code_end(data):
	search_end = len(data)
	fallback = -1
	while True:
		pos = data.rfind(b'\x17', 0, search_end)
		if pos < 0:
			return fallback + 1
		if fallback < 0:
			fallback = pos
		pad_end = ((pos + 1 + 0x0F) // 0x10) * 0x10
		if pad_end <= len(data) and not data[pos + 1:pad_end].strip(b'\x00'):
			return pos + 1
		search_end = pos

def _encode_text(text, encoding):
	value = asdis.unescape(text)
	if value.lower().startswith('@hex:'):
		blob = value[5:]
		if len(blob) % 2 != 0:
			raise ValueError('invalid hex text blob')
		return bytes.fromhex(blob)
	return value.encode(bpop.normalize_encoding(encoding))


def _iter_strdata_regions(str_data, rel_refs, encoding):
	"""Yield string and raw-data regions in one forward pass.

	The old implementation searched all reference offsets for every gap,
	which made sparse string tables quadratic in the number of references.
	"""
	data_len = len(str_data)
	sorted_offsets = sorted(rel_refs)
	ref_index = 0
	current_pos = 0
	while current_pos < data_len:
		while ref_index < len(sorted_offsets) and sorted_offsets[ref_index] < current_pos:
			ref_index += 1
		if current_pos in rel_refs:
			string = rel_refs[current_pos]
			end_pos = current_pos + len(_encode_text(string, encoding)) + 1
			yield True, current_pos, end_pos, string
			current_pos = end_pos
			continue
		next_ref_pos = sorted_offsets[ref_index] if ref_index < len(sorted_offsets) else data_len
		if next_ref_pos <= current_pos:
			# Defensive guard for malformed reference maps.
			next_ref_pos = current_pos + 1
		yield False, current_pos, next_ref_pos, None
		current_pos = next_ref_pos

def out_smart_strdata(fo, str_data, string_refs, str_data_offset, code_refs_order, encoding, force_output=False):
	valid_refs = {k: v for k, v in string_refs.items() if 0 <= k - str_data_offset < len(str_data)}
	current_calc_pos = 0
	seen_offsets = set()
	is_standard = True
	for abs_offset in code_refs_order:
		if abs_offset not in valid_refs or abs_offset in seen_offsets:
			continue
		seen_offsets.add(abs_offset)
		rel_offset = abs_offset - str_data_offset
		if rel_offset != current_calc_pos:
			is_standard = False
			break
		current_calc_pos += len(_encode_text(valid_refs[abs_offset], encoding)) + 1
	if is_standard and current_calc_pos != len(str_data):
		is_standard = False
	if is_standard and len(seen_offsets) != len(valid_refs):
		is_standard = False
	if is_standard and not force_output:
		return
	rel_refs = {k - str_data_offset: v for k, v in valid_refs.items()}
	for is_string, current_pos, next_pos, string in _iter_strdata_regions(str_data, rel_refs, encoding):
		if is_string:
			fo.write('"%s"\n' % string)
			continue
		gap_data = str_data[current_pos:next_pos]
		hex_str = gap_data.hex()
		for i in range(0, len(hex_str), 128):
			fo.write('#strdata "%s"\n' % hex_str[i:i+128])

def parse(code, exact_mode=False, engine=None):
	with engine_scope(get_profile(engine)):
		return _parse(code, exact_mode)


def _parse(code, exact_mode=False):
	global last_string_refs, last_string_ref_order, last_debug_string_refs, last_str_data_offset
	bpop.clear_offsets()
	inst = {}
	string_refs = {}
	string_ref_order = []
	debug_string_refs = {}
	size = get_code_end(code)
	pos = 0
	while pos < size:
		addr = pos
		op = code[addr]
		entry = bpop.get_op_entry(op)
		if entry is None:
			raise ValueError('undefined opcode %02x for BP engine %s @ offset %05x' % (op, get_profile().name, addr))
		pos += 1
		if op in bpop.subop_group_ops or op in bpop.var_ops:
			text, n = bpop.decode_var_instr(op, code, addr, pos)
			inst[addr] = text
			pos += n
			continue
		fmt, pfmt, fcn = entry
		if fmt:
			op_struct = _OP_STRUCTS[fmt]
			n = op_struct.size
			raw_args = op_struct.unpack_from(code, pos)
			args = raw_args
			if fcn:
				args = fcn(code, addr, *raw_args)
			if op == 0x05:
				target = addr + raw_args[0]
				if exact_mode:
					# CP932 has duplicate byte spellings for some characters. A
					# successful decode alone does not guarantee a lossless rebuild.
					raw_text = code[target:code.find(b'\x00', target)]
					try:
						lossless = _encode_text(args, bpop.current_text_encoding) == raw_text
					except (ValueError, UnicodeError):
						lossless = False
					if not lossless:
						args = '@hex:%s' % raw_text.hex()
				debug_string_refs[addr] = (target, args)
				string_refs[target] = args
				string_ref_order.append(target)
				if exact_mode:
					inst[addr] = 'push_string("%s")' % args
				else:
					inst[addr] = pfmt % args
			else:
				inst[addr] = pfmt % args
			pos += n
		else:
			inst[addr] = pfmt
	offsets = bpop.offsets.copy()
	last_string_refs = string_refs
	last_string_ref_order = string_ref_order
	last_debug_string_refs = debug_string_refs
	last_str_data_offset = size
	return inst, offsets, size, code[size:]
	
def out(fo, inst, offsets, size, engine=None):
	fo.write('#runtime-relative-branches\n')
	if engine is not None:
		profile = get_profile(engine)
		fo.write('#bp-engine %s\n' % profile.name)
		if PROFILES.get(profile.name) != profile:
			fo.write('#bp-profile %s\n' % json.dumps(asdict(profile), ensure_ascii=False))
	addrs = list(inst)
	extra_offsets = sorted(x for x in offsets if x not in inst)
	eidx = 0
	for addr in addrs:
		while eidx < len(extra_offsets) and extra_offsets[eidx] < addr:
			extra = extra_offsets[eidx]
			fo.write('\n#label-addend L%05x %+d\n' % (extra, extra - addr))
			fo.write('L%05x:\n' % extra)
			eidx += 1
		if addr in offsets:
			fo.write('\nL%05x:\n' % addr)
		fo.write('\t%s;\n' % inst[addr])
	while eidx < len(extra_offsets):
		extra = extra_offsets[eidx]
		fo.write('\n#label-addend L%05x %+d\n' % (extra, extra - size))
		fo.write('L%05x:\n' % extra)
		eidx += 1

def out_strdata(fo, str_data, encoding):
	if not str_data:
		return
	fo.write('\n#strings\n')
	out_smart_strdata(fo, str_data, last_string_refs, last_str_data_offset, last_string_ref_order, encoding, True)

def out_debug_strdata(fo, str_data, encoding):
	if not str_data:
		return
	fo.write('\n#strings\n')
	valid_refs = {k: v for k, v in last_string_refs.items() if 0 <= k - last_str_data_offset < len(str_data)}
	rel_refs = {k - last_str_data_offset: v for k, v in valid_refs.items()}
	for is_string, current_pos, next_pos, string in _iter_strdata_regions(str_data, rel_refs, encoding):
		if is_string:
			abs_offset = last_str_data_offset + current_pos
			fo.write('L%05x: "%s"\n' % (abs_offset, string))
			continue
		gap_data = str_data[current_pos:next_pos]
		hex_str = gap_data.hex()
		for i in range(0, len(hex_str), 128):
			fo.write('#strdata "%s"\n' % hex_str[i:i+128])

def out_debug(fo, inst, code, size, encoding):
	addrs = sorted(inst)
	for idx, addr in enumerate(addrs):
		next_addr = addrs[idx + 1] if idx + 1 < len(addrs) else size
		last_addr = next_addr - 1
		raw = code[addr:next_addr]
		text = inst[addr]
		if addr in last_debug_string_refs:
			target, string = last_debug_string_refs[addr]
			text = 'push_string(L%05x) // "%s"' % (target, string)
		fo.write('%05x-%05x  %-16s  %s\n' % (addr, last_addr, raw.hex(), text))
	out_debug_strdata(fo, code[size:], encoding)

def dis(file, debug=False, exact_mode=False, encoding='cp932', output_path=None,
        engine='auto', games_path=None, executable=None):
	detection = detect_engine(file, engine, games_path=games_path, executable=executable)
	bpop.set_text_encoding(encoding)
	ofile = output_path or (os.path.splitext(file)[0] + '.bpd')
	with open(file, 'rb') as fi:
		hdrsize, = struct.unpack('<I', fi.read(4))
		fi.seek(hdrsize, 0)
		code = fi.read()
	inst, offsets, size, str_data = parse(code, exact_mode=exact_mode, engine=detection.profile)
	with open(ofile, 'w', encoding='utf-8') as fo:
		buffer = io.StringIO()
		out(buffer, inst, offsets, size, engine=detection.profile)
		if exact_mode:
			out_strdata(buffer, str_data, encoding)
		fo.write(buffer.getvalue())
	if debug:
		dbg_file = os.path.splitext(file)[0] + '.bpd.debug.txt'
		with open(dbg_file, 'w', encoding='utf-8') as fd:
			out_debug(fd, inst, code, size, encoding)
	return detection
	
if __name__ == '__main__':
	debug = False
	exact_mode = False
	encoding = 'cp932'
	args = []
	argv = sys.argv[1:]
	i = 0
	while i < len(argv):
		arg = argv[i]
		if arg in ('-d', '--debug'):
			debug = True
		elif arg in ('-e', '--exact'):
			exact_mode = True
		elif arg in ('-c', '--encoding') and i + 1 < len(argv):
			encoding = argv[i + 1]
			i += 1
		else:
			args.append(arg)
		i += 1
	if len(args) < 1:
		print('Usage: bpdis.py [--debug] [--exact] [-c|--encoding <enc>] <file(s)>')
		print('(only ._bp files amongst <file(s)> will be processed)')
		print('  -e, --exact: Output exact trailing string area and keep push_string as readable strings')
		sys.exit(1)
	for arg in args:
		for script in glob.glob(arg):
			base, ext = os.path.splitext(script)
			if ext == '._bp':
				print('Disassembling %s...' % script)
				dis(script, debug=debug, exact_mode=exact_mode, encoding=encoding)
	
