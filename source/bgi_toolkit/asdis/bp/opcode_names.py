"""Reviewed BP spellings, keyed by original family/selector identity.

The aliases below only preserve names corrected by this review. They do not
restore the previously retired VM instruction names or cross-version meanings.
Native handler paths and cross-project labels are recorded in
artifacts/bp_naming_alignment_20261023/.
"""
from __future__ import annotations


# (family, selector): (previous spelling, canonical spelling)
SUBOPCODE_RENAMES = {
    (0x90, 0x00): ('grp1_text_dirty_set', 'grp1_present_request'),
    (0x90, 0x01): ('grp1_message_speed_raw_set', 'grp1_display_enable_set'),
    (0x90, 0x02): ('grp1_message_interval_set', 'grp1_frame_rate_set'),
    (0x90, 0x12): ('grp1_surface_exists_get', 'grp1_bitmap_release'),
    (0x90, 0x20): ('grp1_sprite_create_basic', 'grp1_object_alpha_control_start'),
    (0x90, 0x21): ('grp1_sprite_create_layered', 'grp1_object_move_control_start'),
    (0x90, 0x32): ('grp1_object_blend_mode_set', 'grp1_object_alpha_level_set'),
    (0x90, 0x36): ('grp1_object_size_set', 'grp1_object_secondary_offset_set'),
    (0x90, 0x37): ('grp1_object_origin_set', 'grp1_object_primary_offset_set'),
    (0x90, 0x50): ('grp1_layer_selected_get', 'grp1_sprite_create'),
    (0x90, 0x51): ('grp1_layer_resource_release', 'grp1_sprite_release'),
    (0x90, 0x60): ('grp1_layer_controller_create', 'grp1_filter_create'),
    (0x90, 0x61): ('grp1_release_layer_controller_handle', 'grp1_filter_release'),
    (0x90, 0x70): ('grp1_anim_object_create_get_handle', 'grp1_map_create'),
    (0x90, 0x71): ('grp1_anim_object_release', 'grp1_map_release'),
    (0x90, 0x80): ('grp1_object_status_get', 'grp1_window_create'),
    (0x90, 0x81): ('grp1_scene_object_state_set', 'grp1_window_release'),
    (0x90, 0x87): ('grp1_object_alpha_set', 'grp1_window_isolated_composition_set'),
    (0x90, 0xE0): ('grp1_tail_object_create_get', 'grp1_group_create'),
    (0x90, 0xE1): ('grp1_render_controller_release', 'grp1_group_release'),
    (0x90, 0xE8): ('grp1_render_controller_resource_set', 'grp1_group_add_object'),
    (0x90, 0xE9): ('grp1_render_controller_resource_bind', 'grp1_group_remove_object'),
    (0x91, 0x88): ('grp2_current_set_render_percent', 'grp2_window_font_set'),
    (0x91, 0x89): ('grp2_current_limit_set', 'grp2_window_line_spacing_percent_set'),
    (0x91, 0x8C): ('grp2_current_set_origin', 'grp2_window_text_cursor_set'),
    (0x91, 0x8D): ('grp2_current_get_origin3', 'grp2_window_text_cursor_get'),
}

OPCODE_ALIASES = {previous: canonical for previous, canonical in SUBOPCODE_RENAMES.values()}


def canonical_opcode_name(name: str) -> str:
    return OPCODE_ALIASES.get(name, name)
