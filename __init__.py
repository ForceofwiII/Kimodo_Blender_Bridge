"""
Kimodo Blender Bridge
=====================
在 Blender 中通过独立桥接进程运行 NVIDIA Kimodo，用文本生成角色动作，
并自动导入到当前场景。
"""

bl_info = {
    "name":        "Kimodo Blender Bridge",
    "author":      "Lewdineer",
    "version":     (1, 5, 5),
    "blender":     (4, 2, 0),
    "location":    "View3D › Sidebar (N-Panel) › Kimodo",
    "description": "使用 NVIDIA Kimodo AI 生成角色动作，并通过桥接进程导入 Blender。",
    "doc_url":     "https://github.com/ForceofwiII/Kimodo_Blender_Bridge",
    "tracker_url": "https://github.com/ForceofwiII/Kimodo_Blender_Bridge/issues",
    "category":    "Animation",
    "support":     "COMMUNITY",
}

import bpy

# Sub-modules (imported after bl_info for Blender's enable/disable system)
from . import properties, operators, ui_list, panels, constraints, timeline
from . import setup_operator
from . import subprocess_client as sc


def register():
    properties.register()
    operators.register()
    setup_operator.register()
    ui_list.register()
    panels.register()
    timeline.register()


def unregister():
    # Kill the bridge process so we don't leave orphaned GPU processes
    sc.stop()
    timeline.unregister()
    panels.unregister()
    ui_list.unregister()
    setup_operator.unregister()
    operators.unregister()
    properties.unregister()
