"""
Kimodo Blender Bridge — Properties
All bpy.props definitions: addon preferences, scene-level settings, bone mapping.
"""

import bpy
from bpy.props import (
    StringProperty, FloatProperty, IntProperty, BoolProperty,
    EnumProperty, CollectionProperty, PointerProperty, FloatVectorProperty,
)
from bpy.types import PropertyGroup, AddonPreferences


# ---------------------------------------------------------------------------
# Motion segment (one prompt + time range bar in the timeline)
# ---------------------------------------------------------------------------

# Default colours cycling for new segments
_SEGMENT_COLORS = [
    (0.20, 0.55, 0.90, 0.85),  # blue
    (0.20, 0.75, 0.45, 0.85),  # green
    (0.90, 0.60, 0.15, 0.85),  # orange
    (0.75, 0.25, 0.75, 0.85),  # purple
    (0.90, 0.25, 0.25, 0.85),  # red
    (0.15, 0.80, 0.80, 0.85),  # cyan
    (0.90, 0.85, 0.20, 0.85),  # yellow
]


def _on_end_frame_update(self, context):
    """When a segment's end_frame changes, push the next segment's start_frame forward."""
    s = context.scene.kimodo
    segs = s.motion_segments
    for i, seg in enumerate(segs):
        if seg == self and i + 1 < len(segs):
            next_seg = segs[i + 1]
            duration = next_seg.end_frame - next_seg.start_frame
            next_seg.start_frame = self.end_frame + 1
            next_seg.end_frame = next_seg.start_frame + duration
            
            break
    
    # Update scene frame_end if any segment's end_frame is larger
    if segs:
        max_end_frame = max(seg.end_frame for seg in segs)
        if max_end_frame > context.scene.frame_end:
            context.scene.frame_end = max_end_frame


class KIMODO_MotionSegment(PropertyGroup):
    """One motion segment: a text prompt mapped to a frame range."""

    prompt: StringProperty(
        name="提示词",
        description="此动作片段的文本描述；建议使用英文以获得更稳定的生成效果",
        default="a person walks forward",
    )
    start_frame: IntProperty(
        name="起始帧",
        description="此动作片段的第一帧",
        default=1,
        min=0,
    )
    end_frame: IntProperty(
        name="结束帧",
        description="此动作片段的最后一帧",
        default=60,
        min=1,
        update=_on_end_frame_update,
    )
    model_type: EnumProperty(
        name="模型",
        items=[
            ("smpl",  "SOMA / SMPL",  "标准人体骨架"),
            #("smplx", "SMPL-X",       "包含手部和面部的扩展人体骨架"),
        ],
        default="smpl",
    )
    seed: IntProperty(
        name="种子",
        description="随机种子（-1 = 每次随机）",
        default=-1,
        min=-1,
    )
    color: FloatVectorProperty(
        name="颜色",
        description="时间轴中该片段条的颜色",
        subtype='COLOR_GAMMA',
        size=4,
        min=0.0, max=1.0,
        default=(0.20, 0.55, 0.90, 0.85),
    )
    enabled: BoolProperty(
        name="启用",
        description="生成时包含此片段",
        default=True,
    )
    # State tracking
    last_bvh_path: StringProperty(default="")
    generated: BoolProperty(default=False)


# ---------------------------------------------------------------------------
# Constraint item (one Kimodo motion constraint)
# ---------------------------------------------------------------------------

class KIMODO_ConstraintItem(PropertyGroup):
    """A single Kimodo constraint: a Blender object at a frame defines a spatial goal."""

    constraint_type: EnumProperty(
        name="类型",
        description="Kimodo 约束类型",
        items=[
            ('root2d',      "根节点路径点",      "二维地面位置（XZ）。把空物体放在希望角色根节点经过的位置。",                  'EMPTY_ARROWS',   0),
            ('fullbody',    "全身姿态",          "全身关键帧。在此帧把骨架摆成希望角色达到的姿态。",                            'ARMATURE_DATA',  1),
            ('left_hand',   "左手",              "左手腕/手部末端目标。把空物体放在期望的手部位置。",                          'VIEW_PAN',       2),
            ('right_hand',  "右手",              "右手腕/手部末端目标。",                                                     'VIEW_PAN',       3),
            ('left_foot',   "左脚",              "左脚/脚跟末端目标。把空物体放在期望的脚部位置。",                            'SNAP_FACE',      4),
            ('right_foot',  "右脚",              "右脚/脚跟末端目标。",                                                       'SNAP_FACE',      5),
        ],
        default='root2d',
    )
    frame: IntProperty(
        name="帧",
        description="此约束生效的 Blender 帧",
        default=1,
        min=0,
    )
    marker_object: PointerProperty(
        name="对象",
        description="在视口中定义空间约束的空物体或骨架",
        type=bpy.types.Object,
    )
    enabled: BoolProperty(
        name="启用",
        description="生成时包含此约束",
        default=True,
    )
    # root2d extras
    include_heading: BoolProperty(
        name="包含朝向",
        description="同时约束此路径点处的面向方向",
        default=False,
    )
    heading_angle: FloatProperty(
        name="朝向（度）",
        description="期望面向方向，单位为度（0 = Blender 中 +Y 向前 / Kimodo 中 -Z 向前）",
        default=0.0,
        subtype='ANGLE',
    )
    # display label
    label: StringProperty(
        name="标签",
        description="此约束的可选备注标签",
        default="",
    )


# ---------------------------------------------------------------------------
# Generation history entry
# ---------------------------------------------------------------------------

class KIMODO_HistoryEntry(PropertyGroup):
    """One entry in the rolling generation history."""
    prompt: StringProperty(name="提示词", default="")
    seed: IntProperty(name="种子", default=0)
    duration: FloatProperty(name="时长", default=5.0)
    bvh_path: StringProperty(name="BVH 路径", default="")
    timestamp: StringProperty(name="时间戳", default="")


# ---------------------------------------------------------------------------
# Bone mapping item (one row in the UIList)
# ---------------------------------------------------------------------------

def _on_inherit_rotation_update(self, context):
    """Push this entry's inherit-rotation override onto the target bone immediately."""
    arm = context.scene.kimodo.target_armature
    if not arm or not self.target_bone:
        return
    bone = arm.data.bones.get(self.target_bone)
    if bone is not None:
        try:
            bone.use_inherit_rotation = self.inherit_rotation
        except Exception:
            pass


class KIMODO_BoneMappingItem(PropertyGroup):
    """A single source → target bone pair for retargeting."""
    source_bone: StringProperty(
        name="源骨骼",
        description="Kimodo 生成骨架中的骨骼名称",
        default="",
    )
    target_bone: StringProperty(
        name="目标骨骼",
        description="目标角色骨架中的骨骼名称",
        default="",
    )
    enabled: BoolProperty(
        name="启用",
        description="重定向时包含此骨骼",
        default=True,
    )
    retarget_mode: EnumProperty(
        name="模式",
        description="此骨骼对的驱动方式",
        items=[
            ("COPY_ROTATION",    "复制旋转",          "仅复制旋转；根骨骼还会复制位置"),
            ("COPY_TRANSFORMS",  "复制变换",          "同时复制位置、旋转和缩放"),
            ("CHILD_OF",         "子级关联",          "完整父子关系；保留静止姿态偏移"),
            ("CHILD_OF_ROTATION", "子级关联（仅旋转）", "仅启用旋转的 Child Of 约束（不复制位置或缩放）"),
        ],
        default="CHILD_OF",
    )
    inherit_rotation: BoolProperty(
        name="继承旋转",
        description=(
            "覆盖目标骨骼的“继承旋转”属性。取消勾选时，此骨骼不会从父级继承旋转。"
            "切换时立即应用，并会在点击“应用约束”时再次应用"
        ),
        default=True,
        update=_on_inherit_rotation_update,
    )


# ---------------------------------------------------------------------------
# Scene-level settings
# ---------------------------------------------------------------------------

class KIMODO_SceneSettings(PropertyGroup):
    """Stored on bpy.context.scene.kimodo — all per-scene settings."""

    # --- Connection (subprocess bridge) ---
    show_advanced_connection: BoolProperty(
        name="高级",
        description=(
            "显示高级连接设置：Python 可执行文件路径、HuggingFace Token "
            "和 Kimodo 虚拟环境安装位置"
        ),
        default=False,
    )
    python_executable: StringProperty(
        name="Python",
        description=(
            "已安装 Kimodo 的 Python 可执行文件路径（或 venv/conda 环境根目录）。"
            "留空则从 PATH 自动检测。"
        ),
        default="",
        subtype='FILE_PATH',
    )
    kimodo_model: EnumProperty(
        name="模型",
        description="桥接进程要加载的 Kimodo 模型",
        items=[
            ("Kimodo-SOMA-RP-v1",  "Kimodo SOMA",   "标准 SOMA 人体骨架（推荐）"),
            ("Kimodo-SMPLX-RP-v1", "Kimodo SMPL-X（暂不支持）", "包含手部和面部的扩展人体模型"),
            ("Kimodo-G1-RP-v1",    "Kimodo G1（暂不支持）",     "Unitree G1 机器人骨架"),
        ],
        default="Kimodo-SOMA-RP-v1",
    )
    use_offload: BoolProperty(
        name="启用内存卸载",
        description="为低显存 GPU 启用 CPU/RAM/VRAM 卸载（例如 <= 8GB）",
        default=True,
    )
    connection_status: StringProperty(
        name="状态",
        default="未启动",
    )
    is_connected: BoolProperty(default=False)

    # --- Generation ---
    model_type: EnumProperty(
        name="模型",
        description="要使用的 Kimodo 骨架/模型",
        items=[
            ("smpl",  "SOMA / SMPL",  "标准人体骨架（SOMA），适合大多数用途。"),
            #("smplx", "SMPL-X",       "包含手部和面部的扩展 SMPL。需要安装 Kimodo-SMPLX。"),
        ],
        default="smpl",
    )
    prompt: StringProperty(
        name="提示词",
        description="要生成的动作文本描述；建议使用英文以获得更稳定的生成效果",
        default="a person walks forward",
    )
    duration: FloatProperty(
        name="时长（秒）",
        description="生成动作的长度，单位为秒",
        default=5.0,
        min=1.0,
        max=30.0,
        step=50,
    )
    seed: IntProperty(
        name="种子",
        description="随机种子（-1 = 每次随机）",
        default=-1,
        min=-1,
    )
    output_format: EnumProperty(
        name="格式",
        description="Kimodo 导出的文件格式",
        items=[
            ("bvh", "BVH",  "标准动作捕捉格式。Blender 可原生导入。"),
            ("npz", "NPZ",  "Kimodo 原生格式（需要手动导入）。"),
        ],
        default="bvh",
    )
    bvh_standard_tpose: BoolProperty(
        name="使用标准 T 姿势",
        description="导出 BVH 时使用标准 T 姿势静止姿态，而不是 BONES-SEED 姿态（仅 SOMA 模型）",
        default=True,
    )
    reuse_armature: PointerProperty(
        name="复用骨架",
        description=(
            "把生成动作应用到此骨架，而不是创建新骨架。"
            "会保留已经指向它的重定向约束。"
            "留空则每次创建新骨架。"
        ),
        type=bpy.types.Object,
        poll=lambda self, obj: obj.type == 'ARMATURE',
    )

    # Generation state (used by the modal operator)
    is_generating: BoolProperty(default=False)
    generation_progress: StringProperty(default="")
    last_bvh_path: StringProperty(
        name="最近 BVH 路径",
        description="最近一次导入的动作文件路径",
        default="",
    )

    # --- Motion Segments (timeline bars) ---
    motion_segments: CollectionProperty(type=KIMODO_MotionSegment)
    segment_index: IntProperty(
        name="当前片段",
        description="当前选中的动作片段",
        default=0,
    )
    # Which segment is currently being generated (for multi-generate progress)
    generating_segment_index: IntProperty(default=-1)

    # --- Retargeting ---
    source_armature: PointerProperty(
        name="源骨架",
        description="Kimodo 生成的骨架（从 BVH 导入）",
        type=bpy.types.Object,
        poll=lambda self, obj: obj.type == 'ARMATURE',
    )
    target_armature: PointerProperty(
        name="目标骨架",
        description="要由动作驱动的角色骨架",
        type=bpy.types.Object,
        poll=lambda self, obj: obj.type == 'ARMATURE',
    )
    bone_mappings: CollectionProperty(type=KIMODO_BoneMappingItem)
    bone_mapping_index: IntProperty(default=0)
    retarget_root_bone: StringProperty(
        name="根骨骼（目标）",
        description="目标骨架上的根/髋部骨骼（接收位置 + 旋转）",
        default="",
    )
    bake_start_frame: IntProperty(name="起始帧", default=1, min=0)
    bake_end_frame: IntProperty(name="结束帧", default=250, min=1)

    # --- Motion Constraints ---
    motion_constraints: CollectionProperty(type=KIMODO_ConstraintItem)
    constraint_index: IntProperty(default=0)
    kimodo_fps: FloatProperty(
        name="Kimodo FPS",
        description="Kimodo 生成动作使用的帧率（默认 30）。"
                    "用于把 Blender 帧号转换为 Kimodo 帧索引。",
        default=30.0,
        min=1.0,
        max=120.0,
    )
    auto_canonicalize: BoolProperty(
        name="自动归一原点",
        description="自动偏移所有约束位置，使最早的路径点落在 Kimodo 的 (0,0) 原点。",
        default=False,
    )
    constraint_json_preview: StringProperty(
        name="约束 JSON",
        description="最近生成的约束 JSON（只读预览）",
        default="",
    )

    # --- Multi-segment generation ---
    num_transition_frames: IntProperty(
        name="过渡帧数",
        description="多提示词生成时片段之间的混合帧数",
        default=5,
        min=1,
        max=30,
    )

    # --- Generation history ---
    generation_history: CollectionProperty(type=KIMODO_HistoryEntry)
    history_index: IntProperty(name="当前历史记录", default=0)
    history_expanded: BoolProperty(
        name="显示历史",
        description="展开/折叠生成历史列表",
        default=False,
    )

    # --- Variations ---
    num_variations: IntProperty(
        name="变体数量",
        description="要生成的随机种子变体数量",
        default=3,
        min=2,
        max=5,
    )

    # --- Curve path sampling ---
    path_curve: PointerProperty(
        name="路径曲线",
        description="用于采样根节点 XZ 路径点的贝塞尔或 NURBS 曲线",
        type=bpy.types.Object,
        poll=lambda self, obj: obj.type == 'CURVE',
    )
    path_waypoints: IntProperty(
        name="路径点",
        description="从曲线中均匀采样的路径点数量",
        default=8,
        min=2,
        max=30,
    )
    path_start_frame: IntProperty(
        name="起始帧",
        description="第一个路径点所在的时间轴帧",
        default=1,
        min=0,
    )
    path_end_frame: IntProperty(
        name="结束帧",
        description="最后一个路径点所在的时间轴帧",
        default=90,
        min=1,
    )

    # Preset name for saving
    preset_name: StringProperty(
        name="预设名称",
        description="保存/加载骨骼映射预设使用的名称",
        default="my_rig",
    )


# ---------------------------------------------------------------------------
# Addon preferences
# ---------------------------------------------------------------------------

class KIMODO_AddonPreferences(AddonPreferences):
    bl_idname = __package__

    saved_presets: StringProperty(
        name="已保存预设",
        description="所有已保存骨骼映射预设的 JSON 数据",
        default="{}",
    )

    hf_token: StringProperty(
        name="HuggingFace Token",
        description=(
            "可选的 HuggingFace 访问令牌（hf_...）。可避免模型下载时受到速率限制。"
            "可在 "
            "huggingface.co/settings/tokens"
            " 获取免费只读令牌。"
        ),
        default="",
        subtype='PASSWORD',
    )

    system_python_override: StringProperty(
        name="系统 Python",
        description=(
            "用于创建 Kimodo venv 的 Python 3.10–3.12 可执行文件路径"
            "（python3.12 / python.exe）。请选择可执行文件，不要选择文件夹。"
            "留空则从 PATH 自动检测。"
        ),
        default="",
        subtype='FILE_PATH',   # renders as text field + file-browser button in Blender
    )

    install_location: StringProperty(
        name="安装位置",
        description=(
            "创建并查找 Kimodo 虚拟环境的文件夹。"
            "该设置会保存在插件偏好中，以便 Blender 重启和切换场景后仍能记住。"
            "留空则使用默认 ~/.kimodo-venv。"
        ),
        default="",
        subtype='DIR_PATH',
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

_classes = [
    KIMODO_MotionSegment,
    KIMODO_ConstraintItem,
    KIMODO_BoneMappingItem,
    KIMODO_HistoryEntry,
    KIMODO_SceneSettings,
    KIMODO_AddonPreferences,
]


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.kimodo = PointerProperty(type=KIMODO_SceneSettings)


def unregister():
    del bpy.types.Scene.kimodo
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
