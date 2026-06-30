#!/usr/bin/env python
"""
Kimodo Blender Bridge Server

Persistent process that runs under the Kimodo Python environment.
Loads the model once, then handles JSON generation requests from stdin.

stdin  → one JSON line per request:   {"cmd": "generate"|"ping"|"quit", ...}
stdout → one JSON line per message:   {"status": "loading"|"ready"|"progress"|"done"|"error", ...}

stderr is left alone (Kimodo/PyTorch logging goes there).
"""

import json
import os
import sys
import tempfile
import traceback

# On Windows with a non-UTF-8 locale (e.g. cp1251 Cyrillic), third-party
# code may print() error messages containing characters the system codec
# can't encode, crashing before we can report the real error.  Reconfigure
# stdio to UTF-8 so those prints succeed over the pipe.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def _out(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------

def _save_output(output, output_format, model, device, standard_tpose, prefix="kimodo_"):
    """Save model output dict to a temp file; returns the saved file path."""
    import torch
    from kimodo.exports.bvh import save_motion_bvh
    from kimodo.exports.motion_io import save_kimodo_npz
    from kimodo.skeleton import SOMASkeleton30, global_rots_to_local_rots

    skeleton   = model.skeleton
    want_bvh   = (output_format == "bvh")
    can_do_bvh = False

    if want_bvh:
        if isinstance(skeleton, SOMASkeleton30):
            skeleton = skeleton.somaskel77.to(device)
        can_do_bvh = hasattr(skeleton, "name") and "somaskel" in skeleton.name
        if not can_do_bvh:
            _out({"status": "progress",
                  "message": "此模型不支持 BVH，已改用 NPZ。"})

    suffix = ".bvh" if can_do_bvh else ".npz"
    fd, out_path = tempfile.mkstemp(suffix=suffix, prefix=prefix)
    os.close(fd)

    fps = model.fps
    if can_do_bvh:
        joints_pos = torch.from_numpy(output["posed_joints"][0]).to(device)
        joints_rot = torch.from_numpy(output["global_rot_mats"][0]).to(device)
        local_rots = global_rots_to_local_rots(joints_rot, skeleton)
        root_pos   = joints_pos[:, skeleton.root_idx, :]
        save_motion_bvh(
            out_path, local_rots, root_pos,
            skeleton=skeleton, fps=fps, standard_tpose=standard_tpose,
        )
    else:
        single = {
            k: (v[0] if hasattr(v, "shape") and v.ndim > 0 and v.shape[0] == 1 else v)
            for k, v in output.items()
        }
        save_kimodo_npz(out_path, single)

    return out_path


def _load_constraints(constraints_json, model):
    """Write constraints JSON to a temp file and load them; returns constraint_lst."""
    from kimodo.constraints import load_constraints_lst

    constraint_lst = []
    tmp_con = None
    if constraints_json:
        try:
            fd, tmp_con = tempfile.mkstemp(suffix=".json", prefix="kimodo_con_")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(constraints_json)
            constraint_lst = load_constraints_lst(tmp_con, model.skeleton)
        except Exception as exc:
            _out({"status": "progress",
                  "message": f"警告：已跳过约束（{exc}）"})
        finally:
            if tmp_con and os.path.exists(tmp_con):
                try:
                    os.unlink(tmp_con)
                except OSError:
                    pass
    return constraint_lst


def _generate(req: dict, model, device: str) -> None:
    from kimodo.tools import seed_everything

    prompt: str        = req.get("prompt", "A person walks forward.")
    duration: float    = float(req.get("duration", 5.0))
    seed               = req.get("seed")          # None means random
    output_format: str = req.get("output_format", "bvh")
    constraints_json   = req.get("constraints_json")  # JSON string or None
    diffusion_steps    = int(req.get("diffusion_steps", 100))
    standard_tpose     = bool(req.get("bvh_standard_tpose", False))

    # Normalise prompt
    text = prompt.strip()
    if not text.endswith("."):
        text += "."

    fps: float = model.fps
    num_frames = max(1, int(duration * fps))

    if seed is not None:
        seed_everything(int(seed))

    constraint_lst = _load_constraints(constraints_json, model)

    if getattr(model, "offload_enabled", False) and getattr(model, "text_encoder", None) and type(model.text_encoder).__name__ == "CachedTextEncoder":
        try:
            from kimodo.demo.memory_manager import manager as memory_manager
            prompt_set = {text, "", " "}
            prompt_set = {p for p in prompt_set if p.strip()}
            if prompt_set:
                _out({"status": "progress", "message": "正在预编码文本提示词（CPU 卸载）..."})
                model.text_encoder.reload()
                import torch
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
                import gc
                gc.collect()
                model.text_encoder.prewarm(list(prompt_set))
                memory_manager.purge_encoder_completely()
                memory_manager.touch_and_move(model.model_name, device)
        except Exception as exc:
            _out({"status": "progress", "message": f"警告：卸载预热失败（{exc}）"})

    _out({"status": "progress",
          "message": f"正在运行扩散生成（{diffusion_steps} 步）…"})

    output = model(
        [text],
        [num_frames],
        constraint_lst=constraint_lst,
        num_denoising_steps=diffusion_steps,
        num_samples=1,
        multi_prompt=True,
        num_transition_frames=5,
        post_processing=True,
        return_numpy=True,
    )

    _out({"status": "progress", "message": "正在保存输出文件…"})
    out_path = _save_output(output, output_format, model, device, standard_tpose)
    _out({"status": "done", "path": out_path})


def _generate_multi(req: dict, model, device: str) -> None:
    """Generate a single continuous motion from multiple prompts in one model call."""
    from kimodo.tools import seed_everything

    prompts        = req.get("prompts", [])
    durations      = req.get("durations", [])
    seed           = req.get("seed")
    seeds          = req.get("seeds")
    output_format  = req.get("output_format", "bvh")
    diffusion_steps = int(req.get("diffusion_steps", 100))
    standard_tpose = bool(req.get("bvh_standard_tpose", False))
    num_transition_frames = int(req.get("num_transition_frames", 5))

    if not prompts or not durations or len(prompts) != len(durations):
        _out({"status": "error",
              "message": "generate_multi 需要非空且长度一致的 prompts 和 durations 列表。"})
        return

    # Normalise all prompts
    texts = [p.strip() + ("" if p.strip().endswith(".") else ".") for p in prompts]

    fps: float = model.fps
    num_frames_list = [max(1, int(d * fps)) for d in durations]

    if seed is not None:
        seed_everything(int(seed))

    constraints_json = req.get("constraints_json")
    constraint_lst = _load_constraints(constraints_json, model)

    if getattr(model, "offload_enabled", False) and getattr(model, "text_encoder", None) and type(model.text_encoder).__name__ == "CachedTextEncoder":
        try:
            from kimodo.demo.memory_manager import manager as memory_manager
            prompt_set = set(texts)
            prompt_set.add("")
            prompt_set.add(" ")
            prompt_set = {p for p in prompt_set if p.strip()}
            if prompt_set:
                _out({"status": "progress", "message": "正在预编码多个文本提示词（CPU 卸载）..."})
                model.text_encoder.reload()
                import torch
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
                import gc
                gc.collect()
                model.text_encoder.prewarm(list(prompt_set))
                memory_manager.purge_encoder_completely()
                memory_manager.touch_and_move(model.model_name, device)
        except Exception as exc:
            _out({"status": "progress", "message": f"警告：卸载预热失败（{exc}）"})

    n = len(texts)
    _out({"status": "progress",
          "message": f"正在运行多提示词扩散生成（{n} 个片段，{diffusion_steps} 步）…"})

    output = model(
        texts,
        num_frames_list,
        constraint_lst=constraint_lst,
        num_denoising_steps=diffusion_steps,
        num_samples=1,
        multi_prompt=True,
        num_transition_frames=num_transition_frames,
        post_processing=True,
        return_numpy=True,
        seeds=seeds,
    )

    _out({"status": "progress", "message": "正在保存合并后的输出文件…"})
    out_path = _save_output(output, output_format, model, device, standard_tpose,
                            prefix="kimodo_multi_")
    _out({"status": "done", "path": out_path})


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Kimodo Blender 桥接服务")
    parser.add_argument("--model",  default="Kimodo-SOMA-RP-v1",
                        help="Kimodo 模型名称（例如 Kimodo-SOMA-RP-v1）")
    parser.add_argument("--device", default=None,
                        help="覆盖计算设备（例如 cuda:0、cpu）")
    parser.add_argument("--offload", action="store_true",
                        help="为低内存环境启用 CPU / 磁盘卸载")
    args = parser.parse_args()

    _out({"status": "loading", "message": "正在导入 Kimodo…"})

    try:
        import torch
        from kimodo import load_model
    except ImportError as exc:
        _out({"status": "error",
              "message": (
                  f"当前 Python 环境中没有找到 Kimodo：{exc}\n"
                  "请确认插件指向了正确的 Python 可执行文件。"
              )})
        sys.exit(1)

    device = args.device or ("cuda:0" if torch.cuda.is_available() else "cpu")
    _out({"status": "loading",
          "message": f"正在 {device} 上加载 {args.model}…"})

    try:
        model = load_model(args.model, device=device)
        model.model_name = args.model
        model.offload_enabled = bool(args.offload)
        
        if args.offload:
            try:
                from kimodo.demo.memory_manager import manager as memory_manager
                from kimodo.demo.embedding_cache import CachedTextEncoder
                
                memory_manager.offload_enabled = True
                memory_manager.register_model(args.model, model)
                if hasattr(model, "text_encoder"):
                    memory_manager.register_encoder(model.text_encoder)
                    model.text_encoder = CachedTextEncoder(model.text_encoder, model_name=args.model)
            except Exception as exc:
                print(f"[Bridge Server] 设置 MemoryManager 时出现警告：{exc}", file=sys.stderr)
    except Exception as exc:
        _out({"status": "error",
              "message": f"模型加载失败：{exc}\n{traceback.format_exc()}"})
        sys.exit(1)

    _out({"status": "ready",
          "model": args.model, "device": device, "fps": model.fps})

    # Main request loop — one JSON object per line
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue

        try:
            req = json.loads(raw)
        except json.JSONDecodeError as exc:
            _out({"status": "error", "message": f"JSON 格式错误：{exc}"})
            continue

        cmd = req.get("cmd", "")

        if cmd == "ping":
            _out({"status": "pong"})

        elif cmd == "generate":
            try:
                _generate(req, model, device)
            except Exception as exc:
                _out({"status": "error",
                      "message": str(exc),
                      "traceback": traceback.format_exc()})

        elif cmd == "generate_multi":
            try:
                _generate_multi(req, model, device)
            except Exception as exc:
                _out({"status": "error",
                      "message": str(exc),
                      "traceback": traceback.format_exc()})

        elif cmd == "quit":
            _out({"status": "bye"})
            break

        else:
            _out({"status": "error", "message": f"未知命令：{cmd!r}"})


if __name__ == "__main__":
    main()
