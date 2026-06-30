"""
Kimodo Blender Bridge — Panels
All N-panel UI panels in the 3D Viewport → Kimodo tab.
"""

import os

import bpy
import json
from bpy.types import Panel


# ---------------------------------------------------------------------------
# Base class — common settings
# ---------------------------------------------------------------------------

class KIMODO_PanelBase:
    bl_space_type  = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category    = 'Kimodo'


# ---------------------------------------------------------------------------
# Panel 1: Connection
# ---------------------------------------------------------------------------

class KIMODO_PT_Connection(KIMODO_PanelBase, Panel):
    bl_label    = "⚙  连接"
    bl_idname   = "KIMODO_PT_Connection"
    bl_order    = 10
    

    def draw(self, context):
        layout = self.layout
        s = context.scene.kimodo

        running = s.is_connected

        # --- Auto-install section ---
        from . import setup_operator as so

        if so.is_installing():
            box = layout.box()
            box.label(text="正在安装 Kimodo…", icon='TIME')
            box.label(text=so.install_status())
            dl_pct = so.download_progress()
            if dl_pct > 0.0:
                label = so.download_label()
                short = (
                    label.replace("Downloading ", "")
                    .replace("下载 ", "")
                    .replace(" (attempt 1/3)", "")
                    .replace("（第 1/3 次）", "")
                )
                box.progress(factor=dl_pct, text=f"{short}  {int(dl_pct * 100)}%")
            layout.separator(factor=0.5)

        elif so.install_failed() or (so.venv_exists() and not so.is_installed()):
            # install_failed()  → failed this session
            # venv_exists() but not is_installed() → partial venv from a
            # previous session (no sentinel file); treat it the same way.
            box = layout.box()
            if so.needs_python():
                box.label(text="需要 Python 3.10–3.12！", icon='ERROR')
                box.separator(factor=0.3)
                box.label(text="系统中没有找到兼容的 Python。")
                box.label(text="点击下方按钮下载 Python 3.12 安装器（Windows 64 位）。")
                box.label(text='运行安装器，并勾选 "Add Python to PATH"。')
                box.label(text="Windows：请以管理员身份运行安装器。")
                box.label(text="点击“重试安装”前请先重启 Blender。", icon='ERROR')
                box.label(text="Windows 用户建议重启电脑（不只是 Blender）", icon='BLANK1')
                box.label(text="以确保新的 PATH 生效。", icon='BLANK1')
                box.separator(factor=0.3)
                box.label(text="选择你的 Python 3.10–3.12 可执行文件：", icon='FILEBROWSER')
                box.label(text="例如 /usr/bin/python3.12 或 C:\\Python312\\python.exe",
                          icon='BLANK1')
                try:
                    prefs = context.preferences.addons[__package__].preferences
                    box.prop(prefs, "system_python_override", text="")
                except Exception:
                    pass
                box.separator(factor=0.3)
                box.label(text="也可以从 python.org 安装 Python：", icon='URL')
                box.operator("kimodo.open_python_download",
                             text="下载 Python 3.12 安装器", icon='URL')
                box.separator(factor=0.3)
            else:
                box.label(text="安装未完成", icon='ERROR')
                if so.install_status():
                    box.label(text=so.install_status(), icon='BLANK1')
                try:
                    prefs = context.preferences.addons[__package__].preferences
                    box.label(text="安装位置（留空 = 默认 ~/.kimodo-venv）：",
                              icon='FILE_FOLDER')
                    box.prop(prefs, "install_location", text="")
                except Exception:
                    pass
            # Reuse the already-chosen location on retry — no folder prompt.
            op = box.operator("kimodo.install_kimodo",
                              text="重试安装", icon='FILE_REFRESH')
            op.prompt_location = False
            box.operator("kimodo.reset_venv",
                         text="重置虚拟环境", icon='TRASH')
            layout.separator(factor=0.5)

        elif not so.is_installed() and not so.is_kimodo_venv(s.python_executable):
            box = layout.box()
            has_gpu = so.has_nvidia_gpu()
            if not has_gpu:
                box.label(text="需要 NVIDIA GPU！", icon='ERROR')
                box.label(text="Kimodo 仅支持 NVIDIA GPU（CUDA）。")
                box.label(text="不支持 AMD 和 Intel GPU。")
                box.separator(factor=0.3)
            else:
                box.label(text="Kimodo 尚未安装", icon='INFO')
            box.label(text="要求：Python 3.10 - 3.12、约 8 GB 磁盘空间、网络")
            box.label(text="点击安装，然后选择 Kimodo venv 的存放文件夹。",
                      icon='FILE_FOLDER')
            row = box.row()
            row.scale_y = 1.3
            row.enabled = has_gpu
            row.operator("kimodo.install_kimodo", icon='IMPORT')
            # Advanced overrides (Python / HF token / explicit install location).
            self._draw_advanced(box, context, s, show_python=False)
            layout.separator(factor=0.5)

        elif not s.python_executable or not os.path.isfile(s.python_executable):
            box = layout.box()
            box.label(text="Kimodo venv 已就绪", icon='CHECKMARK')
            box.operator("kimodo.use_installed_kimodo", icon='CONSOLE')
            self._draw_advanced(box, context, s, show_python=True)
            layout.separator(factor=0.5)
        else:
            # Installed and a Python executable is set — keep overrides one click
            # away under Advanced instead of always on screen.
            box = layout.box()
            self._draw_advanced(box, context, s, show_python=True)
            layout.separator(factor=0.5)

        # --- Model selector ---
        row = layout.row(align=True)
        row.label(text="模型：", icon='ARMATURE_DATA')
        row.prop(s, "kimodo_model", text="")
        row.enabled = not running

        # --- Offload toggle ---
        row = layout.row(align=True)
        row.prop(s, "use_offload", text="启用内存卸载")
        row.enabled = not running

        layout.separator(factor=0.5)

        # --- Start / Stop buttons ---
        if running:
            layout.operator("kimodo.stop_kimodo",
                            text="停止 Kimodo", icon='CANCEL')
        else:
            layout.operator("kimodo.start_kimodo",
                            text="启动 Kimodo", icon='PLAY')

        # --- Status ---
        status_row = layout.row()
        if running:
            status_row.label(text=s.connection_status, icon='CHECKMARK')
        elif s.connection_status in ("Not started", "Stopped", "未启动", "已停止"):
            status_row.label(text=s.connection_status, icon='RADIOBUT_OFF')
        else:
            # Loading or error
            is_err = s.connection_status.startswith(("Failed", "Error", "失败", "错误"))
            status_row.label(
                text=s.connection_status,
                icon='ERROR' if is_err else 'TIME',
            )


        # --- Delete venv (always shown when installed) ---
        if so.is_installed() and not so.is_installing():
            layout.separator(factor=0.5)
            row = layout.row()
            row.alignment = 'RIGHT'
            row.operator("kimodo.reset_venv", text="删除虚拟环境", icon='TRASH', emboss=False)

    def _draw_advanced(self, box, context, s, show_python=False):
        """Collapsible Advanced overrides: Python path, HF token, install location.

        Keeps the default Connection view clean while leaving both override
        capabilities (Python executable + venv install location) one click away.
        """
        expanded = s.show_advanced_connection
        box.prop(
            s, "show_advanced_connection",
            text="高级",
            icon='TRIA_DOWN' if expanded else 'TRIA_RIGHT',
            emboss=False,
        )
        if not expanded:
            return

        col = box.column(align=True)
        if show_python:
            col.label(text="Kimodo Python：", icon='CONSOLE')
            row = col.row(align=True)
            row.prop(s, "python_executable", text="")
            row.enabled = not s.is_connected
            col.label(text="留空则从 PATH / 相邻 venv 自动检测",
                      icon='INFO')
            col.separator(factor=0.5)

        try:
            prefs = context.preferences.addons[__package__].preferences
            col.label(text="HF Token（可选；模型下载卡住时填写）：",
                      icon='LOCKED')
            col.prop(prefs, "hf_token", text="")
            col.label(text="系统 Python 3.10–3.12（覆盖自动检测）：",
                      icon='CONSOLE')
            col.prop(prefs, "system_python_override", text="")
            col.label(text="安装位置（留空 = 默认 ~/.kimodo-venv）：",
                      icon='FILE_FOLDER')
            col.prop(prefs, "install_location", text="")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Panel 2: Motion Segments (timeline bars)
# ---------------------------------------------------------------------------

class KIMODO_PT_Segments(KIMODO_PanelBase, Panel):
    bl_label  = "🏞  动作片段"
    bl_idname = "KIMODO_PT_Segments"
    bl_order  = 15
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        s      = context.scene.kimodo

        # --- Toolbar ---
        row = layout.row(align=True)
        row.operator("kimodo.add_segment",    text="添加",       icon='ADD')
        row.operator("kimodo.remove_segment", text="移除",       icon='REMOVE')
        row.separator()
        row.operator("kimodo.duplicate_segment", text="", icon='DUPLICATE')
        row.operator("kimodo.move_segment_up",   text="", icon='TRIA_UP')
        row.operator("kimodo.move_segment_down", text="", icon='TRIA_DOWN')

        layout.separator(factor=0.5)


        row = layout.row(align=True)
        row.label(text="模型：")
        row.prop(s, "model_type", expand=True)

        # --- FPS warning ---
        scene_fps = context.scene.render.fps / context.scene.render.fps_base
        if abs(scene_fps - 30.0) > 0.01:
            fps_box = layout.box()
            fps_box.alert = True
            fps_box.label(text=f"当前场景为 {scene_fps:.4g} FPS，Kimodo 需要 30 FPS", icon='ERROR')
            fps_box.operator("kimodo.set_to_30fps", text="设为 30 FPS", icon='RECOVER_LAST')

        # --- Segment list ---
        if not s.motion_segments:
            col = layout.column()
            col.label(text="还没有动作片段。", icon='INFO')
            col.label(text="点击“添加”创建一个片段。")
            return

        for i, seg in enumerate(s.motion_segments):
            is_active     = (i == s.segment_index)
            is_generating = (i == s.generating_segment_index)

            box = layout.box()
            #box.alert = is_active   # blue highlight on active segment

            # --- Header row: [enabled] [color] [label] [select] [generate] ---
            header = box.row(align=True)

            header.prop(seg, "enabled", text="", emboss=False,
                        icon='CHECKBOX_HLT' if seg.enabled else 'CHECKBOX_DEHLT')

            # Segment title: prompt preview + frame range
            title = f"  {seg.prompt[:28]}{'…' if len(seg.prompt) > 28 else ''}"
            header.label(text=title)

            frames_label = f"{seg.start_frame}–{seg.end_frame}"
            header.label(text=frames_label)

            if is_generating:
                header.label(text="", icon='TIME')
            elif seg.generated:
                header.label(text="", icon='CHECKMARK')

            # Select button — sets this as the active segment
            #select_icon = 'RADIOBUT_ON' if is_active else 'RADIOBUT_OFF'
            #op_sel = header.operator("kimodo.select_segment", text="", icon=select_icon, emboss=False)
            #op_sel.index = i

            # Remove button
            op_rem = header.operator("kimodo.remove_segment_by_index", text="", icon='X', emboss=False)
            op_rem.index = i

            # --- Body: always visible ---
            col = box.column(align=True)
            col.prop(seg, "prompt", text="")

            row2 = col.row(align=True)
            start_sub = row2.row(align=True)
            start_sub.enabled = (i == 0)
            start_sub.prop(seg, "start_frame", text="起始")
            row2.prop(seg, "end_frame",   text="结束")

            fps = context.scene.render.fps / context.scene.render.fps_base
            dur = (seg.end_frame - seg.start_frame + 1) / fps
            
            row3 = col.row(align=True)
            row3.label(text=f"  {dur:.1f}秒 · {seg.end_frame - seg.start_frame + 1} 帧",
                      icon='TIME')
            row3.prop(seg, "seed", text="种子")



        layout.separator()

        # --- Generate buttons ---
        layout.prop(s, "bvh_standard_tpose", icon='ARMATURE_DATA')

        # Reuse armature eyedropper + auto-pick button
        reuse_row = layout.row(align=True)
        reuse_row.prop(s, "reuse_armature", text="复用", icon='ARMATURE_DATA')
        reuse_row.operator("kimodo.pick_latest_armature", text="", icon='SORTTIME')

        # Transition frames control
        trans_row = layout.row(align=True)
        trans_row.enabled = s.is_connected and not s.is_generating
        trans_row.label(text="过渡帧数：")
        trans_row.prop(s, "num_transition_frames", text="")

        gen_row = layout.row(align=True)
        gen_row.enabled = s.is_connected and not s.is_generating
        #gen_row.operator("kimodo.generate_segment",      text="Generate Selected", icon='PLAY')
        #use tpose button below

        gen_row.scale_y = 2
        gen_row.operator("kimodo.generate_all_segments", text="生成动作", icon='PLAY')

        if s.is_generating:
            box2 = layout.box()
            box2.label(text=s.generation_progress or "处理中…", icon='TIME')
            box2.operator("kimodo.cancel_generation", text="取消", icon='X')

        # --- Generation History ---
        layout.separator()
        hist_header = layout.row(align=True)
        hist_header.prop(
            s, "history_expanded",
            icon='DISCLOSURE_TRI_DOWN' if s.history_expanded else 'DISCLOSURE_TRI_RIGHT',
            icon_only=True, emboss=False,
        )
        hist_header.label(
            text=f"历史记录（{len(s.generation_history)}）", icon='TIME'
        )
        if s.history_expanded:
            if not s.generation_history:
                layout.label(text="还没有生成记录。", icon='INFO')
            else:
                layout.template_list(
                    "KIMODO_UL_History", "",
                    s, "generation_history",
                    s, "history_index",
                    rows=min(len(s.generation_history), 5),
                )
                if 0 <= s.history_index < len(s.generation_history):
                    entry = s.generation_history[s.history_index]
                    detail = layout.box()
                    detail.label(text=entry.prompt, icon='TEXT')
                    detail.label(
                        text=f"种子：{entry.seed}  |  {entry.duration:.1f}秒  |  {entry.timestamp}"
                    )
                    op_row = detail.row(align=True)
                    reimport_op = op_row.operator(
                        "kimodo.reimport_from_history",
                        text="重新导入 BVH", icon='IMPORT',
                    )
                    reimport_op.index = s.history_index
            layout.operator("kimodo.clear_history", text="清空历史", icon='TRASH')



# ---------------------------------------------------------------------------
# Panel 3: Generate (single prompt, kept for quick use)
# ---------------------------------------------------------------------------

class KIMODO_PT_Generate(KIMODO_PanelBase, Panel):
    bl_label   = "🎬  快速生成"
    bl_idname  = "KIMODO_PT_Generate"
    bl_order   = 14
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        s = context.scene.kimodo

        # Disable panel while generating
        layout.enabled = not s.is_generating

        # Model selector
        row = layout.row(align=True)
        row.label(text="模型：")
        row.prop(s, "model_type", expand=True)

        # Prompt
        layout.label(text="提示词：")
        layout.prop(s, "prompt", text="")

        # Duration + Seed
        split = layout.split(factor=0.6)
        split.prop(s, "duration", slider=True)
        split.prop(s, "seed")

        # Output format
        row = layout.row(align=True)
        row.label(text="输出：")
        row.prop(s, "output_format", expand=True)

        # BVH T-pose option (only show for BVH format)
        if s.output_format == "bvh":
            layout.prop(s, "bvh_standard_tpose", icon='ARMATURE_DATA')

        layout.separator()

        # Reuse armature eyedropper + auto-pick button
        reuse_row = layout.row(align=True)
        reuse_row.prop(s, "reuse_armature", text="复用", icon='ARMATURE_DATA')
        reuse_row.operator("kimodo.pick_latest_armature", text="", icon='SORTTIME')

        layout.separator()

        # Generate / Cancel button
        if s.is_generating:
            col = layout.column()
            col.enabled = True   # re-enable for cancel
            col.operator("kimodo.cancel_generation", text="⏹  取消", icon='X')
            # Progress
            box = layout.box()
            box.label(text=s.generation_progress or "处理中…", icon='TIME')
        else:
            connected_icon = 'PLAY' if s.is_connected else 'UNLINKED'
            row = layout.column()
            
            row.enabled = s.is_connected
            
            scene_fps = context.scene.render.fps / context.scene.render.fps_base
            
            if abs(scene_fps - 30.0) > 0.01:
                fps_box = row.box()
                fps_box.alert = True
                fps_box.label(text=f"当前场景为 {scene_fps:.4g} FPS，Kimodo 需要 30 FPS", icon='ERROR')
                fps_box.operator("kimodo.set_to_30fps", text="设为 30 FPS", icon='RECOVER_LAST')

            row.scale_y = 2
            row.operator("kimodo.generate", text="生成动作", icon=connected_icon)
            if s.generation_progress:
                layout.label(text=s.generation_progress,
                             icon='CHECKMARK' if ("Done" in s.generation_progress or "完成" in s.generation_progress) else 'ERROR')

        # --- Generate N Variations ---
        layout.separator()
        var_row = layout.row(align=True)
        var_row.enabled = s.is_connected and not s.is_generating
        var_row.prop(s, "num_variations", text="变体数量")
        var_row.operator(
            "kimodo.generate_variations",
            text=f"生成 {s.num_variations} 个变体",
            icon='DUPLICATE',
        )

        # --- Generation History ---
        layout.separator()
        hist_header = layout.row(align=True)
        hist_header.prop(
            s, "history_expanded",
            icon='DISCLOSURE_TRI_DOWN' if s.history_expanded else 'DISCLOSURE_TRI_RIGHT',
            icon_only=True, emboss=False,
        )
        hist_header.label(
            text=f"历史记录（{len(s.generation_history)}）", icon='TIME'
        )
        if s.history_expanded:
            if not s.generation_history:
                layout.label(text="还没有生成记录。", icon='INFO')
            else:
                layout.template_list(
                    "KIMODO_UL_History", "",
                    s, "generation_history",
                    s, "history_index",
                    rows=min(len(s.generation_history), 5),
                )
                if 0 <= s.history_index < len(s.generation_history):
                    entry = s.generation_history[s.history_index]
                    detail = layout.box()
                    detail.label(text=entry.prompt, icon='TEXT')
                    detail.label(
                        text=f"种子：{entry.seed}  |  {entry.duration:.1f}秒  |  {entry.timestamp}"
                    )
                    op_row = detail.row(align=True)
                    reimport_op = op_row.operator(
                        "kimodo.reimport_from_history",
                        text="重新导入 BVH", icon='IMPORT',
                    )
                    reimport_op.index = s.history_index
            layout.operator("kimodo.clear_history", text="清空历史", icon='TRASH')

        # Manual import fallback
        layout.separator()
        box = layout.box()
        box.label(text="手动导入", icon='IMPORT')
        row = box.row()
        row.prop(s, "last_bvh_path", text="BVH 路径")
        row.operator("kimodo.import_bvh", text="", icon='FILE_FOLDER').filepath = ""


# ---------------------------------------------------------------------------
# Panel 3: Motion Constraints
# ---------------------------------------------------------------------------

class KIMODO_PT_Constraints(KIMODO_PanelBase, Panel):
    bl_label   = "🎯  动作约束"
    bl_idname  = "KIMODO_PT_Constraints"
    bl_order   = 25
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        s = context.scene.kimodo

        # --- Quick-add buttons ---
        layout.label(text="在当前帧添加约束：", icon='ADD')
        grid = layout.grid_flow(row_major=True, columns=3, even_columns=True, align=True)

        add_types = [
            ('root2d',      "根 XZ",      'EMPTY_ARROWS'),
            ('fullbody',    "全身",       'ARMATURE_DATA'),
            ('left_hand',   "左手",       'VIEW_PAN'),
            ('right_hand',  "右手",       'VIEW_PAN'),
            ('left_foot',   "左脚",       'SNAP_FACE'),
            ('right_foot',  "右脚",       'SNAP_FACE'),
        ]
        for ctype, label, icon in add_types:
            op = grid.operator("kimodo.add_constraint", text=label, icon=icon)
            op.constraint_type = ctype

        # Fullbody tip — shown when Full-Body button is in the grid
        has_fullbody = any(ci.constraint_type == 'fullbody' for ci in s.motion_constraints)
        if not has_fullbody:
            tip = layout.box()
            tip.label(text="全身约束提示：", icon='INFO')
            tip.label(text="请先选择一个骨架，再点击“全身”。")
            tip.label(text="也可以先生成一次动作，插件会复制源骨架。")

        # --- Curve path waypoint sampler ---
        layout.separator()
        path_box = layout.box()
        path_box.label(text="将曲线采样为路径点", icon='CURVE_DATA')
        split = path_box.split(factor=0.8, align=True)
        split.prop(s, "path_curve", text="曲线")
        split.operator("kimodo.draw_freehand_curve", text="绘制", icon='GREASEPENCIL')
        if s.path_curve:
            prow = path_box.row(align=True)
            prow.prop(s, "path_waypoints", text="点数")
            prow.prop(s, "path_start_frame", text="起始帧")
            prow.prop(s, "path_end_frame", text="结束帧")
            path_box.operator(
                "kimodo.sample_curve_as_waypoints",
                text=f"采样 {s.path_waypoints} 个路径点",
                icon='NORMALIZE_FCURVES',
            )

        layout.separator()
        n = len(s.motion_constraints)
        if n:
            layout.label(text=f"{n} 个约束：", icon='SEQUENCE')
        else:
            layout.label(text="没有约束，动作将自由生成", icon='INFO')

        for i, ci in enumerate(s.motion_constraints):
            box = layout.box()
            row = box.row(align=True)

            # Enabled toggle
            row.prop(ci, "enabled", text="", emboss=False,
                     icon='CHECKBOX_HLT' if ci.enabled else 'CHECKBOX_DEHLT')

            # Type icon
            type_icons = {
                'root2d': 'EMPTY_ARROWS', 'fullbody': 'ARMATURE_DATA',
                'left_hand': 'VIEW_PAN',  'right_hand': 'VIEW_PAN',
                'left_foot': 'SNAP_FACE', 'right_foot': 'SNAP_FACE',
            }
            row.label(text="", icon=type_icons.get(ci.constraint_type, 'DOT'))

            # Type selector
            row.prop(ci, "constraint_type", text="")

            # Frame
            row.label(text="F:")
            row.prop(ci, "frame", text="")

            # Go to frame
            op_goto = row.operator("kimodo.goto_constraint_frame", text="", icon='TIME')
            op_goto.frame = ci.frame

            # Select object
            op_sel = row.operator("kimodo.select_constraint_object", text="", icon='RESTRICT_SELECT_OFF')
            op_sel.index = i

            # Remove
            op_rem = row.operator("kimodo.remove_constraint", text="", icon='X')
            op_rem.index = i
            op_rem.delete_object = False

            # --- Object picker row — type-aware ---
            sub = box.row(align=True)

            if ci.constraint_type == 'fullbody':
                # Armature picker with validation warning
                obj = ci.marker_object
                if obj and obj.type != 'ARMATURE':
                    # Wrong type — show error
                    sub.alert = True
                    sub.label(text="⚠ 不是骨架！", icon='ERROR')
                    sub.prop(ci, "marker_object", text="修正 →")
                elif not obj:
                    # Nothing set — show hint
                    sub.alert = True
                    sub.label(text="先在上方选择骨架，然后再次点击“全身”", icon='INFO')
                    sub.prop(ci, "marker_object", text="或设置 →")
                else:
                    # Valid armature — show normally with bone count hint
                    bone_n = len(obj.data.bones)
                    sub.prop(ci, "marker_object", text=f"姿态参考（{bone_n} 根骨骼）")
                    sub.operator(
                        "kimodo.select_constraint_object",
                        text="", icon='EDITMODE_HLT',
                    ).index = i
            else:
                # Regular Empty picker for all other types
                sub.prop(ci, "marker_object", text="标记")

            # root2d heading extras
            if ci.constraint_type == 'root2d':
                sub2 = box.row(align=True)
                sub2.prop(ci, "include_heading", text="朝向")
                if ci.include_heading:
                    sub2.prop(ci, "heading_angle", text="")

        layout.separator()

        # --- Settings ---
        box = layout.box()
        box.label(text="设置", icon='PREFERENCES')
        row = box.row(align=True)
        row.prop(s, "kimodo_fps")
        row.prop(s, "auto_canonicalize", toggle=True, text="自动原点")

        layout.separator()

        # --- Actions ---
        row = layout.row(align=True)
        row.operator("kimodo.preview_constraints_json", icon='TEXT', text="预览 JSON")
        row.operator("kimodo.clear_constraints",        icon='TRASH', text="全部清空")


# ---------------------------------------------------------------------------
# Panel 4: Retarget
# ---------------------------------------------------------------------------

class KIMODO_PT_Retarget(KIMODO_PanelBase, Panel):
    bl_label   = "🦴  重定向"
    bl_idname  = "KIMODO_PT_Retarget"
    bl_order   = 30
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        s = context.scene.kimodo

        # Armature pickers
        box = layout.box()
        box.label(text="骨架", icon='ARMATURE_DATA')
        box.prop(s, "source_armature", text="源（Kimodo）")
        box.prop(s, "target_armature", text="目标（你的绑定）")
        box.prop(s, "retarget_root_bone", text="根骨骼")

        layout.separator()

        # Bone mapping list
        layout.label(text="骨骼映射：", icon='BONE_DATA')
        layout.label(text="链接开关 = 目标骨骼的“继承旋转”", icon='LINKED')

        if s.source_armature and s.target_armature:
            # Auto-match button
            layout.operator("kimodo.auto_map_bones",
                            text="自动匹配骨骼", icon='SHADERFX')

        row = layout.row()
        row.template_list(
            "KIMODO_UL_BoneMappings", "",
            s, "bone_mappings",
            s, "bone_mapping_index",
            rows=6,
        )

        col = row.column(align=True)
        col.operator("kimodo.add_bone_mapping",    text="", icon='ADD')
        col.operator("kimodo.remove_bone_mapping", text="", icon='REMOVE')

        layout.separator()

        # Apply / Remove constraints
        row = layout.row(align=True)
        row.operator("kimodo.apply_retargeting",  text="应用约束", icon='CONSTRAINT_BONE')
        row.operator("kimodo.remove_retargeting", text="",                  icon='X')

        layout.separator()

        # Bake section
        box = layout.box()
        box.label(text="烘焙动画", icon='RENDER_ANIMATION')
        row = box.row(align=True)
        row.prop(s, "bake_start_frame", text="起始")
        row.prop(s, "bake_end_frame",   text="结束")
        box.operator("kimodo.bake_retargeting",
                     text="烘焙并移除约束", icon='NLA_PUSHDOWN')

        layout.separator()

        # Presets
        box = layout.box()
        box.label(text="骨骼映射预设", icon='PRESET')
        row = box.row(align=True)
        row.prop(s, "preset_name", text="")
        row.operator("kimodo.save_preset", text="", icon='FILE_TICK')
        row.operator("kimodo.load_preset", text="", icon='IMPORT').preset_name = s.preset_name

        # List saved presets
        try:
            prefs = context.preferences.addons[__package__].preferences
            from . import retarget as rt
            preset_names = rt.list_presets(prefs)
        except Exception:
            preset_names = []

        if preset_names:
            col = box.column(align=True)
            for name in preset_names:
                row2 = col.row(align=True)
                op_load = row2.operator("kimodo.load_preset",   text=name, icon='IMPORT')
                op_load.preset_name = name
                op_del  = row2.operator("kimodo.delete_preset", text="",   icon='TRASH')
                op_del.preset_name = name

        # File export / import
        row = box.row(align=True)
        row.operator("kimodo.export_preset_file", text="导出到文件", icon='EXPORT')
        row.operator("kimodo.import_preset_file", text="从文件导入", icon='IMPORT')


# ---------------------------------------------------------------------------
# Panel 4: Help / About
# ---------------------------------------------------------------------------

class KIMODO_PT_Help(KIMODO_PanelBase, Panel):
    bl_label   = "ℹ  帮助"
    bl_idname  = "KIMODO_PT_Help"
    bl_order   = 90
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        col = layout.column(align=True)
        col.label(text="快速开始：", icon='QUESTION')
        col.separator()
        col.label(text="1. 在“连接”面板点击“自动安装 Kimodo”")
        col.label(text="   （或把 Kimodo Python 指向你自己的 venv）")
        col.label(text="2. 启动 Kimodo")
        col.label(text="   （模型只加载一次，并保持常驻）")
        col.separator()
        col.label(text="3. 输入提示词 → 生成动作")
        col.label(text="4. 在“重定向”面板选择源骨架和目标骨架")
        col.label(text="5. 自动匹配 → 应用约束")
        col.label(text="6. 如果匹配不理想，手动添加控制骨骼")
        col.label(text="7. 点击“应用约束”")
        col.label(text="8. 满意后进行烘焙")
        col.separator()
        col.label(text="文档与源码：", icon='URL')
        col.label(text="github.com/ForceofwiII/Kimodo_Blender_Bridge")


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

_classes = [
    KIMODO_PT_Connection,
    KIMODO_PT_Segments,
    KIMODO_PT_Generate,
    KIMODO_PT_Constraints,
    KIMODO_PT_Retarget,
    KIMODO_PT_Help,
]


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
