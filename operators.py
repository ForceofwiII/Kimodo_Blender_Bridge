"""
Kimodo Blender Bridge — Operators
All bpy.ops.kimodo.* operators.
"""

import bpy
import addon_utils
import os
import math
import json
import threading
import random
import time
from bpy.types import Operator
from bpy.props import StringProperty, BoolProperty, IntProperty

from . import subprocess_client as sc
from . import retarget as rt
from . import constraints as cmod
from . import setup_operator as so


# ---------------------------------------------------------------------------
# BVH importer compatibility (Blender 5.0+)
# ---------------------------------------------------------------------------

def _bvh_operator_registered() -> bool:
    """True only when import_anim.bvh is actually registered.

    Note: ``hasattr(bpy.ops.import_anim, "bvh")`` is useless for this — bpy.ops
    resolves lazily and always returns a wrapper, so it reports True even when
    the operator does not exist. A registered operator, however, gets a real
    type ``bpy.types.IMPORT_ANIM_OT_bvh``, so check for that instead.
    """
    return hasattr(bpy.types, "IMPORT_ANIM_OT_bvh")


def _ensure_bvh_importer() -> bool:
    """Make sure bpy.ops.import_anim.bvh is registered, enabling it if needed.

    Blender 5.0+ ships the legacy BVH importer (io_anim_bvh) disabled by
    default, so the operator is missing on a fresh install and calling it
    raises "could not be found". Enable it on demand. Returns True if the
    operator is available afterwards.
    """
    if _bvh_operator_registered():
        return True

    # Try the plain bundled module name first, then any extension-namespaced
    # variant (e.g. "bl_ext.blender_org.io_anim_bvh") discovered at runtime.
    candidates = ["io_anim_bvh"]
    try:
        candidates += [
            m.__name__ for m in addon_utils.modules()
            if m.__name__ != "io_anim_bvh" and m.__name__.endswith("io_anim_bvh")
        ]
    except Exception:
        pass

    for module_name in candidates:
        try:
            addon_utils.enable(module_name, default_set=True, persistent=True)
        except Exception:
            continue
        if _bvh_operator_registered():
            return True

    return _bvh_operator_registered()


def _import_bvh(**kwargs):
    """Import a BVH file, self-healing a missing/disabled importer add-on.

    Raises RuntimeError with an actionable message if the importer cannot be
    made available, so callers can report it cleanly instead of leaking a
    cryptic operator traceback.
    """
    if not _ensure_bvh_importer():
        raise RuntimeError(
            "Blender 的 BVH 导入器不可用。请在“编辑 > 偏好设置 > 插件”中启用"
            "“Import-Export: BioVision Motion Capture (BVH)”（搜索 BVH）。"
            "Blender 5.0+ 可能需要先从“获取扩展”安装。"
        )
    try:
        return bpy.ops.import_anim.bvh(**kwargs)
    except (AttributeError, RuntimeError) as e:
        # Operator vanished between the check and the call, or import failed.
        raise RuntimeError(f"BVH 导入失败：{e}")


# ---------------------------------------------------------------------------
# Shared async state (thread → modal operator communication)
# ---------------------------------------------------------------------------

_generation_state = {
    "running": False,
    "done": False,
    "success": False,
    "cancelled": False, # user requested cancel; discard the result
    "result": "",       # file path on success, error message on failure
    "progress": "",
}


def _reset_state():
    _generation_state.update(running=False, done=False, success=False,
                              cancelled=False, result="", progress="")


_HISTORY_MAX = 20


def _push_history(s, prompt: str, seed: int, duration: float, bvh_path: str) -> None:
    """Prepend a new entry to generation_history, keeping newest-first, capped at _HISTORY_MAX."""
    import datetime
    entry = s.generation_history.add()
    entry.prompt    = prompt
    entry.seed      = seed
    entry.duration  = duration
    entry.bvh_path  = bvh_path
    entry.timestamp = datetime.datetime.now().isoformat(timespec='seconds')
    # Move the new item (appended at the end) to index 0
    last = len(s.generation_history) - 1
    for i in range(last, 0, -1):
        s.generation_history.move(i, i - 1)
    # Trim to max size
    while len(s.generation_history) > _HISTORY_MAX:
        s.generation_history.remove(len(s.generation_history) - 1)


# ---------------------------------------------------------------------------
# Bridge start/stop state  (thread → modal operator communication)
# ---------------------------------------------------------------------------

_start_state = {
    "running": False,
    "done":    False,
    "success": False,
    "message": "",
}


def _reset_start_state():
    _start_state.update(running=False, done=False, success=False, message="")


# ---------------------------------------------------------------------------
# Connection operators
# ---------------------------------------------------------------------------

class KIMODO_OT_StartKimodo(Operator):
    """Load the Kimodo model in the background and keep it ready for generation"""
    bl_idname = "kimodo.start_kimodo"
    bl_label  = "启动 Kimodo"

    _timer  = None
    _thread = None

    def _run_start(self, python_exe: str, model_name: str, use_offload: bool):
        def progress(msg):
            _start_state["message"] = msg

        success, msg = sc.start(python_exe, model_name, use_offload=use_offload, progress_callback=progress)
        _start_state["success"] = success
        _start_state["message"] = msg
        _start_state["done"]    = True
        _start_state["running"] = False

    def invoke(self, context, event):
        s = context.scene.kimodo

        if sc.is_running():
            self.report({'INFO'}, "Kimodo 已经在运行。")
            return {'CANCELLED'}

        _reset_start_state()
        _start_state["running"] = True
        s.is_connected      = False
        s.connection_status = "正在启动…"

        # Resolve the Python hint on the main thread. When the scene has no
        # explicit path, fall back to the remembered managed-venv location
        # (addon preference) so a fresh scene still finds the install.
        python_hint = (s.python_executable or "").strip()
        if not python_hint:
            try:
                python_hint = so.managed_python()
            except Exception:
                python_hint = ""

        self._thread = threading.Thread(
            target=self._run_start,
            args=(python_hint, s.kimodo_model, s.use_offload),
            daemon=True,
        )
        self._thread.start()

        wm = context.window_manager
        self._timer = wm.event_timer_add(0.5, window=context.window)
        wm.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    # Allow calling without a click event (e.g. from scripts)
    def execute(self, context):
        return self.invoke(context, None)

    def modal(self, context, event):
        s = context.scene.kimodo

        if event.type != 'TIMER':
            return {'PASS_THROUGH'}

        s.connection_status = _start_state["message"]
        for area in context.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()

        if not _start_state["done"]:
            return {'RUNNING_MODAL'}

        context.window_manager.event_timer_remove(self._timer)

        if _start_state["success"]:
            s.is_connected      = True
            s.connection_status = _start_state["message"]
            self.report({'INFO'}, f"Kimodo 已就绪：{_start_state['message']}")
        else:
            s.is_connected      = False
            s.connection_status = _start_state["message"]
            self.report({'ERROR'}, f"Kimodo 启动失败：{_start_state['message']}")

        return {'FINISHED'}

    def cancel(self, context):
        context.window_manager.event_timer_remove(self._timer)


class KIMODO_OT_StopKimodo(Operator):
    """Shut down the Kimodo bridge process and free GPU memory"""
    bl_idname = "kimodo.stop_kimodo"
    bl_label  = "停止 Kimodo"

    def execute(self, context):
        sc.stop()
        s = context.scene.kimodo
        s.is_connected      = False
        s.connection_status = "已停止"
        self.report({'INFO'}, "Kimodo 桥接进程已停止。")
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Generation operators
# ---------------------------------------------------------------------------

class KIMODO_OT_Generate(Operator):
    """Generate motion with Kimodo. Runs in background thread."""
    bl_idname = "kimodo.generate"
    bl_label = "生成动作"

    _timer = None
    _thread = None

    def _run_generation(self, prompt, duration, seed, fmt, constraints_json=None, bvh_standard_tpose=False):
        """Runs in background thread."""
        def progress_cb(msg):
            _generation_state["progress"] = msg

        success, result = sc.generate_motion(
            prompt=prompt,
            duration=duration,
            seed=seed,
            output_format=fmt,
            constraints_json=constraints_json,
            bvh_standard_tpose=bvh_standard_tpose,
            progress_callback=progress_cb,
        )
        _generation_state["success"] = success
        _generation_state["result"] = result
        _generation_state["done"] = True
        _generation_state["running"] = False

    def invoke(self, context, event):
        s = context.scene.kimodo

        if not sc.is_running():
            self.report({'WARNING'}, "Kimodo 尚未运行，请先点击“启动 Kimodo”。")
            return {'CANCELLED'}
        if s.is_generating:
            self.report({'WARNING'}, "正在生成，请稍候。")
            return {'CANCELLED'}

        # Resolve seed (upper bound must stay within a 32-bit IntProperty)
        seed = s.seed if s.seed >= 0 else random.randint(0, 2**31 - 1)
        self._resolved_seed = seed

        # Launch background thread
        _reset_state()
        _generation_state["running"] = True
        s.is_generating = True
        s.generation_progress = "正在启动…"

        # Build constraints JSON if any are defined
        constraints_json = None
        enabled_constraints = [c for c in s.motion_constraints if c.enabled and c.marker_object]
        if enabled_constraints:
            try:
                constraints_data = cmod.build_constraints_json(
                    s.motion_constraints,
                    context.scene,
                    kimodo_fps=s.kimodo_fps,
                    auto_canonicalize=s.auto_canonicalize,
                )
                constraints_json = json.dumps(constraints_data)
                s.constraint_json_preview = constraints_json
            except Exception as e:
                self.report({'WARNING'}, f"约束构建失败，将不带约束生成：{e}")

        self._thread = threading.Thread(
            target=self._run_generation,
            args=(
                s.prompt,
                s.duration,
                seed,
                s.output_format,
                constraints_json,
                s.bvh_standard_tpose,
            ),
            daemon=True,
        )
        self._thread.start()

        # Add modal timer to poll thread
        wm = context.window_manager
        self._timer = wm.event_timer_add(0.5, window=context.window)
        wm.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        s = context.scene.kimodo

        if event.type != 'TIMER':
            return {'PASS_THROUGH'}

        # Update progress display
        s.generation_progress = _generation_state.get("progress", "")
        # Force N-panel redraw
        for area in context.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()

        if not _generation_state["done"]:
            return {'RUNNING_MODAL'}

        # Generation finished
        context.window_manager.event_timer_remove(self._timer)
        s.is_generating = False

        if _generation_state["cancelled"]:
            s.generation_progress = "已取消"
            self.report({'INFO'}, "生成已取消，结果已丢弃。")
        elif _generation_state["success"]:
            file_path = _generation_state["result"]
            s.last_bvh_path = file_path
            s.generation_progress = "完成 ✓"
            _push_history(s, s.prompt, self._resolved_seed, s.duration, file_path)
            # Auto-import if BVH
            ext = os.path.splitext(file_path)[1].lower()
            if ext == ".bvh":
                result = bpy.ops.kimodo.import_bvh('EXEC_DEFAULT', filepath=file_path)
                if 'FINISHED' in result:
                    self.report({'INFO'}, f"动作已生成并从 {os.path.basename(file_path)} 导入。")
                else:
                    self.report({'WARNING'}, f"动作已生成（{os.path.basename(file_path)}），但 BVH 导入失败，请查看上方错误。")
            else:
                self.report({'INFO'}, f"动作已保存到 {file_path}（NPZ，需要手动导入）")
        else:
            s.generation_progress = "失败 ✗"
            self.report({'ERROR'}, f"生成失败：{_generation_state['result']}")

        return {'FINISHED'}

    def cancel(self, context):
        context.window_manager.event_timer_remove(self._timer)
        context.scene.kimodo.is_generating = False
        context.scene.kimodo.generation_progress = "已取消"
        _generation_state["running"] = False


class KIMODO_OT_CancelGeneration(Operator):
    """Cancel ongoing generation. The bridge cannot abort mid-diffusion, so the
    in-flight result is discarded when it arrives; a new generation can start
    once the bridge finishes the abandoned job."""
    bl_idname = "kimodo.cancel_generation"
    bl_label = "取消"

    def execute(self, context):
        if not context.scene.kimodo.is_generating:
            self.report({'INFO'}, "当前没有正在生成的任务。")
            return {'CANCELLED'}
        _generation_state["cancelled"] = True
        sc.request_cancel()
        context.scene.kimodo.generation_progress = "正在取消…"
        self.report({'INFO'}, "已请求取消，生成结果将被丢弃。")
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Import helpers
# ---------------------------------------------------------------------------

# Stable slot name reused for every Kimodo action so the slot identifier stays
# constant across generations (see _bind_action_with_slot / issue #37).
_KIMODO_SLOT_NAME = "Kimodo"


def _bind_action_with_slot(obj: bpy.types.Object, action: bpy.types.Action) -> None:
    """Assign *action* to *obj* and bind its animation slot (Blender 4.4+).

    Blender 4.4 introduced "slotted" Actions: an Action carries one or more
    slots, and an object's ``animation_data`` must be bound to a specific slot
    for the keyframes to drive the rig.  Setting ``animation_data.action`` only
    auto-binds a slot whose identifier matches the previously used one
    (``last_slot_identifier``).  BVH import gives every freshly imported Action a
    slot with a randomized name, so when we swap actions on the reused source
    armature (i.e. generating motion a second time) the identifiers never match:
    no slot binds, and the armature stops animating until the slot is picked by
    hand (issue #37).

    To fix it we give the slot a stable name (so the identifier is constant
    across generations) and bind it explicitly.  On Blender <4.4, where slots
    don't exist, this degrades to a plain action assignment.
    """
    if obj.animation_data is None:
        obj.animation_data_create()
    ad = obj.animation_data

    slots = getattr(action, "slots", None)
    if slots is None or not hasattr(ad, "action_slot"):
        # Pre-4.4 Blender: no slot system, a plain assignment is all there is.
        ad.action = action
        return

    # Unify the slot name so the identifier stays constant across generations.
    if len(slots):
        try:
            slots[0].name_display = _KIMODO_SLOT_NAME
        except Exception:
            pass  # name_display may be read-only on some builds — binding still works.

    ad.action = action
    # Assigning the action may already auto-bind a slot; only force it otherwise.
    if len(slots) and ad.action_slot is None:
        ad.action_slot = slots[0]


def _apply_to_existing_source(s, new_arm: bpy.types.Object) -> bpy.types.Object:
    """
    If reuse_armature is set, transfer the action from new_arm to it and
    delete new_arm.  Returns the armature that should be used going forward.
    """
    existing = s.reuse_armature
    if not existing or existing.type != 'ARMATURE' or existing == new_arm:
        return new_arm  # nothing to reuse — keep the freshly imported one

    new_action = new_arm.animation_data.action if new_arm.animation_data else None
    old_action = existing.animation_data.action if existing.animation_data else None
    new_arm_data = new_arm.data

    # Transfer the action (the data-block survives object deletion).  Bind the
    # slot explicitly so Blender 4.4+ keeps animating after the swap (issue #37).
    if new_action:
        _bind_action_with_slot(existing, new_action)

    # Remove the temporary stand-in armature object and its now-orphan data, so
    # re-importing (e.g. from history) doesn't leave Kimodo_Source.001/.002 …
    bpy.data.objects.remove(new_arm, do_unlink=True)
    if new_arm_data and new_arm_data.users == 0:
        bpy.data.armatures.remove(new_arm_data)

    # Drop the action that used to be on the reused armature once nothing else
    # references it — otherwise every re-import piles up an orphan Action.001/.002.
    if old_action and old_action is not new_action and old_action.users == 0:
        bpy.data.actions.remove(old_action)

    return existing


# ---------------------------------------------------------------------------
# Import operators
# ---------------------------------------------------------------------------

class KIMODO_OT_ImportBVH(Operator):
    """Import a BVH file and register it as the Kimodo source armature"""
    bl_idname = "kimodo.import_bvh"
    bl_label = "导入 BVH"

    filepath: StringProperty(subtype='FILE_PATH')

    def execute(self, context):
        s = context.scene.kimodo
        path = self.filepath or s.last_bvh_path

        if not path or not os.path.exists(path):
            self.report({'ERROR'}, f"找不到文件：{path}")
            return {'CANCELLED'}

        # Remember which objects exist before import
        before = set(bpy.context.scene.objects)

        try:
            _import_bvh(
                filepath=path,
                axis_forward='-Z',
                axis_up='Y',
                target='ARMATURE',
                global_scale=0.01,   # BVH is usually in cm; Blender expects meters
                frame_start=1,
                use_fps_scale=False,
                update_scene_fps=False,
                update_scene_duration=True,
                use_cyclic=False,
                rotate_mode='NATIVE',
            )
        except RuntimeError as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}

        # Find newly added armature
        after = set(bpy.context.scene.objects)
        new_objs = after - before
        new_arm = next((o for o in new_objs if o.type == 'ARMATURE'), None)

        if new_arm:
            new_arm.name = "Kimodo_Source"
            new_arm["kimodo_source"] = True
            new_arm["kimodo_creation_time"] = time.time()
            new_arm = _apply_to_existing_source(s, new_arm)
            s.source_armature = new_arm
            s.reuse_armature = new_arm
            self.report({'INFO'}, f"已导入“{new_arm.name}”，包含 {len(new_arm.data.bones)} 根骨骼。")
        else:
            self.report({'WARNING'}, "BVH 已导入，但场景中没有找到骨架。")

        return {'FINISHED'}

    def invoke(self, context, event):
        if self.filepath:
            return self.execute(context)
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}




# ---------------------------------------------------------------------------
# Retargeting operators
# ---------------------------------------------------------------------------

class KIMODO_OT_AutoMapBones(Operator):
    """Auto-match bone names between Kimodo source and target armature"""
    bl_idname = "kimodo.auto_map_bones"
    bl_label = "自动匹配骨骼"

    def execute(self, context):
        s = context.scene.kimodo
        if not s.source_armature:
            self.report({'ERROR'}, "请先设置源骨架。")
            return {'CANCELLED'}
        if not s.target_armature:
            self.report({'ERROR'}, "请先设置目标骨架。")
            return {'CANCELLED'}

        pairs = rt.auto_build_mapping(s.source_armature, s.target_armature, s.model_type)
        s.bone_mappings.clear()

        for src, tgt in pairs:
            item = s.bone_mappings.add()
            item.source_bone = src
            item.target_bone = tgt
            item.enabled = True

        self.report({'INFO'}, f"已自动匹配 {len(pairs)} 对骨骼。")
        return {'FINISHED'}


class KIMODO_OT_AddBoneMapping(Operator):
    """Add a new empty bone mapping row"""
    bl_idname = "kimodo.add_bone_mapping"
    bl_label = "添加骨骼对"

    def execute(self, context):
        s = context.scene.kimodo
        item = s.bone_mappings.add()
        item.source_bone = ""
        item.target_bone = ""
        item.enabled = True
        s.bone_mapping_index = len(s.bone_mappings) - 1
        return {'FINISHED'}


class KIMODO_OT_RemoveBoneMapping(Operator):
    """Remove the selected bone mapping row"""
    bl_idname = "kimodo.remove_bone_mapping"
    bl_label = "移除骨骼对"

    def execute(self, context):
        s = context.scene.kimodo
        idx = s.bone_mapping_index
        if 0 <= idx < len(s.bone_mappings):
            s.bone_mappings.remove(idx)
            s.bone_mapping_index = max(0, idx - 1)
        return {'FINISHED'}


class KIMODO_OT_ApplyRetargeting(Operator):
    """Apply Copy Rotation/Location constraints to drive target rig from Kimodo motion"""
    bl_idname = "kimodo.apply_retargeting"
    bl_label = "应用重定向"

    def execute(self, context):
        s = context.scene.kimodo
        if not s.source_armature:
            self.report({'ERROR'}, "请设置源骨架。")
            return {'CANCELLED'}
        if not s.target_armature:
            self.report({'ERROR'}, "请设置目标骨架。")
            return {'CANCELLED'}
        if not s.bone_mappings:
            self.report({'ERROR'}, "尚未定义骨骼映射。请使用自动匹配或手动添加。")
            return {'CANCELLED'}

        pairs = [(item.source_bone, item.target_bone, item.enabled,
                  item.retarget_mode, item.inherit_rotation)
                 for item in s.bone_mappings]

        n, warnings = rt.apply_retargeting_constraints(
            s.source_armature, s.target_armature, pairs, s.retarget_root_bone
        )

        for w in warnings:
            self.report({'WARNING'}, w)

        self.report({'INFO'}, f"已为 {n} 根骨骼应用重定向约束。")
        return {'FINISHED'}


class KIMODO_OT_RemoveRetargeting(Operator):
    """Remove all Kimodo retargeting constraints from target armature"""
    bl_idname = "kimodo.remove_retargeting"
    bl_label = "移除约束"

    def execute(self, context):
        s = context.scene.kimodo
        if not s.target_armature:
            self.report({'ERROR'}, "请设置目标骨架。")
            return {'CANCELLED'}
        n = rt.remove_retargeting_constraints(s.target_armature)
        self.report({'INFO'}, f"已移除 {n} 个 Kimodo 约束。")
        return {'FINISHED'}


class KIMODO_OT_BakeRetargeting(Operator):
    """Bake the retargeted animation into keyframes and remove constraints"""
    bl_idname = "kimodo.bake_retargeting"
    bl_label = "烘焙动画"

    def execute(self, context):
        s = context.scene.kimodo
        if not s.target_armature:
            self.report({'ERROR'}, "请设置目标骨架。")
            return {'CANCELLED'}

        success = rt.bake_retargeted_animation(
            s.target_armature,
            s.bake_start_frame,
            s.bake_end_frame,
        )
        if success:
            self.report({'INFO'}, "动画烘焙成功 ✓")
        else:
            self.report({'ERROR'}, "烘焙失败，请查看控制台详情。")
        return {'FINISHED'} if success else {'CANCELLED'}


# ---------------------------------------------------------------------------
# Preset operators
# ---------------------------------------------------------------------------

class KIMODO_OT_SavePreset(Operator):
    """Save current bone mapping as a named preset"""
    bl_idname = "kimodo.save_preset"
    bl_label = "保存预设"

    def execute(self, context):
        s = context.scene.kimodo
        prefs = context.preferences.addons[__package__].preferences
        name = s.preset_name.strip()
        if not name:
            self.report({'ERROR'}, "请先输入预设名称。")
            return {'CANCELLED'}

        pairs = [{"src": item.source_bone, "tgt": item.target_bone,
                  "en": item.enabled, "mode": item.retarget_mode,
                  "inherit_rot": item.inherit_rotation}
                 for item in s.bone_mappings]
        rt.save_preset(prefs, name, pairs)
        self.report({'INFO'}, f"预设“{name}”已保存（{len(pairs)} 对骨骼）。")
        return {'FINISHED'}


class KIMODO_OT_LoadPreset(Operator):
    """Load a saved bone mapping preset"""
    bl_idname = "kimodo.load_preset"
    bl_label = "加载预设"

    preset_name: StringProperty()

    def execute(self, context):
        s = context.scene.kimodo
        prefs = context.preferences.addons[__package__].preferences
        name = self.preset_name or s.preset_name.strip()

        pairs = rt.load_preset(prefs, name)
        if pairs is None:
            self.report({'ERROR'}, f"未找到预设“{name}”。")
            return {'CANCELLED'}

        s.bone_mappings.clear()
        for p in pairs:
            item = s.bone_mappings.add()
            item.source_bone     = p.get("src", "")
            item.target_bone     = p.get("tgt", "")
            item.enabled         = p.get("en", True)
            item.retarget_mode   = p.get("mode", "COPY_ROTATION")
            item.inherit_rotation = p.get("inherit_rot", True)

        self.report({'INFO'}, f"已加载预设“{name}”（{len(pairs)} 对骨骼）。")
        return {'FINISHED'}


class KIMODO_OT_DeletePreset(Operator):
    """Delete a saved bone mapping preset"""
    bl_idname = "kimodo.delete_preset"
    bl_label = "删除预设"

    preset_name: StringProperty()

    def execute(self, context):
        prefs = context.preferences.addons[__package__].preferences
        name = self.preset_name
        try:
            presets = json.loads(prefs.saved_presets)
            if name in presets:
                del presets[name]
                prefs.saved_presets = json.dumps(presets)
                self.report({'INFO'}, f"已删除预设“{name}”。")
            else:
                self.report({'WARNING'}, f"未找到预设“{name}”。")
        except Exception as e:
            self.report({'ERROR'}, str(e))
        return {'FINISHED'}


class KIMODO_OT_ExportPresetFile(Operator):
    """Export the current bone mapping to a JSON file"""
    bl_idname  = "kimodo.export_preset_file"
    bl_label   = "导出骨骼映射"

    filepath:   StringProperty(subtype='FILE_PATH')
    filename_ext = ".json"
    filter_glob: StringProperty(default="*.json", options={'HIDDEN'})

    def invoke(self, context, event):
        s = context.scene.kimodo
        self.filepath = (s.preset_name.strip() or "bone_map") + ".json"
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        s = context.scene.kimodo
        pairs = [{"src": item.source_bone, "tgt": item.target_bone,
                  "en": item.enabled, "mode": item.retarget_mode,
                  "inherit_rot": item.inherit_rotation}
                 for item in s.bone_mappings]
        try:
            with open(self.filepath, "w", encoding="utf-8") as f:
                json.dump(pairs, f, indent=2)
            self.report({'INFO'}, f"已将 {len(pairs)} 对骨骼导出到 {self.filepath}。")
        except Exception as e:
            self.report({'ERROR'}, f"导出失败：{e}")
            return {'CANCELLED'}
        return {'FINISHED'}


class KIMODO_OT_ImportPresetFile(Operator):
    """Import a bone mapping from a JSON file"""
    bl_idname  = "kimodo.import_preset_file"
    bl_label   = "导入骨骼映射"

    filepath:   StringProperty(subtype='FILE_PATH')
    filename_ext = ".json"
    filter_glob: StringProperty(default="*.json", options={'HIDDEN'})

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        s = context.scene.kimodo
        try:
            with open(self.filepath, "r", encoding="utf-8") as f:
                pairs = json.load(f)
            if not isinstance(pairs, list):
                self.report({'ERROR'}, "文件内容不是 JSON 数组。")
                return {'CANCELLED'}
        except Exception as e:
            self.report({'ERROR'}, f"导入失败：{e}")
            return {'CANCELLED'}

        s.bone_mappings.clear()
        for p in pairs:
            item = s.bone_mappings.add()
            item.source_bone     = p.get("src", "")
            item.target_bone     = p.get("tgt", "")
            item.enabled         = p.get("en", True)
            item.retarget_mode   = p.get("mode", "COPY_ROTATION")
            item.inherit_rotation = p.get("inherit_rot", True)

        self.report({'INFO'}, f"已从 {self.filepath} 导入 {len(pairs)} 对骨骼。")
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Motion segment operators
# ---------------------------------------------------------------------------

from .properties import _SEGMENT_COLORS


class KIMODO_OT_SelectSegment(Operator):
    """Set a segment as the active one"""
    bl_idname = "kimodo.select_segment"
    bl_label  = "选择片段"
    index: IntProperty()

    def execute(self, context):
        s = context.scene.kimodo
        if 0 <= self.index < len(s.motion_segments):
            s.segment_index = self.index
        return {'FINISHED'}


class KIMODO_OT_RemoveSegmentByIndex(Operator):
    """Remove a specific segment by index"""
    bl_idname = "kimodo.remove_segment_by_index"
    bl_label  = "移除片段"
    index: IntProperty()

    def execute(self, context):
        s = context.scene.kimodo
        if 0 <= self.index < len(s.motion_segments):
            s.motion_segments.remove(self.index)
            s.segment_index = max(0, min(s.segment_index, len(s.motion_segments) - 1))
            _tag_timeline_redraw(context)
        return {'FINISHED'}


class KIMODO_OT_AddSegment(Operator):
    """Add a new motion segment at the current frame"""
    bl_idname = "kimodo.add_segment"
    bl_label = "添加片段"

    def execute(self, context):
        s = context.scene.kimodo
        scene = context.scene

        # Auto-assign a cycling colour
        color = _SEGMENT_COLORS[len(s.motion_segments) % len(_SEGMENT_COLORS)]

        # Place segment after the last existing one, or at current frame
        start = scene.frame_current
        if s.motion_segments:
            last = max(s.motion_segments, key=lambda seg: seg.end_frame)
            start = last.end_frame + 1
            

        fps = scene.render.fps / scene.render.fps_base
        end = start + int(fps * 5) - 1  # default 5 seconds

        seg = s.motion_segments.add()
        seg.prompt      = "a person walks forward"
        seg.start_frame = start
        seg.end_frame   = end
        seg.model_type  = s.model_type
        seg.seed        = s.seed
        seg.color       = color
        seg.enabled     = True

        s.segment_index = len(s.motion_segments) - 1

        # Force timeline redraw
        _tag_timeline_redraw(context)
        return {'FINISHED'}


class KIMODO_OT_RemoveSegment(Operator):
    """Remove the selected motion segment"""
    bl_idname = "kimodo.remove_segment"
    bl_label = "移除片段"

    def execute(self, context):
        s = context.scene.kimodo
        idx = s.segment_index
        if 0 <= idx < len(s.motion_segments):
            s.motion_segments.remove(idx)
            s.segment_index = max(0, idx - 1)
            _tag_timeline_redraw(context)
        return {'FINISHED'}


class KIMODO_OT_DuplicateSegment(Operator):
    """Duplicate the selected segment and place it immediately after"""
    bl_idname = "kimodo.duplicate_segment"
    bl_label = "复制片段"

    def execute(self, context):
        s = context.scene.kimodo
        idx = s.segment_index
        if not (0 <= idx < len(s.motion_segments)):
            return {'CANCELLED'}

        src = s.motion_segments[idx]
        duration = src.end_frame - src.start_frame

        new_seg = s.motion_segments.add()
        new_seg.prompt      = src.prompt
        new_seg.start_frame = src.end_frame + 1
        new_seg.end_frame   = src.end_frame + 1 + duration
        new_seg.model_type  = src.model_type
        new_seg.seed        = src.seed
        new_seg.color       = src.color
        new_seg.enabled     = src.enabled

        s.segment_index = len(s.motion_segments) - 1
        _tag_timeline_redraw(context)
        return {'FINISHED'}


class KIMODO_OT_MoveSegmentUp(Operator):
    """Move segment up in list (earlier in sequence)"""
    bl_idname = "kimodo.move_segment_up"
    bl_label = "上移"

    def execute(self, context):
        s = context.scene.kimodo
        idx = s.segment_index
        if idx > 0:
            s.motion_segments.move(idx, idx - 1)
            s.segment_index -= 1
        return {'FINISHED'}


class KIMODO_OT_MoveSegmentDown(Operator):
    """Move segment down in list (later in sequence)"""
    bl_idname = "kimodo.move_segment_down"
    bl_label = "下移"

    def execute(self, context):
        s = context.scene.kimodo
        idx = s.segment_index
        if idx < len(s.motion_segments) - 1:
            s.motion_segments.move(idx, idx + 1)
            s.segment_index += 1
        return {'FINISHED'}


class KIMODO_OT_SyncSeeds(Operator):
    """Sync all segment seeds with global seed setting"""
    bl_idname = "kimodo.sync_seeds"
    bl_label = "同步种子"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        s = context.scene.kimodo
        global_seed = s.seed
        
        for seg in s.motion_segments:
            seg.seed = global_seed
            
        self.report({'INFO'}, f"已将 {len(s.motion_segments)} 个片段更新为种子 {global_seed}。")
        return {'FINISHED'}


class KIMODO_OT_GenerateSegment(Operator):
    """Generate motion for the selected segment"""
    bl_idname = "kimodo.generate_segment"
    bl_label = "生成选中片段"

    _timer  = None
    _thread = None
    _target_segment_idx: int = -1

    def invoke(self, context, event):
        s = context.scene.kimodo
        idx = s.segment_index

        if not (0 <= idx < len(s.motion_segments)):
            self.report({'ERROR'}, "未选择片段。")
            return {'CANCELLED'}
        if not sc.is_running():
            self.report({'WARNING'}, "Kimodo 尚未运行，请先点击“启动 Kimodo”。")
            return {'CANCELLED'}
        if s.is_generating:
            self.report({'WARNING'}, "正在生成，请稍候。")
            return {'CANCELLED'}

        seg = s.motion_segments[idx]
        self._target_segment_idx = idx
        return self._start_generation(context, s, seg)

    def _start_generation(self, context, s, seg):
        import random as _random

        seed = seg.seed if seg.seed >= 0 else _random.randint(0, 2**31 - 1)
        self._resolved_seed = seed
        fps  = context.scene.render.fps / context.scene.render.fps_base
        duration = (seg.end_frame - seg.start_frame + 1) / fps
        self._segment_duration = duration

        # Build constraints for this segment if any exist
        constraints_json, con_err = _build_segment_constraints(context, seg)
        if con_err:
            self.report({'WARNING'},
                        f"约束构建失败，将不带约束生成：{con_err}")

        _reset_state()
        _generation_state["running"] = True
        s.is_generating = True
        s.generation_progress = f"正在生成：{seg.prompt[:40]}…"
        s.generating_segment_index = self._target_segment_idx

        self._thread = threading.Thread(
            target=self._run_generation,
            args=(seg.prompt, duration, seed, s.output_format, constraints_json, s.bvh_standard_tpose),
            daemon=True,
        )
        self._thread.start()

        wm = context.window_manager
        self._timer = wm.event_timer_add(0.5, window=context.window)
        wm.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def _run_generation(self, prompt, duration, seed, fmt, constraints_json, bvh_standard_tpose):
        def progress_cb(msg):
            _generation_state["progress"] = msg

        success, result = sc.generate_motion(
            prompt=prompt,
            duration=duration,
            seed=seed,
            output_format=fmt,
            constraints_json=constraints_json,
            bvh_standard_tpose=bvh_standard_tpose,
            progress_callback=progress_cb,
        )
        _generation_state["success"] = success
        _generation_state["result"]  = result
        _generation_state["done"]    = True
        _generation_state["running"] = False

    def modal(self, context, event):
        s = context.scene.kimodo
        if event.type != 'TIMER':
            return {'PASS_THROUGH'}

        s.generation_progress = _generation_state.get("progress", "")
        _tag_timeline_redraw(context)
        for area in context.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()

        if not _generation_state["done"]:
            return {'RUNNING_MODAL'}

        context.window_manager.event_timer_remove(self._timer)
        s.is_generating = False
        s.generating_segment_index = -1

        if _generation_state["cancelled"]:
            s.generation_progress = "已取消"
            self.report({'INFO'}, "生成已取消，结果已丢弃。")
        elif _generation_state["success"]:
            file_path = _generation_state["result"]
            seg = s.motion_segments[self._target_segment_idx]
            seg.last_bvh_path = file_path
            seg.generated = True
            s.generation_progress = "完成 ✓"
            _push_history(s, seg.prompt, self._resolved_seed, self._segment_duration, file_path)

            if os.path.splitext(file_path)[1].lower() == ".bvh":
                bpy.ops.kimodo.import_bvh_at_frame(
                    'EXEC_DEFAULT',
                    filepath=file_path,
                    start_frame=seg.start_frame,
                    label=seg.prompt[:30],
                )
                self.report({'INFO'}, f"片段“{seg.prompt[:40]}”已生成 ✓")
            else:
                self.report({'INFO'}, f"NPZ 已保存到 {file_path}")
        else:
            s.generation_progress = "失败 ✗"
            self.report({'ERROR'}, f"生成失败：{_generation_state['result']}")

        _tag_timeline_redraw(context)
        return {'FINISHED'}

    def cancel(self, context):
        context.window_manager.event_timer_remove(self._timer)
        context.scene.kimodo.is_generating = False
        context.scene.kimodo.generating_segment_index = -1
        _generation_state["running"] = False


def _enforce_segment_continuity(ordered_segs):
    """
    Make segments contiguous: each segment starts the frame after the previous ends.
    Preserves each segment's duration (frame count). Returns True if any were changed.
    """
    changed = False
    prev_end = None
    for seg in ordered_segs:
        if prev_end is not None:
            expected_start = prev_end + 1
            if seg.start_frame != expected_start:
                duration_frames = seg.end_frame - seg.start_frame
                seg.start_frame = expected_start
                seg.end_frame   = expected_start + duration_frames
                changed = True
        prev_end = seg.end_frame
    return changed


def _build_multi_prompt_constraints(context, first_start_frame: int) -> "tuple[str | None, str | None]":
    """Build scene constraints JSON for multi-prompt generation.

    Uses first_start_frame as the Kimodo sequence origin so constraint frame
    indices align with the combined BVH that starts at that Blender frame.
    Returns (constraints_json, error_message) — error_message is set when the
    build failed so the caller can warn instead of silently dropping them.
    """
    s = context.scene.kimodo
    enabled = [c for c in s.motion_constraints if c.enabled and c.marker_object]
    if not enabled:
        return None, None
    try:
        data = cmod.build_constraints_json(
            s.motion_constraints, context.scene,
            kimodo_fps=s.kimodo_fps,
            auto_canonicalize=s.auto_canonicalize,
            scene_start_override=first_start_frame,
        )
        return (json.dumps(data) if data else None), None
    except Exception as exc:
        return None, str(exc)


class KIMODO_OT_GenerateAllSegments(Operator):
    """Generate all enabled segments as a single multi-prompt sequence with smooth transitions"""
    bl_idname = "kimodo.generate_all_segments"
    bl_label  = "生成全部片段"

    _timer           = None
    _thread          = None
    _segment_indices: list = []
    _start_frame: int = 1
    _resolved_seed: int = -1

    def invoke(self, context, event):
        s = context.scene.kimodo
        if not sc.is_running():
            self.report({'WARNING'}, "Kimodo 尚未运行，请先点击“启动 Kimodo”。")
            return {'CANCELLED'}
        if s.is_generating:
            self.report({'WARNING'}, "正在生成。")
            return {'CANCELLED'}

        # Collect enabled segments sorted chronologically
        ordered = sorted(
            [(i, seg) for i, seg in enumerate(s.motion_segments) if seg.enabled],
            key=lambda x: x[1].start_frame,
        )
        if not ordered:
            self.report({'WARNING'}, "没有启用的片段。")
            return {'CANCELLED'}

        # Auto-fix any gaps or overlaps between segments
        ordered_segs = [seg for _, seg in ordered]
        if _enforce_segment_continuity(ordered_segs):
            self.report({'INFO'},
                "已调整片段帧范围，使其连续（无间隙/重叠）。")

        self._segment_indices = [i for i, _ in ordered]
        self._start_frame = ordered[0][1].start_frame

        fps = context.scene.render.fps / context.scene.render.fps_base
        prompts   = [seg.prompt for _, seg in ordered]
        durations = [(seg.end_frame - seg.start_frame + 1) / fps for _, seg in ordered]

        # Resolve seeds for all segments
        seeds = [seg.seed if seg.seed >= 0 else random.randint(0, 2**31 - 1) for _, seg in ordered]
        seed = seeds[0]
        self._resolved_seed = seed

        # Build constraints relative to the start of the combined sequence
        constraints_json, con_err = _build_multi_prompt_constraints(context, self._start_frame)
        if con_err:
            self.report({'WARNING'},
                        f"约束构建失败，将不带约束生成：{con_err}")

        s.is_generating = True
        s.generating_segment_index = self._segment_indices[0]
        _reset_state()
        _generation_state["running"] = True
        s.generation_progress = (
            f"正在将 {len(ordered)} 个片段生成为多提示词序列…"
        )

        num_transition_frames = s.num_transition_frames

        self._thread = threading.Thread(
            target=self._run_all,
            args=(prompts, durations, seed, s.output_format,
                  constraints_json, s.bvh_standard_tpose, num_transition_frames, seeds),
            daemon=True,
        )
        self._thread.start()

        wm = context.window_manager
        self._timer = wm.event_timer_add(0.5, window=context.window)
        wm.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def _run_all(self, prompts, durations, seed, fmt, constraints_json, bvh_standard_tpose,
                 num_transition_frames=5, seeds=None):
        def progress_cb(msg):
            _generation_state["progress"] = msg

        success, result = sc.generate_motion_multi(
            prompts=prompts,
            durations=durations,
            seed=seed,
            output_format=fmt,
            constraints_json=constraints_json,
            bvh_standard_tpose=bvh_standard_tpose,
            num_transition_frames=num_transition_frames,
            progress_callback=progress_cb,
            seeds=seeds,
        )
        _generation_state["success"] = success
        _generation_state["result"]  = result
        _generation_state["done"]    = True
        _generation_state["running"] = False

    def modal(self, context, event):
        s = context.scene.kimodo
        if event.type != 'TIMER':
            return {'PASS_THROUGH'}

        s.generation_progress = _generation_state.get("progress", "")
        _tag_timeline_redraw(context)
        for area in context.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()

        if not _generation_state["done"]:
            return {'RUNNING_MODAL'}

        context.window_manager.event_timer_remove(self._timer)
        s.is_generating = False
        s.generating_segment_index = -1

        if _generation_state["cancelled"]:
            s.generation_progress = "已取消"
            self.report({'INFO'}, "生成已取消，结果已丢弃。")
        elif _generation_state["success"]:
            file_path = _generation_state["result"]

            # Mark all segments as generated and store the shared path
            generated_segments = [s.motion_segments[idx] for idx in self._segment_indices]
            for seg in generated_segments:
                seg.last_bvh_path = file_path
                seg.generated = True

            fps = context.scene.render.fps / context.scene.render.fps_base
            prompt = " | ".join(seg.prompt for seg in generated_segments)
            duration = sum((seg.end_frame - seg.start_frame + 1) / fps for seg in generated_segments)
            _push_history(s, prompt, self._resolved_seed, duration, file_path)

            if os.path.splitext(file_path)[1].lower() == ".bvh":
                label = f"{len(self._segment_indices)}-prompt"
                bpy.ops.kimodo.import_bvh_at_frame(
                    'EXEC_DEFAULT',
                    filepath=file_path,
                    start_frame=self._start_frame,
                    label=label,
                )

            n = len(self._segment_indices)
            s.generation_progress = f"全部 {n} 个片段已生成 ✓"
            self.report({'INFO'}, f"多提示词生成完成（{n} 个片段）✓")
        else:
            s.generation_progress = "失败 ✗"
            self.report({'ERROR'}, f"生成失败：{_generation_state['result']}")

        _tag_timeline_redraw(context)
        return {'FINISHED'}

    def cancel(self, context):
        context.window_manager.event_timer_remove(self._timer)
        context.scene.kimodo.is_generating = False
        context.scene.kimodo.generating_segment_index = -1
        _generation_state["running"] = False


# ---------------------------------------------------------------------------
# Frame-aware BVH import (offsets keyframes to start_frame)
# ---------------------------------------------------------------------------

class KIMODO_OT_ImportBVHAtFrame(Operator):
    """Import BVH and shift its keyframes to start at start_frame"""
    bl_idname = "kimodo.import_bvh_at_frame"
    bl_label  = "在指定帧导入 BVH"

    filepath:    StringProperty(subtype='FILE_PATH')
    start_frame: IntProperty(default=1)
    label:       StringProperty(default="")

    def execute(self, context):
        if not os.path.exists(self.filepath):
            self.report({'ERROR'}, f"找不到文件：{self.filepath}")
            return {'CANCELLED'}

        before = set(bpy.data.objects)

        try:
            _import_bvh(
                filepath=self.filepath,
                axis_forward='-Z', axis_up='Y',
                target='ARMATURE',
                global_scale=0.01,
                frame_start=self.start_frame,
                use_fps_scale=False,
                update_scene_fps=False,
                update_scene_duration=False,
                use_cyclic=False,
                rotate_mode='NATIVE',
            )
        except RuntimeError as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}

        after   = set(bpy.data.objects)
        new_arm = next((o for o in after - before if o.type == 'ARMATURE'), None)

        if new_arm:
            name = f"Kimodo_{self.label}" if self.label else "Kimodo_Source"
            new_arm.name = name
            new_arm["kimodo_source"] = True
            new_arm["kimodo_creation_time"] = time.time()
            s = context.scene.kimodo
            new_arm = _apply_to_existing_source(s, new_arm)
            s.source_armature = new_arm
            s.reuse_armature = new_arm

        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Helpers shared by segment operators
# ---------------------------------------------------------------------------

def _tag_timeline_redraw(context):
    """Force Timeline and DopeSheet areas to redraw."""
    for area in context.screen.areas:
        if area.type == 'DOPESHEET_EDITOR':
            area.tag_redraw()


def _build_segment_constraints(context, seg) -> "tuple[str | None, str | None]":
    """Build constraints JSON for a segment (reuses scene-level constraints for now).

    Returns (constraints_json, error_message) — error_message is set when the
    build failed so the caller can warn instead of silently dropping them.
    """
    s = context.scene.kimodo
    enabled = [c for c in s.motion_constraints if c.enabled and c.marker_object]
    if not enabled:
        return None, None
    try:
        data = cmod.build_constraints_json(
            s.motion_constraints, context.scene,
            kimodo_fps=s.kimodo_fps,
            auto_canonicalize=s.auto_canonicalize,
        )
        return json.dumps(data), None
    except Exception as exc:
        return None, str(exc)


# ---------------------------------------------------------------------------
# Constraint authoring operators
# ---------------------------------------------------------------------------

_CONSTRAINT_COLORS_RGBA = {
    'root2d':      (0.9, 0.2, 0.2, 1.0),   # red
    'fullbody':    (0.9, 0.6, 0.1, 0.7),   # orange (semi-transparent — it's an armature)
    'left_hand':   (0.9, 0.9, 0.1, 1.0),   # yellow
    'right_hand':  (0.2, 0.8, 0.2, 1.0),   # green
    'left_foot':   (0.1, 0.7, 0.8, 1.0),   # cyan
    'right_foot':  (0.2, 0.3, 0.9, 1.0),   # blue
}


def _unique_name(base: str) -> str:
    """Return a name not already in bpy.data.objects."""
    existing = {o.name for o in bpy.data.objects}
    if base not in existing:
        return base
    i = 1
    while f"{base}_{i:02d}" in existing:
        i += 1
    return f"{base}_{i:02d}"


def _sample_curve_arc_length(curve_obj, n_samples, depsgraph):
    """Return n_samples evenly arc-length-spaced world-space positions along curve_obj."""
    evaluated = curve_obj.evaluated_get(depsgraph)
    mesh = evaluated.to_mesh()
    mw = curve_obj.matrix_world

    verts = [mw @ v.co for v in mesh.vertices]
    evaluated.to_mesh_clear()

    if len(verts) < 2:
        return verts

    # Build cumulative arc-length table
    lengths = [0.0]
    for i in range(len(verts) - 1):
        lengths.append(lengths[-1] + (verts[i + 1] - verts[i]).length)
    total = lengths[-1]

    if total == 0.0:
        return [verts[0]] * n_samples

    samples = []
    seg = 0
    for i in range(n_samples):
        t = (i / (n_samples - 1)) * total if n_samples > 1 else 0.0
        while seg < len(lengths) - 2 and lengths[seg + 1] < t:
            seg += 1
        seg_len = lengths[seg + 1] - lengths[seg]
        frac = (t - lengths[seg]) / seg_len if seg_len > 0 else 0.0
        samples.append(verts[seg].lerp(verts[seg + 1], frac))

    return samples


class KIMODO_OT_DrawFreehandCurve(Operator):
    """Create a new curve and activate Blender's Draw tool to sketch a path freehand"""
    bl_idname  = "kimodo.draw_freehand_curve"
    bl_label   = "绘制曲线"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        s = context.scene.kimodo

        # If we are already in Edit Mode, switch to Object Mode first
        if context.mode != 'OBJECT':
            try:
                bpy.ops.object.mode_set(mode='OBJECT')
            except Exception:
                pass

        # Create new Bezier Curve data block and object
        curve_data = bpy.data.curves.new(name="Kimodo_Draw_Path", type='CURVE')
        curve_data.dimensions = '3D'
        
        curve_obj = bpy.data.objects.new(name="Kimodo_Draw_Path", object_data=curve_data)
        context.scene.collection.objects.link(curve_obj)
        
        # Set it active & selected
        bpy.ops.object.select_all(action='DESELECT')
        context.view_layer.objects.active = curve_obj
        curve_obj.select_set(True)
        
        # Assign to panel property
        s.path_curve = curve_obj
        
        # Enter edit mode
        bpy.ops.object.mode_set(mode='EDIT')
        
        # Set tool to built-in Draw tool
        try:
            bpy.ops.wm.tool_set_by_id(name="builtin.draw")
            self.report({'INFO'}, "绘制工具已激活！请在 3D 视图中绘制路径，完成后点击“采样曲线”。")
        except Exception as e:
            self.report({'WARNING'}, f"已创建曲线，但无法切换到绘制工具：{e}")

        return {'FINISHED'}


class KIMODO_OT_SampleCurveAsWaypoints(Operator):
    """Sample a curve into evenly-spaced Root XZ waypoint constraints"""
    bl_idname  = "kimodo.sample_curve_as_waypoints"
    bl_label   = "采样曲线为路径点"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        s = context.scene.kimodo

        if context.mode != 'OBJECT':
            try:
                bpy.ops.object.mode_set(mode='OBJECT')
            except Exception:
                pass

        if not s.path_curve:
            self.report({'ERROR'}, "请在“路径曲线”字段中设置一个曲线对象。")
            return {'CANCELLED'}
        if s.path_start_frame >= s.path_end_frame:
            self.report({'ERROR'}, "起始帧必须小于结束帧。")
            return {'CANCELLED'}

        n = s.path_waypoints
        depsgraph = context.evaluated_depsgraph_get()
        positions = _sample_curve_arc_length(s.path_curve, n, depsgraph)

        if not positions:
            self.report({'ERROR'}, "无法从曲线中采样任何点。")
            return {'CANCELLED'}

        # Heading per sample: the forward direction of the curve on the
        # ground plane, so the character walks *along* the path instead of
        # always facing +Y. Use a forward difference (and the previous
        # segment for the final point); reuse the last valid angle across any
        # duplicate/zero-length samples.  angle = atan2(dx, -dy) gives the
        # heading_angle (consumed by build_constraints_json) that makes the
        # character face forward along the curve in Kimodo's frame.
        headings = []
        last_angle = 0.0
        n_pos = len(positions)
        for i in range(n_pos):
            if i + 1 < n_pos:
                d = positions[i + 1] - positions[i]      # forward difference
            elif i > 0:
                d = positions[i] - positions[i - 1]       # last point: previous segment
            else:
                d = None                                  # single point: no direction
            if d is not None and math.hypot(d.x, d.y) > 1e-6:
                last_angle = math.atan2(d.x, -d.y)
            headings.append(last_angle)

        start_f = s.path_start_frame
        end_f   = s.path_end_frame
        color   = _CONSTRAINT_COLORS_RGBA['root2d']

        saved_active   = context.active_object
        saved_selected = list(context.selected_objects)
        bpy.ops.object.select_all(action='DESELECT')

        for i, pos in enumerate(positions):
            frame = round(start_f + (end_f - start_f) * i / (n - 1)) if n > 1 else start_f

            angle = headings[i]

            bpy.ops.object.empty_add(type='ARROWS', location=pos)
            empty = context.active_object
            empty.name = _unique_name(f"Kimodo_Path_{i + 1:02d}")
            empty.color = color
            empty.show_name = True
            empty.empty_display_size = 0.15
            # Point the arrow empty along the curve so the heading is visible.
            empty.rotation_euler.z = angle
            empty["kimodo_constraint"] = True
            empty["kimodo_type"]       = 'root2d'

            item = s.motion_constraints.add()
            item.constraint_type = 'root2d'
            item.frame           = frame
            item.marker_object   = empty
            item.enabled         = True
            item.include_heading = True
            item.heading_angle   = angle
            item.label           = empty.name

        s.constraint_index = len(s.motion_constraints) - 1

        bpy.ops.object.select_all(action='DESELECT')
        for o in saved_selected:
            o.select_set(True)
        if saved_active:
            context.view_layer.objects.active = saved_active

        self.report({'INFO'}, f"已添加 {n} 个路径点（帧 {start_f}–{end_f}）。")
        return {'FINISHED'}


class KIMODO_OT_AddConstraint(Operator):
    """Add a Kimodo motion constraint marker at the 3D cursor"""
    bl_idname = "kimodo.add_constraint"
    bl_label = "添加约束标记"

    constraint_type: bpy.props.StringProperty(default='root2d')

    def execute(self, context):
        s = context.scene.kimodo
        ctype = self.constraint_type
        cur_frame = context.scene.frame_current
        active = context.active_object

        # ---------------------------------------------------------------
        # fullbody needs an armature, not an Empty.
        # Priority: (1) active object is an armature → use it directly
        #           (2) source_armature exists → duplicate it as a reference
        #           (3) no armature available → create Empty but warn loudly
        # ---------------------------------------------------------------
        if ctype == 'fullbody':
            marker_obj = self._resolve_fullbody_armature(context, s, cur_frame)
            if marker_obj is None:
                return {'CANCELLED'}
        else:
            marker_obj = self._create_empty(context, ctype, cur_frame)

        item = s.motion_constraints.add()
        item.constraint_type = ctype
        item.frame = cur_frame
        item.marker_object = marker_obj
        item.enabled = True
        item.label = marker_obj.name
        s.constraint_index = len(s.motion_constraints) - 1

        self.report({'INFO'}, f"已在第 {cur_frame} 帧添加 {ctype} 约束 → “{marker_obj.name}”。")
        return {'FINISHED'}

    # ------------------------------------------------------------------

    def _resolve_fullbody_armature(self, context, s, cur_frame):
        """
        Return the armature object to use for a fullbody constraint.

        Logic:
        - Case 1: an armature is selected AND active is an armature → use active armature
        - Case 2: no armature in the selection set → duplicate source_armature for posing
        - Case 3: otherwise → error
        """
        active = context.active_object
        has_selected_armature = any(obj.type == 'ARMATURE' for obj in context.selected_objects)

        # Case 1: an armature is selected and active is an armature → use active as-is
        if has_selected_armature and active and active.type == 'ARMATURE':
            self.report({'INFO'},
                f"使用选中的骨架“{active.name}”作为全身姿态参考。"
                f"请在第 {cur_frame} 帧摆出姿态来定义关键帧。")
            return active

        # Case 2: no armature selected at all → duplicate source_armature for posing
        if not has_selected_armature and s.source_armature:
            return self._duplicate_source_for_posing(context, s, cur_frame)

        # Case 3: nothing to work with
        self.report({'ERROR'},
            "全身约束需要骨架。"
            "请先选择一个骨架，或先生成一次动作以便复制源骨架。")
        return None
        
    def _duplicate_source_for_posing(self, context, s, cur_frame):
        """Duplicate source_armature and freeze its pose at cur_frame.

        Plain duplication inherits the source's BVH F-curves, so any bone the
        user rotates without explicitly keyframing gets reverted to the
        animated value the next time the depsgraph evaluates the frame —
        including during build_constraints_json, which silently undoes the
        user's posing.

        Fix: evaluate the source at cur_frame, copy that pose into the
        duplicate, then strip the duplicate's animation_data so its bone
        properties stick until the user changes them.
        """
        scene = context.scene
        saved_frame = scene.frame_current
        scene.frame_set(cur_frame)
        context.view_layer.update()

        bpy.ops.object.select_all(action='DESELECT')
        s.source_armature.select_set(True)
        context.view_layer.objects.active = s.source_armature
        bpy.ops.object.duplicate(linked=False)
        dup = context.active_object

        # Make sure the duplicate's pose reflects the source's frame-N state
        # before we strip animation data.
        context.view_layer.update()

        # Snapshot every pose bone's transform so we can re-apply after the
        # animation_data wipe (clearing the action can otherwise reset values).
        pose_snapshot = {}
        for pb in dup.pose.bones:
            pose_snapshot[pb.name] = (
                pb.rotation_mode,
                pb.location.copy(),
                pb.rotation_quaternion.copy(),
                pb.rotation_euler.copy(),
                tuple(pb.rotation_axis_angle),
                pb.scale.copy(),
            )

        # Strip both the object-level action (root motion / object xform) and
        # any data-level action (rare for BVH but possible). After this, the
        # bone properties are no longer overwritten on frame change.
        if dup.animation_data:
            dup.animation_data_clear()
        if dup.data.animation_data:
            dup.data.animation_data_clear()

        # Re-apply the snapshot to lock the pose in place.
        for pb in dup.pose.bones:
            snap = pose_snapshot.get(pb.name)
            if snap is None:
                continue
            mode, loc, qrot, erot, aarot, sc = snap
            pb.rotation_mode = mode
            pb.location = loc
            pb.rotation_quaternion = qrot
            pb.rotation_euler = erot
            pb.rotation_axis_angle = aarot
            pb.scale = sc

        name = _unique_name(f"Kimodo_PoseRef_{cur_frame:04d}")
        dup.name = name
        dup.data.name = name + "_data"
        # Visually distinguish — semi-transparent orange tint.
        dup.color = (0.9, 0.5, 0.1, 0.7)
        dup["kimodo_constraint"] = True
        dup["kimodo_type"] = 'fullbody'
        dup.show_name = True

        scene.frame_set(saved_frame)
        context.view_layer.update()

        self.report({'INFO'},
            f"已将源骨架复制为“{name}”，并冻结在第 {cur_frame} 帧。"
            f"进入姿态模式后可调整，修改会保留（不会被 F 曲线覆盖）。")
        return dup

    def _create_empty(self, context, ctype, cur_frame):
        """Create a colour-coded Empty for non-fullbody constraint types."""
        empty_types = {
            'root2d':      'ARROWS',
            'left_hand':   'CUBE',
            'right_hand':  'CUBE',
            'left_foot':   'PLAIN_AXES',
            'right_foot':  'PLAIN_AXES',
        }
        bpy.ops.object.empty_add(
            type=empty_types.get(ctype, 'ARROWS'),
            location=context.scene.cursor.location,
        )
        empty = context.active_object

        type_labels = {
            'root2d':     'Kimodo_Waypoint',
            'left_hand':  'Kimodo_LHand',
            'right_hand': 'Kimodo_RHand',
            'left_foot':  'Kimodo_LFoot',
            'right_foot': 'Kimodo_RFoot',
        }
        base_name = type_labels.get(ctype, 'Kimodo_Marker')
        name = _unique_name(f"{base_name}_{cur_frame:04d}")
        empty.name = name
        empty.color = _CONSTRAINT_COLORS_RGBA.get(ctype, (1, 1, 1, 1))
        empty.show_name = True
        empty.empty_display_size = 0.15
        empty["kimodo_constraint"] = True
        empty["kimodo_type"] = ctype
        return empty


class KIMODO_OT_LinkExistingAsConstraint(Operator):
    """Link the currently selected object as a Kimodo constraint marker"""
    bl_idname = "kimodo.link_as_constraint"
    bl_label = "将所选对象链接为约束"

    constraint_type: bpy.props.StringProperty(default='root2d')

    def execute(self, context):
        s = context.scene.kimodo
        obj = context.active_object
        if not obj:
            self.report({'ERROR'}, "请先选择一个对象。")
            return {'CANCELLED'}

        ctype = self.constraint_type
        item = s.motion_constraints.add()
        item.constraint_type = ctype
        item.frame = context.scene.frame_current
        item.marker_object = obj
        item.enabled = True
        item.label = obj.name
        s.constraint_index = len(s.motion_constraints) - 1

        self.report({'INFO'}, f"已将“{obj.name}”链接为第 {item.frame} 帧的 {ctype} 约束。")
        return {'FINISHED'}


class KIMODO_OT_RemoveConstraint(Operator):
    """Remove the selected constraint from the list (optionally delete the marker)"""
    bl_idname = "kimodo.remove_constraint"
    bl_label = "移除约束"

    index: IntProperty(
        name="索引",
        description="要移除的约束索引（-1 = 当前项）",
        default=-1,
    )
    delete_object: BoolProperty(
        name="删除标记对象",
        description="同时从场景中删除空物体/对象",
        default=False,
    )

    def execute(self, context):
        s = context.scene.kimodo
        idx = self.index if self.index >= 0 else s.constraint_index
        if not (0 <= idx < len(s.motion_constraints)):
            return {'CANCELLED'}

        item = s.motion_constraints[idx]
        if self.delete_object and item.marker_object:
            bpy.data.objects.remove(item.marker_object, do_unlink=True)

        s.motion_constraints.remove(idx)
        s.constraint_index = max(0, idx - 1)
        return {'FINISHED'}


class KIMODO_OT_GotoConstraintFrame(Operator):
    """Jump the timeline to this constraint's frame"""
    bl_idname = "kimodo.goto_constraint_frame"
    bl_label = "跳转到帧"

    frame: bpy.props.IntProperty()

    def execute(self, context):
        context.scene.frame_set(self.frame)
        return {'FINISHED'}


class KIMODO_OT_SelectConstraintObject(Operator):
    """Select and focus the constraint's marker object in the viewport"""
    bl_idname = "kimodo.select_constraint_object"
    bl_label = "选择标记"

    index: bpy.props.IntProperty()

    def execute(self, context):
        s = context.scene.kimodo
        if not (0 <= self.index < len(s.motion_constraints)):
            return {'CANCELLED'}
        obj = s.motion_constraints[self.index].marker_object
        if not obj:
            self.report({'WARNING'}, "此约束没有链接对象。")
            return {'CANCELLED'}
        bpy.ops.object.select_all(action='DESELECT')
        obj.select_set(True)
        context.view_layer.objects.active = obj
        # Frame the selection
        for area in context.screen.areas:
            if area.type == 'VIEW_3D':
                region = next((r for r in area.regions if r.type == 'WINDOW'), None)
                if region:
                    with context.temp_override(area=area, region=region):
                        bpy.ops.view3d.view_selected()
                break
        return {'FINISHED'}


class KIMODO_OT_PreviewConstraintsJSON(Operator):
    """Build and show the constraints JSON in a Blender Text Editor block"""
    bl_idname = "kimodo.preview_constraints_json"
    bl_label = "预览约束 JSON"

    def execute(self, context):
        s = context.scene.kimodo
        try:
            json_str = cmod.constraints_to_json_string(
                s.motion_constraints,
                context.scene,
                kimodo_fps=s.kimodo_fps,
                auto_canonicalize=s.auto_canonicalize,
            )
        except Exception as e:
            self.report({'ERROR'}, f"构建 JSON 失败：{e}")
            return {'CANCELLED'}

        s.constraint_json_preview = json_str

        # Write to a text block in the Text Editor
        block_name = "kimodo_constraints.json"
        if block_name in bpy.data.texts:
            bpy.data.texts.remove(bpy.data.texts[block_name])
        text_block = bpy.data.texts.new(block_name)
        text_block.write(json_str)

        self.report({'INFO'}, f"约束 JSON 已写入文本编辑器：“{block_name}”。")
        return {'FINISHED'}


class KIMODO_OT_ClearConstraints(Operator):
    """Remove all Kimodo constraints from the list"""
    bl_idname = "kimodo.clear_constraints"
    bl_label = "清空全部约束"
    bl_options = {'REGISTER', 'UNDO'}

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        context.scene.kimodo.motion_constraints.clear()
        self.report({'INFO'}, "已清空全部约束。")
        return {'FINISHED'}


class KIMODO_OT_SetTo30FPS(Operator):
    """Set the scene frame rate to 30 FPS for Kimodo compatibility"""
    bl_idname = "kimodo.set_to_30fps"
    bl_label = "将场景设为 30 FPS"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        context.scene.render.fps = 30
        context.scene.render.fps_base = 1.0
        self.report({'INFO'}, "场景 FPS 已设为 30。")
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Armature picker utility
# ---------------------------------------------------------------------------

class KIMODO_OT_PickLatestKimodoArmature(Operator):
    """Auto-select the most recently generated Kimodo armature as the reuse target"""
    bl_idname = "kimodo.pick_latest_armature"
    bl_label  = "选择最近生成的骨架"

    def execute(self, context):
        s = context.scene.kimodo
        candidates = [
            obj for obj in context.scene.objects
            if obj.type == 'ARMATURE' and obj.get("kimodo_source")
        ]
        if not candidates:
            self.report({'WARNING'}, "场景中没有找到 Kimodo 生成的骨架。")
            return {'CANCELLED'}

        latest = max(candidates, key=lambda obj: obj.get("kimodo_creation_time", 0.0))
        s.reuse_armature = latest
        self.report({'INFO'}, f"复用目标已设为“{latest.name}”。")
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Generation history operators
# ---------------------------------------------------------------------------

class KIMODO_OT_ReimportFromHistory(Operator):
    """Re-import the BVH file from a history entry"""
    bl_idname = "kimodo.reimport_from_history"
    bl_label  = "重新导入 BVH"
    bl_options = {'REGISTER', 'UNDO'}

    index: IntProperty(default=0)

    def execute(self, context):
        s = context.scene.kimodo
        if not (0 <= self.index < len(s.generation_history)):
            self.report({'ERROR'}, "无效的历史记录索引。")
            return {'CANCELLED'}
        entry = s.generation_history[self.index]
        if not os.path.isfile(entry.bvh_path):
            self.report({'ERROR'}, f"BVH 文件已不存在：{entry.bvh_path}")
            return {'CANCELLED'}
        bpy.ops.kimodo.import_bvh('EXEC_DEFAULT', filepath=entry.bvh_path)
        self.report({'INFO'}, f"已重新导入“{os.path.basename(entry.bvh_path)}”。")
        return {'FINISHED'}


class KIMODO_OT_ClearHistory(Operator):
    """Clear all generation history entries"""
    bl_idname = "kimodo.clear_history"
    bl_label  = "清空历史"
    bl_options = {'REGISTER', 'UNDO'}

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        context.scene.kimodo.generation_history.clear()
        self.report({'INFO'}, "生成历史已清空。")
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Generate N Variations operator
# ---------------------------------------------------------------------------

class KIMODO_OT_GenerateVariations(Operator):
    """Generate N variations of the current prompt with different random seeds"""
    bl_idname = "kimodo.generate_variations"
    bl_label  = "生成变体"

    _timer       = None
    _thread      = None
    _seeds:  list = []
    _total:  int  = 0
    _current_idx: int = 0

    def invoke(self, context, event):
        s = context.scene.kimodo

        if not sc.is_running():
            self.report({'WARNING'}, "Kimodo 尚未运行，请先点击“启动 Kimodo”。")
            return {'CANCELLED'}
        if s.is_generating:
            self.report({'WARNING'}, "正在生成，请稍候。")
            return {'CANCELLED'}

        self._total       = s.num_variations
        self._seeds       = [random.randint(0, 2**31 - 1) for _ in range(self._total)]
        self._current_idx = 0

        s.is_generating = True
        _reset_state()
        _generation_state["running"] = True
        self._start_next(context, s)

        wm = context.window_manager
        self._timer = wm.event_timer_add(0.5, window=context.window)
        wm.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def _start_next(self, context, s):
        var_num = self._current_idx + 1
        seed    = self._seeds[self._current_idx]
        s.generation_progress = f"变体 {var_num}/{self._total}…"

        _reset_state()
        _generation_state["running"] = True

        self._thread = threading.Thread(
            target=self._run_one,
            args=(s.prompt, s.duration, seed, s.output_format, s.bvh_standard_tpose),
            daemon=True,
        )
        self._thread.start()

    def _run_one(self, prompt, duration, seed, fmt, bvh_standard_tpose):
        def progress_cb(msg):
            _generation_state["progress"] = msg

        success, result = sc.generate_motion(
            prompt=prompt,
            duration=duration,
            seed=seed,
            output_format=fmt,
            constraints_json=None,
            bvh_standard_tpose=bvh_standard_tpose,
            progress_callback=progress_cb,
        )
        _generation_state["success"] = success
        _generation_state["result"]  = result
        _generation_state["done"]    = True
        _generation_state["running"] = False

    def modal(self, context, event):
        s = context.scene.kimodo

        if event.type != 'TIMER':
            return {'PASS_THROUGH'}

        var_num = self._current_idx + 1
        s.generation_progress = (
            f"变体 {var_num}/{self._total}："
            + _generation_state.get("progress", "")
        )
        for area in context.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()

        if not _generation_state["done"]:
            return {'RUNNING_MODAL'}

        # One variation finished
        seed      = self._seeds[self._current_idx]
        var_num   = self._current_idx + 1

        if _generation_state["cancelled"]:
            context.window_manager.event_timer_remove(self._timer)
            s.is_generating = False
            s.generation_progress = "已取消"
            self.report({'INFO'},
                        f"变体生成已取消，已完成 {self._current_idx}/{self._total}。")
            return {'FINISHED'}

        if not _generation_state["success"]:
            context.window_manager.event_timer_remove(self._timer)
            s.is_generating = False
            s.generation_progress = f"变体 {var_num} 失败 ✗"
            self.report({'ERROR'}, f"变体 {var_num} 失败：{_generation_state['result']}")
            return {'FINISHED'}

        file_path = _generation_state["result"]
        ext = os.path.splitext(file_path)[1].lower()

        if ext == ".bvh":
            before = set(bpy.data.objects)
            try:
                _import_bvh(
                    filepath=file_path,
                    axis_forward='-Z', axis_up='Y',
                    target='ARMATURE',
                    global_scale=0.01,
                    frame_start=1,
                    use_fps_scale=False,
                    update_scene_fps=False,
                    update_scene_duration=True,
                    use_cyclic=False,
                    rotate_mode='NATIVE',
                )
            except RuntimeError as e:
                context.window_manager.event_timer_remove(self._timer)
                s.is_generating = False
                s.generation_progress = f"变体 {var_num} 失败 ✗"
                self.report({'ERROR'}, str(e))
                return {'FINISHED'}
            after   = set(bpy.data.objects)
            new_arm = next((o for o in after - before if o.type == 'ARMATURE'), None)
            if new_arm:
                new_arm.name = f"Kimodo_Var_{var_num}"
                new_arm["kimodo_source"]        = True
                new_arm["kimodo_creation_time"] = time.time()

        _push_history(s, s.prompt, seed, s.duration, file_path)

        self._current_idx += 1
        if self._current_idx < self._total:
            self._start_next(context, s)
            return {'RUNNING_MODAL'}

        # All done
        context.window_manager.event_timer_remove(self._timer)
        s.is_generating = False
        s.generation_progress = f"全部 {self._total} 个变体已完成 ✓"
        self.report({'INFO'}, f"已生成 {self._total} 个变体 ✓")
        return {'FINISHED'}

    def cancel(self, context):
        context.window_manager.event_timer_remove(self._timer)
        context.scene.kimodo.is_generating = False
        context.scene.kimodo.generation_progress = "已取消"
        _generation_state["running"] = False


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

_classes = [
    KIMODO_OT_StartKimodo,
    KIMODO_OT_StopKimodo,
    KIMODO_OT_Generate,
    KIMODO_OT_CancelGeneration,
    KIMODO_OT_ImportBVH,
    KIMODO_OT_ImportBVHAtFrame,
    KIMODO_OT_PickLatestKimodoArmature,
    KIMODO_OT_AutoMapBones,
    KIMODO_OT_AddBoneMapping,
    KIMODO_OT_RemoveBoneMapping,
    KIMODO_OT_ApplyRetargeting,
    KIMODO_OT_RemoveRetargeting,
    KIMODO_OT_BakeRetargeting,
    KIMODO_OT_SavePreset,
    KIMODO_OT_LoadPreset,
    KIMODO_OT_DeletePreset,
    KIMODO_OT_ExportPresetFile,
    KIMODO_OT_ImportPresetFile,
    # Segment operators
    KIMODO_OT_SelectSegment,
    KIMODO_OT_RemoveSegmentByIndex,
    KIMODO_OT_AddSegment,
    KIMODO_OT_RemoveSegment,
    KIMODO_OT_DuplicateSegment,
    KIMODO_OT_MoveSegmentUp,
    KIMODO_OT_MoveSegmentDown,
    KIMODO_OT_SyncSeeds,
    KIMODO_OT_GenerateSegment,
    KIMODO_OT_GenerateAllSegments,
    # History operators
    KIMODO_OT_ReimportFromHistory,
    KIMODO_OT_ClearHistory,
    KIMODO_OT_GenerateVariations,
    # Curve path operator
    KIMODO_OT_DrawFreehandCurve,
    KIMODO_OT_SampleCurveAsWaypoints,
    # Constraint operators
    KIMODO_OT_AddConstraint,
    KIMODO_OT_LinkExistingAsConstraint,
    KIMODO_OT_RemoveConstraint,
    KIMODO_OT_GotoConstraintFrame,
    KIMODO_OT_SelectConstraintObject,
    KIMODO_OT_PreviewConstraintsJSON,
    KIMODO_OT_ClearConstraints,
    KIMODO_OT_SetTo30FPS,
]


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
