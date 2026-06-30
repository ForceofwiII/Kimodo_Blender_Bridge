# Kimodo Blender Bridge（中文汉化版）

这是一个 Blender 插件，可以通过 [NVIDIA Kimodo](https://github.com/nv-tlabs/kimodo) 根据文本提示生成 AI 角色动作，并自动导入到当前 Blender 场景中。插件通过独立 Python 虚拟环境运行 Kimodo，避免把 PyTorch 和模型直接塞进 Blender 自带 Python。

## Star History

<a href="https://www.star-history.com/?repos=ForceofwiII%2FKimodo_Blender_Bridge&type=date&legend=top-left">
 <picture>
   <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/chart?repos=ForceofwiII/Kimodo_Blender_Bridge&type=date&theme=dark&legend=top-left" />
   <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/chart?repos=ForceofwiII/Kimodo_Blender_Bridge&type=date&legend=top-left" />
   <img alt="Star History Chart" src="https://api.star-history.com/chart?repos=ForceofwiII/Kimodo_Blender_Bridge&type=date&legend=top-left" />
 </picture>
</a>

## 链接 / 视频教程

YouTube 教程：https://youtu.be/nbiaS43Ncng

Superhive：https://superhivemarket.com/products/kimodoblenderbridge

---

## 已测试环境

**Blender 5.1 / 4.4**

**Arch Linux RTX 3090 Python 3.12**

**Windows 11 RTX 1080 Python 3.12**

**Windows 11 RTX 5070 Python 3.13**

<img width="1237" height="1257" alt="image" src="https://github.com/user-attachments/assets/71a1666d-a460-40eb-af23-1dbb8ab750cb" />

---

## 工作原理

Blender 内置 Python 通常不适合直接加载 PyTorch / Kimodo。这个插件使用双进程桥接：

```text
Blender（插件）              Kimodo 虚拟环境
  subprocess_client.py  ───▶  bridge_server.py
                        ◀───  模型只加载一次，之后处理生成请求
```

桥接服务启动后会加载 Kimodo 模型，并通过 stdin/stdout 接收 JSON 请求。Blender 端保持响应，动作生成在后台线程中执行。

---

## 环境要求

| 要求 | 说明 |
|---|---|
| Blender 4.0+ | 已在 Blender 5.1（Windows / Arch Linux）测试 |
| Python 3.10–3.12 | 用系统 Python 创建受管理的虚拟环境 |
| NVIDIA GPU | 建议 8 GB+ 显存，16 GB+ 更稳 |
| CUDA | 需要与 PyTorch 构建匹配，自动安装默认选择合适 CUDA wheel |
| 约 10 GB 磁盘空间 | 用于虚拟环境、模型权重和 LLM2Vec 编码器 |

显存较低时，可以在激活 Kimodo 虚拟环境后单独运行：

```bash
kimodo_textencoder --device cpu
```

这会把文本编码器放到 CPU 上，释放几 GB 显存。

---

## 安装

从 v1.2.0 起，Kimodo 可由插件自动安装，不需要手动敲终端。插件也兼容 Blender 4.2+ 扩展系统（包含 `blender_manifest.toml`）。

### 1. 安装 Blender 插件

1. 下载或克隆本仓库（右上角 **Code → Download ZIP**）。
2. 打开 Blender → **编辑 → 偏好设置 → 插件 → 从磁盘安装...**
3. 选择下载的 zip，并启用 **Kimodo Blender Bridge**。

### 2. 点击“自动安装 Kimodo”

在 3D 视图中按 `N` 打开侧栏，进入 **Kimodo** 标签，展开 **连接** 面板，然后点击 **自动安装 Kimodo**。

安装器会自动完成：

- 创建受管理的 Python 虚拟环境；
- 安装 PyTorch、Kimodo 依赖和 [Aero-Ex 离线分支](https://github.com/Aero-Ex/kimodo)；
- 下载 LLM2Vec 文本编码器并修补为离线可用；
- 下载 `Kimodo-SOMA-RP-v1` 模型权重；
- 安装完成后自动写入插件的 Python 路径。

安装进度会显示在 **连接** 面板中。完整日志会输出到系统控制台；Windows 可在 Blender 中使用 **Window → Toggle System Console** 打开。

> 需要系统中已有 Python 3.10–3.12、网络连接，以及约 10 GB 可用磁盘空间。首次安装完成后，Kimodo 可以离线运行。

### 手动安装（高级）

如果你已经在自己的虚拟环境中安装好了 Kimodo，可以跳过自动安装，在 **连接** 面板的 **Kimodo Python** 字段中填入该虚拟环境的 Python 可执行文件路径。

---

## 快速开始

### 根据文本生成动作

1. 在 **连接** 面板点击 **启动 Kimodo**。状态会先显示模型加载中，加载完成后显示就绪。首次加载通常需要 10–60 秒。
2. 打开 **动作片段** 面板。
3. 点击 **添加** 创建一个片段，输入英文提示词（例如 `a person jogs in a circle`），并设置帧范围。
4. 点击 **生成动作**。如果启用了多个片段，插件会一次性发送给 Kimodo，并在片段之间生成平滑过渡。
5. 场景中会出现带有生成动作的 `Kimodo_Source` 骨架。

> Kimodo 固定以 30 FPS 生成动作。如果当前场景不是 30 FPS，插件会提示你点击 **设为 30 FPS**。

### 使用多个动作片段

每个片段都是一个独立文本提示词，并映射到一段帧范围。启用的片段会按顺序合成一个连续动作：

- 片段按列表顺序生成。
- 第一个片段之后，每个片段的起始帧会自动跟随上一个片段的结束帧。
- 使用复制按钮可以快速复制片段并放到后面。
- 使用上移 / 下移按钮调整片段顺序。

<img width="2676" height="1181" alt="image" src="https://github.com/user-attachments/assets/a5d336e9-f32f-44c7-9aca-a09983e869d6" />

### 重定向到自己的角色骨架

生成动作后，你可以把 Kimodo 源骨架的动画重定向到任意角色骨架：

1. 打开 **重定向** 面板。
2. 将 **源骨架** 设置为 `Kimodo_Source`，将 **目标骨架** 设置为你的角色骨架。
3. 点击 **自动匹配骨骼**，插件会根据名称模糊匹配骨骼。
4. 检查映射关系，按需启用 / 禁用骨骼对，并选择每根骨骼的重定向模式。建议先调整角色骨架缩放，使其接近源骨架，然后用 `Ctrl+A` 应用缩放。
5. 选择约束类型，例如 Child Of、Copy Rotation、Copy Transforms 等。
6. 点击 **应用约束**，插件会为目标骨架添加约束驱动。
7. 满意后执行 **烘焙动画**，把约束结果写成关键帧，并移除 Kimodo 约束。

可以使用 **保存预设 / 加载预设** 保存某个角色的骨骼映射，之后重复使用。

<img width="1233" height="839" alt="image" src="https://github.com/user-attachments/assets/d76290db-7662-4223-9cd6-7083f89b35ca" />

### 动作约束

你可以给 Kimodo 指定空间约束，让生成动作经过特定位置：

| 约束 | 控制内容 |
|---|---|
| Root XZ | 角色根节点在地面平面上的位置 |
| Full-Body | 全身关节姿态关键帧 |
| Left / Right Hand | 左右手腕末端位置 |
| Left / Right Foot | 左右脚 / 脚跟末端位置 |

添加约束的基本流程：

1. 将 3D 光标移动到目标位置，或选择一个骨架 / 对象。
2. 把时间轴切到目标帧。
3. 在 **动作约束** 面板点击对应约束类型。

**自动归一原点** 默认关闭。开启后，插件会整体偏移约束位置，让最早的根节点路径点落在 Kimodo 世界原点，方便你在场景任意位置编辑约束。

---

## 面板说明

| 面板 | 内容 |
|---|---|
| **连接** | Kimodo Python 路径、模型选择、启动 / 停止桥接进程 |
| **动作片段** | 提示词列表、帧范围、多片段生成 |
| **快速生成** | 单提示词生成，包含时长和种子控制 |
| **动作约束** | 生成动作的空间路径点和末端约束 |
| **重定向** | 骨骼映射、应用约束、烘焙动画 |
| **帮助** | 快速流程和常见提示 |

---

## 常见问题

**桥接无法启动 / 显示启动失败**

- 查看系统控制台中的 `[Kimodo Bridge]` 日志，完整 Python / PyTorch 错误会打印在那里。
- 确认 Python 路径指向已经安装 Kimodo 的虚拟环境。
- 如果自动安装中途失败，点击 **重试安装**，插件会清理未完成的虚拟环境并重新开始。

**CUDA 显存不足**

- 缩短生成时长，或减少一次生成的片段数量。
- 使用上方的 `kimodo_textencoder --device cpu` 方式把文本编码器放到 CPU。

**重定向后的角色姿态不对**

- 尝试给不同骨骼切换重定向模式，例如 Copy Rotation、Copy Transforms、Child Of。
- 确保源骨架和目标骨架都处于正确的静止姿态，并且目标骨架已经应用缩放。

**导入动画帧数对不上**

- Kimodo 固定生成 30 FPS 动作。请使用插件提示的 **设为 30 FPS** 按钮，将场景帧率切到 30。

---

## 文件概览

| 文件 | 作用 |
|---|---|
| `__init__.py` | Blender 插件入口 |
| `bridge_server.py` | 子进程服务：加载 Kimodo 并处理生成请求 |
| `subprocess_client.py` | Blender 侧桥接进程管理 |
| `operators.py` | 所有 `bpy.ops.kimodo.*` 操作 |
| `properties.py` | 场景属性和插件偏好设置 |
| `panels.py` | N 面板 UI |
| `constraints.py` | 将 Blender 约束标记转换成 Kimodo JSON |
| `retarget.py` | 应用 / 烘焙重定向约束 |
| `ui_list.py` | 骨骼映射面板使用的 UIList |
| `setup_operator.py` | Kimodo 和依赖的一键自动安装器 |

---

## 许可证

见 [LICENSE](LICENSE)。
