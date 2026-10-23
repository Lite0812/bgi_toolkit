"""Older SDC-wrapped GDBs with fixed name/flag tables and no GDB magic."""
from __future__ import annotations
from .i18n import tr
import struct
from .gdb_tool import (LEGACY_SCHEMA, MAX_UNPACKED_SIZE, GdbFormatError,
    _decode_text, _encode_text, _sparse_region, _set_ranges, _mapping, _keys, _integer,
    _list, _byte_array, _trailing_bytes, _build_region, _build_named_bits)

GLOBAL_SIZE = 1024
NAME_SIZE = 32
FLAG_NAME_SIZE = 24
FLAG_RECORD_SIZE = 32
FLAG_SLOTS = 2048
SYSTEM_SIZES = (262144, 1048576)
NAME_SLOTS = (4096, 8192, 16384, 32768, 65536)


def _read_slot(raw):
    end = raw.find(b'\0')
    if end < 0:
        return {'text':_decode_text(raw), 'terminated':False}
    result = {'text':_decode_text(raw[:end])}
    padding = raw[end+1:]
    if any(padding):
        result['padding_bytes'] = list(padding)
    return result


def _write_slot(item, size, path):
    raw = _encode_text(item['text'],f'{path}.text')
    terminated = item.get('terminated',True)
    if type(terminated) is not bool:
        raise GdbFormatError(f'{path}.terminated must be a boolean')
    if not terminated:
        if len(raw)!=size or item.get('padding_bytes'):
            raise GdbFormatError(f'{path}: unterminated text must occupy exactly {size} bytes')
        return raw
    if len(raw)>=size:
        raise GdbFormatError(f'{path}: text exceeds {size-1} CP932 bytes')
    remaining = size-len(raw)-1
    padding = _byte_array(item['padding_bytes'],f'{path}.padding_bytes') if 'padding_bytes' in item else bytes(remaining)
    if len(padding)!=remaining:
        raise GdbFormatError(f'{path}: padding length does not match the edited text')
    return raw+b'\0'+padding


def _flag_name(item):
    value={'text':item['name']}
    if 'name_padding_bytes' in item:value['padding_bytes']=item['name_padding_bytes']
    if 'name_terminated' in item:value['terminated']=item['name_terminated']
    return value


def _candidate(payload, system_size, name_slots):
    names_start=8+GLOBAL_SIZE+system_size
    flags_start=names_start+name_slots*NAME_SIZE
    bits_start=flags_start+FLAG_SLOTS*FLAG_RECORD_SIZE
    if bits_start>len(payload):return None
    records=[]
    for slot in range(FLAG_SLOTS):
        position=flags_start+slot*FLAG_RECORD_SIZE
        bits,offset=struct.unpack_from('<II',payload,position+FLAG_NAME_SIZE)
        if not bits:continue
        name=payload[position:position+FLAG_NAME_SIZE]
        count=(bits+7)//8
        if not name[0] or offset+count>len(payload)-bits_start:return None
        records.append((offset,count,slot,bits))
    # Require a complete, contiguous partition. This distinguishes the actual
    # fixed-table size from zero-filled prefixes and rejects unrelated SDC data.
    current=0
    for offset,count,_,_ in sorted(records):
        if offset!=current:return None
        current+=count
    if current!=len(payload)-bits_start:return None
    return names_start,flags_start,bits_start,records


def parse_legacy_gdb(payload, seed, trailing):
    matches=[]
    for system_size in SYSTEM_SIZES:
        for name_slots in NAME_SLOTS:
            result=_candidate(payload,system_size,name_slots)
            if result is not None:matches.append((system_size,name_slots,result))
    if len(matches)!=1:
        raise GdbFormatError(tr('无法识别旧版 GDB 固定布局：名称表或标志数据不完整、越界或结构不受支持'))
    system_size,name_slots,(names_start,flags_start,bits_start,records)=matches[0]
    strings=[]
    for slot in range(name_slots):
        raw=payload[names_start+slot*NAME_SIZE:names_start+(slot+1)*NAME_SIZE]
        if any(raw):strings.append(dict(slot=slot,**_read_slot(raw)))
    arrays=[];unused=[]
    for slot in range(FLAG_SLOTS):
        position=flags_start+slot*FLAG_RECORD_SIZE
        raw=payload[position:position+FLAG_RECORD_SIZE]
        if not any(raw):continue
        info=_read_slot(raw[:FLAG_NAME_SIZE]);bits,offset=struct.unpack_from('<II',raw,FLAG_NAME_SIZE)
        item={'slot':slot,'name':info['text']}
        if 'padding_bytes' in info:item['name_padding_bytes']=info['padding_bytes']
        if 'terminated' in info:item['name_terminated']=info['terminated']
        if not bits:
            unused.append(dict(item,data_offset=offset))
            continue
        data=payload[bits_start+offset:bits_start+offset+(bits+7)//8]
        item.update(bit_length=bits,set_ranges=_set_ranges(data,bits),
            unused_tail_bits=data[-1]>>(bits&7) if bits&7 else 0, data_offset=offset)
        arrays.append(item)
    arrays.sort(key=lambda item:item['data_offset'])
    x,y=struct.unpack_from('<ii',payload)
    return {'_schema':LEGACY_SCHEMA,'sdc':{'seed':seed,'trailing_bytes':list(trailing)},
        'gdb':{'layout':'legacy-fixed','system_area_size':system_size,'name_slot_count':name_slots,
            'flag_slot_count':FLAG_SLOTS,'unused_flag_records':unused},
        'window_position':{'x':x,'y':y},'fixed_region':_sparse_region(payload[8:8+GLOBAL_SIZE]),
        'data_region':_sparse_region(payload[8+GLOBAL_SIZE:names_start]),
        'strings':strings,'named_bit_arrays':arrays}


def build_legacy_gdb(document):
    root=_mapping(document,'root')
    _keys(root,{'_schema','sdc','gdb','window_position','fixed_region','data_region','strings','named_bit_arrays'},set(),'root')
    sdc=_mapping(root['sdc'],'sdc');_keys(sdc,{'seed'},{'trailing_bytes'},'sdc')
    seed=_integer(sdc['seed'],'sdc.seed',0,0xffffffff)
    metadata=_mapping(root['gdb'],'gdb')
    _keys(metadata,{'layout','system_area_size','name_slot_count','flag_slot_count'},{'unused_flag_records'},'gdb')
    if metadata['layout']!='legacy-fixed':raise GdbFormatError('unsupported legacy GDB layout')
    system_size=_integer(metadata['system_area_size'],'gdb.system_area_size',0,1048576)
    name_slots=_integer(metadata['name_slot_count'],'gdb.name_slot_count',1,65536)
    if system_size not in SYSTEM_SIZES or name_slots not in NAME_SLOTS or metadata['flag_slot_count']!=FLAG_SLOTS:
        raise GdbFormatError('unsupported legacy GDB section sizes')
    window=_mapping(root['window_position'],'window_position');_keys(window,{'x','y'},set(),'window_position')
    x=_integer(window['x'],'window_position.x',-0x80000000,0x7fffffff)
    y=_integer(window['y'],'window_position.y',-0x80000000,0x7fffffff)
    fixed=_build_region(root['fixed_region'],'fixed_region',GLOBAL_SIZE)
    data=_build_region(root['data_region'],'data_region',1048576)
    if len(fixed)!=GLOBAL_SIZE or len(data)!=system_size:
        raise GdbFormatError('legacy GDB global/system region sizes do not match its layout')
    names=bytearray(name_slots*NAME_SIZE);seen=set()
    for index,value in enumerate(_list(root['strings'],'strings')):
        path=f'strings[{index}]';item=_mapping(value,path)
        _keys(item,{'slot','text'},{'padding_bytes','terminated'},path)
        slot=_integer(item['slot'],path+'.slot',0,name_slots-1)
        if slot in seen:raise GdbFormatError(f'{path}: duplicate name slot {slot}')
        seen.add(slot);names[slot*NAME_SIZE:(slot+1)*NAME_SIZE]=_write_slot(item,NAME_SIZE,path)
    table=bytearray(FLAG_SLOTS*FLAG_RECORD_SIZE);bits_data=bytearray();seen=set()
    arrays=_list(root['named_bit_arrays'],'named_bit_arrays')
    if len(arrays)>FLAG_SLOTS:raise GdbFormatError('too many legacy GDB flag records')
    for index,value in enumerate(arrays):
        path=f'named_bit_arrays[{index}]';item=_mapping(value,path)
        _keys(item,{'slot','name','bit_length','set_ranges','unused_tail_bits'},
            {'name_padding_bytes','name_terminated','data_offset'},path)
        slot=_integer(item['slot'],path+'.slot',0,FLAG_SLOTS-1)
        if slot in seen:raise GdbFormatError(f'{path}: duplicate flag slot {slot}')
        seen.add(slot)
        _,count,raw=_build_named_bits({k:item[k] for k in ('name','bit_length','set_ranges','unused_tail_bits')},path)
        if len(bits_data)+len(raw)>MAX_UNPACKED_SIZE:raise GdbFormatError('legacy GDB flag data too large')
        position=slot*FLAG_RECORD_SIZE
        table[position:position+FLAG_NAME_SIZE]=_write_slot(_flag_name(item),FLAG_NAME_SIZE,path+'.name')
        struct.pack_into('<II',table,position+FLAG_NAME_SIZE,count,len(bits_data));bits_data.extend(raw)
    for index,value in enumerate(_list(metadata.get('unused_flag_records',[]),'gdb.unused_flag_records')):
        path=f'gdb.unused_flag_records[{index}]';item=_mapping(value,path)
        _keys(item,{'slot','name','data_offset'},{'name_padding_bytes','name_terminated'},path)
        slot=_integer(item['slot'],path+'.slot',0,FLAG_SLOTS-1)
        if slot in seen:raise GdbFormatError(f'{path}: duplicate flag slot {slot}')
        seen.add(slot);position=slot*FLAG_RECORD_SIZE
        table[position:position+FLAG_NAME_SIZE]=_write_slot(_flag_name(item),FLAG_NAME_SIZE,path+'.name')
        struct.pack_into('<II',table,position+FLAG_NAME_SIZE,0,_integer(item['data_offset'],path+'.data_offset',0,0xffffffff))
    payload=struct.pack('<ii',x,y)+fixed+data+bytes(names)+bytes(table)+bytes(bits_data)
    if len(payload)>MAX_UNPACKED_SIZE:raise GdbFormatError('legacy GDB payload too large')
    return payload,seed,_trailing_bytes(sdc,'sdc')
