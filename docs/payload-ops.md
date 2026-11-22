# Payload 操作清单

动作名称同时充当 payload 操作名，参数原样透传。
标记为 *pending* 的操作在核心层已完全接通（参数校验、返回码、WebUI），但
仍需要在原生 payload 中补上 QQ 侧实现。

- 核心层已支持的动作：**74**
- 尚待 QQ 侧联调的 payload 适配器：**168**

## 待实现操作

| 分类 | 操作 | 参数 |
| --- | --- | --- |
| ark | `get_mini_app_ark` | — |
| ark | `get_share_link` | — |
| extended | `.get_word_slices` | — |
| extended | `ArkSharePeer` | — |
| extended | `_get_friend_dress` | — |
| extended | `_get_model_show` | — |
| extended | `_mark_all_as_read` | — |
| extended | `_set_model_show` | — |
| extended | `add_custom_face` | — |
| extended | `bot_exit` | — |
| extended | `check_url_safely` | — |
| extended | `click_inline_keyboard_button` | — |
| extended | `create_collection` | — |
| extended | `delete_custom_face` | — |
| extended | `delete_essence_msg` | — |
| extended | `fetch_ptt_text` | — |
| extended | `forward_group_single_msg` | — |
| extended | `get_ai_characters` | — |
| extended | `get_ai_record` | — |
| extended | `get_clientkey` | — |
| extended | `get_collection_list` | — |
| extended | `get_essence_msg_list` | — |
| extended | `get_fileset_id` | — |
| extended | `get_fileset_info` | — |
| extended | `get_group_todo_list` | — |
| extended | `get_qzone_comment_list` | — |
| extended | `get_qzone_comment_replies` | — |
| extended | `get_robot_uin_range` | — |
| extended | `list_filesets` | — |
| extended | `mark_group_msg_as_read` | — |
| extended | `modify_custom_face` | — |
| extended | `move_custom_face_to_front` | — |
| extended | `nc_get_rkey` | — |
| extended | `nc_get_user_status` | — |
| extended | `ocr_image` | — |
| extended | `request_decrypt_key` | — |
| extended | `send_ark_share` | — |
| extended | `send_group_ark_share` | — |
| extended | `send_group_forward_msg` | — |
| extended | `send_private_forward_msg` | — |
| extended | `send_tuwen_ark` | — |
| extended | `set_essence_msg` | — |
| extended | `set_friends_category` | — |
| extended | `set_group_reaction` | — |
| extended | `set_qq_avatar` | — |
| extended | `share_group_ex` | — |
| extended | `share_peer` | — |
| extended | `translate_en2zh` | — |
| extended | `upload_forward_msg` | — |
| extended | `upload_foward_msg` | — |
| extended | `upload_images_to_qun_album` | — |
| face | `fetch_custom_face` | — |
| face | `fetch_custom_face_detail` | — |
| face | `fetch_face_entity` | — |
| face | `fetch_super_face_id` | — |
| face | `fetch_sys_faces` | — |
| face | `search_sys_faces` | — |
| flash | `create_flash_task` | — |
| flash | `send_flash_msg` | — |
| friend | `forward_friend_single_msg` | — |
| friend | `get_doubt_friends_add_request` | — |
| friend | `get_emoji_likes` | — |
| friend | `get_profile_like` | — |
| friend | `set_doubt_friends_add_request` | — |
| friend | `set_qq_profile` | — |
| friend | `set_self_longnick` | — |
| group | `_del_group_notice` | — |
| group | `_get_group_notice` | — |
| group | `_send_group_notice` | — |
| group | `cancel_group_todo` | — |
| group | `complete_group_todo` | — |
| group | `delete_group_folder` | — |
| group | `get_group_detail_info` | — |
| group | `get_group_ignore_add_request` | — |
| group | `get_group_ignored_notifies` | — |
| group | `get_group_info_ex` | — |
| group | `get_group_share_link` | — |
| group | `get_group_signed_list` | — |
| group | `get_group_system_msg` | — |
| group | `send_group_ai_record` | — |
| group | `set_group_new_member_history_visibility` | — |
| group | `set_group_portrait` | — |
| group | `set_group_sign` | — |
| group | `set_group_todo` | — |
| group_admin | `add_group_tag` | — |
| group_admin | `delete_group_tag` | — |
| group_admin | `get_group_admin_permissions` | — |
| group_admin | `get_group_admin_settings` | — |
| group_admin | `get_group_tags` | — |
| group_admin | `set_group_add_option` | — |
| group_admin | `set_group_admin_permissions` | — |
| group_admin | `set_group_admin_receive_verify` | — |
| group_admin | `set_group_anonymous` | — |
| group_admin | `set_group_anonymous_ban` | — |
| group_admin | `set_group_kick_members` | — |
| group_admin | `set_group_member_invite_policy` | — |
| group_admin | `set_group_member_permissions` | — |
| group_admin | `set_group_robot_add_option` | — |
| group_admin | `set_group_search` | — |
| group_album | `cancel_group_album_media_like` | — |
| group_album | `del_group_album_media` | — |
| group_album | `do_group_album_comment` | — |
| group_album | `get_group_album_list` | — |
| group_album | `get_group_album_media_list` | — |
| group_album | `get_qun_album_list` | — |
| group_album | `set_group_album_media_like` | — |
| group_album | `upload_image_to_qun_album` | — |
| group_album | `upload_video_to_qun_album` | — |
| group_file | `cancel_online_file` | — |
| group_file | `delete_group_file` | — |
| group_file | `delete_group_file_folder` | — |
| group_file | `download_file` | — |
| group_file | `download_fileset` | — |
| group_file | `get_group_file_system_info` | — |
| group_file | `get_online_file_msg` | — |
| group_file | `move_group_file` | — |
| group_file | `receive_online_file` | — |
| group_file | `refuse_online_file` | — |
| group_file | `rename_group_file` | — |
| group_file | `rename_group_file_folder` | — |
| group_file | `send_online_file` | — |
| group_file | `send_online_folder` | — |
| group_file | `trans_group_file` | — |
| group_file | `upload_group_file` | — |
| group_file | `upload_private_file` | — |
| guild | `get_guild_list` | — |
| guild | `get_guild_service_profile` | — |
| interaction | `set_diy_online_status` | — |
| interaction | `set_online_status` | — |
| message | `mark_private_msg_as_read` | — |
| message | `send_custom_face` | — |
| message | `send_forward_msg` | — |
| packet | `nc_get_packet_status` | — |
| qzone | `comment_qzone` | — |
| qzone | `comment_qzone_public_feed` | — |
| qzone | `delete_qzone_msg` | — |
| qzone | `delete_qzone_public_comment` | — |
| qzone | `delete_qzone_public_reply` | — |
| qzone | `forward_qzone_public_feed` | — |
| qzone | `get_qzone_feed_detail` | — |
| qzone | `get_qzone_feeds` | — |
| qzone | `get_qzone_home_feeds` | — |
| qzone | `get_qzone_msg_detail` | — |
| qzone | `get_qzone_msg_list` | — |
| qzone | `get_qzone_profile` | — |
| qzone | `get_qzone_public_capabilities` | — |
| qzone | `get_qzone_public_comments` | — |
| qzone | `get_qzone_public_feed_detail` | — |
| qzone | `get_qzone_public_feeds` | — |
| qzone | `get_qzone_public_replies` | — |
| qzone | `get_qzone_share_ark` | — |
| qzone | `like_qzone` | — |
| qzone | `reply_qzone_public_feed` | — |
| qzone | `send_qzone_msg` | — |
| qzone | `set_qzone_ban` | — |
| qzone | `set_qzone_msg_right` | — |
| qzone | `set_qzone_profile` | — |
| qzone | `set_qzone_public_comment_like` | — |
| qzone | `set_qzone_public_follow` | — |
| qzone | `set_qzone_public_like` | — |
| qzone | `set_qzone_public_upvote` | — |
| qzone | `unlike_qzone` | — |
| stream | `clean_stream_temp_file` | — |
| stream | `download_file_image_stream` | — |
| stream | `download_file_record_stream` | — |
| stream | `download_file_stream` | — |
| stream | `upload_file_stream` | — |
| system | `get_rkey_server` | — |

## 核心层已支持的动作（无需 payload 改动）

`can_send_image`, `can_send_record`, `clean_cache`, `create_group_file_folder`, `delete_flash_file`, `delete_friend`, `delete_msg`, `fetch_emoji_like`, `friend_poke`, `get_cookies`, `get_credentials`, `get_csrf_token`, `get_file`, `get_flash_file_list`, `get_flash_file_url`, `get_forward_msg`, `get_friend_list`, `get_friend_msg_history`, `get_friend_relation_list`, `get_friend_status_list`, `get_friends_with_category`, `get_group_admin_list`, `get_group_at_all_remain`, `get_group_file_url`, `get_group_files_by_folder`, `get_group_honor_info`, `get_group_info`, `get_group_list`, `get_group_member_count`, `get_group_member_info`, `get_group_member_list`, `get_group_msg_history`, `get_group_root_files`, `get_group_share_ark`, `get_group_shut_list`, `get_image`, `get_login_info`, `get_msg`, `get_msg_emoji_likes`, `get_online_clients`, `get_private_file_url`, `get_recent_contact`, `get_recent_contact_list`, `get_record`, `get_rkey`, `get_status`, `get_stranger_info`, `get_unidirectional_friend_list`, `get_version_info`, `group_poke`, `mark_msg_as_read`, `oidb_call`, `rename_flash_file`, `send_group_msg`, `send_like`, `send_msg`, `send_packet`, `send_poke`, `send_private_msg`, `set_friend_add_request`, `set_friend_remark`, `set_group_add_request`, `set_group_admin`, `set_group_ban`, `set_group_card`, `set_group_kick`, `set_group_leave`, `set_group_name`, `set_group_remark`, `set_group_special_title`, `set_group_whole_ban`, `set_input_status`, `set_msg_emoji_like`, `set_restart`

## 调用约定

1. 核心层依据 `actions.json` 校验参数，并调用 `op(params)`。
2. payload 回应该动作对应的 OneBot 形态 `data`（核心层将其原样转发），
   或返回非零状态码并附带可读的错误信息。
3. 缺失的操作必须显式失败（`status != 0`）；静默返回空结果视为缺陷。
