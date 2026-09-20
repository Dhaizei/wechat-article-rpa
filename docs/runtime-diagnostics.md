# 远端版本与导航诊断

重启控制台后，日志出现 `control_panel_runtime_identity`；点击立即执行或启动定时采集后，出现 `runtime_identity`。两者分别记录进程 PID、实际入口文件、代码目录、工作目录、Python 路径、Git 提交和分支。比较 `entry_file` 与 `code_directory`，确认控制台和采集器来自预期部署目录。`startup_file_sha256` 是启动时磁盘文件的指纹；`relevant_files_modified` 仅检查列出的核心文件，并非整个仓库状态。无 Git 或 Git 超时时对应值为 null，不能解释成“没有改动”。

`browser_tab_probe` 记录标准/自定义标签识别类型、标签数量、结构签名有效性和活动标签是否已绑定。相同账号只在这些状态变化时输出。

`browser_navigation` 的 `direct_navigation_fallback` 阶段记录回退原因列表，可同时出现多项：

- `baseline_tabs_missing` / `current_tabs_missing`：点击前或当前标签证据不完整。
- `origin_tab_unbound` / `active_tab_unconfirmed`：原页面或当前页面尚未绑定标签。
- `not_single_added_tab`：不是唯一新增标签，可能是复用或同页导航，不一定是故障。
- `baseline_tabs_changed`：原标签消失或发生变化。
- `selected_tab_not_added`：当前活动标签不是唯一新增项。
- `document_missing_or_existing`、`window_changed`、`page_role_mismatch`、`account_mismatch`：目标页面身份不符合快速路径条件。

`phase` 区分打开后的归属检查（`track_created`）与采集后的返回（`restore`）。`created_direct` 和 `restore_direct` 才表示实际使用了直接导航。新增日志只用于定位问题，不代表远端兼容问题已经修复。

