from __future__ import annotations

from dataclasses import dataclass, replace
from functools import lru_cache

from .opcode_stack_effects import STACK_EFFECTS
from .opcode_provenance import CHECKED_RESULT_SOURCES, DEFERRED_RESULT_OPCODES
from ..bp_engine import EngineProfile, get_profile


CORE_OPCODE_STACK_EFFECTS: dict[str, tuple[int, int]] = {
    "store_inline": (1, 0),
    "muldiv": (3, 1),
    "atan2": (2, 1),
    "veclen3": (3, 1),
    "rotate3d": (5, 0),
    "lerp": (4, 1),
    "lerp_q16": (3, 1),
    "add64": (3, 0),
    "sub64": (3, 0),
    "mul64": (3, 0),
    "div64": (3, 0),
    "mod64": (3, 0),
    "pow_q16": (2, 1),
    "mul64_shift16": (2, 1),
    "div_q16": (2, 1),
    "vec4_add": (3, 0),
    "vec4_sub": (3, 0),
    "vec4_mul_q16": (3, 0),
    "vec4_div_q16": (3, 0),
    "scalevec4_q16": (3, 0),
    "divvec4scalar_q16": (3, 0),
    "memfind_block": (4, 1),
    "getchar": (1, 4),
    "strquote": (3, 0),
    "malloc": (1, 1),
    "free": (1, 1),
    "watch_mode": (1, 0),
    "watch_add": (3, 1),
    "dumpmem": (3, 0),
    "listselectdlg": (2, 1),
    "drawhexdump": (7, 0),
}


GENERIC_GROUP_STACK_EFFECTS: dict[str, tuple[int, int]] = {
    "grp2_f6": (1, 0),
    "sys1_ef": (1, 0),
}


DYNAMIC_EXTERNAL_STACK_EFFECTS: dict[str, tuple[int | None, int]] = {
    # Sub-opcodes below F0 dispatch through the runtime mediation table.  The
    # callee owns the argument contract, so consume the prepared frame without
    # inventing a fixed arity.  No result is observed for these two corpus ops.
    "userscript_40": (None, 0),
    "userscript_41": (None, 0),
}


@dataclass(frozen=True, slots=True)
class OpcodeSemantics:
    name: str
    argument_count: int | None
    result_count: int
    is_group: bool = True
    stack_effect_source: str = "static"
    result_count_verified: bool = False
    deferred_results: bool = False


def load_opcode_semantics(engine=None) -> dict[str, OpcodeSemantics]:
    return _load_opcode_semantics(get_profile(engine))


@lru_cache(maxsize=64)
def _load_opcode_semantics(profile: EngineProfile) -> dict[str, OpcodeSemantics]:
    definitions = {
        name: OpcodeSemantics(
            name=name,
            argument_count=argument_count,
            result_count=result_count,
            stack_effect_source="handler" if handler_verified else "static",
            result_count_verified=name in CHECKED_RESULT_SOURCES,
            deferred_results=name in DEFERRED_RESULT_OPCODES,
        )
        for name, (argument_count, result_count, handler_verified) in STACK_EFFECTS.items()
    }
    definitions.update(
        {
            name: OpcodeSemantics(
                name=name,
                argument_count=argument_count,
                result_count=result_count,
                is_group=False,
                stack_effect_source="handler",
                result_count_verified=name in CHECKED_RESULT_SOURCES,
            )
            for name, (argument_count, result_count) in CORE_OPCODE_STACK_EFFECTS.items()
        }
    )
    definitions.update(
        {
            name: OpcodeSemantics(
                name=name,
                argument_count=argument_count,
                result_count=result_count,
                stack_effect_source="static",
            )
            for name, (argument_count, result_count) in GENERIC_GROUP_STACK_EFFECTS.items()
        }
    )
    definitions.update(
        {
            name: OpcodeSemantics(
                name=name,
                argument_count=argument_count,
                result_count=result_count,
                stack_effect_source="runtime-mediation",
            )
            for name, (argument_count, result_count) in DYNAMIC_EXTERNAL_STACK_EFFECTS.items()
        }
    )
    if profile.historical:
        overrides = {
            'getchar': (1, 3 if profile.before('1.654') else 4),
            'sys1_gdb_load': (0, 1 if profile.before('1.69/451') else 3),
            # The older cursor handler does not push the native scheduler code.
            'grp1_text_cursor_mode_position_set': (3, 0),
            'sys1_string_array_table_append': (2, 0 if profile.before('1.599') else 1),
            'usr1_set_main_window_pos': (2, 0 if profile.generation == '1.69/451' else 1),
            # WaitSeqLoad_Destroy pushes the status in asynchronous builds too.
            'grp1_buriko_mv_loader_create': (4, 1),
        }
        for name, (arguments, results) in overrides.items():
            definitions[name] = replace(definitions[name], argument_count=arguments,
                                        result_count=results, result_count_verified=True,
                                        stack_effect_source='versioned-handler')
        historical = {
            'sys1_check_registration': (0, 1), 'sys1_read_comap': (1, 1),
            'sys1_machine_key': (0, 1), 'sys1_machine_key_xor': (1, 1),
            'grp2_set_rich_text_parameters': (5 if profile.generation == '1.58' else 6, 0),
        }
        if profile.before('1.66'):
            historical.update({
                'grp1_sprite_ripple_at_59': (10, 0), 'grp1_window_set_font_at_87': (7, 0),
                'grp2_window_show_text_at_8c': (2, 0), 'grp2_window_draw_to_text_at_8d': (6, 1),
                'grp2_window_clear_at_8e': (1, 0),
            })
            if profile.generation == '1.58':
                historical.update({
                    'sys1_minimize_main_window_at_63': (0, 0),
                    'sys1_set_main_window_title_at_64': (1, 0),
                    'sys1_set_cursor_shape_at_66': (1, 0),
                    'grp1_sprite_scale_at_5a': (10, 0), 'grp1_sprite_rescale_at_5b': (5, 0),
                })
            else:
                historical.update({
                    'grp1_sprite_transform_at_5a': (13, 0), 'grp1_sprite_transform_at_5b': (13, 0),
                })
        for name in ('sys1_ethornell_plugin_load', 'sys1_ethornell_plugin_unload', 'sys1_ethornell_plugin_call'):
            definitions.pop(name, None)
        if profile.before('1.653'):
            historical.update({
                'sys1_named_lock_create': (2, 1), 'sys1_named_lock_delete': (1, 1),
                'sys1_named_lock_wait': (2, 1), 'sys1_named_lock_release': (1, 1),
                'sys1_named_lock_query': (2, 1),
            })
            for name in tuple(definitions):
                if name.startswith('sys1_critical_section_handle_'):
                    del definitions[name]
        for name, (arguments, results) in historical.items():
            definitions[name] = OpcodeSemantics(name, arguments, results,
                stack_effect_source='versioned-handler', result_count_verified=True,
                deferred_results=name == 'sys1_named_lock_wait')
    return definitions


def get_opcode_semantics(name: str, engine=None) -> OpcodeSemantics | None:
    return load_opcode_semantics(engine).get(name)
