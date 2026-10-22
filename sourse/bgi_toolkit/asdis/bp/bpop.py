# BGI ._bp file opcode table.
# Historical "base" / "code_ref" spellings for local data refer to the
# frame pointer (ctx+0xE8). Instruction decoding uses IP at ctx+0xE4;
# the current instruction's starting IP is kept separately at ctx+0xE0.

from ..common import asdis
from ...bp_engine import get_profile, historical_names, opcode_available
import re
import struct

re_fcn = re.compile(r'([A-Za-z_][A-Za-z0-9_:]*)\(.*\)')

offsets = set()

# Fixed-width instructions whose signed 16-bit operand is relative to the
# instruction's starting IP. Variable-length conditional branches are handled
# separately by encode_var_instr().
fixed_relative_ops = frozenset((0x06, 0x13, 0xEE))
subop_group_ops = {0x7F, 0x80, 0x81, 0x90, 0x91, 0x92, 0xA0, 0xB0, 0xC0, 0xD0, 0xE0, 0xFF}
current_text_encoding = 'cp932'

precise_subop_name_text = """
7F:00 sys0_sort_records_by_integer_field
7F:80 sys0_byte_buffer_create
7F:81 sys0_byte_buffer_destroy
7F:82 sys0_byte_buffer_resize_preserve
7F:83 sys0_byte_buffer_get_size
7F:84 sys0_byte_buffer_write_range
7F:85 sys0_byte_buffer_read_range
7F:86 sys0_byte_buffer_insert_range
7F:88 sys0_string_buffer_create
7F:89 sys0_string_buffer_destroy
7F:8A sys0_string_buffer_insert
7F:8B sys0_string_buffer_assign
80:00 sys1_random_seed_set
80:01 sys1_random_next_15bit
80:02 sys1_random_bounded
80:03 sys1_copy_recent_event_ring
80:04 sys1_get_monotonic_time_ms
80:05 sys1_get_performance_counter_ns
80:06 sys1_frame_timing_metrics_enable
80:07 sys1_get_frame_timing_metric
80:08 sys1_get_cursor_pos_client
80:09 sys1_get_last_present_time_ms
80:0A sys1_copy_builtin_constant_block
80:0B sys1_get_text_context_status
80:0C sys1_get_local_time
80:0D sys1_get_memory_status_legacy
80:0E sys1_get_window_minimize_flag
80:0F sys1_is_main_window_active
80:10 sys1_reset_input_tracking
80:11 sys1_get_async_key_edge
80:12 sys1_sum_key_activity_counts
80:13 sys1_get_input_dispatch_state
80:14 sys1_set_input_aggregate_suppress_flag
80:15 sys1_set_input_aggregate_force_flag
80:16 sys1_refresh_registered_input_events
80:17 sys1_get_aggregate_input_trigger
80:18 sys1_refresh_pointer_hit_group_primary
80:19 sys1_refresh_secondary_pointer_hits
80:1A sys1_get_input_code_trigger_state
80:1B sys1_bind_input_category_key_codes
80:1C sys1_sum_input_categories_by_mask
80:1D sys1_test_input_trigger_in_hit_region
80:1E sys1_set_cursor_policy_enabled
80:1F sys1_set_cursor_tracking_region
80:20 sys1_runtime_pool_alloc
80:21 sys1_runtime_pool_free
80:24 sys1_count_files_in_tree
80:25 sys1_list_files
80:26 sys1_list_subdirectories
80:27 sys1_move_file
80:28 sys1_create_directory
80:29 sys1_remove_directory
80:2A sys1_is_directory
80:2B sys1_split_path_components
80:2C sys1_get_file_attributes
80:2D sys1_set_file_attributes
80:2F sys1_copy_file_overwrite
80:30 sys1_read_resource_with_prefix_retry
80:31 sys1_decode_resource
80:32 sys1_write_file
80:33 sys1_delete_file_by_optional_base_path
80:34 sys1_file_exists
80:35 sys1_file_size
80:36 sys1_enable_resource_search_prefixes
80:37 sys1_append_resource_search_prefix
80:38 sys1_create_vm_object_from_strings
80:39 sys1_is_directory_path
80:3A sys1_get_special_folder_path
80:3B sys1_file_dialog_filter_build
80:3C sys1_wait_for_removable_media_file
80:3D sys1_get_resource_base_path
80:3E sys1_path_has_directory_attribute
80:3F sys1_initialize_resource_base_paths
80:40 sys1_load_program
80:41 sys1_unload_program
80:44 sys1_spawn_thread
80:45 sys1_exit_thread
80:46 sys1_get_current_vm_thread_id
80:47 sys1_thread_exists
80:48 sys1_thread_post
80:49 sys1_thread_recv
80:4A sys1_thread_post_array
80:4B sys1_thread_recv_array
80:4C sys1_thread_notify
80:50 sys1_set_wait_blocking
80:52 sys1_set_vm_dispatch_mode
80:53 sys1_request_vm_dispatch
80:54 sys1_create_wait_message_process
80:58 sys1_timer_set
80:59 sys1_timer_add
80:5A sys1_timer_wait
80:5C sys1_wait_time_key
80:5D sys1_set_thread_exclusive
80:5E sys1_switch_thread
80:5F sys1_yield
80:60 sys1_set_display_mode
80:61 sys1_get_display_mode
80:62 sys1_set_allowed_input_codes
80:63 sys1_set_device_policy_inverted
80:64 sys1_show_main_window_and_clear_input
80:65 sys1_minimize_main_window
80:66 sys1_set_main_window_title
80:67 sys1_set_select_mode_checked
80:68 sys1_set_system_menu_close_enabled
80:69 sys1_post_main_window_close
80:6A sys1_quit
80:6B sys1_configure_system_archive_path
80:6C sys1_set_drag_accept_files
80:6D sys1_take_last_dropped_file_path
80:6E sys1_reapply_display_mode_option
80:6F sys1_has_desktop_aspect_mismatch
80:70 sys1_save_buffer_alloc
80:71 sys1_save_buffer_clear
80:74 sys1_set_save_data_obfuscation_enabled
80:78 sys1_save_slot_write
80:79 sys1_save_slot_read
80:7A sys1_save_slot_read_header
80:7B sys1_save_slot_probe
80:80 sys1_gdb_load
80:81 sys1_gdb_save
80:82 sys1_gdb_data_region_write
80:83 sys1_gdb_data_region_read
80:84 sys1_gdb_string_table_append
80:85 sys1_gdb_string_table_contains
80:88 sys1_named_bit_array_resize
80:89 sys1_named_bit_array_set
80:8A sys1_named_bit_array_set_range
80:8B sys1_named_bit_array_get
80:90 sys1_record_log_reset
80:91 sys1_record_log_get_count
80:94 sys1_record_log_append_fields
80:95 sys1_record_log_read_compact
80:96 sys1_record_log_append_struct
80:97 sys1_record_log_read_extended
80:98 sys1_fixed_data_queue_create
80:99 sys1_fixed_data_queue_destroy
80:9A sys1_fixed_data_queue_get_count
80:9C sys1_fixed_data_queue_append
80:9D sys1_fixed_data_queue_read_at
80:9E sys1_fixed_data_queue_erase_range
80:9F sys1_fixed_data_queue_set_string_mode
80:A0 sys1_event_triplet_dequeue
80:A1 sys1_event_triplet_enqueue
80:A8 sys1_object_handle_user_state_set
80:A9 sys1_object_handle_user_state_get
80:AC sys1_object_append_integer_block
80:AF sys1_set_object_handle_table_enabled
80:B0 sys1_critical_section_handle_create
80:B1 sys1_critical_section_handle_destroy
80:B4 sys1_critical_section_handle_try_lock
80:B5 sys1_critical_section_handle_lock
80:B6 sys1_critical_section_handle_unlock
80:C0 sys1_create_encode_data_process
80:C1 sys1_decompress_buffer
80:C4 sys1_create_encode_struct_process
80:C5 sys1_decompress_get_payload_size
80:CF sys1_create_decode_data_process
80:D0 sys1_keyed_value_table_create
80:D1 sys1_keyed_value_table_destroy
80:D2 sys1_keyed_value_table_set
80:D3 sys1_keyed_value_table_erase
80:D4 sys1_keyed_value_table_get
80:D8 sys1_clear_string_tables_except_gdb
80:D9 sys1_string_array_table_get_count
80:DA sys1_string_array_table_replace
80:DB sys1_string_array_table_serialize
80:DC sys1_string_array_table_append
80:DD sys1_string_array_table_read_at
80:DE sys1_string_array_table_get_length_at
80:E0 sys1_process_launch_wait
80:E1 sys1_launch_process_after_exit
80:E2 sys1_process_launch_wait_uninstaller
80:E3 sys1_shell_execute_open
80:E8 sys1_get_engine_target_name
80:E9 sys1_load_resource_byte_vector
80:EA sys_set_uninstaller_mutex_name
80:EC sys1_ethornell_plugin_load
80:ED sys1_ethornell_plugin_unload
80:EE sys1_ethornell_plugin_call
80:F0 sys_open_config_dialog_string_2ints
80:F1 sys1_open_three_choice_dialog
80:F2 sys_installer_resource_select_apply
80:F3 sys1_prepare_installer_resources
80:F4 sys1_prune_uninstall_delete_list
80:F5 sys_uninstall_list_merge_write
80:F6 sys1_delete_installer_artifacts
80:F7 sys1_resource_com_export_copy
80:F8 sys1_installer_path_probe
80:F9 sys1_installer_path_validate
80:FA sys1_installer_path_apply
80:FB sys1_get_windows_directory_path
80:FC sys1_register_file_association
80:FD sys1_get_launcher_mode
80:FE sys1_get_current_char_node_raw_length
81:00 sys2_set_script_text_encoding
81:01 sys2_is_ui_language_ja
81:02 sys2_set_cached_ui_language
81:03 sys2_get_cached_ui_language_id
81:04 sys2_set_clock_jump_tolerance
81:06 sys2_secure_random_bounded_i32
81:07 sys2_get_system_metric_slot_u64
81:08 sys2_get_user_name
81:09 sys2_get_computer_name
81:0A sys2_expand_display_layout_records
81:0B sys2_copy_graphics_format_info
81:0C sys2_get_windows_version_info
81:0D sys2_get_memory_status_ex_mib
81:0E sys2_get_adjusted_display_size
81:0F sys2_is_window_minimized
81:10 sys2_exchange_key_activity_count
81:11 sys2_get_keyboard_state
81:14 sys2_set_scene_dispatch_mode
81:16 sys2_reset_path_records
81:17 sys2_copy_path_records
81:18 sys2_invoke_window_callback
81:19 sys2_copy_linked_records
81:1B sys2_set_render_option_entry
81:1D sys2_aggregate_device_state
81:1E sys2_synthesize_mouse_button_click
81:1F sys2_set_scene_process_mode
81:20 sys2_convert_text_encoding
81:21 sys2_convert_cp932_to_utf8
81:27 sys2_detect_text_encoding
81:28 sys2_file_open_dx
81:29 sys2_queue_file_read_offset0_mode1
81:2A sys2_queue_file_read
81:2B sys2_queue_file_read_offset_mode0
81:2C sys2_get_file_timestamps
81:2D sys2_set_file_timestamps
81:2F sys2_test_directory_write_access
81:30 sys2_start_binary_resource_read
81:31 sys2_start_internet_file_read
81:32 sys2_read_file_unbuffered_aligned
81:34 sys2_check_file_health
81:35 sys2_get_resource_size
81:36 sys2_get_drive_types_a_to_z
81:37 sys2_get_disk_free_space_mib
81:38 sys2_open_file_dialog
81:39 sys2_enumerate_archive_entry_names
81:3A sys2_browse_for_folder
81:3B sys2_select_archive_entry_dialog
81:3C sys2_resource_entry_exists
81:3D sys2_get_volume_label_by_drive_letter
81:3E sys2_get_file_device_power_state
81:44 sys2_start_child_thread_and_push_state
81:48 sys2_draw_vm_object_all_devices
81:60 sys2_set_window_size_slot
81:61 sys2_get_display_mode_effective
81:62 sys2_set_binary_display_mode
81:63 sys2_set_display_mode
81:64 sys2_apply_display_size
81:65 sys2_set_script_runtime_mode
81:66 sys2_set_window_style_mode
81:67 sys2_get_current_game_context_ptr
81:68 sys2_swap_background_process_enable
81:69 sys2_toggle_comma_hotkeys
81:6A sys2_set_error_recovery_capture_mode
81:6B sys2_get_last_error_message
81:6C sys2_check_aspect_ratio_size
81:6D sys2_get_display_driver_version
81:6E sys2_push_true
81:6F sys2_set_antialias_postprocess_mode
81:80 sys2_load_buriko_gdb3
81:8C sys2_unimplemented_f70b0_i32_string
81:8D sys2_unimplemented_f70a0_two_strings
81:8E sys2_unimplemented_f7090_i32_string
81:8F sys2_unimplemented_f7080_two_strings
81:B0 sys2_score_utf16_sequence_merge
81:B7 sys2_decode_multibyte_to_utf16
81:B8 sys2_find_matching_record
81:B9 sys2_collect_matching_records
81:D0 sys2_create_blob_slot_table
81:D1 sys2_destroy_blob_slot_table
81:D2 sys2_write_blob_slot
81:D3 sys2_erase_blob_slot
81:D4 sys2_read_blob_slot
81:D5 sys2_enumerate_blob_slots
81:D8 sys2_deserialize_nested_string_map
81:DA sys2_get_nested_string_map_value
81:E0 sys2_run_process_wait
81:E9 sys2_update_checksum
81:EA sys2_compute_md5_digest
81:EC sys2_create_named_mutex_handle
81:ED sys2_release_named_mutex_handle
81:F2 sys2_create_dcproc_installation
81:F7 sys2_export_resource_com_data
90:00 grp1_text_dirty_set
90:01 grp1_message_speed_raw_set
90:02 grp1_message_interval_set
90:03 grp1_timer_resource_create
90:04 grp1_surface_capture_current
90:05 grp1_surface_capture_priority
90:06 grp1_current_surface_size_set
90:07 grp1_draw_mode_set
90:08 grp1_current_surface_invalidate
90:09 grp1_current_surface_priority_set
90:0A grp1_text_cursor_position_set
90:0B grp1_draw_layer_offset_set
90:0C grp1_surface_mode_set
90:0D grp1_text_antialias_mode_set
90:0E grp1_surface_text_draw_rect
90:0F grp1_alpha_background_color_set
90:10 grp1_surface_load_from_resource
90:11 grp1_surface_create
90:12 grp1_surface_exists_get
90:13 grp1_surface_fill_uniform_color
90:14 grp1_draw_resource_apply
90:15 grp1_surface_load_pair
90:16 grp1_surface_info_get
90:17 grp1_surface_status_get
90:18 grp1_surface_blit
90:19 grp1_surface_rect_composite
90:1A grp1_surface_composite
90:1B grp1_surface_copy_masked
90:1C grp1_surface_scaled_blit_rect
90:1D grp1_surface_copy_mode
90:1E grp1_surface_rect_blit
90:1F grp1_surface_create_and_blit
90:20 grp1_sprite_create_basic
90:21 grp1_sprite_create_layered
90:22 grp1_sprite_create_scaled
90:23 grp1_sprite_create_extended
90:24 grp1_resource_sprite_create_ext
90:28 grp1_resource_rect_object_create
90:29 grp1_resource_named_object_create
90:2C grp1_resource_transition_create
90:30 grp1_object_active_set
90:31 grp1_object_state_recursive_set
90:32 grp1_object_blend_mode_set
90:33 grp1_object_position_set
90:34 grp1_object_opacity_set
90:35 grp1_object_render_mode_set
90:36 grp1_object_size_set
90:37 grp1_object_origin_set
90:38 grp1_object_layer_set
90:39 grp1_object_anchor_mode_set
90:3A grp1_object_priority_set
90:3C grp1_scene_state_pair_set
90:3D grp1_layer_screen_delta_get
90:3F grp1_scene_state_set
90:40 grp1_current_layer_resource_set
90:41 grp1_current_layer_resource_pair_set
90:42 grp1_layer_quad_resource_set
90:43 grp1_layer_resource_rect_set
90:44 grp1_current_layer_string_tag_bind
90:45 grp1_current_layer_rect_mode_set
90:46 grp1_current_layer_index_mode_set
90:47 grp1_current_layer_rect_alpha_set
90:48 grp1_current_layer_rect_blend_set
90:49 grp1_current_layer_short_transform_set
90:4A grp1_current_layer_resource_blend_set
90:4C grp1_layer_order_set
90:4D grp1_layer_count_get
90:50 grp1_layer_selected_get
90:51 grp1_layer_resource_release
90:53 grp1_layer_anim_resource_rect_set
90:54 grp1_layer_anim_enable_set
90:55 grp1_layer_anim_state_set
90:56 grp1_layer_anim_timing_rect_set
90:57 grp1_layer_anim_pos_set
90:58 grp1_layer_anim_key_rect_set
90:59 grp1_layer_anim_complex13_set
90:5A grp1_layer_anim_complex10_set
90:5B grp1_layer_anim_key10_set
90:5C grp1_layer_anim_complex17_set
90:5D grp1_layer_anim_complex20_set
90:60 grp1_layer_controller_create
90:61 grp1_release_layer_controller_handle
90:64 grp1_layer_controller_mode_set
90:65 grp1_layer_controller_rect_set
90:66 grp1_layer_controller_rect_ext_set
90:70 grp1_anim_object_create_get_handle
90:71 grp1_anim_object_release
90:74 grp1_anim_object_set_active_mode
90:75 grp1_anim_object_transform6_set
90:76 grp1_anim_curve_set
90:78 grp1_anim_resource_bind
90:79 grp1_anim_keyframe_set
90:7A grp1_anim_mode_set
90:80 grp1_object_status_get
90:81 grp1_scene_object_state_set
90:82 grp1_object_visibility_set
90:83 grp1_object_composite_mode_set
90:84 grp1_object_z_order_set
90:85 grp1_object_transform_set
90:86 grp1_object_clip_rect_set
90:87 grp1_object_alpha_set
90:88 grp1_object_color_transform_set
90:89 grp1_object_named_metric_get
90:90 grp1_resource_proc_create
90:91 grp1_object_resource_mode_set
90:92 grp1_object_state_reset
90:94 grp1_object_enabled_set
90:95 grp1_object_position_range_set
90:96 grp1_object_scale_range_set
90:97 grp1_object_rotation_set
90:98 grp1_graphics_object_apply
90:99 grp1_object_frame_set
90:9A grp1_text_cursor_mode_position_set
90:9B grp1_object_pivot_set
90:9C grp1_object_loop_mode_set
90:9D grp1_object_viewport_set
90:9E grp1_object_value_range_set
90:9F grp1_object_state_commit
90:A0 grp1_resource_effect_object_create
90:A1 grp1_resource_effect_apply
90:A2 grp1_noop_primary
90:A3 grp1_noop_secondary
90:A4 grp1_effect_mode_set
90:A5 grp1_effect_strength_set
90:A6 grp1_effect_transform_set
90:A7 grp1_effect_object_apply
90:AF grp1_effect_enabled_set
90:B0 grp1_message_object_create
90:B1 grp1_message_object_create_ext
90:B4 grp1_apply_message_page_to_object
90:B5 grp1_message_page_apply
90:B6 grp1_resource_collision_x_get
90:B7 grp1_graph_descriptor_apply
90:B8 grp1_graphics_extent_x_get
90:B9 grp1_interaction_controller_release
90:BA grp1_resource_named_collision_get
90:BC grp1_interaction_state_get
90:BD grp1_resource_height_get
90:BE grp1_resource_format_get
90:BF grp1_interaction_event_pop
90:C0 grp1_text_layout_apply
90:C1 grp1_text_layout_width_get
90:C2 grp1_text_style_set
90:C3 grp1_text_font_set
90:C4 grp1_text_width_get
90:C5 grp1_text_height_get
90:C6 grp1_text_ascent_get
90:C7 grp1_text_descent_get
90:C8 grp1_text_color_set
90:CA grp1_text_position_set
90:CC grp1_text_render_apply
90:CD grp1_text_clip_set
90:CE grp1_text_draw_apply
90:CF grp1_text_effect_apply
90:D0 grp1_text_window_active_get
90:D1 grp1_text_window_mode_set
90:D4 grp1_text_window_state_set
90:D5 grp1_text_window_bounds_set
90:D6 grp1_text_window_pair_set
90:D7 grp1_text_window_pair_get
90:D8 grp1_text_window_position_set
90:D9 grp1_text_window_size_set
90:DA grp1_text_window_core_get
90:DB grp1_text_window_global_get
90:DC grp1_text_window_core_pair_set
90:DD grp1_text_window_counter_get
90:DE grp1_text_window_visibility_set
90:DF grp1_text_window_mode_direct_set
90:E0 grp1_tail_object_create_get
90:E1 grp1_render_controller_release
90:E4 grp1_render_controller_mode_set
90:E5 grp1_render_controller_transform_set
90:E8 grp1_render_controller_resource_set
90:E9 grp1_render_controller_resource_bind
90:F0 grp1_movie_open_and_play
90:F1 grp1_movie_stop
90:F2 grp1_movie_is_playing
90:F3 grp1_movie_volume_set
90:F4 grp1_buriko_mv_loader_create
90:F5 grp1_movie_sequence_release
90:F6 grp1_resource_frame_extract_bind
90:F7 grp1_resource_lock_handle_create
90:F8 grp1_resource_handle_release_all
90:FA grp1_resource_handle_capture
90:FB grp1_resource_handle_release
90:FC grp1_resource_handle_hit_test
90:FD grp1_resource_handle_flag_get
91:00 grp2_set_game_i32
91:03 grp2_get_draw_text_object
91:04 grp2_attach_runtime_object_3str
91:05 grp2_get_game_rect4
91:06 grp2_set_game_i32_pair
91:09 grp2_graphic_resource_selector_set_status
91:0A grp2_global_graph_option_set_status
91:0B grp2_global_graphics_mode_set
91:0C grp2_ui_graphics_state_get
91:0D grp2_ui_graphics_state_set
91:0E grp2_scene_state_apply
91:0F grp2_scene_state_set
91:10 grp2_make_4bpp_radial_wave_map
91:11 grp2_make_4bpp_linear_offset_map
91:12 grp2_make_4bpp_radial_cos_alpha_map
91:13 grp2_draw_translation_set
91:14 grp2_draw_scale_set
91:15 grp2_draw_rotation_set
91:16 grp2_draw_pivot_set
91:17 grp2_draw_clip_set
91:18 grp2_layer_transform_set
91:19 grp2_layer_transform_blended_set
91:1A grp2_draw_color_set
91:1B grp2_draw_blend_set
91:1C grp2_draw_uv_set
91:1D grp2_draw_transform_indexed
91:1E grp2_draw_projection_set
91:1F grp2_draw_viewport_set
91:31 grp2_set_text_core
91:33 grp2_obj_set_rect3
91:36 grp2_obj_set_transform3
91:37 grp2_obj_set_aux3
91:38 grp2_named_game_record_set
91:3D grp2_layer_state_apply
91:3E grp2_layer_state_set
91:3F grp2_layer_state_secondary_set
91:40 grp2_init_type12_composite
91:41 grp2_type12_apply_resource
91:42 grp2_type12_set_enable
91:43 grp2_type12_set_target_i32
91:44 grp2_type12_set_bounded_i32
91:45 grp2_type12_set_u8
91:46 grp2_type12_set_resource_rect
91:47 grp2_type12_set_geometry5
91:48 grp2_type12_set_rect4
91:49 grp2_type12_secondary_rect_set
91:4A grp2_type12_transform_set
91:55 grp2_get_game_i32_pair
91:60 grp2_effector_create_slot
91:61 grp2_effector_destroy_slot
91:64 grp2_effector_set_enabled
91:65 grp2_effector_bind_two_resources
91:66 grp2_effector_set_mode_basic
91:67 grp2_effector_set_vector_map
91:68 grp2_effector_set_grid_wave
91:69 grp2_effector_set_identity_matrix
91:70 grp2_landscape_create_slot
91:71 grp2_landscape_destroy_slot
91:73 grp2_landscape_get_mouse_cell
91:74 grp2_landscape_set_enabled
91:75 grp2_landscape_set_cell_attr
91:76 grp2_landscape_transform_resource_set
91:78 grp2_landscape_resource_list_apply
91:79 grp2_landscape_apply_resource
91:7A grp2_landscape_set_regions
91:7B grp2_landscape_cell_resources_set
91:7C grp2_landscape_copy_tile_state
91:7D grp2_landscape_set_neighbor_tile
91:7E grp2_landscape_get_cell_code
91:7F grp2_landscape_copy_cell_resource
91:88 grp2_current_set_render_percent
91:89 grp2_current_limit_set
91:8A grp2_current_set_bool_mode
91:8B grp2_current_set_axis_mode
91:8C grp2_current_set_origin
91:8D grp2_current_get_origin3
91:8E grp2_current_get_arrival_flag
91:90 grp2_resource_object_create
91:91 grp2_resource_object_create_blended
91:92 grp2_resource_object_create_extended
91:93 grp2_resource_object_create_blend_ext
91:94 grp2_named_object_resource_set
91:95 grp2_named_object_resource_get
91:96 grp2_named_object_status_get
91:97 grp2_named_object_transform_set
91:98 grp2_named_object_blend_set
91:99 grp2_graph_fixed_point_divisor_set_status
91:9A grp2_global_graph_property_set
91:9B grp2_named_object_bounds_get
91:9C grp2_text_resource_measure
91:9D grp2_blended_text_resource_measure
91:9E grp2_named_object_text_get
91:9F grp2_named_object_text_set
91:B8 grp2_interaction_controller_create
91:BA grp2_interaction_descriptor_apply
91:BB grp2_resource_frame_count_get
91:BC grp2_resource_width_mode_get
91:BD grp2_resource_duration_get
91:BF grp2_resource_metric_apply
91:DB grp2_text_window_status_get
91:F0 grp2_resource_measure_get
91:F1 grp2_resource_play_duration_get
91:F2 grp2_resource_unload_slot_status
91:F3 grp2_resource_set_active_status
91:F7 grp2_resource_get_current_count
91:F8 grp2_play_movie_by_mf
91:F9 grp2_resource_close_context_status
91:FA grp2_resource_context_ready_busy_test
91:FB grp2_resource_set_volume_status
92:00 grp3_game_rect_set
92:01 grp3_game_transform_set
92:0E grp3_game_vector_set
92:0F grp3_set_named_game_string_record
92:10 grp3_make_6bpp_radial_vector_map
92:11 grp3_make_6bpp_axis_vector_map
92:12 grp3_draw_pixel_get
92:13 grp3_draw_blend_get
92:14 grp3_attach_runtime_text_object
92:15 grp3_request_draw_refresh
92:16 grp3_draw_resource_metric_get
92:17 grp3_draw_text_metric_get
92:18 grp3_draw_context_mode_set
92:19 grp3_draw_context_status_get
92:1A grp3_draw_transform_set
92:1B grp3_draw_resource_set
92:1C grp3_draw_text_width_get
92:1D grp3_draw_text_height_get
92:1E grp3_draw_resource_bounds_get
92:1F grp3_draw_context_result_get
92:31 grp3_get_game_i32
92:3D grp3_get_scene_by_name
92:88 grp3_current_set_layer
92:89 grp3_current_blit_resource_rect
92:8A grp3_current_clear_rect
92:8C grp3_current_object_param_set
92:8D grp3_current_blit_resource_ext
92:8E grp3_current_reset_view_state
92:90 grp3_resource_draw_object_create
92:91 grp3_resource_blend_geometry_set
92:94 grp3_draw_get_text_resource
92:95 grp3_current_object_status_get
92:97 grp3_text_draw
92:98 grp3_draw_composite6
92:99 grp3_draw_get_4int_3ptr
92:9B grp3_text_draw_style_set
92:9C grp3_text_resource_render
92:9D grp3_resource_text_draw
92:9E grp3_text_measure_resource_ptr
92:9F grp3_text_set_render_mode
92:F0 grp3_resource_measure_render_extent
92:F1 grp3_load_movie_sequence
92:F2 grp3_resource_measure_get
92:F4 grp3_resource_seek_count_status
92:F5 grp3_resource_count_write
92:F6 grp3_resource_aux_operate_status
A0:00 snd1_return_constant_20
A0:08 snd1_bgm_set_volume
A0:09 snd1_se_set_volume
A0:10 snd1_bgm_play
A0:11 snd1_bgm_play_intro_loop
A0:12 snd1_bgm_play_multi_segment
A0:14 snd1_bgm_set_pause_state
A0:15 snd1_bgm_get_position_wait
A0:16 snd1_bgm_fade_volume
A0:17 snd1_bgm_set_pan
A0:18 snd1_bgm_set_loop_flag
A0:19 snd1_bgm_set_channel_flag
A0:1C snd1_bgm_set_playback_rate
A0:20 snd1_se_play
A0:21 snd1_se_play_with_volume
A0:22 snd1_se_stop
A0:23 snd1_se_play_with_rate_2x
A0:24 snd1_se_fade_and_get_remaining
A0:25 snd1_se_set_pause_stop_mode
A0:26 snd1_se_fade_to_volume
A0:27 snd1_se_play_with_volume_pan
A0:28 snd1_se_play_short_waitable
A0:2C snd1_se_set_volume_direct
A0:2F snd1_se_get_remaining_ms
A0:80 snd1_noop_primary
A0:81 snd1_noop_secondary
A0:84 snd1_mci_cd_play_track
A0:85 snd1_noop_mci_stub
A0:86 snd1_mci_get_status
A0:C0 snd1_playsound_resource
B0:00 usr1_set_game_resource_binding
B0:02 usr1_restore_main_window_position
B0:03 usr1_set_main_window_pos
B0:04 usr1_bind_cursor_target_region
B0:05 usr1_set_cursor_active_range
B0:06 usr1_is_system_cursor_visible
B0:07 reserved_unregistered
B0:08 usr1_create_screen_shake_proc
B0:10 usr1_child_window_create
B0:11 usr1_child_window_close
B0:12 usr1_child_window_create_styled
B0:14 usr1_update_child_window_visibility
B0:15 usr1_child_window_set_title
B0:16 usr1_child_window_move_resize
B0:17 usr1_child_window_get_position
B0:18 usr1_fill_child_window_dc
B0:19 usr1_child_window_draw_resource
B0:1A usr1_child_window_draw_text
B0:1B usr1_child_window_draw_resource_rect
B0:1C usr1_child_window_text_buffer_set
B0:1E usr1_child_window_set_scroll_info
B0:1F usr1_child_window_property_get
B0:20 usr1_create_text_input_edit
B0:21 usr1_destroy_text_input_edit
B0:22 usr1_set_text_input_width_percent
B0:23 usr1_get_text_input_visible
B0:24 usr1_show_text_input_edit
B0:25 usr1_set_text_input_color_bgr
B0:26 usr1_set_text_input_initial_text
B0:27 usr1_get_text_input_text
B0:28 usr1_set_text_input_enter_hides
B0:29 usr1_set_text_input_ascii_block
B0:2A usr1_set_text_input_style_mode
B0:60 usr1_dialog_create
B0:61 usr1_destroy_dialog_chain_entry
B0:62 usr1_dialog_move
B0:63 usr1_dialog_get_position
B0:66 usr1_initialize_dialog_listview_items
B0:67 usr1_dialog_get_selected_item
B0:68 usr1_dialog_send_item_message
B0:6A usr1_dialog_set_item_text
B0:6C usr1_send_dialog_control_message
B0:6D usr1_send_dialog_control_message_ext
B0:6F usr1_get_dialog_control_value
B0:80 usr1_message_box_text_default_flags
B0:81 usr1_message_box_text_with_flags
B0:82 usr1_message_box_typed
B0:83 usr1_set_global_message_text
B0:84 usr1_modal_input_dialog
B0:85 usr1_open_multifield_dialog
B0:86 usr1_modal_choice_dialog
B0:87 usr1_open_multifield_dialog_ext
B0:8C usr1_modal_string_dialog_4str
B0:8F usr1_set_screen_size_limit
B0:A0 usr1_modeless_custom_dialog_create
B0:A1 usr1_modeless_dialog_close
B0:A2 usr1_modeless_dialog_show_state
B0:A3 usr1_get_modeless_dialog_result
B0:C0 usr1_text_layout_default_measure
B0:C1 usr1_get_text_layout_metric_by_mode
B0:C2 usr1_text_resource_metric_get
B0:C3 usr1_text_resource_metric_write
B0:C4 usr1_enum_font_names_default_charset
B0:C5 usr1_enum_font_names_by_charset
B0:C6 usr1_check_font_face_charset_support
B0:C7 usr1_set_font_alias_default
B0:C8 usr1_clear_font_alias_record
B0:CF usr1_text_font_defaults
B0:F0 usr1_set_desktop_wallpaper
C0:00 usr2_resource_slot_get
C0:01 usr2_resource_delete
C0:04 usr2_resource_set_visible
C0:05 usr2_resource_set_transform
C0:06 usr2_load_resource_from_string
C0:08 usr2_resource_flush_pending_cells
C0:09 usr2_resource_set_aux_timed_link
C0:0A usr2_resource_sequence_state_set
C0:0B usr2_resource_numeric_profile_set
C0:0C usr2_resource_set_mode_checked
C0:0D usr2_resource_renderer_cache_reset
C0:0F usr2_resource_renderer_state_reset
C0:10 usr2_set_resource_renderer_numeric12
C0:18 usr2_timed_sequence_extended_set
C0:1A usr2_timed_sequence_checked_add
C0:1B usr2_set_resource_sequence_u8_triplet
C0:1F usr2_resource_animation_timing_set
C0:20 usr2_sequence_plane0_slot_set
C0:24 usr2_timed_sequence_primary_set
C0:25 usr2_compact_sequence_profile_set
C0:28 usr2_sequence_plane1_slot_set
C0:29 usr2_resource_render_profile_set
C0:2C usr2_timed_sequence_secondary_set
C0:2D usr2_extended_sequence_profile_set
C0:40 usr2_resource_layer_create
C0:41 usr2_resource_layer_delete
C0:42 usr2_resource_layer_is_active
C0:43 usr2_resource_layer_load
C0:44 usr2_resource_layer_set_visible
C0:45 usr2_resource_layer_transform
C0:46 usr2_resource_layer_rect_transform
C0:47 usr2_layer_position_scale_set
C0:48 usr2_layer_rotation_scale_set
C0:49 usr2_layer_opacity_scale_set
C0:4A usr2_layer_depth_scale_set
C0:4B usr2_layer_animation_scale_set
C0:4C usr2_layer_primary_vector_set
C0:4D usr2_layer_secondary_vector_set
C0:4E usr2_layer_frame_scale_set
C0:4F usr2_layer_frame_rate_set
C0:C0 usr2_polyline_object_create
C0:C1 usr2_polyline_object_delete
C0:C2 usr2_polyline_rebuild
C0:C3 usr2_polyline_sample_to_vm_triplet
C0:F0 usr2_decode_image_resource_to_graphics
D0:00 usr3_field_manager_create
D0:01 usr3_field_manager_delete
D0:04 usr3_field_manager_allocate_grid
D0:05 usr3_field_manager_add_layer_data
D0:10 usr3_field_manager_add_entity
D0:11 usr3_field_manager_delete_entity
D0:12 usr3_field_entity_clear_attachment
D0:14 usr3_field_entity_set_cell_position
D0:15 usr3_field_entity_set_size
D0:16 usr3_field_entity_set_userdata64
D0:17 usr3_field_entity_type_set
D0:18 usr3_field_entity_set_layer_index
D0:20 usr3_replace_entity_collision
D0:21 usr3_entity_grid_bounds_query
D0:22 usr3_execute_entity_grid_ptr_target
D0:23 usr3_entity_grid_mode_query
D0:28 usr3_field_multi_target_query
D0:2C usr3_entity_attachment_query
D0:2D usr3_field_query_write_result
D0:40 usr3_field_path_context_create
D0:41 usr3_field_path_context_delete
D0:60 usr3_field_entity_transform_set
D0:61 usr3_field_option_set
D0:62 usr3_field_slot_set_scalar_component
D0:63 usr3_field_slot_get_scalar_component
D0:64 usr3_field_slot_set_position_vec3
D0:65 usr3_field_slot_get_position_vec3
D0:66 usr3_field_slot_direction_set
D0:67 usr3_field_slot_get_direction_vec3
D0:68 usr3_field_slot_link_neighbor
D0:69 usr3_field_slot_get_neighbor_count
D0:6A usr3_field_slot_list_neighbors
D0:70 usr3_field_cast_from_slot_position
D0:71 usr3_field_trace_slot_to_point
D0:72 usr3_compute_reachable_region
D0:74 usr3_hit_test_slot_forward
D0:75 usr3_field_hit_test_explicit_point
D0:78 usr3_field_find_nearby_slots_sorted
D0:79 usr3_field_get_distance_between_slots
D0:7A usr3_slot_to_point_vector_get
D0:7B usr3_field_region_collect_slots
D0:80 usr3_field_manager_create_threaded
D0:81 usr3_threaded_field_status_get
D0:84 usr3_threaded_field_set_slot_count
D0:87 usr3_threaded_field_poll_status
D0:88 usr3_bind_threaded_field
D0:8A usr3_threaded_field_enqueue_command2
D0:8C usr3_threaded_field_bind_manager
D0:8D usr3_threaded_field_set_slot_record
D0:8E usr3_threaded_field_get_slot_record
D0:C0 usr3_path_graph_init_node_table
D0:C1 usr3_path_graph_count_key_range
D0:C2 usr3_reset_path_graph
D0:C4 usr3_path_graph_set_node_payload
D0:C5 usr3_path_graph_clear_node_payload
D0:C6 usr3_path_graph_add_weighted_rule
D0:C7 usr3_path_graph_remove_rule
D0:C8 usr3_path_graph_compute_route
E0:00 usr4_dump_display_objects
E0:20 usr4_debug_property_table
E0:3F usr4_parse_register_property
E0:40 usr4_dump_display_objects_to_text
E0:80 usr4_show_text_selection_dialog
E0:90 usr4_execute_debug_runtime
E0:91 usr4_count_runtime_table_items
E0:92 usr4_dump_runtime_table
E0:93 usr4_set_runtime_table_value
E0:C0 usr4_set_global_debug_flag
E0:C2 usr4_export_runtime_table_text
FF:F0 userscript_register_mediation_program
FF:F1 userscript_unregister_mediation
FF:F8 userscript_return_from_mediation
"""

def _parse_precise_subop_names(text):
	names = {}
	for line in text.splitlines():
		line = line.strip()
		if not line:
			continue
		key, name = line.split(None, 1)
		op, sub = key.split(':', 1)
		names[(int(op, 16), int(sub, 16))] = name
	return names

def _make_subop_pseudo(op, sub):
	return 0x10000 | ((op & 0xFF) << 8) | (sub & 0xFF)

def _is_subop_pseudo(op):
	return isinstance(op, int) and (op & 0x10000) != 0

def _split_subop_pseudo(op):
	return (op >> 8) & 0xFF, op & 0xFF

precise_subop_names = _parse_precise_subop_names(precise_subop_name_text)

def _make_subop_sizes(subops, payload_sizes=None):
	sizes = {sub: 0 for sub in subops}
	if payload_sizes:
		sizes.update(payload_sizes)
	return sizes

def _expand_subop_ranges(spec):
	subops = []
	for chunk in spec.split(','):
		chunk = chunk.strip()
		if not chunk:
			continue
		if '-' in chunk:
			start, end = chunk.split('-', 1)
			subops.extend(range(int(start, 16), int(end, 16) + 1))
		else:
			subops.append(int(chunk, 16))
	return subops

subop_size_table = {
	0x7F: _make_subop_sizes(_expand_subop_ranges('00, 80-86, 88-8B')),
	0x80: _make_subop_sizes(_expand_subop_ranges('00-21, 24-2D, 2F-41, 44-4C, 50, 52-54, 58-5A, 5C-71, 74, 78-7B, 80-85, 88-8B, 90-91, 94-9A, 9C-A1, A8-A9, AC, AF-B1, B4-B6, C0-C1, C4-C5, CF-D4, D8-DE, E0-E3, E8-EA, EC-EE, F0-FE')),
	0x81: _make_subop_sizes(_expand_subop_ranges('00-04, 06-11, 14, 16-19, 1B, 1D-21, 27-2D, 2F-32, 34-3E, 44, 48, 60-6F, 80, 8C-8F, B0, B7-B9, D0-D5, D8, DA, E0, E9-EA, EC-ED, F2, F7')),
	0x90: _make_subop_sizes(_expand_subop_ranges('00-24, 28-29, 2C, 30-3A, 3C-3D, 3F-4A, 4C-4D, 50-51, 53-5D, 60-61, 64-66, 70-71, 74-76, 78-7A, 80-89, 90-92, 94-A7, AF-B1, B4-BA, BC-C8, CA, CC-D1, D4-E1, E4-E5, E8-E9, F0-F8, FA-FD')),
	0x91: _make_subop_sizes(_expand_subop_ranges('00, 03-06, 09-1F, 31, 33, 36-38, 3D-4A, 55, 60-61, 64-69, 70-71, 73-76, 78-7F, 88-8E, 90-9F, B8, BA-BD, BF, DB, F0-F3, F7-FB')),
	0x92: _make_subop_sizes(_expand_subop_ranges('00-01, 0E-1F, 31, 3D, 88-8A, 8C-8E, 90-91, 94-95, 97-99, 9B-9F, F0-F2, F4-F6')),
	0xA0: _make_subop_sizes(_expand_subop_ranges('00, 08-09, 10-12, 14-19, 1C, 20-28, 2C, 2F, 80-81, 84-86, C0')),
	0xB0: _make_subop_sizes(_expand_subop_ranges('00, 02-06, 08, 10-12, 14-1C, 1E-2A, 60-63, 66-68, 6A, 6C-6D, 6F, 80-87, 8C, 8F, A0-A3, C0-C8, CF, F0')),
	0xC0: _make_subop_sizes(_expand_subop_ranges('00-01, 04-06, 08-0D, 0F-10, 18, 1A-1B, 1F-20, 24-25, 28-29, 2C-2D, 40-4F, C0-C3, F0')),
	0xD0: _make_subop_sizes(_expand_subop_ranges('00-01, 04-05, 10-12, 14-18, 20-23, 28, 2C-2D, 40-41, 60-6A, 70-72, 74-75, 78-7B, 80-81, 84, 87-88, 8A, 8C-8E, C0-C2, C4-C8')),
	0xE0: _make_subop_sizes(_expand_subop_ranges('00, 20, 3F-40, 80, 90-93, C0, C2')),
	0xFF: _make_subop_sizes([0xF0, 0xF1, 0xF8]),
}

def clear_offsets():
	offsets.clear()

def normalize_encoding(encoding):
	return asdis.normalize_encoding(encoding) or 'cp932'

def set_text_encoding(encoding):
	global current_text_encoding
	current_text_encoding = normalize_encoding(encoding)

def get_string(code, addr, *args):
	pos0 = addr + args[0]
	pos1 = code.find(b'\x00', pos0)
	raw = code[pos0:pos1]
	if any((x < 0x20 and x not in (0x09, 0x0A, 0x0D)) for x in raw):
		return '@hex:%s' % raw.hex()
	try:
		string = raw.decode(current_text_encoding)
	except UnicodeDecodeError:
		return '@hex:%s' % raw.hex()
	string = asdis.escape(string)
	return string
	
def get_offset(code, addr, *args):
	offset = addr + args[0]
	offsets.add(offset)
	return offset

def get_offset_from_base(base, rel):
	offset = base + rel
	offsets.add(offset)
	return offset

def is_fixed_relative_op(op):
	return op in fixed_relative_ops

def get_op_entry(op):
	if not opcode_available(op):
		return None
	if op == 0x09 and get_profile().store_keeps_value:
		return ('b', 'store_keep(%#x)', None)
	if op == 0x7F and not get_profile().compact:
		return ('', 'debug_hook()', None)
	return ops.get(op)

def _parse_int_token(token):
	token = token.strip()
	if token.startswith('L-'):
		return -int(token[2:], 16)
	if token.startswith('L'):
		return int(token[1:], 16)
	if token.startswith('0x') or token.startswith('-0x'):
		return int(token, 16)
	return int(token)

def decode_sleb128(code, pos):
	result = 0
	shift = 0
	size = 0
	while True:
		byte = code[pos + size]
		size += 1
		result |= (byte & 0x7F) << shift
		shift += 7
		if (byte & 0x80) == 0:
			break
	if byte & 0x40:
		result |= - (1 << shift)
	return result, size

def encode_sleb128(value):
	out = bytearray()
	more = True
	while more:
		byte = value & 0x7F
		value >>= 7
		sign_bit = byte & 0x40
		if (value == 0 and sign_bit == 0) or (value == -1 and sign_bit != 0):
			more = False
		else:
			byte |= 0x80
		out.append(byte & 0xFF)
	return bytes(out)

def decode_typed_sleb128(code, pos):
	value, size = decode_sleb128(code, pos)
	return value & 0x3, value >> 2, size

def encode_typed_sleb128(type_id, value):
	return encode_sleb128((value << 2) | (type_id & 0x3))

def _scan_sleb128_sequence(code, pos, count):
	size = 0
	for _ in range(count):
		_, n = decode_sleb128(code, pos + size)
		size += n
	return size

def _validate_sleb128_sequence(raw, count):
	pos = 0
	for _ in range(count):
		if pos >= len(raw):
			raise Exception('truncated sleb128 sequence')
		_, n = decode_sleb128(raw, pos)
		pos += n
	if pos != len(raw):
		raise Exception('unexpected trailing bytes in sleb128 sequence')
	return pos

def _format_list(values, value_formatter=str):
	return '[%s]' % ', '.join(value_formatter(x) for x in values)

def _decode_sleb128_values(raw, count):
	values = []
	pos = 0
	for _ in range(count):
		value, n = decode_sleb128(raw, pos)
		values.append(value)
		pos += n
	return values

def _decode_multi_offsets(raw):
	return [x[0] for x in struct.iter_unpack('<H', raw)]

def _parse_list_token(token):
	value = token.strip()
	if not value.startswith('[') or not value.endswith(']'):
		raise Exception('invalid list token %s' % token)
	body = value[1:-1].strip()
	if not body:
		return []
	return [_parse_int_token(item.strip()) for item in body.split(',')]

def _encode_sleb128_values(values):
	out = bytearray()
	for value in values:
		out.extend(encode_sleb128(value))
	return bytes(out)

def _encode_multi_offsets(values):
	out = bytearray()
	for value in values:
		out.extend(struct.pack('<H', value & 0xFFFF))
	return bytes(out)

def decode_var_instr(op, code, addr, pos):
	if op == 0x7F and not get_profile().compact:
		return 'debug_hook()', 0
	if op == 0x15 and not get_profile().compact:
		return 'jc_legacy(%#x)' % code[pos], 1
	if op == 0x0B:
		if pos >= len(code):
			raise ValueError('truncated store_inline length @ offset %05x' % addr)
		count = code[pos]
		end = pos + 1 + count
		if end > len(code):
			raise ValueError('truncated store_inline payload @ offset %05x' % addr)
		return 'store_inline("%s")' % code[pos + 1:end].hex(), 1 + count
	if op in subop_group_ops:
		sub = code[pos]
		extra_size = _get_subop_size(op, sub)
		name = _get_subop_name(op, sub)
		if extra_size == 0:
			return '%s()' % name, 1
		raw = code[pos+1:pos+1+extra_size]
		if len(raw) != extra_size:
			raise Exception('truncated sub-op payload op %02x sub %02x @ offset %05x' % (op, sub, addr))
		return '%s("%s")' % (name, raw.hex()), 1 + extra_size
	if op == 0x03:
		mode = struct.unpack('b', code[pos:pos+1])[0]
		if mode >= 0:
			count = mode + 1
			payload_size = _scan_sleb128_sequence(code, pos + 1, count)
			raw = code[pos + 1:pos + 1 + payload_size]
			values = _decode_sleb128_values(raw, count)
			return 'pushx(%#x, %s)' % (mode & 0xFF, _format_list(values)), 1 + payload_size
		if (mode & 0x7C) != 0:
			return 'pushx(%#x)' % (mode & 0xFF), 1
		width = mode & 0x3
		if width == 0:
			value = struct.unpack('b', code[pos + 1:pos + 2])[0]
			return 'pushx(%#x, %d)' % (mode & 0xFF, value), 2
		if width == 1:
			value, = struct.unpack('<h', code[pos + 1:pos + 3])
			return 'pushx(%#x, %d)' % (mode & 0xFF, value), 3
		if width == 2:
			value, = struct.unpack('<i', code[pos + 1:pos + 5])
			return 'pushx(%#x, %d)' % (mode & 0xFF, value), 5
		value, = struct.unpack('<q', code[pos + 1:pos + 9])
		return 'pushx(%#x, %d)' % (mode & 0xFF, value), 9
	if op == 0x0D:
		type_id, imm, n = decode_typed_sleb128(code, pos)
		return 'store_typed_imm(%#x, %d)' % (type_id, imm), n
	if op == 0x0E:
		rel, = struct.unpack('<H', code[pos:pos+2])
		type_id, imm, n = decode_typed_sleb128(code, pos + 2)
		return 'store_rel_typed_imm(%#x, %#x, %d)' % (rel, type_id, imm), 2 + n
	if op == 0x12:
		imm, n = decode_sleb128(code, pos)
		return 'add_frame_imm(%d)' % imm, n
	if op == 0x15:
		flag = code[pos]
		if flag & 0x08:
			rel, = struct.unpack('<h', code[pos+1:pos+3])
			dst = get_offset_from_base(addr, rel)
			return 'jc(%#x, L%05x)' % (flag, dst), 3
		return 'jc(%#x)' % flag, 1
	if op == 0x1A:
		t, = struct.unpack('<H', code[pos:pos+2])
		imm, n = decode_sleb128(code, pos + 2)
		return 'load_base_typed_add(%#x, %d)' % (t, imm), 2 + n
	if op == 0x1B:
		t, = struct.unpack('<H', code[pos:pos+2])
		imm, n = decode_sleb128(code, pos + 2)
		return '%s(%#x, %d)' % (_get_op_name(op), t, imm), 2 + n
	if op == 0x1F:
		imm, n = decode_sleb128(code, pos)
		return '%s(%d)' % (_get_op_name(op), imm), n
	if op == 0x2C:
		imm, n = decode_sleb128(code, pos)
		return 'add_imm(%d)' % imm, n
	if op == 0x2D:
		imm, n = decode_sleb128(code, pos)
		return 'mul_imm(%d)' % imm, n
	if op == 0x2E:
		imm, n = decode_sleb128(code, pos)
		return 'mad_imm(%d)' % imm, n
	if op == 0x2F:
		imm, n = decode_sleb128(code, pos)
		return 'div_imm(%d)' % imm, n
	if op == 0x36:
		flag = code[pos]
		imm, n = decode_sleb128(code, pos + 1)
		return 'cmp_imm(%#x, %d)' % (flag, imm), 1 + n
	if op == 0x37:
		flag = code[pos]
		rel, = struct.unpack('<h', code[pos+1:pos+3])
		imm, n = decode_sleb128(code, pos + 3)
		dst = get_offset_from_base(addr, rel)
		return 'cmp_imm_jc(%#x, L%05x, %d)' % (flag, dst, imm), 3 + n
	if op == 0x3B:
		flag = code[pos]
		rel, = struct.unpack('<h', code[pos+1:pos+3])
		dst = get_offset_from_base(addr, rel)
		return 'jcmp(%#x, L%05x)' % (flag, dst), 3
	if op == 0x3F:
		flag = code[pos]
		imm, n = decode_sleb128(code, pos + 1)
		return 'cmp_store_imm(%#x, %d)' % (flag, imm), 1 + n
	if op == 0xD5:
		rel, = struct.unpack('<H', code[pos:pos+2])
		imm, n = decode_sleb128(code, pos + 2)
		return 'load_code_ref_add_imm(%#x, %d)' % (rel, imm), 2 + n
	if op == 0xD6:
		rel, = struct.unpack('<H', code[pos:pos+2])
		type_id, imm, n = decode_typed_sleb128(code, pos + 2)
		return 'load_code_ref_indexed_typed(%#x, %#x, %d)' % (rel, type_id, imm), 2 + n
	if op == 0xD7:
		rel0, rel1 = struct.unpack('<HH', code[pos:pos+4])
		type_id, imm, n = decode_typed_sleb128(code, pos + 4)
		return 'store_code_ref_indexed_typed(%#x, %#x, %#x, %d)' % (rel0, rel1, type_id, imm), 4 + n
	if op == 0xD8:
		rel, = struct.unpack('<H', code[pos:pos+2])
		imm, n = decode_sleb128(code, pos + 2)
		return 'load_code_ref_scaled_add(%#x, %d)' % (rel, imm), 2 + n
	if op == 0xDA:
		flag = code[pos]
		rel, = struct.unpack('<H', code[pos+1:pos+3])
		imm, n = decode_sleb128(code, pos + 3)
		return 'cmp_code_ref_imm(%#x, %#x, %d)' % (flag, rel, imm), 3 + n
	if op == 0xDB:
		flag = code[pos]
		rel, = struct.unpack('<H', code[pos+1:pos+3])
		type_id, imm0, n0 = decode_typed_sleb128(code, pos + 3)
		imm1, n1 = decode_sleb128(code, pos + 3 + n0)
		return 'cmp_code_ref_indexed_imm(%#x, %#x, %#x, %d, %d)' % (flag, rel, type_id, imm0, imm1), 3 + n0 + n1
	if op == 0xDC:
		flag = code[pos]
		rel, ref = struct.unpack('<hH', code[pos+1:pos+5])
		imm, n = decode_sleb128(code, pos + 5)
		dst = get_offset_from_base(addr, rel)
		return 'jc_cmp_code_ref_imm(%#x, L%05x, %#x, %d)' % (flag, dst, ref, imm), 5 + n
	if op == 0xDD:
		flag = code[pos]
		rel, ref = struct.unpack('<hH', code[pos+1:pos+5])
		type_id, imm0, n0 = decode_typed_sleb128(code, pos + 5)
		imm1, n1 = decode_sleb128(code, pos + 5 + n0)
		dst = get_offset_from_base(addr, rel)
		return 'jc_cmp_code_ref_indexed_imm(%#x, L%05x, %#x, %#x, %d, %d)' % (flag, dst, ref, type_id, imm0, imm1), 5 + n0 + n1
	if op == 0xDE:
		rel, ref = struct.unpack('<hH', code[pos:pos+4])
		dst = get_offset_from_base(addr, rel)
		return 'jc_test_code_ref(L%05x, %#x)' % (dst, ref), 4
	if op == 0xDF:
		rel, ref = struct.unpack('<hH', code[pos:pos+4])
		type_id, imm, n = decode_typed_sleb128(code, pos + 4)
		dst = get_offset_from_base(addr, rel)
		return 'jc_test_code_ref_indexed(L%05x, %#x, %#x, %d)' % (dst, ref, type_id, imm), 4 + n
	if op == 0xEA:
		rel0, rel1 = struct.unpack('<HH', code[pos:pos+4])
		imm, n = decode_sleb128(code, pos + 4)
		return 'store_indirect_imm(%#x, %#x, %d)' % (rel0, rel1, imm), 4 + n
	if op == 0xEC:
		imm, n = decode_sleb128(code, pos)
		return 'memcpy_stack(%d)' % imm, n
	if op == 0xED:
		rel, = struct.unpack('<H', code[pos:pos+2])
		imm, n = decode_sleb128(code, pos + 2)
		return 'memcpy_rel_stack(%#x, %d)' % (rel, imm), 2 + n
	if op == 0xE2 or op == 0xE3:
		count = code[pos] + 1
		raw = code[pos + 1:pos + 1 + count * 2]
		if len(raw) != count * 2:
			raise Exception('truncated multi-offset payload op %02x @ offset %05x' % (op, addr))
		name = 'store_multi' if op == 0xE2 else 'load_multi'
		offsets = _decode_multi_offsets(raw)
		return '%s(%s)' % (name, _format_list(offsets, lambda x: '%#x' % x)), 1 + count * 2
	if op == 0xE4:
		rel, = struct.unpack('<H', code[pos:pos+2])
		type_id, imm, n = decode_typed_sleb128(code, pos + 2)
		return 'load_rel_add(%#x, %#x, %d)' % (rel, type_id, imm), 2 + n
	if op == 0xE5:
		rel, = struct.unpack('<H', code[pos:pos+2])
		type_id, imm, n = decode_typed_sleb128(code, pos + 2)
		return '%s(%#x, %#x, %d)' % (_get_op_name(op), rel, type_id, imm), 2 + n
	if op == 0xE6:
		rel, = struct.unpack('<H', code[pos:pos+2])
		type_id, imm, n = decode_typed_sleb128(code, pos + 2)
		return '%s(%#x, %#x, %d)' % (_get_op_name(op), rel, type_id, imm), 2 + n
	if op == 0xE7:
		rel, = struct.unpack('<H', code[pos:pos+2])
		type_id, imm, n = decode_typed_sleb128(code, pos + 2)
		return '%s(%#x, %#x, %d)' % (_get_op_name(op), rel, type_id, imm), 2 + n
	if op == 0xF0:
		ptr, rel = struct.unpack('<IH', code[pos:pos+6])
		imm, n = decode_sleb128(code, pos + 6)
		return 'store_ptr_add(%#x, %#x, %d)' % (ptr, rel, imm), 6 + n
	if op == 0xF1:
		rel0, rel1 = struct.unpack('<HH', code[pos:pos+4])
		imm, n = decode_sleb128(code, pos + 4)
		return 'store_rel_add32(%#x, %#x, %d)' % (rel0, rel1, imm), 4 + n
	if op == 0xF2:
		rel0, rel1, rel2 = struct.unpack('<HHH', code[pos:pos+6])
		imm, n = decode_sleb128(code, pos + 6)
		return 'store_indirect_add32(%#x, %#x, %#x, %d)' % (rel0, rel1, rel2, imm), 6 + n
	raise Exception('unknown variable instruction op %02x @ offset %05x' % (op, addr))


def get_instr_size(op, args):
	if op == 0x7F and not get_profile().compact:
		if args:
			raise ValueError('debug_hook takes no inline operands')
		return 1
	if op == 0x15 and not get_profile().compact:
		if len(args) != 1:
			raise ValueError('legacy conditional branch takes one condition byte')
		return 2
	if op == 0x0B:
		return 2 + len(_inline_payload(args))
	if _is_subop_pseudo(op):
		group_op, sub = _split_subop_pseudo(op)
		extra_size = _get_subop_size(group_op, sub)
		if extra_size == 0:
			if len(args) != 0:
				raise Exception('unexpected payload for sub-op')
			return 2
		if len(args) != 1:
			raise Exception('missing payload for sub-op')
		raw = _parse_hex_blob_token(args[0])
		if len(raw) != extra_size:
			raise Exception('invalid payload size for sub-op group %02x sub %02x: got %d expect %d' % (group_op, sub, len(raw), extra_size))
		return 2 + extra_size
	if op == 0x03:
		if len(args) not in (1, 2, 3):
			raise Exception('invalid arg count for pushx')
		mode = _parse_int_token(args[0]) & 0xFF
		smode = struct.unpack('b', struct.pack('B', mode))[0]
		if smode >= 0:
			if len(args) not in (2, 3):
				raise Exception('missing payload for pushx multi form')
			if args[1].strip().startswith('['):
				values = _parse_list_token(args[1])
				if len(values) != smode + 1:
					raise Exception('invalid pushx multi value count: got %d expect %d' % (len(values), smode + 1))
				raw = _encode_sleb128_values(values)
			else:
				raw = _parse_hex_blob_token(args[1])
			_validate_sleb128_sequence(raw, smode + 1)
			return 2 + len(raw)
		if (mode & 0x7C) != 0:
			if len(args) != 1:
				raise Exception('unexpected payload for pushx reserved form')
			return 2
		if len(args) != 2:
			raise Exception('missing immediate for pushx scalar form')
		width = mode & 0x3
		return 2 + (1 if width == 0 else 2 if width == 1 else 4 if width == 2 else 8)
	if op == 0x0D:
		if len(args) != 2:
			raise Exception('invalid arg count for store_typed_imm')
		type_id = _parse_int_token(args[0]) & 0x3
		imm = _parse_int_token(args[1])
		return 1 + len(encode_typed_sleb128(type_id, imm))
	if op == 0x0E:
		if len(args) != 3:
			raise Exception('invalid arg count for store_rel_typed_imm')
		type_id = _parse_int_token(args[1]) & 0x3
		imm = _parse_int_token(args[2])
		return 1 + 2 + len(encode_typed_sleb128(type_id, imm))
	if op == 0x15:
		if len(args) == 1:
			return 2
		if len(args) == 2:
			return 4
		raise Exception('invalid arg count for jc')
	if op == 0x1A:
		if len(args) != 2:
			raise Exception('invalid arg count for load_base_typed_add')
		return 1 + 2 + len(encode_sleb128(_parse_int_token(args[1])))
	if op in (0x12, 0x1F, 0x2C, 0x2D, 0x2E, 0x2F):
		if len(args) != 1:
			raise Exception('invalid arg count for variable op')
		return 1 + len(encode_sleb128(_parse_int_token(args[0])))
	if op == 0x36:
		if len(args) != 2:
			raise Exception('invalid arg count for cmp_imm')
		return 2 + len(encode_sleb128(_parse_int_token(args[1])))
	if op == 0x37:
		if len(args) != 3:
			raise Exception('invalid arg count for cmp_imm_jc')
		return 4 + len(encode_sleb128(_parse_int_token(args[2])))
	if op == 0x1B:
		if len(args) != 2:
			raise Exception('invalid arg count for load_base_scaled_add')
		return 1 + 2 + len(encode_sleb128(_parse_int_token(args[1])))
	if op == 0x3B:
		if len(args) != 2:
			raise Exception('invalid arg count for jcmp')
		return 4
	if op == 0x3F:
		if len(args) != 2:
			raise Exception('invalid arg count for cmp_store_imm')
		return 2 + len(encode_sleb128(_parse_int_token(args[1])))
	if op == 0xD5:
		if len(args) != 2:
			raise Exception('invalid arg count for load_code_ref_add_imm')
		return 1 + 2 + len(encode_sleb128(_parse_int_token(args[1])))
	if op == 0xD6:
		if len(args) != 3:
			raise Exception('invalid arg count for load_code_ref_indexed_typed')
		type_id = _parse_int_token(args[1]) & 0x3
		imm = _parse_int_token(args[2])
		return 1 + 2 + len(encode_typed_sleb128(type_id, imm))
	if op == 0xD7:
		if len(args) != 4:
			raise Exception('invalid arg count for store_code_ref_indexed_typed')
		type_id = _parse_int_token(args[2]) & 0x3
		imm = _parse_int_token(args[3])
		return 1 + 2 + 2 + len(encode_typed_sleb128(type_id, imm))
	if op == 0xD8:
		if len(args) != 2:
			raise Exception('invalid arg count for load_code_ref_scaled_add')
		return 1 + 2 + len(encode_sleb128(_parse_int_token(args[1])))
	if op == 0xDA:
		if len(args) != 3:
			raise Exception('invalid arg count for cmp_code_ref_imm')
		return 1 + 1 + 2 + len(encode_sleb128(_parse_int_token(args[2])))
	if op == 0xDB:
		if len(args) != 5:
			raise Exception('invalid arg count for cmp_code_ref_indexed_imm')
		type_id = _parse_int_token(args[2]) & 0x3
		imm0 = _parse_int_token(args[3])
		imm1 = _parse_int_token(args[4])
		return 1 + 1 + 2 + len(encode_typed_sleb128(type_id, imm0)) + len(encode_sleb128(imm1))
	if op == 0xDC:
		if len(args) != 4:
			raise Exception('invalid arg count for jc_cmp_code_ref_imm')
		return 1 + 1 + 2 + 2 + len(encode_sleb128(_parse_int_token(args[3])))
	if op == 0xDD:
		if len(args) != 6:
			raise Exception('invalid arg count for jc_cmp_code_ref_indexed_imm')
		type_id = _parse_int_token(args[3]) & 0x3
		imm0 = _parse_int_token(args[4])
		imm1 = _parse_int_token(args[5])
		return 1 + 1 + 2 + 2 + len(encode_typed_sleb128(type_id, imm0)) + len(encode_sleb128(imm1))
	if op == 0xDE:
		if len(args) != 2:
			raise Exception('invalid arg count for jc_test_code_ref')
		return 5
	if op == 0xDF:
		if len(args) != 4:
			raise Exception('invalid arg count for jc_test_code_ref_indexed')
		type_id = _parse_int_token(args[2]) & 0x3
		imm = _parse_int_token(args[3])
		return 1 + 2 + 2 + len(encode_typed_sleb128(type_id, imm))
	if op == 0xEA:
		if len(args) != 3:
			raise Exception('invalid arg count for store_indirect_imm')
		return 1 + 4 + len(encode_sleb128(_parse_int_token(args[2])))
	if op == 0xEC:
		if len(args) != 1:
			raise Exception('invalid arg count for memcpy_stack')
		return 1 + len(encode_sleb128(_parse_int_token(args[0])))
	if op == 0xED:
		if len(args) != 2:
			raise Exception('invalid arg count for memcpy_rel_stack')
		return 1 + 2 + len(encode_sleb128(_parse_int_token(args[1])))
	if op == 0xE2 or op == 0xE3:
		if len(args) not in (1, 2):
			raise Exception('invalid arg count for multi-offset op')
		if args[0].strip().startswith('['):
			values = _parse_list_token(args[0])
			raw = _encode_multi_offsets(values)
		else:
			raw = _parse_hex_blob_token(args[0])
		if len(raw) == 0 or len(raw) % 2 != 0:
			raise Exception('invalid multi-offset payload')
		return 2 + len(raw)
	if op in (0xE4, 0xE5, 0xE6, 0xE7):
		if len(args) != 3:
			raise Exception('invalid arg count for typed indexed op')
		type_id = _parse_int_token(args[1]) & 0x3
		imm = _parse_int_token(args[2])
		return 1 + 2 + len(encode_typed_sleb128(type_id, imm))
	if op == 0xF0:
		if len(args) != 3:
			raise Exception('invalid arg count for store_ptr_add')
		return 1 + 4 + 2 + len(encode_sleb128(_parse_int_token(args[2])))
	if op == 0xF1:
		if len(args) != 3:
			raise Exception('invalid arg count for store_rel_add32')
		return 1 + 2 + 2 + len(encode_sleb128(_parse_int_token(args[2])))
	if op == 0xF2:
		if len(args) != 4:
			raise Exception('invalid arg count for store_indirect_add32')
		return 1 + 2 + 2 + 2 + len(encode_sleb128(_parse_int_token(args[3])))
	entry = get_op_entry(op)
	if entry is None:
		raise Exception('unknown instruction op %02x' % op)
	return struct.calcsize(entry[0]) + 1

def _resolve_arg_value(arg, symbols):
	if arg in symbols:
		return symbols[arg]
	return _parse_int_token(arg)

def _get_subop_group_name(op):
	return _get_op_name(op)

def _get_op_name(op):
	fcn, = re_fcn.match(ops[op][1]).groups()
	return fcn

def _get_subop_name(op, sub):
	if not opcode_available(op, sub):
		raise ValueError('opcode %02X:%02X is not defined by BP engine %s' % (op, sub, get_profile().name))
	name = historical_names().get((op, sub))
	if name is not None:
		return name
	return precise_subop_names.get((op, sub), '%s_%02x' % (_get_subop_group_name(op), sub))

def _get_subop_size(op, sub):
	return subop_size_table.get(op, {}).get(sub, 0)

def _parse_hex_blob_token(token):
	value = token.strip()
	if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
		value = value[1:-1]
	if value.startswith('0x') or value.startswith('0X'):
		value = value[2:]
	if len(value) % 2 != 0:
		raise Exception('invalid sub-op hex blob %s' % token)
	if value:
		return bytes.fromhex(value)
	return b''

def _branch_rel(arg, pos, symbols):
	return _resolve_arg_value(arg, symbols) - pos


def _inline_payload(args):
	if len(args) != 1 or not args[0].strip().startswith('"'):
		raise ValueError('store_inline requires a quoted hex payload; re-disassemble old read_data(count) sources from the original BP')
	raw = _parse_hex_blob_token(args[0])
	if len(raw) > 255:
		raise ValueError('store_inline payload exceeds 255 bytes')
	return raw


def encode_var_instr(op, args, pos, symbols):
	if op == 0x7F and not get_profile().compact:
		return b'\x7f'
	if op == 0x15 and not get_profile().compact:
		return struct.pack('BB', op, _parse_int_token(args[0]))
	if op == 0x0B:
		raw = _inline_payload(args)
		return bytes((op, len(raw))) + raw
	if _is_subop_pseudo(op):
		group_op, sub = _split_subop_pseudo(op)
		extra_size = _get_subop_size(group_op, sub)
		if extra_size == 0:
			if len(args) != 0:
				raise Exception('unexpected payload for sub-op')
			return struct.pack('BB', group_op, sub)
		if len(args) != 1:
			raise Exception('missing payload for sub-op')
		raw = _parse_hex_blob_token(args[0])
		if len(raw) != extra_size:
			raise Exception('invalid payload size for sub-op group %02x sub %02x: got %d expect %d' % (group_op, sub, len(raw), extra_size))
		return struct.pack('BB', group_op, sub) + raw
	if op == 0x03:
		mode = _parse_int_token(args[0]) & 0xFF
		smode = struct.unpack('b', struct.pack('B', mode))[0]
		if smode >= 0:
			if args[1].strip().startswith('['):
				values = _parse_list_token(args[1])
				if len(values) != smode + 1:
					raise Exception('invalid pushx multi value count: got %d expect %d' % (len(values), smode + 1))
				raw = _encode_sleb128_values(values)
			else:
				raw = _parse_hex_blob_token(args[1])
			_validate_sleb128_sequence(raw, smode + 1)
			return struct.pack('BB', op, mode) + raw
		if (mode & 0x7C) != 0:
			return struct.pack('BB', op, mode)
		value = _parse_int_token(args[1])
		width = mode & 0x3
		if width == 0:
			return struct.pack('<BBb', op, mode, value)
		if width == 1:
			return struct.pack('<BBh', op, mode, value)
		if width == 2:
			return struct.pack('<BBi', op, mode, value)
		return struct.pack('<BBq', op, mode, value)
	if op == 0x0D:
		type_id = _parse_int_token(args[0]) & 0x3
		imm = _parse_int_token(args[1])
		return struct.pack('B', op) + encode_typed_sleb128(type_id, imm)
	if op == 0x0E:
		rel = _parse_int_token(args[0]) & 0xFFFF
		type_id = _parse_int_token(args[1]) & 0x3
		imm = _parse_int_token(args[2])
		return struct.pack('<BH', op, rel) + encode_typed_sleb128(type_id, imm)
	if op == 0x12:
		return struct.pack('B', op) + encode_sleb128(_parse_int_token(args[0]))
	if op == 0x15:
		if len(args) == 1:
			flag = _parse_int_token(args[0]) & 0xFF
			return struct.pack('BB', op, flag)
		flag = _parse_int_token(args[0]) & 0xFF
		rel = _branch_rel(args[1], pos, symbols)
		return struct.pack('<BBh', op, flag, rel)
	if op == 0x1A:
		t = _parse_int_token(args[0]) & 0xFFFF
		imm = _parse_int_token(args[1])
		return struct.pack('<BH', op, t) + encode_sleb128(imm)
	if op == 0x1B:
		t = _parse_int_token(args[0]) & 0xFFFF
		imm = _parse_int_token(args[1])
		return struct.pack('<BH', op, t) + encode_sleb128(imm)
	if op == 0x1F:
		return struct.pack('B', op) + encode_sleb128(_parse_int_token(args[0]))
	if op == 0x2C:
		return struct.pack('B', op) + encode_sleb128(_parse_int_token(args[0]))
	if op == 0x2D:
		return struct.pack('B', op) + encode_sleb128(_parse_int_token(args[0]))
	if op == 0x2E:
		return struct.pack('B', op) + encode_sleb128(_parse_int_token(args[0]))
	if op == 0x2F:
		return struct.pack('B', op) + encode_sleb128(_parse_int_token(args[0]))
	if op == 0x36:
		flag = _parse_int_token(args[0]) & 0xFF
		imm = _parse_int_token(args[1])
		return struct.pack('BB', op, flag) + encode_sleb128(imm)
	if op == 0x37:
		flag = _parse_int_token(args[0]) & 0xFF
		imm = _parse_int_token(args[2])
		rel = _branch_rel(args[1], pos, symbols)
		return struct.pack('<BBh', op, flag, rel) + encode_sleb128(imm)
	if op == 0x3B:
		flag = _parse_int_token(args[0]) & 0xFF
		rel = _branch_rel(args[1], pos, symbols)
		return struct.pack('<BBh', op, flag, rel)
	if op == 0x3F:
		flag = _parse_int_token(args[0]) & 0xFF
		imm = _parse_int_token(args[1])
		return struct.pack('BB', op, flag) + encode_sleb128(imm)
	if op == 0xD5:
		rel = _parse_int_token(args[0]) & 0xFFFF
		imm = _parse_int_token(args[1])
		return struct.pack('<BH', op, rel) + encode_sleb128(imm)
	if op == 0xD6:
		rel = _parse_int_token(args[0]) & 0xFFFF
		type_id = _parse_int_token(args[1]) & 0x3
		imm = _parse_int_token(args[2])
		return struct.pack('<BH', op, rel) + encode_typed_sleb128(type_id, imm)
	if op == 0xD7:
		rel0 = _parse_int_token(args[0]) & 0xFFFF
		rel1 = _parse_int_token(args[1]) & 0xFFFF
		type_id = _parse_int_token(args[2]) & 0x3
		imm = _parse_int_token(args[3])
		return struct.pack('<BHH', op, rel0, rel1) + encode_typed_sleb128(type_id, imm)
	if op == 0xD8:
		rel = _parse_int_token(args[0]) & 0xFFFF
		imm = _parse_int_token(args[1])
		return struct.pack('<BH', op, rel) + encode_sleb128(imm)
	if op == 0xDA:
		flag = _parse_int_token(args[0]) & 0xFF
		rel = _parse_int_token(args[1]) & 0xFFFF
		imm = _parse_int_token(args[2])
		return struct.pack('<BBH', op, flag, rel) + encode_sleb128(imm)
	if op == 0xDB:
		flag = _parse_int_token(args[0]) & 0xFF
		rel = _parse_int_token(args[1]) & 0xFFFF
		type_id = _parse_int_token(args[2]) & 0x3
		imm0 = _parse_int_token(args[3])
		imm1 = _parse_int_token(args[4])
		return struct.pack('<BBH', op, flag, rel) + encode_typed_sleb128(type_id, imm0) + encode_sleb128(imm1)
	if op == 0xDC:
		flag = _parse_int_token(args[0]) & 0xFF
		ref = _parse_int_token(args[2]) & 0xFFFF
		imm = _parse_int_token(args[3])
		imm_raw = encode_sleb128(imm)
		rel = _branch_rel(args[1], pos, symbols)
		return struct.pack('<BBhH', op, flag, rel, ref) + imm_raw
	if op == 0xDD:
		flag = _parse_int_token(args[0]) & 0xFF
		ref = _parse_int_token(args[2]) & 0xFFFF
		type_id = _parse_int_token(args[3]) & 0x3
		imm0 = _parse_int_token(args[4])
		imm1 = _parse_int_token(args[5])
		imm0_raw = encode_typed_sleb128(type_id, imm0)
		imm1_raw = encode_sleb128(imm1)
		rel = _branch_rel(args[1], pos, symbols)
		return struct.pack('<BBhH', op, flag, rel, ref) + imm0_raw + imm1_raw
	if op == 0xDE:
		ref = _parse_int_token(args[1]) & 0xFFFF
		rel = _branch_rel(args[0], pos, symbols)
		return struct.pack('<BhH', op, rel, ref)
	if op == 0xDF:
		ref = _parse_int_token(args[1]) & 0xFFFF
		type_id = _parse_int_token(args[2]) & 0x3
		imm = _parse_int_token(args[3])
		imm_raw = encode_typed_sleb128(type_id, imm)
		rel = _branch_rel(args[0], pos, symbols)
		return struct.pack('<BhH', op, rel, ref) + imm_raw
	if op == 0xEA:
		rel0 = _parse_int_token(args[0]) & 0xFFFF
		rel1 = _parse_int_token(args[1]) & 0xFFFF
		imm = _parse_int_token(args[2])
		return struct.pack('<BHH', op, rel0, rel1) + encode_sleb128(imm)
	if op == 0xEC:
		return struct.pack('B', op) + encode_sleb128(_parse_int_token(args[0]))
	if op == 0xED:
		rel = _parse_int_token(args[0]) & 0xFFFF
		imm = _parse_int_token(args[1])
		return struct.pack('<BH', op, rel) + encode_sleb128(imm)
	if op == 0xE2 or op == 0xE3:
		if args[0].strip().startswith('['):
			raw = _encode_multi_offsets(_parse_list_token(args[0]))
		else:
			raw = _parse_hex_blob_token(args[0])
		if len(raw) == 0 or len(raw) % 2 != 0:
			raise Exception('invalid multi-offset payload')
		count = len(raw) // 2
		return struct.pack('BB', op, count - 1) + raw
	if op in (0xE4, 0xE5, 0xE6, 0xE7):
		rel = _parse_int_token(args[0]) & 0xFFFF
		type_id = _parse_int_token(args[1]) & 0x3
		imm = _parse_int_token(args[2])
		return struct.pack('<BH', op, rel) + encode_typed_sleb128(type_id, imm)
	if op == 0xF0:
		ptr = _parse_int_token(args[0]) & 0xFFFFFFFF
		rel = _parse_int_token(args[1]) & 0xFFFF
		imm = _parse_int_token(args[2])
		return struct.pack('<BIH', op, ptr, rel) + encode_sleb128(imm)
	if op == 0xF1:
		rel0 = _parse_int_token(args[0]) & 0xFFFF
		rel1 = _parse_int_token(args[1]) & 0xFFFF
		imm = _parse_int_token(args[2])
		return struct.pack('<BHH', op, rel0, rel1) + encode_sleb128(imm)
	if op == 0xF2:
		rel0 = _parse_int_token(args[0]) & 0xFFFF
		rel1 = _parse_int_token(args[1]) & 0xFFFF
		rel2 = _parse_int_token(args[2]) & 0xFFFF
		imm = _parse_int_token(args[3])
		return struct.pack('<BHHH', op, rel0, rel1, rel2) + encode_sleb128(imm)
	raise Exception('unknown variable instruction op %02x @ offset %05x' % (op, pos))

var_ops = {0x03, 0x0B, 0x0D, 0x0E, 0x12, 0x15, 0x1A, 0x1B, 0x1F, 0x2C, 0x2D, 0x2E, 0x2F, 0x36, 0x37, 0x3B, 0x3F, 0xD5, 0xD6, 0xD7, 0xD8, 0xDA, 0xDB, 0xDC, 0xDD, 0xDE, 0xDF, 0xEA, 0xEC, 0xED, 0xE2, 0xE3, 0xE4, 0xE5, 0xE6, 0xE7, 0xF0, 0xF1, 0xF2}

def is_var_op(op):
	return op in subop_group_ops or _is_subop_pseudo(op) or op in var_ops

ops = {
0x00: ('b', 'push_byte(%#x)', None), # 读 8 位有符号立即数并压栈。
0x01: ('<h', 'push_word(%#x)', None), # 读 16 位有符号立即数并压栈。
0x02: ('<i', 'push_dword(%#x)', None), # 读 32 位立即数并压栈。
0x03: ('', 'pushx(%#x)', None), # 扩展立即数压栈。mode>=0 时读取 mode+1 个 sleb128 逐个压栈；mode<0 且低 2 位为 0/1/2/3 时分别读 i8/i16/i32/i64 标量压栈。

0x04: ('<H', 'push_base_offset(%#x)', None), # 压入 (frame_pointer - u16) | DATA_TAG 局部数据地址。
0x05: ('<h', 'push_string("%s")', get_string), # 压入 (cur_ip + rel16)
0x06: ('<h', 'push_offset(L%05x)', get_offset), # 压入代码位置/IP 引用。

0x08: ('b', 'load(%#x)', None), # 取目标地址，按宽度 0/1/2 -> i8/i16/i32 读取并压栈。
0x09: ('b', 'move(%#x)', None), # 弹栈值，取目标地址，按宽度 0/1/2 -> 8/16/32 位写入。
0x0A: ('b', 'store_ptr_typed(%#x)', None), # 从栈取目标指针与写入值，再读 1 字节宽度标记，按 0/1/2 -> 8/16/32 位写入目标指针。
0x0B: ('', 'store_inline("%s")', None), # 读取 u8 长度及随后的原始字节，复制到栈给定目标；载荷属于本条指令。
0x0C: ('BB', 'copystack(%#x, %#x)', None), # 弹出 N 个 32 位值，按元素宽度 1/2/4 字节逆序写入连续目标内存。
0x0D: ('', 'store_typed_imm(%#x, %d)', None), # 读取 typed-sleb128，把立即数按 type_id 对应宽度写到目标地址。
0x0E: ('', 'store_rel_typed_imm(%#x, %#x, %d)', None), # 构造帧指针相对局部引用，按 typed 宽度写入立即数。

0x10: ('', 'load_frame_pointer()', None), # 压入当前局部数据帧指针 ctx+0xE8。
0x11: ('', 'store_frame_pointer()', None), # 弹出并校验、设置局部数据帧指针，不改变 IP。
0x12: ('', 'add_frame_imm(%d)', None), # 局部数据帧指针加 sleb128，不改变 IP。
0x13: ('<h', 'jump_rel16(L%05x)', get_offset), # 当前 IP 加 rel16，用作无条件跳转。
0x0F: ('<H', 'store_base_typed(%#x)', None), # 弹栈值，构造帧指针相对局部引用，按编码高 2 位宽度写入。

0x14: ('', 'jmp()', None), # 弹栈得到目标 IP，校验后无条件跳转。
0x15: ('', 'jc(%#x)', None), # 按 flag & 7 选谓词；flag & 8 决定目标来自 rel16 还是栈；满足条件则跳转。
0x16: ('', 'call()', None), # 保存返回点后跳转到目标 IP。
0x17: ('', 'ret()', None), # 从调用/控制流记录恢复 IP。
0x18: ('<I', 'load_typed_ptr(%#x)', None), # 解码带宽度的编码地址，按 8/16/32 位读取并压栈。
0x19: ('<H', 'load_base_typed(%#x)', None), # 构造帧指针相对局部引用，按编码宽度读取并压栈。
0x1A: ('', 'load_base_typed_add(%#x, %d)', None), # 读取帧指针相对局部引用 值，加 sleb128 后压栈。
0x1B: ('', 'load_base_scaled_add(%#x, %d)', None), # 弹出基值，读取帧指针相对 typed 局部引用 的值，计算 base + scale * value 后压栈。
0x1C: ('<Hb', 'shift_base_typed(%#x, %d)', None), # 读取 code-ref 值，按 i8 位移量左移或算术右移后压栈。
0x1D: ('<H', 'mul_base_typed(%#x)', None), # pop() * deref(code_ref_typed)。
0x1E: ('<H', 'div_base_typed(%#x)', None), # pop() / deref(code_ref_typed)，除零返回 0x80000000。
0x1F: ('', 'load_ptr_offset_typed(%d)', None), # 解码 typed 偏移并弹出基址，解析 base + offset 为 VM 指针，再按 typed 宽度读取并压栈。

0x20: ('', 'add()', None), # 两个栈值相加。
0x21: ('', 'sub()', None), # 后弹值减先弹值。
0x22: ('', 'mul()', None), # 整数乘法。
0x23: ('', 'div()', None), # 整数除法，除零返回 0x80000000。
0x24: ('', 'mod()', None), # 整数取模，除零返回 0x80000000。
0x25: ('', 'and()', None), # 位与。
0x26: ('', 'or()', None), # 位或。
0x27: ('', 'xor()', None), # 位异或。
0x28: ('', 'not()', None), # 按位非。
0x29: ('', 'shl()', None), # 逻辑左移。
0x2A: ('', 'shr()', None), # 逻辑右移。
0x2B: ('', 'sar()', None), # 算术右移。
0x2C: ('', 'add_imm(%d)', None), # pop() + sleb128。
0x2D: ('', 'mul_imm(%d)', None), # pop() * sleb128。
0x2E: ('', 'mad_imm(%d)', None), # pop() + imm * pop()。
0x2F: ('', 'div_imm(%d)', None), # pop() / imm。

0x30: ('', 'eq()', None), # 相等比较。
0x31: ('', 'neq()', None), # 不等比较。
0x32: ('', 'leq()', None), # 小于等于。
0x33: ('', 'geq()', None), # 大于等于。
0x34: ('', 'lt()', None), # 小于。
0x35: ('', 'gt()', None), # 大于。
0x36: ('', 'cmp_imm(%#x, %d)', None), # 对栈值与立即数执行 flag 指定的比较/位逻辑/布尔逻辑，结果压栈。
0x37: ('', 'cmp_imm_jc(%#x, L%05x, %d)', None), # 对栈值与立即数执行 flag 指定逻辑，按跳转判定规则跳转。

0x38: ('', 'bool_and()', None), # 逻辑与。
0x39: ('', 'bool_or()', None), # 逻辑或。
0x3A: ('', 'bool_zero()', None), # 判零。
0x3B: ('', 'jcmp(%#x, L%05x)', None), # 对两个栈值执行 flag 指定逻辑，按跳转判定规则跳转。
0x3C: ('b', 'shift_imm(%d)', None), # 立即数非负时左移，负值时算术右移 abs(n)。
0x3E: ('B', 'cmp_store(%#x)', None), # 对两个栈值执行 flag 指定算术/比较/位逻辑，结果按 flag>>6 宽度写回目标地址。
0x3F: ('', 'cmp_store_imm(%#x, %d)', None), # 对栈值与立即数执行 flag 指定算术/比较/位逻辑，结果按 flag>>6 宽度写回目标地址。

0x40: ('', 'ternary()', None), # 弹出三项；条件非零取真值，否则取假值。

0x42: ('', 'muldiv()', None), # 计算 a * b / c，除零返回 0x80000000。
0x43: ('', 'atan2()', None), # 弹出两个整数分量，调用双参数角度 helper，压回对应角度编码。
0x44: ('', 'veclen3()', None), # 弹出三个整数分量，计算 sqrt(x*x + y*y + z*z) 后截断为整数压栈。
0x45: ('', 'rotate3d()', None), # 输入三个角度、源 vec3 和目标 vec3，按三轴旋转并把结果四舍五入写入目标。
0x46: ('', 'lerp()', None), # 用 num/den 得到 clamp 到 [0,1] 的比例，在两个值间插值。
0x47: ('', 'lerp_q16()', None), # 第一个参数为 Q16.16 比例，clamp 到 [0,1] 后插值。

0x48: ('', 'sin()', None), # 输入角度编码，输出 Q16.16 正弦。
0x49: ('', 'cos()', None), # 输入角度编码，输出 Q16.16 余弦。
0x50: ('', 'add64()', None), # *dst = *a + *b，操作数为 64 位地址对象。
0x51: ('', 'sub64()', None), # *dst = *a - *b。
0x52: ('', 'mul64()', None), # *dst = *a * *b。
0x53: ('', 'div64()', None), # *dst = *a / *b，除零返回 0x8000000000000000。
0x54: ('', 'mod64()', None), # *dst = *a % *b。
0x55: ('', 'pow_q16()', None), # 两个 Q16.16 值做 powf，结果转回 Q16.16。
0x56: ('', 'mul64_shift16()', None), # (*a * *b) >> 16。
0x57: ('', 'div_q16()', None), # 定点除法。
0x58: ('', 'vec4_add()', None), # 四个 32 位分量逐元素相加。
0x59: ('', 'vec4_sub()', None), # 四个 32 位分量逐元素相减。
0x5A: ('', 'vec4_mul_q16()', None), # 逐分量计算 (a[i] * b[i]) >> 16。
0x5B: ('', 'vec4_div_q16()', None), # 逐分量定点除法，分母为 0 的分量写 0。
0x5D: ('', 'normalizevec()', None), # 向量归一化，长度非法时输出零向量。
0x5E: ('', 'scalevec4_q16()', None), # 标量 Q16.16 缩放 vec4 每个分量。
0x5F: ('', 'divvec4scalar_q16()', None), # vec4 每分量除以同一 Q16.16 标量，标量为 0 时整向量清零。

0x60: ('', 'memcpy()', None), # memcpy(dst, src, len)。
0x61: ('', 'memclr()', None), # memset(ptr, 0, len)。
0x62: ('', 'memset()', None), # memset(ptr, byte, len)。
0x63: ('', 'memcmp()', None), # 比较两块内存是否完全相等，压回布尔值。
0x64: ('', 'memcpy_repeat()', None), # 取 src、count、len、dst；把同一源块按 len 字节连续复制到 count 个目标块。
0x65: ('', 'memfind_block()', None), # 在 count 个连续 len 字节块中查找与样本块相等的首个块，返回索引或 -1。

0x66: ('', 'strstr()', None), # 返回子串首次出现位置，找不到返回 -1。
0x67: ('', 'strreplace()', None), # 四参字符串替换：在源字符串中搜索模式串，把每次匹配替换为替换串，写入目标缓冲，返回替换次数。
0x68: ('', 'strlen()', None), # 返回 C 字符串长度。
0x69: ('', 'streq()', None), # 先转宽字符后逐 wchar 比较，返回相等布尔值。
0x6A: ('', 'strcpy()', None), # strcpy(dst, src)。
0x6B: ('', 'concat()', None), # 复制左串并拼接右串。
0x6C: ('', 'getchar()', None), # 解码单字符/多字节字符信息，压回主结果、两个附加值和一个分类值。
0x6D: ('', 'strlower_inplace()', None), # 取字符串指针并原地执行小写/规范化转换。
0x6E: ('', 'strquote()', None), # 取长度、源字符串、目标缓冲，把源字符串按指定长度转义/加引号写入目标。
0x6F: ('', 'sprintf()', None), # 格式串驱动的字符串格式化写入。
0x70: ('', 'malloc()', None), # 分配 VM 管理内存，返回 0x30000000 + offset 编码引用。
0x71: ('', 'free()', None), # 释放 0x30000000 段编码引用对应内存。
0x73: ('', 'pop_discard()', None), # 弹出一个 32 位栈值后返回 0，不产生其他副作用。
0x74: ('', 'watch_mode()', None), # 切换内存写入监视版 handler。
0x75: ('', 'watch_add()', None), # 登记 addr、size、name 内存监视范围，返回是否登记。

0x77: ('', 'getgamestate()', None), # 弹出 key；key 为 0..7/16/17 时读取全局状态结构 qword_1401EAD20 中对应 32 位状态值，其他 key 返回 -1。
0x78: ('', 'confirm()', None), # 显示确认框，压回是否为 IDYES(6)。
0x79: ('', 'message()', None), # 显示消息框，压回窗口返回码。
0x7A: ('', 'assert_number()', None), # 弹出整数，格式化为 Number : %d ( $%.8x )，构造诊断文本并显示对话框；对话框返回 1 时返回 0，否则返回 6。
0x7B: ('', 'dumpmem()', None), # 输入地址、长度、标题等，生成十六进制/可打印字符双栏内存转储并显示。
0x7C: ('', 'listselectdlg()', None), # 列表选择对话框：wrapper 取标题/提示字符串与候选列表上下文，helper 构造换行分隔候选文本后调用 UI 对话框 helper，返回选择结果。

0x7D: ('', 'drawhexdump()', None), # 绘制/显示 hexdump 类视图：弹出 6 个整数和 1 个缓冲指针，校验显示尺寸 1..0x400，底层按每 16 字节生成 %.4X : %.2X ... 行并绘制。

0x7E: ('', 'setclipboardtext()', None), # 设置剪贴板文本：把输入多字节字符串转为宽字符串，分配 CF_UNICODETEXT 全局内存，OpenClipboard/EmptyClipboard/SetClipboardData/CloseClipboard，成功返回 1。
0x7F: ('B', 'sys0(%#04x)', None), # 读取 sub-op，从 0x7F 组函数表跳转执行。
0x80: ('B', 'sys1(%#04x)', None), # 读取 sub-op，从 0x80 组函数表跳转执行。
0x81: ('B', 'sys2(%#04x)', None), # 读取 sub-op，从 0x81 组函数表跳转执行。

0x90: ('B', 'grp1(%#04x)', None), # 读取 sub-op，从 0x90 组函数表跳转执行。
0x91: ('B', 'grp2(%#04x)', None), # 读取 sub-op，从 0x91 组函数表跳转执行。
0x92: ('B', 'grp3(%#04x)', None), # 读取 sub-op，从 0x92 组函数表跳转执行。

0xA0: ('B', 'snd1(%#04x)', None), # 读取 sub-op，从音频组函数表跳转执行。

0xB0: ('B', 'usr1(%#04x)', None), # 读取 sub-op，从用户扩展组 1 函数表跳转执行。

0xC0: ('B', 'usr2(%#04x)', None), # 读取 sub-op，从用户扩展组 2 函数表跳转执行。
0xD0: ('B', 'usr3(%#04x)', None), # 读取 sub-op，从用户扩展组 3 函数表跳转执行。
0xD4: ('<H', 'load_code_ref32(%#x)', None), # 读取“帧指针回退局部引用”定位到的 32 位值并压栈。
0xD5: ('', 'load_code_ref_add_imm(%#x, %d)', None), # 读取 code-ref 32 位值，加 sleb128 立即数后压栈。
0xD6: ('', 'load_code_ref_indexed_typed(%#x, %#x, %d)', None), # 读 code-ref 基值，加 typed 偏移定位，按 typed 宽度读值压栈。
0xD7: ('', 'store_code_ref_indexed_typed(%#x, %#x, %#x, %d)', None), # 读 code-ref 基值，加第二偏移定位，按 typed 宽度写入值。
0xD8: ('', 'load_code_ref_scaled_add(%#x, %d)', None), # 计算 base + imm * (*code_ref32) 并压栈。
0xDA: ('', 'cmp_code_ref_imm(%#x, %#x, %d)', None), # 对 code-ref 32 位值与立即数执行 flag 谓词/逻辑，结果压栈。
0xDB: ('', 'cmp_code_ref_indexed_imm(%#x, %#x, %#x, %d, %d)', None), # 对 code-ref 索引 typed 值与立即数执行 flag 谓词/逻辑，结果压栈。
0xDC: ('', 'jc_cmp_code_ref_imm(%#x, L%05x, %#x, %d)', None), # 对 code-ref 32 位值与立即数执行 flag 逻辑，按判定规则跳转。
0xDD: ('', 'jc_cmp_code_ref_indexed_imm(%#x, L%05x, %#x, %#x, %d, %d)', None), # 对 code-ref 索引 typed 值与立即数执行 flag 逻辑，按判定规则跳转。
0xDE: ('', 'jc_test_code_ref(L%05x, %#x)', None), # 对 code-ref 32 位值做零/非零测试并跳转。
0xDF: ('', 'jc_test_code_ref_indexed(L%05x, %#x, %#x, %d)', None), # 对 code-ref 索引 typed 值做零/非零测试并跳转。
0xE0: ('B', 'usr4(%#04x)', None), # 读取 sub-op，从用户扩展组 4 函数表跳转执行。
0xE2: ('', 'store_multi("%s")', None), # 读取多个帧指针相对局部引用 描述符；每个描述符按自身宽度写入一个弹栈值。
0xE3: ('', 'load_multi("%s")', None), # 读取多个帧指针相对局部引用 描述符；依次按各自宽度取值并逐个压栈。
0xE4: ('', 'load_rel_add(%#x, %#x, %d)', None), # 读帧指针相对局部引用 值，加 typed 偏移后按 typed 宽度取值压栈。
0xE5: ('', 'load_indexed_typed(%#x, %#x, %d)', None), # 读取 code-ref 基值作为索引，计算 base + scale * index_value，按 typed 宽度读取并压栈。
0xE6: ('', 'store_rel_add_value(%#x, %#x, %d)', None), # 取目标指针，读取帧指针相对局部引用 值，加 typed 偏移后按 typed 宽度写入目标指针。
0xE7: ('', 'store_indexed_value(%#x, %#x, %d)', None), # 取目标指针，读取 code-ref 基值作为索引，计算 base + scale * index_value 后按 typed 宽度写入目标指针。
0xE8: ('<H', 'select_store(%#x)', None), # 读两个候选值与条件，按条件选择后按 code-ref 编码宽度写入当前 IP 相对目标。
0xE9: ('<HH', 'store_indirect(%#x, %#x)', None), # 读 code-ref 得到基址，加第二个 16 位偏移得到目标，按第一描述符宽度写入弹栈值。
0xEA: ('', 'store_indirect_imm(%#x, %#x, %d)', None), # 读 code-ref 得到基址，加第二个 16 位偏移得到目标，把 sleb128 立即数按第一描述符宽度写入。
0xEC: ('', 'memcpy_stack(%d)', None), # len 由 sleb128 给出，从栈取得 src 与 dst，执行 memcpy(dst, src, len)。
0xED: ('', 'memcpy_rel_stack(%#x, %d)', None), # 从帧指针相对局部引用 解出的地址复制 len 字节到目标缓冲。
0xEE: ('<h', 'call_base_rel(L%05x)', get_offset), # 以 prev_ip + rel16 为目标调用，并推进调用前 IP。
0xEF: ('<I', 'call_ptr(%#x)', None), # 解码指针并按其宽度读出目标 IP 后调用。
0xF0: ('', 'store_ptr_add(%#x, %#x, %d)', None), # 解码一个绝对/编码目标地址，读取帧指针相对局部引用 32 位值，加 sleb128 立即数后按 code-ref 宽度写入目标。
0xF1: ('', 'store_rel_add32(%#x, %#x, %d)', None), # 以帧指针相对局部引用 作为写入目标，读取另一 code-ref 32 位值，加 sleb128 立即数后按宽度写入。
0xF2: ('', 'store_indirect_add32(%#x, %#x, %#x, %d)', None), # 先由一级 code-ref 得到基址，再加二级偏移得到写入目标；读取另一 code-ref 32 位值，加 sleb128 后按宽度写入。
0xF4: ('<HIHh', 'store_affine_imm(%#x, %#x, %#x, %d)', None), # 目标为帧指针相对局部引用；写入值为 imm + scale * deref(code_ref_typed)，按 code-ref 宽度写入。
0xF5: ('<HHhHh', 'store_affine_rel(%#x, %#x, %d, %#x, %d)', None), # 目标为帧指针相对局部引用；写入值为 base_value + bias + scale * deref(code_ref_typed)，按 code-ref 宽度写入。
0xF7: ('<HHh', 'load_affine_rel(%#x, %#x, %d)', None), # 取一个帧指针相对局部引用 的基址，再加第二偏移取得被乘值，计算 base + scale * value 并压栈。
0xF8: ('<IHh', 'load_affine_imm(%#x, %#x, %d)', None), # 读取绝对/编码基值与帧指针相对局部引用 值，计算 imm + scale * value 并压栈。
0xF9: ('<HhHh', 'load_affine_stack(%#x, %d, %#x, %d)', None), # 读取两个 code-ref 32 位值，计算 imm + value0 + scale * value1 并压栈。
0xFA: ('<IHh', 'load_ptr_indexed(%#x, %#x, %d)', None), # 弹出/读取编码基址，结合 code-ref 32 位索引值与比例，定位后按宽度读值压栈。
0xFB: ('<HHh', 'load_indirect_indexed(%#x, %#x, %d)', None), # 取第一 code-ref 得到间接基址，再用第二 code-ref 的 typed 值乘比例形成索引，定位后按第二描述符宽度读值压栈。
0xFF: ('B', 'userscript(%#04x)', None), # 读取 sub-op。F0/F1/F8 走显式 handler；其他 <F0 子码通过 mediation program 表进入外部脚本/中介程序机制。

}


rops = {}


def make_rops():
	for op in ops:
		if op in subop_group_ops:
			continue
		fcn, = re_fcn.match(ops[op][1]).groups()
		rops[fcn] = op
	for op in subop_group_ops:
		group_name = _get_subop_group_name(op)
		for sub in range(0x100):
			name = precise_subop_names.get((op, sub), '%s_%02x' % (group_name, sub))
			rops[name] = _make_subop_pseudo(op, sub)

make_rops()

# Distinct historical operations, not aliases for retired BPD names.
rops.update({'store_keep': 0x09, 'debug_hook': 0x7F, 'jc_legacy': 0x15})
for _version in ('1.58', '1.64', '1.662'):
	for _key, _name in historical_names(get_profile(_version)).items():
		rops[_name] = _make_subop_pseudo(*_key)


def validate_instruction_name(name, op):
	profile = get_profile()
	if _is_subop_pseudo(op):
		group, sub = _split_subop_pseudo(op)
		expected = _get_subop_name(group, sub)
		if name != expected and not re.fullmatch(r'[a-z0-9]+_[0-9a-fA-F]{2}', name):
			raise ValueError('opcode %02X:%02X is named %s in BP engine %s' % (group, sub, expected, profile.name))
		return
	if get_op_entry(op) is None:
		raise ValueError('opcode %02X is not defined by BP engine %s' % (op, profile.name))
	if op == 0x09 and name != ('store_keep' if profile.store_keeps_value else 'move'):
		raise ValueError('store operation does not match BP engine %s' % profile.name)
	if name == 'debug_hook' and profile.compact:
		raise ValueError('debug_hook is only defined before BP engine 1.667')
	if op == 0x15 and name != ('jc' if profile.compact else 'jc_legacy'):
		raise ValueError('conditional branch does not match BP engine %s' % profile.name)
