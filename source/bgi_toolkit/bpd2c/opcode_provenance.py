"""New x64 contracts checked against executable handlers and completion paths.

Addresses refer to the supplied トリプルペアリング.exe / export-for-ai.
Unlike legacy static annotations, these result counts must not be increased
merely because later instructions store values already present on the stack.
"""

CHECKED_RESULT_SOURCES = {
    "sys0_string_buffer_create": "1400CCE60",
    "sys1_yield": "1400E8CB0 (scheduler status 1, no VM stack result)",
    "sys1_exit_thread": "1400E9280 (scheduler status 4)",
    "sys1_quit": "1400E8A20 (scheduler status 6)",
    "sys1_refresh_registered_input_events": "1400EA470 (no VM stack accesses)",
    "sys1_ethornell_plugin_call": "1400E6DB0 (alternative typed reads)",
    "sys1_create_wait_message_process": "14007E9D0 (two completion values)",
    "sys1_wait_time_key": "14007E870 (one completion value)",
    "sys1_create_encode_data_process": "14007A870 (one completion value)",
    "sys1_create_encode_struct_process": "14007A970 (one completion value)",
    "sys2_unimplemented_f70b0_i32_string": "1400EB600",
    "sys2_unimplemented_f70a0_two_strings": "1400EB580",
    "sys2_unimplemented_f7090_i32_string": "1400EB500",
    "sys2_unimplemented_f7080_two_strings": "1400EB480",
    "grp1_surface_capture_priority": "1400DE490",
    "grp1_map_create": "1400D9D70",
    "grp1_text_cursor_mode_position_set": "1400D8F90 (one result per path)",
    "grp1_movie_sequence_release": "1400D6970 (one result per path)",
    "grp2_resource_unload_slot_status": "1400DE8B0 (one result per path)",
    "grp2_resource_set_active_status": "1400DE830 (one result per path)",
    "grp2_play_movie_by_mf": "14009CD60 (completion destructor)",
    "grp3_load_movie_sequence": "14009CBD0 / 14009C9E0 (completion destructors)",
    "grp3_resource_seek_count_status": "1400E3380 (one result per path)",
    "grp3_resource_aux_operate_status": "1400E3270 (one result per path)",
    "usr2_resource_delete": "1400D2470",
    "grp3_request_draw_refresh": "1400E4ED0 (no VM arguments)",
    "snd1_noop_primary": "1400E5650 (no VM arguments)",
    "snd1_noop_secondary": "1400E5630 (no VM arguments)",
    "snd1_noop_mci_stub": "1400E55D0 (no VM arguments)",
    "getchar": "1400CDC00 (four pushes)",
    "store_inline": "1400D0410 / 140087DA0 (inline bytes, no result)",
}

DEFERRED_RESULT_OPCODES = frozenset({
    "sys1_create_wait_message_process",
    "sys1_wait_time_key",
    "sys1_create_encode_data_process",
    "sys1_create_encode_struct_process",
    "grp2_play_movie_by_mf",
    "grp3_load_movie_sequence",
})
