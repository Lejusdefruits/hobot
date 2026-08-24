"""Hardware/load detection -- policy-free: this module only answers "how much
RAM/CPU does this machine have, and how busy is it right now," never "should
hobot therefore skip something." That decision belongs to each caller (the
install-time hardware scan in scripts/install_wizard.py, the runtime
scoring-deferral gate in graphs/discovery_graph.py), which also needs
context this module doesn't have (e.g. which LLM_PROVIDER is configured).

Built on psutil so the same two calls work identically on Linux/macOS/Windows
-- the alternative (parsing /proc/meminfo, shelling out to sysctl, querying
WMI via PowerShell, and getting the "load average" concept only on two of the
three platforms) is a lot more platform-specific code for the same answer.

Every public function here is fail-open by construction: a detection failure
(unsupported platform, sandboxed environment, psutil hiccup) returns a safe
"don't know / not busy" result instead of raising, since this feeds into
install-time advice (worst case: an inaccurate suggestion, not a crash) and a
runtime gate (worst case: scoring runs when it maybe shouldn't have, never
"scoring silently stops working forever because a load check broke")."""
import os
import shutil
import subprocess
import sys

import psutil

MIN_RAM_GB_COMFORTABLE = float(os.environ.get("HOBOT_MIN_RAM_GB_COMFORTABLE", "16"))
MIN_RAM_GB_MARGINAL = float(os.environ.get("HOBOT_MIN_RAM_GB_MARGINAL", "8"))


def _gpu_probe() -> str | None:
    """Best-effort, informational only -- never affects assess_install()'s
    tier (RAM alone decides that, see the module docstring and
    assess_install() below). Whatever's detected first wins; returns None on
    any failure, including "no GPU tool found," which fail-open."""
    try:
        if shutil.which("nvidia-smi"):
            out = subprocess.run(
                ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                capture_output=True, text=True, timeout=5, check=True,
            )
            name = out.stdout.strip().splitlines()[0].strip()
            return name or None
        if sys.platform == "darwin":
            out = subprocess.run(
                ["system_profiler", "SPDisplaysDataType"],
                capture_output=True, text=True, timeout=5, check=True,
            )
            for line in out.stdout.splitlines():
                line = line.strip()
                if line.startswith("Chipset Model:"):
                    return line.split(":", 1)[1].strip()
    except Exception:
        return None
    return None


def assess_install() -> dict:
    """One-shot, install-time verdict on whether this machine can comfortably
    run hobot's default local model (OLLAMA_MODEL, an 8B-class Ollama model
    run at this project's own OLLAMA_NUM_CTX=20000 -- a meaningfully larger
    KV cache than a bare "just the weights" estimate). RAM-only: a GPU is
    real signal but its VRAM/driver/how-much-Ollama-can-actually-use-of-it
    isn't reliably knowable across vendors from here, so it's surfaced in
    "reasons" for the user to read, never used to upgrade or downgrade the
    tier itself.

    Returns {"tier": "comfortable"|"marginal"|"not_recommended"|"unknown",
    "ram_gb": float|None, "cpu_count": int, "gpu": str|None, "reasons": [str]}.
    "unknown" (with ram_gb=None) only on total detection failure -- treated
    by callers as "can't tell, don't block the user either way."
    """
    reasons = []
    try:
        ram_gb = psutil.virtual_memory().total / (1024 ** 3)
    except Exception as e:
        return {"tier": "unknown", "ram_gb": None, "cpu_count": 0, "gpu": None,
                "reasons": [f"could not read system memory ({e})"]}

    cpu_count = psutil.cpu_count(logical=True) or 0
    gpu = _gpu_probe()

    if ram_gb >= MIN_RAM_GB_COMFORTABLE:
        tier = "comfortable"
        reasons.append(f"{ram_gb:.0f} GB RAM -- comfortably above the {MIN_RAM_GB_COMFORTABLE:.0f} GB guideline")
    elif ram_gb >= MIN_RAM_GB_MARGINAL:
        tier = "marginal"
        reasons.append(
            f"{ram_gb:.0f} GB RAM -- above the bare {MIN_RAM_GB_MARGINAL:.0f} GB minimum but under the "
            f"{MIN_RAM_GB_COMFORTABLE:.0f} GB comfortable guideline; the model will likely run, slower, "
            "and a smaller OLLAMA_NUM_CTX may help"
        )
    else:
        tier = "not_recommended"
        reasons.append(f"{ram_gb:.0f} GB RAM -- under the {MIN_RAM_GB_MARGINAL:.0f} GB minimum guideline")

    if gpu:
        reasons.append(f"GPU detected: {gpu} (not factored into the verdict above, informational only)")
    else:
        reasons.append("no GPU detected (or detection not supported here) -- inference would run on CPU alone")

    return {"tier": tier, "ram_gb": ram_gb, "cpu_count": cpu_count, "gpu": gpu, "reasons": reasons}


def is_machine_busy(threshold: float = 0.85) -> bool:
    """Repeatable, cheap, runtime check -- "is the CPU currently loaded
    enough that starting local LLM inference right now would compete with
    whatever else is running." Never raises: any detection problem (an
    unsupported platform, a sandboxed process with no permission to read
    this) returns False (not busy), the same "fail open, don't block a core
    feature over a diagnostic that broke" rule as the rest of this module.

    interval=0.3 blocks briefly (psutil samples over that window) -- a
    deliberate, small cost paid once per scheduled scoring run, not per
    offer, so it doesn't add up."""
    try:
        return (psutil.cpu_percent(interval=0.3) / 100) >= threshold
    except Exception:
        return False


if __name__ == "__main__":
    # Manual check: `python -m core.hardware` -- the same assess_install()
    # the guided setup wizard runs, without going through the rest of it.
    result = assess_install()
    print("tier:", result["tier"])
    for reason in result["reasons"]:
        print(" -", reason)
