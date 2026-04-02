#!/usr/bin/env python3
"""
Thermalright Grand Vision AIO LCD System Monitor
=================================================
Renders CPU / GPU0 / GPU1 stats to the 480x480 AIO LCD display.

Modes:
  - ROTATE: cycles through CPU -> GPU0 -> GPU1 every ~3 seconds
  - STATIC: press 1/2/3 to lock on CPU/GPU0/GPU1; auto-returns to rotate after 60s

Controls (keyboard):
  1 = CPU page    2 = GPU0 page    3 = GPU1 page
  r = resume rotation    b/B = brightness down/up
  l = toggle RGB on/off  q = quit

Requires: pyusb, Pillow, liquidctl (optional, for RGB)
Run as root or set udev rule for USB access.
"""

import io
import os
import sys
import time
import struct
import signal
import select
import subprocess
import termios
import tty
import threading
from pathlib import Path
from datetime import datetime

try:
    import usb.core
    import usb.util
except ImportError:
    print("ERROR: pyusb not installed. Run: pip install pyusb")
    sys.exit(1)

try:
    from PIL import Image, ImageDraw, ImageFont, ImageEnhance
except ImportError:
    print("ERROR: Pillow not installed. Run: pip install Pillow")
    sys.exit(1)


# ─── AIO USB Protocol Constants ───────────────────────────────────────────────
VID = 0x87AD
PID = 0x70DB
MAGIC = bytes([0x12, 0x34, 0x56, 0x78])
CMD_JPEG = 2
CHUNK_SIZE = 16384
DISPLAY_W = 480
DISPLAY_H = 480
JPEG_QUALITY = 85

# ─── Color Palette ────────────────────────────────────────────────────────────
BG_COLOR = (15, 15, 20)
HEADER_BG = (30, 30, 40)
LABEL_COLOR = (140, 140, 160)
VALUE_COLOR = (230, 230, 240)
ACCENT_CPU = (0, 180, 255)
ACCENT_GPU0 = (255, 100, 50)
ACCENT_GPU1 = (50, 220, 100)
BAR_BG = (40, 40, 55)
WARN_COLOR = (255, 200, 50)
CRIT_COLOR = (255, 60, 60)
DIM_COLOR = (80, 80, 100)


# ─── Sensor Reading ───────────────────────────────────────────────────────────

def read_sysfs(path):
    """Read a sysfs file, return stripped string or None."""
    try:
        return Path(path).read_text().strip()
    except (OSError, PermissionError):
        return None


def read_sysfs_int(path, divisor=1):
    """Read sysfs int value, divide, return float or None."""
    val = read_sysfs(path)
    if val is not None:
        try:
            return int(val) / divisor
        except ValueError:
            pass
    return None


def get_cpu_temps():
    """Return dict of CPU temps from k10temp (hwmon2)."""
    # Find the k10temp hwmon
    base = None
    for d in Path("/sys/class/hwmon").iterdir():
        name = read_sysfs(d / "name")
        if name == "k10temp":
            base = d
            break
    if not base:
        return {"Tctl": None, "CCD1": None, "CCD2": None}

    return {
        "Tctl": read_sysfs_int(base / "temp1_input", 1000),
        "CCD1": read_sysfs_int(base / "temp3_input", 1000),
        "CCD2": read_sysfs_int(base / "temp4_input", 1000),
    }


def get_cpu_freqs():
    """Return list of per-core frequencies in MHz from /proc/cpuinfo."""
    freqs = []
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("cpu MHz"):
                    try:
                        mhz = float(line.split(":")[1].strip())
                        freqs.append(mhz)
                    except (ValueError, IndexError):
                        pass
    except OSError:
        pass
    # Return only physical cores (first 16 of 32 threads)
    return freqs[:16] if len(freqs) > 16 else freqs


def get_cpu_usage():
    """Get per-core CPU usage via /proc/stat snapshot (blocking ~0.25s)."""
    def read_stat():
        lines = {}
        with open("/proc/stat") as f:
            for line in f:
                if line.startswith("cpu"):
                    parts = line.split()
                    name = parts[0]
                    vals = list(map(int, parts[1:]))
                    lines[name] = vals
        return lines

    s1 = read_stat()
    time.sleep(0.25)
    s2 = read_stat()

    usage = {}
    for key in s2:
        if key not in s1:
            continue
        d = [b - a for a, b in zip(s1[key], s2[key])]
        total = sum(d)
        idle = d[3] + (d[4] if len(d) > 4 else 0)
        usage[key] = round((1 - idle / max(total, 1)) * 100, 1)
    return usage


def get_cpu_power():
    """Try to read CPU package power from RAPL (may need root)."""
    energy_path = "/sys/class/powercap/intel-rapl:0/energy_uj"
    try:
        with open(energy_path) as f:
            e1 = int(f.read().strip())
        time.sleep(0.1)
        with open(energy_path) as f:
            e2 = int(f.read().strip())
        return round((e2 - e1) / 100000, 1)  # 0.1s = 100000 us
    except (OSError, PermissionError, ValueError):
        # Fallback: try turbostat or return None
        return None


# Cache for rocm-smi output — avoids spawning a process on every frame/value read
_rocm_smi_cache = {"timestamp": 0, "output": ""}
_ROCM_SMI_TTL = 5  # seconds between rocm-smi polls


def _get_rocm_smi_output():
    """Run rocm-smi at most once per _ROCM_SMI_TTL seconds, return cached output."""
    now = time.time()
    if now - _rocm_smi_cache["timestamp"] < _ROCM_SMI_TTL:
        return _rocm_smi_cache["output"]
    try:
        result = subprocess.run(
            ["/opt/rocm/bin/rocm-smi", "--showuse", "--showclocks", "--showmemuse", "--showmeminfo", "vram"],
            capture_output=True, text=True, timeout=5
        )
        _rocm_smi_cache["output"] = result.stdout
        _rocm_smi_cache["timestamp"] = now
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass  # keep stale cache on failure
    return _rocm_smi_cache["output"]


def get_gpu_data(gpu_id):
    """Get GPU data from sysfs (temps/power) + cached rocm-smi (clocks/usage)."""
    # Find the right amdgpu hwmon
    hwmon_base = None
    gpu_count = 0
    for d in sorted(Path("/sys/class/hwmon").iterdir(), key=lambda p: p.name):
        name = read_sysfs(d / "name")
        if name == "amdgpu":
            if gpu_count == gpu_id:
                hwmon_base = d
                break
            gpu_count += 1

    data = {
        "edge_temp": None, "junction_temp": None, "mem_temp": None,
        "fan_rpm": None, "voltage": None, "power": None,
        "sclk": None, "mclk": None, "gpu_use": None,
        "vram_used": None, "vram_total": None, "mem_use": None,
    }

    if hwmon_base:
        data["edge_temp"] = read_sysfs_int(hwmon_base / "temp1_input", 1000)
        data["junction_temp"] = read_sysfs_int(hwmon_base / "temp2_input", 1000)
        data["mem_temp"] = read_sysfs_int(hwmon_base / "temp3_input", 1000)
        data["fan_rpm"] = read_sysfs_int(hwmon_base / "fan1_input")
        data["voltage"] = read_sysfs_int(hwmon_base / "in0_input")  # mV
        data["power"] = read_sysfs_int(hwmon_base / "power1_average", 1000000)  # uW -> W

    # Parse cached rocm-smi output for clocks and usage
    out = _get_rocm_smi_output()
    prefix = f"GPU[{gpu_id}]"
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith(prefix):
            continue
        if "sclk" in line and "Mhz" in line:
            try:
                data["sclk"] = int(line.split("(")[1].split("Mhz")[0])
            except (IndexError, ValueError):
                pass
        elif "mclk" in line and "Mhz" in line:
            try:
                data["mclk"] = int(line.split("(")[1].split("Mhz")[0])
            except (IndexError, ValueError):
                pass
        elif "GPU use (%)" in line:
            try:
                data["gpu_use"] = int(line.split(":")[2].strip())
            except (IndexError, ValueError):
                pass
        elif "GPU Memory Allocated (VRAM%)" in line:
            try:
                data["mem_use"] = int(line.split(":")[2].strip())
            except (IndexError, ValueError):
                pass
        elif "VRAM Total Memory (B)" in line:
            try:
                data["vram_total"] = int(line.split(":")[2].strip())
            except (IndexError, ValueError):
                pass
        elif "VRAM Total Used Memory (B)" in line:
            try:
                data["vram_used"] = int(line.split(":")[2].strip())
            except (IndexError, ValueError):
                pass

    return data


def get_ram_temps():
    """Read DIMM temps from jc42 sensors."""
    temps = []
    for d in sorted(Path("/sys/class/hwmon").iterdir(), key=lambda p: p.name):
        name = read_sysfs(d / "name")
        if name == "jc42":
            t = read_sysfs_int(d / "temp1_input", 1000)
            temps.append(t)
    return temps


# ─── Frame Rendering ──────────────────────────────────────────────────────────

def load_font(size):
    """Try to load a good monospace font, fall back to default."""
    font_paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationMono-Bold.ttf",
        "/usr/share/fonts/truetype/ubuntu/UbuntuMono-Bold.ttf",
    ]
    for fp in font_paths:
        if os.path.exists(fp):
            return ImageFont.truetype(fp, size)
    return ImageFont.load_default()


FONT_LG = None
FONT_MD = None
FONT_SM = None
FONT_XS = None
FONT_HUGE = None
FONT_GLANCE_LABEL = None


def init_fonts():
    global FONT_LG, FONT_MD, FONT_SM, FONT_XS, FONT_HUGE, FONT_GLANCE_LABEL
    FONT_LG = load_font(36)
    FONT_MD = load_font(22)
    FONT_SM = load_font(16)
    FONT_XS = load_font(13)
    FONT_HUGE = load_font(140)
    FONT_GLANCE_LABEL = load_font(30)


def temp_color(temp, warn=70, crit=85):
    """Return color based on temperature thresholds."""
    if temp is None:
        return DIM_COLOR
    if temp >= crit:
        return CRIT_COLOR
    if temp >= warn:
        return WARN_COLOR
    return VALUE_COLOR


def draw_bar(draw, x, y, w, h, pct, color, bg=BAR_BG):
    """Draw a horizontal progress bar."""
    draw.rounded_rectangle([x, y, x + w, y + h], radius=4, fill=bg)
    if pct and pct > 0:
        fill_w = max(4, int(w * min(pct, 100) / 100))
        draw.rounded_rectangle([x, y, x + fill_w, y + h], radius=4, fill=color)


def fmt_val(val, unit="", decimals=0):
    """Format a sensor value or return '--'."""
    if val is None:
        return "--"
    if decimals == 0:
        return f"{int(val)}{unit}"
    return f"{val:.{decimals}f}{unit}"


def render_cpu_page(brightness=1.0):
    """Render the CPU stats page as a 480x480 PIL Image."""
    img = Image.new("RGB", (DISPLAY_W, DISPLAY_H), BG_COLOR)
    draw = ImageDraw.Draw(img)

    # Header
    draw.rounded_rectangle([0, 0, 480, 52], radius=0, fill=HEADER_BG)
    draw.text((16, 10), "CPU", font=FONT_LG, fill=ACCENT_CPU)
    draw.text((100, 10), "Ryzen 9 5900XT", font=FONT_MD, fill=DIM_COLOR)

    temps = get_cpu_temps()
    y = 60

    # ── Temperatures ──
    draw.text((16, y), "TEMPERATURES", font=FONT_SM, fill=LABEL_COLOR)
    y += 22
    for label, key, warn, crit in [("Tctl", "Tctl", 75, 90), ("CCD1", "CCD1", 80, 95), ("CCD2", "CCD2", 80, 95)]:
        t = temps.get(key)
        draw.text((24, y), f"{label}:", font=FONT_MD, fill=LABEL_COLOR)
        draw.text((110, y), fmt_val(t, " C"), font=FONT_MD, fill=temp_color(t, warn, crit))
        if t is not None:
            draw_bar(draw, 220, y + 4, 240, 16, t, temp_color(t, warn, crit))
        y += 28

    # RAM temps
    ram_temps = get_ram_temps()
    if ram_temps:
        draw.text((24, y), "DIMM:", font=FONT_MD, fill=LABEL_COLOR)
        dimm_str = " / ".join(fmt_val(t, "") for t in ram_temps) + " C"
        draw.text((110, y), dimm_str, font=FONT_MD, fill=VALUE_COLOR)
        y += 28

    y += 6
    # ── Core Usage ──
    draw.text((16, y), "CORE USAGE", font=FONT_SM, fill=LABEL_COLOR)
    y += 22

    usage = get_cpu_usage()
    # Draw 16 cores in a 4x4 grid
    col_w = 112
    row_h = 22
    for i in range(16):
        col = i % 4
        row = i // 4
        cx = 16 + col * col_w
        cy = y + row * row_h
        key = f"cpu{i}"
        u = usage.get(key, 0)
        color = ACCENT_CPU if u < 70 else (WARN_COLOR if u < 90 else CRIT_COLOR)
        draw.text((cx, cy), f"C{i:02d}", font=FONT_XS, fill=DIM_COLOR)
        draw.text((cx + 30, cy), f"{u:5.1f}%", font=FONT_XS, fill=color)

    y += 4 * row_h + 8

    # ── Clock Speeds ──
    draw.text((16, y), "CLOCKS (MHz)", font=FONT_SM, fill=LABEL_COLOR)
    y += 22
    freqs = get_cpu_freqs()
    if freqs:
        avg_freq = sum(freqs) / len(freqs)
        max_freq = max(freqs)
        min_freq = min(freqs)
        draw.text((24, y), f"Avg: {avg_freq:.0f}", font=FONT_MD, fill=VALUE_COLOR)
        draw.text((200, y), f"Max: {max_freq:.0f}", font=FONT_MD, fill=ACCENT_CPU)
        draw.text((370, y), f"Min: {min_freq:.0f}", font=FONT_MD, fill=DIM_COLOR)
        y += 28

    # ── Power ──
    y += 4
    draw.text((16, y), "POWER", font=FONT_SM, fill=LABEL_COLOR)
    y += 22
    power = get_cpu_power()
    draw.text((24, y), f"Package: {fmt_val(power, ' W', 1)}", font=FONT_MD, fill=VALUE_COLOR)
    if power:
        tdp_pct = min(power / 142 * 100, 100)  # 142W TDP for 5900XT
        draw_bar(draw, 250, y + 4, 210, 16, tdp_pct, ACCENT_CPU)

    # Timestamp
    draw.text((16, 458), datetime.now().strftime("%H:%M:%S"), font=FONT_XS, fill=DIM_COLOR)
    draw.text((400, 458), "CPU", font=FONT_XS, fill=ACCENT_CPU)

    if brightness < 1.0:
        enhancer = ImageEnhance.Brightness(img)
        img = enhancer.enhance(brightness)

    return img


def render_gpu_page(gpu_id, brightness=1.0):
    """Render GPU0 or GPU1 stats page as a 480x480 PIL Image."""
    accent = ACCENT_GPU0 if gpu_id == 0 else ACCENT_GPU1
    label = f"GPU {gpu_id}"

    img = Image.new("RGB", (DISPLAY_W, DISPLAY_H), BG_COLOR)
    draw = ImageDraw.Draw(img)

    # Header
    draw.rounded_rectangle([0, 0, 480, 52], radius=0, fill=HEADER_BG)
    draw.text((16, 10), label, font=FONT_LG, fill=accent)
    draw.text((140, 10), "RX 7900 XTX", font=FONT_MD, fill=DIM_COLOR)
    draw.text((330, 16), "24GB GDDR6", font=FONT_SM, fill=DIM_COLOR)

    data = get_gpu_data(gpu_id)
    y = 60

    # ── Temperatures ──
    draw.text((16, y), "TEMPERATURES", font=FONT_SM, fill=LABEL_COLOR)
    y += 22
    for lbl, key, warn, crit in [
        ("Edge", "edge_temp", 70, 90),
        ("Junction", "junction_temp", 80, 100),
        ("Memory", "mem_temp", 85, 105),
    ]:
        t = data.get(key)
        draw.text((24, y), f"{lbl}:", font=FONT_MD, fill=LABEL_COLOR)
        draw.text((140, y), fmt_val(t, " C"), font=FONT_MD, fill=temp_color(t, warn, crit))
        if t is not None:
            draw_bar(draw, 240, y + 4, 220, 16, t, temp_color(t, warn, crit))
        y += 28

    y += 8
    # ── GPU Usage ──
    draw.text((16, y), "UTILIZATION", font=FONT_SM, fill=LABEL_COLOR)
    y += 22
    gpu_use = data.get("gpu_use")
    draw.text((24, y), "GPU:", font=FONT_MD, fill=LABEL_COLOR)
    draw.text((100, y), fmt_val(gpu_use, "%"), font=FONT_MD, fill=VALUE_COLOR)
    draw_bar(draw, 180, y + 4, 280, 16, gpu_use or 0, accent)
    y += 28

    mem_use = data.get("mem_use")
    draw.text((24, y), "VRAM:", font=FONT_MD, fill=LABEL_COLOR)
    draw.text((100, y), fmt_val(mem_use, "%"), font=FONT_MD, fill=VALUE_COLOR)
    draw_bar(draw, 180, y + 4, 280, 16, mem_use or 0, accent)
    y += 28

    # VRAM details
    vram_used = data.get("vram_used")
    vram_total = data.get("vram_total")
    if vram_used is not None and vram_total is not None:
        used_gb = vram_used / (1024**3)
        total_gb = vram_total / (1024**3)
        draw.text((24, y), f"VRAM: {used_gb:.1f} / {total_gb:.1f} GB",
                  font=FONT_MD, fill=VALUE_COLOR)
        y += 28

    y += 8
    # ── Clocks ──
    draw.text((16, y), "CLOCKS", font=FONT_SM, fill=LABEL_COLOR)
    y += 22
    sclk = data.get("sclk")
    mclk = data.get("mclk")
    draw.text((24, y), f"Core:", font=FONT_MD, fill=LABEL_COLOR)
    draw.text((120, y), fmt_val(sclk, " MHz"), font=FONT_MD, fill=VALUE_COLOR)
    if sclk is not None:
        draw_bar(draw, 280, y + 4, 180, 16, sclk / 25, accent)  # 2500 MHz max
    y += 28
    draw.text((24, y), f"Mem:", font=FONT_MD, fill=LABEL_COLOR)
    draw.text((120, y), fmt_val(mclk, " MHz"), font=FONT_MD, fill=VALUE_COLOR)
    y += 28

    y += 8
    # ── Power & Voltage ──
    draw.text((16, y), "POWER", font=FONT_SM, fill=LABEL_COLOR)
    y += 22
    power = data.get("power")
    voltage = data.get("voltage")
    cap = 320 if gpu_id == 0 else 303
    draw.text((24, y), f"Power:", font=FONT_MD, fill=LABEL_COLOR)
    draw.text((120, y), fmt_val(power, f" W (cap {cap}W)"), font=FONT_MD, fill=VALUE_COLOR)
    if power is not None:
        draw_bar(draw, 280, y + 4, 180, 16, power / cap * 100, accent)
    y += 28
    draw.text((24, y), "Voltage:", font=FONT_MD, fill=LABEL_COLOR)
    draw.text((140, y), fmt_val(voltage, " mV"), font=FONT_MD, fill=VALUE_COLOR)
    fan = data.get("fan_rpm")
    draw.text((280, y), "Fan:", font=FONT_MD, fill=LABEL_COLOR)
    draw.text((340, y), fmt_val(fan, " RPM"), font=FONT_MD, fill=VALUE_COLOR)

    # Timestamp
    draw.text((16, 458), datetime.now().strftime("%H:%M:%S"), font=FONT_XS, fill=DIM_COLOR)
    draw.text((400, 458), label, font=FONT_XS, fill=accent)

    if brightness < 1.0:
        enhancer = ImageEnhance.Brightness(img)
        img = enhancer.enhance(brightness)

    return img


# ─── Glanceable Single-Value Frames ───────────────────────────────────────────

def render_glance_frame(label, value, unit, color, chip_label, brightness=1.0):
    """Render a single big number with label, for across-the-room readability.

    Layout:
      - Thin colored bar at top
      - Chip label (e.g. "CPU", "GPU 0") in chip color, top-left
      - Metric label (e.g. "Tctl", "Edge Temp") centered below
      - HUGE number centered
      - Unit label below the number
    """
    img = Image.new("RGB", (DISPLAY_W, DISPLAY_H), BG_COLOR)
    draw = ImageDraw.Draw(img)

    # Top accent bar
    draw.rectangle([0, 0, 480, 6], fill=color)

    # Chip label top-left
    draw.text((16, 14), chip_label, font=FONT_GLANCE_LABEL, fill=color)

    # Metric label centered
    label_bbox = draw.textbbox((0, 0), label, font=FONT_GLANCE_LABEL)
    label_w = label_bbox[2] - label_bbox[0]
    draw.text(((480 - label_w) // 2, 60), label, font=FONT_GLANCE_LABEL, fill=LABEL_COLOR)

    # Big number centered
    val_str = "--" if value is None else f"{int(value)}" if isinstance(value, (int, float)) else str(value)
    val_bbox = draw.textbbox((0, 0), val_str, font=FONT_HUGE)
    val_w = val_bbox[2] - val_bbox[0]
    val_h = val_bbox[3] - val_bbox[1]
    val_x = (480 - val_w) // 2
    val_y = (480 - val_h) // 2 - 20
    draw.text((val_x, val_y), val_str, font=FONT_HUGE, fill=color)

    # Unit below the number
    unit_bbox = draw.textbbox((0, 0), unit, font=FONT_GLANCE_LABEL)
    unit_w = unit_bbox[2] - unit_bbox[0]
    draw.text(((480 - unit_w) // 2, val_y + val_h + 20), unit, font=FONT_GLANCE_LABEL, fill=DIM_COLOR)

    if brightness < 1.0:
        enhancer = ImageEnhance.Brightness(img)
        img = enhancer.enhance(brightness)

    return img


def build_glance_sequence():
    """Build the list of glanceable frames to cycle through.

    Returns list of dicts with keys: label, source_fn, unit, color, chip_label
    Each source_fn() returns the current numeric value.
    """
    # CPU values (BLUE)
    cpu_color = ACCENT_CPU  # blue

    def cpu_tctl():
        return get_cpu_temps().get("Tctl")

    def cpu_ccd1():
        return get_cpu_temps().get("CCD1")

    def cpu_ccd2():
        return get_cpu_temps().get("CCD2")

    def cpu_dimm_avg():
        temps = get_ram_temps()
        valid = [t for t in temps if t is not None]
        return round(sum(valid) / len(valid), 0) if valid else None

    def cpu_clock_avg():
        freqs = get_cpu_freqs()
        return round(sum(freqs) / len(freqs), 0) if freqs else None

    def cpu_power():
        return get_cpu_power()

    # GPU 0 values (RED)
    gpu0_color = (255, 50, 50)  # red

    def gpu0_edge():
        return get_gpu_data(0).get("edge_temp")

    def gpu0_junction():
        return get_gpu_data(0).get("junction_temp")

    def gpu0_mem_temp():
        return get_gpu_data(0).get("mem_temp")

    def gpu0_power():
        return get_gpu_data(0).get("power")

    def gpu0_sclk():
        return get_gpu_data(0).get("sclk")

    # GPU 1 values (GREEN)
    gpu1_color = ACCENT_GPU1  # green

    def gpu1_edge():
        return get_gpu_data(1).get("edge_temp")

    def gpu1_junction():
        return get_gpu_data(1).get("junction_temp")

    def gpu1_mem_temp():
        return get_gpu_data(1).get("mem_temp")

    def gpu1_power():
        return get_gpu_data(1).get("power")

    def gpu1_sclk():
        return get_gpu_data(1).get("sclk")

    return [
        # CPU block (blue)
        {"label": "Tctl",       "fn": cpu_tctl,     "unit": "\u00b0C",  "color": cpu_color,  "chip": "CPU"},
        {"label": "CCD1",       "fn": cpu_ccd1,     "unit": "\u00b0C",  "color": cpu_color,  "chip": "CPU"},
        {"label": "CCD2",       "fn": cpu_ccd2,     "unit": "\u00b0C",  "color": cpu_color,  "chip": "CPU"},
        {"label": "DIMM Avg",   "fn": cpu_dimm_avg, "unit": "\u00b0C",  "color": cpu_color,  "chip": "CPU"},
        {"label": "Clock Avg",  "fn": cpu_clock_avg,"unit": "MHz",      "color": cpu_color,  "chip": "CPU"},
        {"label": "Pkg Power",  "fn": cpu_power,    "unit": "W",        "color": cpu_color,  "chip": "CPU"},
        # GPU 0 block (red)
        {"label": "Edge Temp",  "fn": gpu0_edge,    "unit": "\u00b0C",  "color": gpu0_color, "chip": "GPU 0"},
        {"label": "Junction",   "fn": gpu0_junction,"unit": "\u00b0C",  "color": gpu0_color, "chip": "GPU 0"},
        {"label": "Mem Temp",   "fn": gpu0_mem_temp,"unit": "\u00b0C",  "color": gpu0_color, "chip": "GPU 0"},
        {"label": "Power",      "fn": gpu0_power,   "unit": "W",        "color": gpu0_color, "chip": "GPU 0"},
        {"label": "Core Clock", "fn": gpu0_sclk,    "unit": "MHz",      "color": gpu0_color, "chip": "GPU 0"},
        # GPU 1 block (green)
        {"label": "Edge Temp",  "fn": gpu1_edge,    "unit": "\u00b0C",  "color": gpu1_color, "chip": "GPU 1"},
        {"label": "Junction",   "fn": gpu1_junction,"unit": "\u00b0C",  "color": gpu1_color, "chip": "GPU 1"},
        {"label": "Mem Temp",   "fn": gpu1_mem_temp,"unit": "\u00b0C",  "color": gpu1_color, "chip": "GPU 1"},
        {"label": "Power",      "fn": gpu1_power,   "unit": "W",        "color": gpu1_color, "chip": "GPU 1"},
        {"label": "Core Clock", "fn": gpu1_sclk,    "unit": "MHz",      "color": gpu1_color, "chip": "GPU 1"},
    ]


# ─── AIO USB Display Driver ──────────────────────────────────────────────────

class AIODisplay:
    """Manages USB communication with the Thermalright Grand Vision LCD."""

    def __init__(self):
        self.dev = None
        self.ep_out = None
        self.connected = False

    def connect(self):
        """Find and claim the USB device."""
        self.dev = usb.core.find(idVendor=VID, idProduct=PID)
        if self.dev is None:
            print("AIO display not found on USB.")
            return False

        # Detach kernel driver if active
        try:
            if self.dev.is_kernel_driver_active(0):
                self.dev.detach_kernel_driver(0)
        except (usb.core.USBError, NotImplementedError):
            pass

        try:
            self.dev.set_configuration()
        except usb.core.USBError:
            pass  # May already be configured

        cfg = self.dev.get_active_configuration()
        intf = cfg[(0, 0)]

        # Find bulk OUT endpoint
        self.ep_out = usb.util.find_descriptor(
            intf,
            custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_OUT
        )

        if self.ep_out is None:
            print("Could not find OUT endpoint on AIO display.")
            return False

        # Handshake
        if self._handshake():
            self.connected = True
            print(f"AIO display connected: {DISPLAY_W}x{DISPLAY_H}")
            return True
        else:
            print("AIO handshake failed, but attempting frame sends anyway.")
            self.connected = True
            return True

    def _handshake(self):
        """Send device info query and read response."""
        pkt = bytearray(64)
        pkt[0:4] = MAGIC
        pkt[56] = 0x01  # DEV_INFO command at offset 0x38

        ep_in = usb.util.find_descriptor(
            self.dev.get_active_configuration()[(0, 0)],
            custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_IN
        )

        try:
            self.ep_out.write(pkt, timeout=1000)
            if ep_in:
                resp = ep_in.read(1024, timeout=1000)
                if len(resp) > 24 and resp[24] != 0:
                    return True
        except usb.core.USBError as e:
            print(f"Handshake USB error: {e}")
        return False

    def send_frame(self, img):
        """Encode PIL Image as JPEG and send to display."""
        if not self.connected:
            return False

        # Convert to JPEG bytes
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=JPEG_QUALITY)
        jpeg_data = buf.getvalue()

        # Build 64-byte header
        header = bytearray(64)
        header[0:4] = MAGIC
        struct.pack_into("<I", header, 4, CMD_JPEG)         # command
        struct.pack_into("<I", header, 8, DISPLAY_W)         # width
        struct.pack_into("<I", header, 12, DISPLAY_H)        # height
        struct.pack_into("<I", header, 56, 0x02)             # mode
        struct.pack_into("<I", header, 60, len(jpeg_data))   # payload length

        payload = bytes(header) + jpeg_data

        try:
            # Send in 16K chunks
            for offset in range(0, len(payload), CHUNK_SIZE):
                chunk = payload[offset:offset + CHUNK_SIZE]
                self.ep_out.write(chunk, timeout=5000)

            # ZLP if payload is 512-aligned
            if len(payload) % 512 == 0:
                self.ep_out.write(b"", timeout=1000)

            time.sleep(0.015)  # 15ms post-transfer
            return True
        except usb.core.USBError as e:
            print(f"Frame send error: {e}")
            return False

    def disconnect(self):
        """Release the USB device."""
        if self.dev:
            try:
                usb.util.dispose_resources(self.dev)
            except Exception:
                pass
        self.connected = False


# ─── RGB Control ──────────────────────────────────────────────────────────────

class RGBController:
    """Control motherboard RGB headers via liquidctl."""

    def __init__(self):
        self.rgb_on = True

    def toggle(self):
        """Toggle all RGB on/off."""
        self.rgb_on = not self.rgb_on
        if self.rgb_on:
            self._set_rgb_on()
        else:
            self._set_rgb_off()
        return self.rgb_on

    def _set_rgb_off(self):
        """Turn off motherboard RGB headers."""
        try:
            for ch in ["led1", "led2", "led3", "led4", "led5"]:
                subprocess.run(
                    ["liquidctl", "set", ch, "color", "off"],
                    capture_output=True, timeout=3
                )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

    def _set_rgb_on(self):
        """Restore rainbow color cycle on motherboard RGB headers."""
        try:
            for ch in ["led1", "led2", "led3", "led4", "led5"]:
                subprocess.run(
                    ["liquidctl", "set", ch, "color", "color-cycle",
                     "--speed", "fastest"],
                    capture_output=True, timeout=3
                )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass


# ─── Terminal Input ───────────────────────────────────────────────────────────

class KeyReader:
    """Non-blocking single-character keyboard input."""

    def __init__(self):
        self.old_settings = None

    def start(self):
        if not sys.stdin.isatty():
            return
        self.old_settings = termios.tcgetattr(sys.stdin)
        tty.setcbreak(sys.stdin.fileno())

    def stop(self):
        if self.old_settings:
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, self.old_settings)

    def read(self, timeout=0.05):
        """Return a character if available, else None."""
        if not sys.stdin.isatty():
            return None
        if select.select([sys.stdin], [], [], timeout)[0]:
            return sys.stdin.read(1)
        return None


# ─── Main Loop ────────────────────────────────────────────────────────────────

class MonitorApp:
    """Main application coordinating display, sensors, and input."""

    PAGES = ["cpu", "gpu0", "gpu1"]

    def __init__(self, headless=False):
        self.headless = headless
        self.display = AIODisplay() if not headless else None
        self.rgb = RGBController()
        self.keys = KeyReader()
        self.brightness = 0.85
        self.mode = "rotate"          # "rotate" or "static"
        self.current_page = 0         # 0=CPU, 1=GPU0, 2=GPU1 (for static detail)
        self.static_since = 0         # timestamp when static mode entered
        self.static_timeout = 60      # seconds before returning to rotate
        self.rotate_interval = 3.0    # seconds per glance frame in rotate mode
        self.last_rotate = time.time()
        self.running = True
        self.glance_seq = []          # built in run()
        self.glance_idx = 0           # current position in glance sequence

    def run(self):
        """Main entry point."""
        init_fonts()
        self.glance_seq = build_glance_sequence()

        # Connect to display
        if self.display:
            if not self.display.connect():
                print("Falling back to headless mode (terminal preview only).")
                self.display = None
                self.headless = True

        self.keys.start()

        print("\n=== AIO System Monitor ===")
        print("Keys: 1=CPU  2=GPU0  3=GPU1  r=rotate  b/B=bright  l=RGB  q=quit")
        print("=" * 40)

        try:
            while self.running:
                self._handle_input()
                self._update_mode()
                frame = self._render_current()

                if self.display:
                    self.display.send_frame(frame)

                # Terminal status line
                rgb_str = "ON" if self.rgb.rgb_on else "OFF"
                if self.mode == "rotate":
                    g = self.glance_seq[self.glance_idx]
                    mode_str = f"GLANCE {g['chip']} / {g['label']}"
                else:
                    page_name = self.PAGES[self.current_page].upper()
                    mode_str = f"DETAIL {page_name}"
                sys.stdout.write(
                    f"\r  [{mode_str}] | "
                    f"Bright: {self.brightness:.0%} | RGB: {rgb_str}    "
                )
                sys.stdout.flush()

                # Pace: glance frames hold 3s, detail pages refresh faster
                time.sleep(0.5 if self.mode == "static" else 0.3)

        except KeyboardInterrupt:
            pass
        finally:
            self.keys.stop()
            if self.display:
                self.display.disconnect()
            print("\nMonitor stopped.")

    def _handle_input(self):
        """Process keyboard input."""
        key = self.keys.read()
        if key is None:
            return

        if key == "q":
            self.running = False
        elif key == "1":
            self._set_static(0)
        elif key == "2":
            self._set_static(1)
        elif key == "3":
            self._set_static(2)
        elif key == "r":
            self.mode = "rotate"
            self.last_rotate = time.time()
            print("\n  >> Rotation resumed")
        elif key == "b":
            self.brightness = max(0.1, self.brightness - 0.1)
            print(f"\n  >> Brightness: {self.brightness:.0%}")
        elif key == "B":
            self.brightness = min(1.0, self.brightness + 0.1)
            print(f"\n  >> Brightness: {self.brightness:.0%}")
        elif key == "l":
            state = self.rgb.toggle()
            print(f"\n  >> RGB: {'ON' if state else 'OFF'}")

    def _set_static(self, page):
        """Switch to static mode on a given page."""
        self.mode = "static"
        self.current_page = page
        self.static_since = time.time()

    def _update_mode(self):
        """Handle rotation and static timeout."""
        now = time.time()

        if self.mode == "static":
            if now - self.static_since > self.static_timeout:
                self.mode = "rotate"
                self.last_rotate = now
        elif self.mode == "rotate":
            if now - self.last_rotate >= self.rotate_interval:
                self.glance_idx = (self.glance_idx + 1) % len(self.glance_seq)
                self.last_rotate = now

    def _render_current(self):
        """Render the current frame."""
        if self.mode == "static":
            # Detailed page
            if self.current_page == 0:
                return render_cpu_page(self.brightness)
            elif self.current_page == 1:
                return render_gpu_page(0, self.brightness)
            else:
                return render_gpu_page(1, self.brightness)
        else:
            # Glanceable single-value frame
            g = self.glance_seq[self.glance_idx]
            value = g["fn"]()
            return render_glance_frame(
                label=g["label"],
                value=value,
                unit=g["unit"],
                color=g["color"],
                chip_label=g["chip"],
                brightness=self.brightness,
            )


# ─── Entry Point ──────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Thermalright Grand Vision AIO System Monitor")
    parser.add_argument("--headless", action="store_true",
                        help="Run without AIO display (terminal status only)")
    parser.add_argument("--brightness", type=float, default=0.85,
                        help="Initial brightness 0.1-1.0 (default: 0.85)")
    parser.add_argument("--rotate-interval", type=float, default=3.0,
                        help="Seconds per page in rotation mode (default: 3)")
    parser.add_argument("--static-timeout", type=float, default=60,
                        help="Seconds before static page returns to rotation (default: 60)")
    args = parser.parse_args()

    app = MonitorApp(headless=args.headless)
    app.brightness = max(0.1, min(1.0, args.brightness))
    app.rotate_interval = args.rotate_interval
    app.static_timeout = args.static_timeout

    # Handle SIGTERM gracefully
    signal.signal(signal.SIGTERM, lambda *_: setattr(app, 'running', False))

    app.run()


if __name__ == "__main__":
    main()
