#!/usr/bin/env python3

# BGI ._bp file assembler

import glob
import os
import re
import struct
import sys

from ..common import asdis
from . import bpop
from ...bp_engine import declared_profile, detect_engine, engine_scope, get_profile

re_runtime_relative_branches = re.compile(r'^#runtime-relative-branches$', re.IGNORECASE)
re_label_addend = re.compile(r'^#label-addend\s+([A-Za-z_][A-Za-z0-9_]*)\s+([+-]?(?:0[xX][0-9A-Fa-f]+|\d+))$', re.IGNORECASE)
re_absolute_label = re.compile(r'^L(-?)([0-9A-Fa-f]+)$')


def _parse_strdata_line(line):
	match = asdis.re_strdata.match(line)
	if match:
		hex_str, = match.groups()
		return bytes.fromhex(hex_str)
	return None

def _encode_text_token(token, encoding):
	text = asdis.unescape(token[1:-1])
	if text.lower().startswith('@hex:'):
		blob = text[5:]
		if len(blob) % 2 != 0:
			raise ValueError('invalid hex text blob')
		return bytes.fromhex(blob)
	return text.encode(bpop.normalize_encoding(encoding))

def _parse_string_data_line(line, encoding):
	line = line.strip()
	if len(line) >= 2 and line[0] == '"' and line[-1] == '"':
		return _encode_text_token(line, encoding) + b'\x00', line
	return None

def _parse_absolute_label(token):
	match = re_absolute_label.match(token)
	if match is None:
		return None
	sign, value = match.groups()
	return -int(value, 16) if sign else int(value, 16)

def _resolve_fixed_relative_arg(arg, pos, symbols, line_no):
	if arg in symbols:
		value = symbols[arg] - pos
	else:
		absolute = _parse_absolute_label(arg)
		if absolute is not None:
			value = absolute - pos
		else:
			# A numeric operand remains an explicit raw displacement for backwards
			# compatibility. Relocatable sources should use a defined label.
			try:
				value = int(arg, 0)
			except ValueError:
				raise asdis.InvalidInstructionFormat(
					'Undefined relative target %s @ line %d' % (arg, line_no))
	if not -0x8000 <= value <= 0x7FFF:
		raise asdis.InvalidInstructionFormat(
			'Relative target out of signed 16-bit range @ line %d' % line_no)
	return value

def parse_instr(line, n, match=None):
	strings = []
	match = match or asdis.re_instr.match(line)
	fcn, argstr = match.groups()
	argstr = argstr.strip()
	if argstr:
		argstr = argstr.replace('\\\\', asdis.backslash_replace).replace('\\"', asdis.quote_replace)
		quotes = asdis.get_quotes(argstr, n)
		if len(quotes) %2 != 0:
			raise asdis.QuoteMismatch('Mismatched quotes @ line %d' % n)
		argstr = asdis.replace_quote_commas(argstr, quotes)
		brackets = 0
		in_quote = False
		argchars = list(argstr)
		for i, ch in enumerate(argchars):
			if ch == '"':
				in_quote = not in_quote
			elif in_quote:
				continue
			elif ch == '[':
				brackets += 1
			elif ch == ']':
				brackets -= 1
				if brackets < 0:
					raise asdis.InvalidInstructionFormat('Mismatched brackets @ line %d' % n)
			elif ch == ',' and brackets > 0:
				argchars[i] = asdis.comma_replace
		if brackets != 0:
			raise asdis.InvalidInstructionFormat('Mismatched brackets @ line %d' % n)
		argstr = ''.join(argchars)
		args = [x.strip().replace(asdis.comma_replace, ',').replace(asdis.quote_replace, '\\"').replace(asdis.backslash_replace, '\\\\') for x in argstr.split(',')]
		for arg in args:
			if arg and arg[0] == '"' and arg[-1] == '"':
				strings.append(arg)
	else:
		args = []
	return fcn, args, strings

class EngineInstructions(list):
	"""Carry the profile through the existing five-item assembler API."""


def parse(asmtxt, encoding='cp932', engine=None):
	declared = declared_profile(asmtxt) if engine is None or engine == 'auto' else None
	profile = (declared or get_profile()) if engine is None or engine == 'auto' else get_profile(engine)
	with engine_scope(profile):
		return _parse(asmtxt, encoding)


def _parse(asmtxt, encoding):
	instrs = EngineInstructions()
	instrs.engine = get_profile()
	symbols = {}
	label_addends = {}
	text_set = set()
	text_list = []
	explicit_items = []
	pos = 0
	for line_no, raw_line in enumerate(asmtxt.split('\n'), start=1):
		stripped_line = raw_line.strip()
		if re.match(r'^#bp-(?:engine|profile)(?:\s|$)', stripped_line):
			continue
		if re_runtime_relative_branches.match(stripped_line):
			continue
		addend_match = re_label_addend.match(stripped_line)
		if addend_match:
			symbol, value = addend_match.groups()
			if symbol in label_addends:
				raise asdis.InvalidInstructionFormat('Duplicate label addend for %s @ line %d' % (symbol, line_no))
			label_addends[symbol] = int(value, 0)
			continue
		line = asdis.remove_comment(stripped_line)
		if not line:
			continue
		if asdis.re_strings_start.match(line):
			continue
		chunk = _parse_strdata_line(line)
		if chunk is not None:
			explicit_items.append((None, chunk))
			continue
		string_chunk = _parse_string_data_line(line, encoding)
		if string_chunk is not None:
			chunk, token = string_chunk
			explicit_items.append((token, chunk))
			continue
		label_match = asdis.re_label.match(line)
		if label_match:
			symbol, = label_match.groups()
			if symbol in symbols:
				raise asdis.InvalidInstructionFormat('Duplicate label %s @ line %d' % (symbol, line_no))
			symbols[symbol] = pos + label_addends.pop(symbol, 0)
		else:
			instruction_match = asdis.re_instr.match(line)
			if instruction_match is None:
				raise asdis.InvalidInstructionFormat('Invalid instruction format @ line %d' % line_no)
			fcn, args, strings = parse_instr(line, line_no, instruction_match)
			record = fcn, args, pos, line_no
			collect_strings = fcn == 'push_string' and len(args) == 1
			for text in strings:
				if not collect_strings or text in text_set:
					continue
				text_set.add(text)
				text_list.append(text)
			instrs.append(record)
			try:
				op = bpop.rops[fcn]
			except KeyError:
				raise asdis.InvalidFunction('Invalid function @ line %d' % line_no)
			bpop.validate_instruction_name(fcn, op)
			pos += bpop.get_instr_size(op, args)
	explicit_blob = b'' if explicit_items else None
	texts = []
	if explicit_blob is None:
		while pos % 0x10 != 0:
			pos += 1
		for text in text_list:
			symbols[text] = pos
			data = _encode_text_token(text, encoding)
			texts.append(data)
			pos += len(data) + 1
		while pos % 0x10 != 0:
			pos += 1
	else:
		explicit_offsets = {}
		explicit_blob_builder = bytearray()
		cursor = 0
		for token, chunk in explicit_items:
			if token is not None:
				explicit_offsets.setdefault(token, []).append(pos + cursor)
			explicit_blob_builder.extend(chunk)
			cursor += len(chunk)
		explicit_blob = bytes(explicit_blob_builder)
		usage = {}
		for idx, (fcn, args, ipos, n) in enumerate(instrs):
			if fcn != 'push_string' or len(args) != 1 or not args[0] or args[0][0] != '"':
				continue
			token = args[0]
			if token not in explicit_offsets:
				raise asdis.InvalidInstructionFormat('Missing string data for push_string @ line %d' % n)
			ref_idx = usage.get(token, 0)
			offsets = explicit_offsets[token]
			target = offsets[ref_idx] if ref_idx < len(offsets) else offsets[0]
			usage[token] = ref_idx + 1
			# Keep string-table offsets numeric. A generated Lxxxxx spelling can
			# collide with a relocated code label after instructions are edited.
			instrs[idx] = (fcn, [token, '0x%x' % target], ipos, n)
		pos += len(explicit_blob)
	if label_addends:
		symbol = next(iter(label_addends))
		raise asdis.InvalidInstructionFormat('Label addend has no matching label: %s' % symbol)
	size = pos
	return instrs, symbols, texts, size, explicit_blob

def out(fo, instrs, symbols, texts, size, explicit_blob=None):
	with engine_scope(getattr(instrs, 'engine', get_profile())):
		return _out(fo, instrs, symbols, texts, size, explicit_blob)


def _out(fo, instrs, symbols, texts, size, explicit_blob=None):
	hdr = struct.pack('<IIII', 0x10, size, 0, 0)
	fo.write(hdr)
	for fcn, args, pos, n in instrs:
		op = bpop.rops[fcn]
		if bpop.is_var_op(op):
			fo.write(bpop.encode_var_instr(op, args, pos, symbols))
			continue
		entry = bpop.get_op_entry(op)
		if entry is None:
			raise asdis.InvalidFunction('Invalid function @ line %d' % n)
		fo.write(struct.pack('B', op))
		if bpop.is_fixed_relative_op(op):
			if len(args) != 1:
				raise asdis.InvalidInstructionFormat(
					'Invalid relative instruction argument count @ line %d' % n)
			rel = _resolve_fixed_relative_arg(args[0], pos, symbols, n)
			fo.write(struct.pack('<h', rel))
			continue
		if op == 0x05 and len(args) >= 1:
			target_arg = args[1] if len(args) >= 2 else args[0]
			if len(args) == 1 and target_arg in symbols:
				target = symbols[target_arg]
				fo.write(struct.pack('<h', target - pos))
				continue
			if target_arg in symbols:
				target = symbols[target_arg]
			else:
				absolute = _parse_absolute_label(target_arg)
				if absolute is not None:
					target = absolute
				elif target_arg.startswith('0x') or target_arg.startswith('-0x'):
					target = int(target_arg, 16)
				else:
					target = int(target_arg)
			fo.write(struct.pack('<h', target - pos))
			continue
		arglist = []
		for arg in args:
			if arg in symbols:
				arglist.append(symbols[arg]-pos)
			elif _parse_absolute_label(arg) is not None:
				value = _parse_absolute_label(arg)
				if op == 0x06:
					arglist.append(value - pos)
				else:
					arglist.append(value)
			elif arg.startswith('0x') or arg.startswith('-0x'):
				arglist.append(int(arg, 16))
			elif arg:
				arglist.append(int(arg))
		fmt = entry[0]
		if fmt:
			# Older listings printed the unsigned local offset as signed i16.
			if op == 0x04 and len(arglist) == 1 and -0x8000 <= arglist[0] < 0:
				arglist[0] &= 0xFFFF
			fo.write(struct.pack(fmt, *tuple(arglist)))
	if explicit_blob is not None:
		fo.write(explicit_blob)
	else:
		while fo.tell() % 0x10 != 0:
			fo.write(b'\x00')
		for text in texts:
			fo.write(text + b'\x00')
		while fo.tell() % 0x10 != 0:
			fo.write(b'\x00')

def asm(file, encoding='cp932', output_path=None, engine='auto', games_path=None, executable=None):
	detection = detect_engine(file, engine, games_path=games_path, executable=executable)
	ofile = output_path or (os.path.splitext(file)[0] + '._bp')
	with open(file, 'r', encoding='utf-8') as source:
		asmtxt = source.read()
	instrs, symbols, texts, size, explicit_blob = parse(asmtxt, encoding=encoding, engine=detection.profile)
	with open(ofile, 'wb') as target:
		out(target, instrs, symbols, texts, size, explicit_blob)

if __name__ == '__main__':
	encoding = 'cp932'
	args = []
	argv = sys.argv[1:]
	i = 0
	while i < len(argv):
		arg = argv[i]
		if arg in ('-c', '--encoding') and i + 1 < len(argv):
			encoding = argv[i + 1]
			i += 1
		else:
			args.append(arg)
		i += 1
	if len(args) < 1:
		print('Usage: bpas.py [-c|--encoding <enc>] <file(s)>')
		print('(only .bpd files amongst <file(s)> will be processed)')
		sys.exit(1)
	for arg in args:
		for script in glob.glob(arg):
			base, ext = os.path.splitext(script)
			if ext == '.bpd':
				print('Assembling %s...' % script)
				asm(script, encoding=encoding)
