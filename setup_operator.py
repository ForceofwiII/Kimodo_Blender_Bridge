"""
Kimodo 自动安装器。

创建受管理的 Python 虚拟环境，安装 Kimodo 及依赖，下载 LLM2Vec 文本编码器，
修补离线路径，并自动写入插件使用的 Python 路径。
"""

import os
import re
import shutil
import subprocess
import sys
import threading
import traceback

import bpy
from bpy.types import Operator
from bpy.props import StringProperty, BoolProperty

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

# Standard system binary directories that desktop-launched processes often
# lack in PATH because display managers (GDM, SDDM, LightDM) only set a
# minimal environment — unlike a login shell which sources ~/.profile.
_SYSTEM_BIN_PATHS = [
    "/usr/local/bin", "/usr/bin", "/bin",
    "/usr/local/sbin", "/usr/sbin", "/sbin",
]

# Common locations where Python interpreters are installed outside of PATH
# (pyenv, deadsnakes PPA, system Python on various distros).
_EXTRA_PYTHON_DIRS = [
    "/usr/bin",
    "/usr/local/bin",
    os.path.expanduser("~/.local/bin"),
    os.path.expanduser("~/.pyenv/shims"),
]

# Prevent a console window from flashing up for every subprocess on Windows
# (Blender is a GUI process; child console apps like python/pip/nvidia-smi
# get their own window unless CREATE_NO_WINDOW is passed).
_NO_WINDOW = (
    {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
)

# ---------------------------------------------------------------------------
# HuggingFace download settings
# ---------------------------------------------------------------------------

_HF_TIMEOUT_SECS = 120   # per-request HTTP stall timeout (seconds)
_HF_MAX_ATTEMPTS = 3     # total download attempts before giving up
_HF_BACKOFF_BASE = 15    # seconds before first retry; doubles each time


def _build_env(extra: "dict | None" = None) -> dict:
    """Return os.environ enriched with standard system paths and HOME.

    Blender launched from a desktop session inherits the display-manager's
    minimal environment. Subprocesses that inherit it (pip, venv, git, …)
    can fail because tools they need are not on PATH. This function ensures
    a complete, safe environment is passed to every subprocess we spawn.
    """
    env = os.environ.copy()

    # Ensure standard bin dirs are present; append any that are missing so
    # user-local paths (pyenv shims, ~/.local/bin) still take priority.
    current_paths = [p for p in env.get("PATH", "").split(os.pathsep) if p]
    for p in _SYSTEM_BIN_PATHS:
        if p not in current_paths:
            current_paths.append(p)
    env["PATH"] = os.pathsep.join(current_paths)

    # Guarantee HOME is set; pip and venv need it to locate config/cache dirs.
    if not env.get("HOME"):
        env["HOME"] = os.path.expanduser("~")

    if extra:
        env.update(extra)
    return env

# Default venv location. The actual location is overridable via the
# 'install_location' addon preference (see managed_venv()), which persists
# across Blender restarts and scenes.
_DEFAULT_VENV_NAME  = ".kimodo-venv"
LLMVEC_MODEL_ID     = "Aero-Ex/KIMODO-Meta3_llm2vec_NF4"
# Names of the per-venv marker / model dir, resolved relative to the venv root
# (so detection still works for a relocated or custom-located venv).
_LLMVEC_NAME        = "llm2vec-model"
# Written at the very end of a successful install; absence means partial/broken.
_SENTINEL_NAME      = ".kimodo_install_complete"
# Placeholder string in Aero-Ex's llm2vec_wrapper.py that we replace with the model dir
_WRAPPER_PLACEHOLDER = "path_to_your_Llama_text-encoders"


def _default_venv() -> str:
    """Return the default ~/.kimodo-venv location."""
    return os.path.join(os.path.expanduser("~"), _DEFAULT_VENV_NAME)


def managed_venv() -> str:
    """Return the configured Kimodo venv location, or the default.

    Reads the 'install_location' addon preference so the user's custom path is
    remembered across Blender restarts and scenes. Falls back to
    ~/.kimodo-venv when unset or unavailable.

    Reads bpy.context — only call from the main thread (panel draw / operator
    execute). Background threads must use the install dir passed into them.
    """
    try:
        prefs = bpy.context.preferences.addons[__package__].preferences
        loc = (prefs.install_location or "").strip()
        if loc:
            return os.path.abspath(bpy.path.abspath(os.path.expanduser(loc)))
    except Exception:
        pass
    return _default_venv()


def _venv_python(venv: str) -> str:
    """Return the python executable inside *venv*, or '' if not present."""
    for rel in ("bin/python3", "bin/python", "Scripts/python.exe"):
        p = os.path.join(venv, rel)
        if os.path.isfile(p):
            return p
    return ""

# ---------------------------------------------------------------------------
# Install state  (module-level; panels poll this via a redraw timer)
# ---------------------------------------------------------------------------

_state: dict = {"running": False, "lines": [], "error": "", "done": False,
                "needs_python": False, "dl_progress": 0.0, "dl_label": ""}
_lock = threading.Lock()


def _log(msg: str) -> None:
    print(f"[Kimodo Install] {msg}", flush=True)
    with _lock:
        _state["lines"].append(msg)
        if len(_state["lines"]) > 12:
            _state["lines"] = _state["lines"][-12:]


def install_status() -> str:
    """Return a one-line summary for the UI."""
    with _lock:
        if _state["error"]:
            return f"错误：{_state['error']}"
        if _state["done"]:
            return "安装成功"
        if _state["running"]:
            return _state["lines"][-1] if _state["lines"] else "正在安装…"
        return ""


def is_installing() -> bool:
    with _lock:
        return _state["running"]


def download_progress() -> float:
    """Return current HF download progress as 0.0–1.0 (0.0 when not downloading)."""
    with _lock:
        return _state["dl_progress"]


def download_label() -> str:
    """Return a short human-readable label for the active download step."""
    with _lock:
        return _state["dl_label"]


def _set_dl_progress(frac: float, label: str = "") -> None:
    with _lock:
        _state["dl_progress"] = max(0.0, min(1.0, frac))
        if label:
            _state["dl_label"] = label


def _parse_tqdm_pct(line: str) -> "float | None":
    """Extract a 0.0–1.0 fraction from a tqdm progress line, or None."""
    m = re.search(r"(\d+)%\|", line)
    if m:
        return int(m.group(1)) / 100.0
    return None


def install_failed() -> bool:
    with _lock:
        return bool(_state["error"])


def needs_python() -> bool:
    with _lock:
        return _state["needs_python"]


def managed_python() -> str:
    """Return path to the managed-venv Python, or '' if not present."""
    return _venv_python(managed_venv())


def venv_root_for(python_exe: str) -> str:
    """Given a python executable inside a venv, return the venv root ('' if none).

    Handles <root>/bin/python3 (POSIX), <root>/Scripts/python.exe (Windows
    venv) and <root>/python.exe (conda env root).
    """
    if not python_exe:
        return ""
    d = os.path.dirname(os.path.abspath(python_exe))
    base = os.path.basename(d).lower()
    return os.path.dirname(d) if base in ("bin", "scripts") else d


def is_kimodo_venv(python_exe: str) -> bool:
    """True if python_exe points into a completed Kimodo venv.

    Recognises a relocated/copied managed venv by the install sentinel that
    sits at its root, so detection no longer depends on the hardcoded
    ~/.kimodo-venv location.
    """
    root = venv_root_for(python_exe)
    return (
        bool(root)
        and os.path.isfile(python_exe)
        and os.path.isfile(os.path.join(root, _SENTINEL_NAME))
    )


def llmvec_dir_for(python_exe: str) -> str:
    """Path to the llm2vec model dir relative to the selected venv ('' if none)."""
    root = venv_root_for(python_exe)
    return os.path.join(root, _LLMVEC_NAME) if root else ""


# Cached GPU presence — panels call has_nvidia_gpu() from draw(), which runs
# on every viewport redraw; spawning nvidia-smi there would stall the UI
# (and flash a console window per redraw on Windows).
_GPU_PRESENT: "bool | None" = None


def has_nvidia_gpu() -> bool:
    """Return True if an NVIDIA GPU is present (nvidia-smi responds).

    The result is detected once and cached for the session — safe to call
    from panel draw() callbacks.
    """
    global _GPU_PRESENT
    if _GPU_PRESENT is None:
        _GPU_PRESENT = _detect_nvidia_gpu()
    return _GPU_PRESENT


def _detect_nvidia_gpu() -> bool:
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True, timeout=5,
            env=_build_env(), **_NO_WINDOW,
        )
        return r.returncode == 0 and bool(r.stdout.strip())
    except Exception:
        return False


def _max_gpu_compute_capability() -> tuple[int, int]:
    """Return the highest (major, minor) compute capability across all GPUs, or (0, 0)."""
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=compute_cap", "--format=csv,noheader"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=5, env=_build_env(), **_NO_WINDOW,
        )
        if r.returncode != 0:
            return (0, 0)
        best = (0, 0)
        for line in r.stdout.strip().splitlines():
            parts = line.strip().split(".")
            if len(parts) == 2:
                cap = (int(parts[0]), int(parts[1]))
                if cap > best:
                    best = cap
        return best
    except Exception:
        return (0, 0)


def venv_exists() -> bool:
    """True if the venv directory is present (even if install is incomplete)."""
    return os.path.isdir(managed_venv())


def is_installed() -> bool:
    """True only when the venv has a Python binary AND the install completed."""
    return bool(managed_python()) and os.path.isfile(
        os.path.join(managed_venv(), _SENTINEL_NAME)
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _find_system_python() -> str:
    """Return a system Python ≥ 3.10 that is not Blender's bundled Python."""
    blender_py = os.path.realpath(sys.executable)

    # Build a deduplicated list of candidate paths to probe.
    # shutil.which respects the current PATH, which may be stripped in a
    # desktop session, so we also probe known install directories directly.
    candidates: list[str] = []
    for name in ("python3.12", "python3.13", "python3.11", "python3.10", "python3", "python"):
        # Honour PATH first (covers pyenv shims, conda envs, user installs).
        via_which = shutil.which(name, path=_build_env()["PATH"])
        if via_which and via_which not in candidates:
            candidates.append(via_which)
        # Then probe common install dirs that may not be on the desktop PATH.
        for d in _EXTRA_PYTHON_DIRS:
            full = os.path.join(d, name)
            if os.path.isfile(full) and full not in candidates:
                candidates.append(full)

    for found in candidates:
        if os.name == "nt" and "windowsapps" in found.lower():
            continue
        if os.path.realpath(found) == blender_py:
            continue
        try:
            r = subprocess.run(
                [found, "-c",
                 "import sys; v=sys.version_info; print(v.major, v.minor)"],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=5, **_NO_WINDOW,
            )
            parts = r.stdout.strip().split()
            if len(parts) == 2 and int(parts[0]) == 3 and int(parts[1]) >= 10:
                return found
        except Exception:
            pass
    return ""


def _git_available() -> bool:
    """Return True if git is on PATH and runnable."""
    try:
        r = subprocess.run(
            ["git", "--version"], capture_output=True, timeout=5,
            env=_build_env(), **_NO_WINDOW,
        )
        return r.returncode == 0
    except Exception:
        return False


def _github_install_url(owner: str, repo: str) -> str:
    """
    Return a pip-installable URL for a GitHub repo.  Uses the git+https form
    when git is available; falls back to the zip archive (no git required).
    """
    if _git_available():
        return f"git+https://github.com/{owner}/{repo}.git"
    # GitHub serves the default branch as a zip that pip can install directly.
    return f"https://github.com/{owner}/{repo}/archive/HEAD.zip"


def _run(cmd: list, step: str, env: "dict | None" = None,
         on_line: "callable | None" = None) -> None:
    """Run *cmd* as a subprocess, stream output to _log, raise on failure."""
    _log(f"▶ {step}")
    # Always build a complete environment so pip/venv work correctly when
    # Blender was launched from a desktop session with a stripped PATH.
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=_build_env(env),
        **_NO_WINDOW,
    )
    for line in proc.stdout:
        stripped = line.rstrip()
        if stripped:
            _log(stripped)
            if on_line:
                on_line(stripped)
    proc.wait()
    if proc.returncode != 0:
        raise RuntimeError(f"{step} 失败（退出码 {proc.returncode}）")


def _download_with_retry(
    venv_py: str,
    step: str,
    repo_id: str,
    local_dir: "str | None" = None,
    hf_token: str = "",
) -> None:
    """Run snapshot_download in the venv with timeout, retry, and progress tracking.

    All variable data (token, paths) is passed via env vars rather than
    interpolated into the script string — this avoids quoting issues with
    Windows paths and tokens containing special characters.
    """
    import time as _time

    if local_dir:
        dl_script = (
            "import os; from huggingface_hub import snapshot_download; "
            "tok = os.environ.get('_KBB_HF_TOKEN') or None; "
            "snapshot_download(repo_id=os.environ['_KBB_REPO_ID'], "
            "local_dir=os.environ['_KBB_LOCAL_DIR'], token=tok)"
        )
    else:
        dl_script = (
            "import os; from huggingface_hub import snapshot_download; "
            "tok = os.environ.get('_KBB_HF_TOKEN') or None; "
            "snapshot_download(repo_id=os.environ['_KBB_REPO_ID'], token=tok)"
        )

    extra_env = {
        "_KBB_REPO_ID":           repo_id,
        "_KBB_HF_TOKEN":          hf_token,
        # Per-request HTTP stall timeout — prevents silent hangs on slow/rate-
        # limited connections.  Individual requests that stall for longer than
        # this value will be aborted and retried by huggingface_hub internally.
        "HF_HUB_DOWNLOAD_TIMEOUT": str(_HF_TIMEOUT_SECS),
        # Force unbuffered stdout so tqdm progress lines reach us in real time
        # instead of sitting in the subprocess's output buffer.
        "PYTHONUNBUFFERED":        "1",
    }
    if local_dir:
        extra_env["_KBB_LOCAL_DIR"] = local_dir

    def _on_line(line: str) -> None:
        pct = _parse_tqdm_pct(line)
        if pct is not None:
            _set_dl_progress(pct, step)

    last_exc: "Exception | None" = None
    for attempt in range(1, _HF_MAX_ATTEMPTS + 1):
        if attempt > 1:
            wait = _HF_BACKOFF_BASE * (2 ** (attempt - 2))   # 15 s, 30 s
            _log(f"  将在 {wait}s 后进行第 {attempt}/{_HF_MAX_ATTEMPTS} 次重试…")
            _time.sleep(wait)
        _set_dl_progress(0.0, step)
        try:
            _run(
                [venv_py, "-c", dl_script],
                f"{step}（第 {attempt}/{_HF_MAX_ATTEMPTS} 次）",
                env=extra_env,
                on_line=_on_line,
            )
            _set_dl_progress(1.0, step)
            return
        except RuntimeError as exc:
            last_exc = exc
            _log(f"  第 {attempt} 次尝试失败：{exc}")

    raise RuntimeError(
        f"{step} 在 {_HF_MAX_ATTEMPTS} 次尝试后仍失败。"
        f"最后错误：{last_exc}"
    )


def _venv_pip() -> list:
    py = managed_python()
    if not py:
        raise RuntimeError(f"在 {managed_venv()} 中找不到虚拟环境 Python")
    return [py, "-m", "pip"]


def _find_wrapper(venv_py: str) -> str:
    """Locate llm2vec_wrapper.py inside the venv's site-packages."""
    r = subprocess.run(
        [venv_py, "-c",
         "import importlib.util; s=importlib.util.find_spec('kimodo'); "
         "print(s.origin if s else '')"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=10, **_NO_WINDOW,
    )
    origin = r.stdout.strip()
    if not origin:
        return ""
    candidate = os.path.join(
        os.path.dirname(origin), "model", "llm2vec", "llm2vec_wrapper.py"
    )
    return candidate if os.path.isfile(candidate) else ""


def _extract_hf_model_id(wrapper_path: str) -> str:
    """Read llm2vec_wrapper.py and extract the HuggingFace repo ID."""
    # Kept for API compatibility; model ID is now hardcoded as LLMVEC_MODEL_ID.
    return LLMVEC_MODEL_ID


def _patch_wrapper(wrapper_path: str, local_dir: str) -> None:
    """
    Replace the placeholder path in llm2vec_wrapper.py with *local_dir* so
    the model loads from disk.  The Aero-Ex fork uses the literal string
    'path_to_your_Llama_text-encoders' as the user-editable slot.
    """
    with open(wrapper_path, encoding="utf-8") as f:
        text = f.read()

    if _WRAPPER_PLACEHOLDER not in text:
        _log("警告：llm2vec_wrapper.py 中没有找到占位符，可能已经修补过。")
        return

    # Use a raw string so Windows backslashes survive the replacement.
    safe_dir = local_dir.replace("\\", "\\\\")
    patched = text.replace(_WRAPPER_PLACEHOLDER, safe_dir, 1)

    with open(wrapper_path, "w", encoding="utf-8") as f:
        f.write(patched)


def find_wrapper_for(python_exe: str) -> str:
    """Locate llm2vec_wrapper.py inside the selected venv (no subprocess)."""
    import glob
    root = venv_root_for(python_exe)
    if not root:
        return ""
    rel = os.path.join("kimodo", "model", "llm2vec", "llm2vec_wrapper.py")
    for pat in (
        os.path.join(root, "lib", "python*", "site-packages", rel),  # POSIX
        os.path.join(root, "Lib", "site-packages", rel),             # Windows
    ):
        hits = glob.glob(pat)
        if hits:
            return hits[0]
    return ""


def heal_wrapper_path(python_exe: str) -> bool:
    """Repair a relocated venv's llm2vec wrapper.

    The installer bakes an absolute path to the llm2vec-model dir into
    llm2vec_wrapper.py (the ``custom_path = r"…"`` line). If the venv is later
    moved or renamed, that path breaks; the wrapper then falls back to a
    non-existent ``models/KIMODO-Meta3_llm2vec_NF4`` dir, and generation fails
    with a HuggingFace "Repo id must be in the form…" error.

    When the baked path no longer exists, rewrite it to this venv's actual
    llm2vec-model dir. A still-valid baked path is left untouched so custom
    setups are not clobbered. Returns True if a change was written.
    """
    import re
    llmvec = llmvec_dir_for(python_exe)
    if not llmvec or not os.path.isdir(llmvec):
        return False
    wrapper = find_wrapper_for(python_exe)
    if not wrapper:
        return False
    try:
        with open(wrapper, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return False

    m = re.search(r'(custom_path\s*=\s*r?")([^"]*)(")', text)
    if not m:
        return False
    current = m.group(2)
    if os.path.isdir(current):
        return False  # baked path still valid — leave it alone

    safe = llmvec.replace("\\", "\\\\")
    patched = text[:m.start(2)] + safe + text[m.end(2):]
    try:
        with open(wrapper, "w", encoding="utf-8") as f:
            f.write(patched)
    except OSError:
        return False
    return True


# ---------------------------------------------------------------------------
# Background install thread
# ---------------------------------------------------------------------------

def _validate_python(python_exe: str) -> bool:
    """Return True if *python_exe* is a runnable Python 3.10–3.12 interpreter."""
    try:
        r = subprocess.run(
            [python_exe, "-c",
             "import sys; v=sys.version_info; print(v.major, v.minor)"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=5, **_NO_WINDOW,
        )
        parts = r.stdout.strip().split()
        return len(parts) == 2 and int(parts[0]) == 3 and int(parts[1]) >= 10
    except Exception:
        return False


def _do_install(hf_token: str = "", system_python: str = "",
                install_dir: str = "") -> None:
    global _state
    try:
        # All venv paths are derived from install_dir (resolved on the main
        # thread and passed in) so this thread never touches bpy.context.
        venv     = install_dir or _default_venv()
        llmvec   = os.path.join(venv, _LLMVEC_NAME)
        sentinel = os.path.join(venv, _SENTINEL_NAME)

        # 1 — Find a system Python ≥ 3.10
        sys_py = ""
        if system_python:
            if os.path.isfile(system_python) and _validate_python(system_python):
                _log(f"使用用户指定的 Python：{system_python}")
                sys_py = system_python
            else:
                _log(f"用户指定的 Python 不是有效的 3.10–3.12 可执行文件："
                     f"{system_python}")
                _log("改用自动检测…")
        if not sys_py:
            _log("正在查找系统 Python 3.10+…")
            sys_py = _find_system_python()
        if not sys_py:
            with _lock:
                _state["needs_python"] = True
            raise RuntimeError(
                "没有找到 Python 3.10+。请从 python.org 安装，"
                "勾选“Add Python to PATH”，然后点击“重试安装”。"
            )
        _log(f"已找到：{sys_py}")

        # 2 — Create venv
        _log(f"安装位置：{venv}")
        os.makedirs(os.path.dirname(venv) or ".", exist_ok=True)
        _run([sys_py, "-m", "venv", venv], "创建虚拟环境")

        venv_py = _venv_python(venv)
        if not venv_py:
            raise RuntimeError("虚拟环境已创建，但没有找到 Python 可执行文件。")
        pip = [venv_py, "-m", "pip"]

        # 3 — Upgrade pip
        _run([*pip, "install", "--upgrade", "pip"], "升级 pip")

        # 4 — Install PyTorch.
        #     Index selection depends on Python version and GPU compute capability:
        #
        #     cu128 (PyTorch 2.7+): required for Blackwell GPUs (sm_120, RTX 50xx)
        #                           also supports Python 3.13
        #     cu124 (PyTorch 2.6+): required for Python 3.13 on older GPUs
        #                           supports up to sm_90
        #     cu121 (PyTorch 2.1+): works for Python ≤3.12, GPUs up to sm_90
        r = subprocess.run(
            [venv_py, "-c", "import sys; print(sys.version_info.minor)"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=5, **_NO_WINDOW,
        )
        py_minor = int(r.stdout.strip() or "0")
        gpu_cap = _max_gpu_compute_capability()
        _log(f"检测到 GPU 计算能力：{gpu_cap[0]}.{gpu_cap[1]}")

        if gpu_cap >= (12, 0):
            # Blackwell (RTX 50xx / sm_120+): only PyTorch 2.7+ / cu128 has kernels
            torch_index = "https://download.pytorch.org/whl/cu128"
            cuda_label = "12.8"
        elif py_minor >= 13:
            # Python 3.13 on Ampere/Ada/Hopper: PyTorch 2.6+ / cu124
            torch_index = "https://download.pytorch.org/whl/cu124"
            cuda_label = "12.4"
        else:
            torch_index = "https://download.pytorch.org/whl/cu121"
            cuda_label = "12.1"
        _log(f"正在安装支持 CUDA {cuda_label} 的 PyTorch，可能需要几分钟…")
        _run(
            [*pip, "install", "torch",
             "--index-url", torch_index],
            "安装 PyTorch",
        )

        # 5 — Install the pre-built motion_correction wheel from Aero-Ex.
        #     motion_correction is a C extension inside Kimodo's setup.py.
        #     Building it from source requires MSVC on Windows (not just cmake),
        #     so the Aero-Ex fork ships pre-built wheels for each Python version.
        #     We install the wheel first; then tell setup.py to skip rebuilding it.
        _log("正在安装预编译的 motion_correction wheel…")
        r = subprocess.run(
            [venv_py, "-c",
             "import sys; print(f'cp{sys.version_info.major}{sys.version_info.minor}')"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=5, **_NO_WINDOW,
        )
        py_tag = r.stdout.strip()  # e.g. "cp312"

        if os.name == "nt":
            platform_tag = "win_amd64"
        elif sys.platform.startswith("linux"):
            platform_tag = "manylinux_2_27_x86_64.manylinux_2_28_x86_64"
        else:
            platform_tag = None  # macOS: no pre-built wheel available

        if platform_tag:
            wheel_url = (
                "https://github.com/Aero-Ex/kimodo/releases/download/v1.0.0/"
                f"motion_correction-1.0.0-{py_tag}-{py_tag}-{platform_tag}.whl"
            )
            _log(f"wheel 地址：{wheel_url}")
            _run([*pip, "install", wheel_url], "安装 motion_correction")
        else:
            _log("macOS 没有预编译 wheel，motion_correction 将从源码构建"
                 "（需要 Xcode Command Line Tools）")

        # 6 — Install packages that Kimodo imports but does not declare in
        #     pyproject.toml (discovered by auditing every import in the source):
        #
        #   bitsandbytes  — NF4 quantization for the LLM2Vec text encoder
        #   safetensors   — hard import in kimodo/model/loading.py (load_file)
        #   psutil        — top-level import in kimodo/demo/memory_manager.py
        #
        #   PyGLM / SpatialTransform are also imported in bvh.py but both
        #   arrive transitively via bvhio (pyglm, spatial-transform) so they
        #   do not need to be listed here.
        _log("正在安装 Kimodo 未声明但实际需要的依赖…")
        _run(
            [*pip, "install",
             "bitsandbytes>=0.46.1",
             "safetensors",
             "psutil"],
            "安装 Kimodo 补充依赖",
        )

        # 7 — Install Kimodo from Aero-Ex fork.
        #     SKIP_MOTION_CORRECTION_IN_SETUP=1 tells setup.py not to rebuild
        #     motion_correction (we already installed it in step 5).
        kimodo_url = _github_install_url("Aero-Ex", "kimodo")
        _log(f"正在从 Aero-Ex 离线分支安装 Kimodo：{kimodo_url}…")
        kimodo_env = os.environ.copy()
        kimodo_env["SKIP_MOTION_CORRECTION_IN_SETUP"] = "1"
        _run(
            [*pip, "install", kimodo_url],
            "安装 Kimodo",
            env=kimodo_env,
        )

        # 8 — Install the NVIDIA kimodo-viser fork.
        #     PyPI viser does not have viser._timeline_api — that submodule is
        #     exclusive to the nv-tlabs fork used by the Kimodo demo.
        #     Kimodo lists this under [project.optional-dependencies] demo = [...],
        #     so it is not pulled in by a plain pip install.
        viser_url = _github_install_url("nv-tlabs", "kimodo-viser")
        _log(f"正在安装 kimodo-viser 分支：{viser_url}…")
        _run(
            [*pip, "install", viser_url],
            "安装 kimodo-viser",
        )

        # 9 — Locate llm2vec_wrapper.py
        _log("正在定位已安装包中的 LLM2Vec wrapper…")
        wrapper = _find_wrapper(venv_py)
        if not wrapper:
            raise RuntimeError(
                "安装后没有找到 llm2vec_wrapper.py。"
                "Kimodo 可能没有正确安装，请检查上方日志。"
            )
        _log(f"已找到 wrapper：{wrapper}")

        # 10 — Download the LLM2Vec text-encoder model to a local folder.
        #     The Aero-Ex fork hosts the model at Aero-Ex/KIMODO-Meta3_llm2vec_NF4
        #     on HuggingFace.  We download it once and point the wrapper at it.
        _log(f"正在下载 LLM2Vec 模型（{LLMVEC_MODEL_ID}），这可能需要一段时间…")
        os.makedirs(llmvec, exist_ok=True)
        _download_with_retry(
            venv_py,
            "下载 LLM2Vec 模型",
            repo_id=LLMVEC_MODEL_ID,
            local_dir=llmvec,
            hf_token=hf_token,
        )

        # 11 — Patch wrapper for fully offline operation
        _log("正在修补 llm2vec_wrapper.py 以支持离线使用…")
        _patch_wrapper(wrapper, llmvec)
        _log("修补完成。")

        # 12 — Download Kimodo model weights into the HF cache.
        #      load_model.py calls snapshot_download unconditionally ("will check
        #      online no matter what"), so the weights must be in the local cache
        #      before we enable HF_HUB_OFFLINE at bridge launch time.
        #      We only download the default SOMA model; the other two are
        #      unsupported in the addon UI and can be fetched later if needed.
        _log("正在下载 Kimodo-SOMA-RP-v1 模型权重，这可能需要一段时间…")
        _download_with_retry(
            venv_py,
            "下载 Kimodo-SOMA-RP-v1 权重",
            repo_id="nvidia/Kimodo-SOMA-RP-v1",
            hf_token=hf_token,
        )

        # 13 — Update the addon's Python path on the main thread, and persist
        #      the install location to the addon preferences so it is remembered
        #      across Blender restarts and scenes.
        def _set_path():
            try:
                for scene in bpy.data.scenes:
                    if not scene.kimodo.python_executable:
                        scene.kimodo.python_executable = venv_py
                prefs = bpy.context.preferences.addons[__package__].preferences
                prefs.install_location = venv
                bpy.ops.wm.save_userpref()
            except Exception:
                pass
        bpy.app.timers.register(_set_path, first_interval=0.1)

        # Mark the install as complete so a partial venv is never mistaken for
        # a successful one after a Blender restart.
        open(sentinel, "w").close()

        with _lock:
            _state["done"] = True
        _log("安装完成！现在可以点击“启动 Kimodo”。")

    except Exception as exc:
        tb = traceback.format_exc()
        print(f"[Kimodo Install] 失败：\n{tb}", flush=True)
        with _lock:
            _state["error"] = str(exc)
    finally:
        with _lock:
            _state["running"] = False


# ---------------------------------------------------------------------------
# Operators
# ---------------------------------------------------------------------------

class KIMODO_OT_InstallKimodo(Operator):
    bl_idname      = "kimodo.install_kimodo"
    bl_label       = "自动安装 Kimodo"
    bl_description = (
        "选择一个文件夹，在其中创建 Kimodo 虚拟环境，安装 Aero-Ex 离线分支，"
        "下载 LLM2Vec 文本编码器，并自动配置插件。需要网络和约 5–10 GB 磁盘空间。"
    )

    # Folder chosen in the file browser (populated by fileselect_add). The venv
    # is created in a "kimodo-venv" subfolder of this directory.
    directory: StringProperty(subtype='DIR_PATH', options={'SKIP_SAVE'})
    # Retry buttons set this False so they reuse the already-chosen location
    # instead of re-opening the folder browser.
    prompt_location: BoolProperty(default=True, options={'SKIP_SAVE'})

    def invoke(self, context, event):
        # Already running, or an explicit no-prompt retry → straight to execute.
        if is_installing() or not self.prompt_location:
            return self.execute(context)
        # Pre-point the browser at the parent of the current default location.
        default = managed_venv()
        parent = os.path.dirname(default) or os.path.expanduser("~")
        self.directory = parent + os.sep
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        if is_installing():
            self.report({"WARNING"}, "安装正在进行中。")
            return {"CANCELLED"}

        # If the user picked a folder in the browser, place the venv in a clearly
        # named "kimodo-venv" subfolder and persist it to the addon preferences
        # so managed_venv() and the background thread agree and it survives
        # Blender restarts.
        chosen = (self.directory or "").strip()
        if self.prompt_location and chosen:
            install_dir = os.path.join(os.path.abspath(bpy.path.abspath(chosen)),
                                       "kimodo-venv")
            try:
                prefs = context.preferences.addons[__package__].preferences
                prefs.install_location = install_dir
            except Exception:
                pass

        # Resolve the install location on the main thread (reads addon prefs).
        install_dir = managed_venv()

        # Remove any partial venv so we always start clean on a retry.
        # A complete install is guarded by the sentinel file; if that's absent
        # the venv is broken and safe to wipe regardless of session state.
        if venv_exists() and not is_installed():
            _log(f"正在移除未完成的虚拟环境以便重新安装：{install_dir}")
            try:
                shutil.rmtree(install_dir)
            except Exception as exc:
                self.report({"ERROR"}, f"无法移除未完成的虚拟环境：{exc}")
                return {"CANCELLED"}

        if is_installed():
            self.report({"INFO"}, "受管理的 Kimodo 虚拟环境已存在。")
            return {"CANCELLED"}

        with _lock:
            _state.update(running=True, lines=[], error="", done=False,
                          needs_python=False, dl_progress=0.0, dl_label="")

        # Read the HF token and Python override on the main thread —
        # preferences are not safe to access from background threads.
        hf_token = ""
        system_python = ""
        try:
            prefs = context.preferences.addons[__package__].preferences
            hf_token = (prefs.hf_token or "").strip()
            system_python = (prefs.system_python_override or "").strip()
        except Exception:
            pass

        threading.Thread(
            target=_do_install,
            args=(hf_token, system_python, install_dir),
            daemon=True,
        ).start()

        # Keep the N-panel refreshing while the install runs
        def _redraw():
            for window in bpy.context.window_manager.windows:
                for area in window.screen.areas:
                    if area.type == "VIEW_3D":
                        area.tag_redraw()
            return 0.5 if is_installing() else None

        bpy.app.timers.register(_redraw, first_interval=0.5)
        self.report({"INFO"}, "Kimodo 安装已开始，请查看“连接”面板。")
        return {"FINISHED"}


class KIMODO_OT_UseInstalledKimodo(Operator):
    bl_idname      = "kimodo.use_installed_kimodo"
    bl_label       = "使用已安装的 Kimodo"
    bl_description = "将插件指向受管理虚拟环境中的 Python"

    def execute(self, context):
        py = managed_python()
        if not py:
            self.report({"ERROR"}, f"在 {managed_venv()} 中找不到受管理虚拟环境")
            return {"CANCELLED"}
        context.scene.kimodo.python_executable = py
        self.report({"INFO"}, f"Python 路径已设置为：{py}")
        return {"FINISHED"}


class KIMODO_OT_ResetVenv(Operator):
    bl_idname      = "kimodo.reset_venv"
    bl_label       = "删除虚拟环境"
    bl_description = (
        "删除 Kimodo 虚拟环境并允许重新安装。"
        "适用于上次安装失败、卡住，或需要为不同 GPU / Python 版本重新安装的情况。"
    )

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        if is_installing():
            self.report({"WARNING"}, "安装过程中不能重置。")
            return {"CANCELLED"}
        if not venv_exists():
            self.report({"INFO"}, "没有找到虚拟环境，无需重置。")
            return {"CANCELLED"}
        install_dir = managed_venv()
        try:
            shutil.rmtree(install_dir)
        except Exception as exc:
            self.report({"ERROR"}, f"无法移除虚拟环境：{exc}")
            return {"CANCELLED"}
        with _lock:
            _state.update(running=False, lines=[], error="", done=False)
        self.report({"INFO"}, f"已删除 {install_dir}，可以重新安装。")
        return {"FINISHED"}


class KIMODO_OT_OpenPythonDownload(Operator):
    bl_idname      = "kimodo.open_python_download"
    bl_label       = "下载 Python 3.12"
    bl_description = "在浏览器中打开 python.org/downloads"

    def execute(self, context):
        import platform, webbrowser
        if os.name == "nt":
            # Direct link to the Windows installer for the user's architecture
            arch = "arm64" if platform.machine().lower() == "arm64" else "amd64"
            webbrowser.open(
                f"https://www.python.org/ftp/python/3.12.10/python-3.12.10-{arch}.exe"
            )
        else:
            # Linux/macOS: no .exe — point at the downloads page (most Linux
            # users will install via their package manager anyway).
            webbrowser.open("https://www.python.org/downloads/")
        return {"FINISHED"}


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

_classes = [
    KIMODO_OT_InstallKimodo,
    KIMODO_OT_UseInstalledKimodo,
    KIMODO_OT_ResetVenv,
    KIMODO_OT_OpenPythonDownload,
]


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
