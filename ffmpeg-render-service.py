import os
import time
import uuid
import subprocess
import requests as req_lib
from flask import Flask, request, jsonify, send_from_directory

app = Flask(__name__)

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rendered")
os.makedirs(OUTPUT_DIR, exist_ok=True)

PORT = int(os.environ.get("PORT", 5000))

# ---------------------------------------------------------------------------
# Frame constants
# ---------------------------------------------------------------------------
FRAME_W = 1080
FRAME_H = 1920
SAFE_MARGIN = 100  # px padding on each side (was 60)
MAX_TEXT_W = FRAME_W - 2 * SAFE_MARGIN  # 880 px usable width (was 960)

# ---------------------------------------------------------------------------
# Font configuration - DejaVu Sans is installed via apt
# ---------------------------------------------------------------------------
FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_PATH_REGULAR = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

# Font-size ranges (max -> min) for each text role
FONT_RANGES = {
    "headline":    {"max": 72, "min": 32, "step": 2},   # min was 44
    "subheadline": {"max": 52, "min": 28, "step": 2},   # min was 32
    "cta":         {"max": 48, "min": 26, "step": 2},   # min was 30
}

# Stroke width used in drawtext (borderw) - must be accounted for in measurement
STROKE_W = 3


def _find_font_path():
    """Return a working .ttf path - try DejaVu first, then fall back."""
    candidates = [
        FONT_PATH,
        FONT_PATH_REGULAR,
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    return "Sans"  # FFmpeg built-in fallback


# ---------------------------------------------------------------------------
# Text measurement with Pillow  (accounts for stroke width + safety buffer)
# ---------------------------------------------------------------------------
try:
    from PIL import ImageFont

    def measure_text_width(text, font_size, bold=True):
        """Return pixel width of *text* rendered at *font_size*.

        Adds stroke-width compensation (borderw * 2) and a 20 % safety buffer
        so the FFmpeg drawtext output never clips at the frame edge.
        """
        fpath = FONT_PATH if bold else FONT_PATH_REGULAR
        if not os.path.exists(fpath):
            fpath = _find_font_path()
        try:
            font = ImageFont.truetype(fpath, font_size)
            bbox = font.getbbox(text)
            raw_w = bbox[2] - bbox[0]
        except Exception:
            raw_w = int(len(text) * font_size * 0.6)
        # Add stroke compensation + 20 % safety buffer
        return int((raw_w + STROKE_W * 2) * 1.20)

except ImportError:
    def measure_text_width(text, font_size, bold=True):
        raw_w = int(len(text) * font_size * 0.6)
        return int((raw_w + STROKE_W * 2) * 1.20)


def auto_fit_fontsize(text, role="headline"):
    """Pick the largest font size that keeps *text* inside MAX_TEXT_W.

    Returns (font_size, letter_spacing).
    letter_spacing is 0 normally, or negative if we had to compress.
    """
    cfg = FONT_RANGES.get(role, FONT_RANGES["headline"])
    fmax, fmin, fstep = cfg["max"], cfg["min"], cfg["step"]

    for fs in range(fmax, fmin - 1, -fstep):
        w = measure_text_width(text, fs)
        if w <= MAX_TEXT_W:
            return fs, 0

    # At min font size, try compressing letter spacing
    fs = fmin
    for spacing in range(0, -6, -1):
        estimated_w = measure_text_width(text, fs) + spacing * max(len(text) - 1, 1)
        if estimated_w <= MAX_TEXT_W:
            return fs, spacing

    return fmin, 0


# ---------------------------------------------------------------------------
# Word-wrapping: split long text into multiple lines when it won't fit
# ---------------------------------------------------------------------------
def wrap_text_to_width(text, role="headline"):
    """Auto-fit *text* and, if it still overflows, word-wrap into multiple lines.

    Returns a list of ``(line_text, font_size, letter_spacing)`` tuples.
    """
    if not text or not text.strip():
        return [("", FONT_RANGES.get(role, FONT_RANGES["headline"])["max"], 0)]

    # First try single-line fit
    fs, sp = auto_fit_fontsize(text, role)
    w = measure_text_width(text, fs) + sp * max(len(text) - 1, 1)
    if w <= MAX_TEXT_W:
        return [(text, fs, sp)]

    # Need to wrap - find font size where wrapped lines fit
    cfg = FONT_RANGES.get(role, FONT_RANGES["headline"])
    fmax, fmin, fstep = cfg["max"], cfg["min"], cfg["step"]

    words = text.split()

    for fs in range(fmax, fmin - 1, -fstep):
        lines = _break_into_lines(words, fs)
        if all(measure_text_width(ln, fs) <= MAX_TEXT_W for ln in lines):
            return [(ln, fs, 0) for ln in lines]

    # At minimum size, force-wrap whatever we get
    fs = fmin
    lines = _break_into_lines(words, fs)
    return [(ln, fs, 0) for ln in lines]


def _break_into_lines(words, font_size):
    """Greedily pack *words* into lines that fit within MAX_TEXT_W."""
    lines = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip() if current else word
        if measure_text_width(candidate, font_size) <= MAX_TEXT_W:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines if lines else [""]


# ---------------------------------------------------------------------------
# FFmpeg helpers
# ---------------------------------------------------------------------------
def get_base_url():
    """Return the public base URL."""
    domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN")
    if domain:
        return f"https://{domain}"
    render_url = os.environ.get("RENDER_EXTERNAL_URL")
    if render_url:
        return render_url.rstrip("/")
    return f"http://0.0.0.0:{PORT}"


def esc(t):
    """Escape text for FFmpeg drawtext filter."""
    return (
        t.replace("\\", "\\\\")
        .replace("'", "\u2019")
        .replace(":", "\\:")
        .replace("%", "%%")
        .replace('"', '\\"')
    )


def clamp_x_expr(margin=SAFE_MARGIN):
    """Return an FFmpeg expression that centres text but clamps to safe margins."""
    m = margin
    return f"max({m}\\,min((w-text_w)/2\\,w-text_w-{m}))"


def drawtext_filter(text, fontsize, yexpr, fontcolor="white",
                    borderw=3, bordercolor="black", bold=True,
                    letter_spacing=0):
    """Build a single drawtext= clause."""
    fpath = _find_font_path()
    parts = [
        f"drawtext=text='{esc(text)}'",
        f"fontsize={fontsize}",
        f"fontcolor={fontcolor}",
        f"borderw={borderw}",
        f"bordercolor={bordercolor}",
        f"x={clamp_x_expr()}",
        f"y={yexpr}",
        f"fontfile={fpath}",
    ]
    if letter_spacing != 0:
        try:
            parts.append(f"letter_spacing={letter_spacing}")
        except Exception:
            pass
    return ":".join(parts)


def build_multiline_drawtext(wrapped_lines, base_y_expr, fontcolor="white",
                             borderw=3, bordercolor="black"):
    """Build a chain of drawtext filters for wrapped (possibly multi-line) text.

    *wrapped_lines* is a list of (text, fontsize, letter_spacing) tuples.
    Returns a list of drawtext filter strings.
    """
    filters = []
    for idx, (text, fontsize, sp) in enumerate(wrapped_lines):
        if not text.strip():
            continue
        line_height = int(fontsize * 1.3)
        if idx == 0:
            yexpr = base_y_expr
        else:
            yexpr = f"{base_y_expr}+{line_height * idx}"
        filters.append(drawtext_filter(
            text, fontsize, yexpr,
            fontcolor=fontcolor, borderw=borderw,
            bordercolor=bordercolor, letter_spacing=sp
        ))
    return filters


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True, "service": "ffmpeg-render", "version": "3.0-textwrap"}), 200


@app.route("/files/<path:filename>", methods=["GET"])
def serve_file(filename):
    return send_from_directory(OUTPUT_DIR, filename, mimetype="video/mp4")


@app.route("/render-reel", methods=["POST"])
def render_reel():
    data = request.get_json(force=True)
    image_urls = data.get("image_urls", [])
    headline_line1 = data.get("headline_line1", "")
    headline_line2 = data.get("headline_line2", "")
    subheadline = data.get("subheadline", "")
    cta_line1 = data.get("cta_line1", "")
    cta_line2 = data.get("cta_line2", "")

    if len(image_urls) < 4:
        return jsonify({"status": "error", "message": "Need exactly 4 image_urls"}), 400

    job_id = f"reel_{int(time.time())}_{uuid.uuid4().hex[:6]}"
    work_dir = os.path.join(OUTPUT_DIR, f"tmp_{job_id}")
    os.makedirs(work_dir, exist_ok=True)

    try:
        # --- Download images ------------------------------------------------
        img_paths = []
        for i, url in enumerate(image_urls[:4]):
            resp = req_lib.get(url, timeout=30, allow_redirects=True)
            resp.raise_for_status()
            img_path = os.path.join(work_dir, f"img{i}.jpg")
            with open(img_path, "wb") as f:
                f.write(resp.content)
            img_paths.append(img_path)

        # --- Auto-fit + wrap text -------------------------------------------
        h1_lines = wrap_text_to_width(headline_line1, "headline")
        h2_lines = wrap_text_to_width(headline_line2, "headline")
        sub_lines = wrap_text_to_width(subheadline, "subheadline")
        cta1_lines = wrap_text_to_width(cta_line1, "cta")
        cta2_lines = wrap_text_to_width(cta_line2, "cta")

        # Extract representative font sizes for response
        h1_size = h1_lines[0][1] if h1_lines else 72
        h2_size = h2_lines[0][1] if h2_lines else 72
        sub_size = sub_lines[0][1] if sub_lines else 52
        cta1_size = cta1_lines[0][1] if cta1_lines else 48
        cta2_size = cta2_lines[0][1] if cta2_lines else 48

        # --- Y positions (proportional to 1920-high frame) ------------------
        h1_y = "h*0.28"
        # h2 starts after h1 block
        h1_block_height = int(h1_size * 1.3) * len(h1_lines) + 12
        h2_y = f"h*0.28+{h1_block_height}"
        sub_y = "h*0.50"
        cta1_y = "h*0.68"
        cta1_block_height = int(cta1_size * 1.3) * len(cta1_lines) + 10
        cta2_y = f"h*0.68+{cta1_block_height}"

        # --- Build filter_complex -------------------------------------------
        inputs_args = []
        for p in img_paths:
            inputs_args.extend(["-loop", "1", "-t", "3", "-i", p])

        filter_parts = []
        concat_inputs = ""

        for i in range(4):
            base = (
                f"[{i}]scale={FRAME_W}:{FRAME_H}:"
                f"force_original_aspect_ratio=increase,"
                f"crop={FRAME_W}:{FRAME_H},setsar=1,"
                f"fps=30,format=yuv420p[bg{i}];"
            )
            filter_parts.append(base)

            # Build all drawtext filters for this frame
            dt_filters = []
            dt_filters.extend(build_multiline_drawtext(
                h1_lines, h1_y, fontcolor="white", borderw=3))
            dt_filters.extend(build_multiline_drawtext(
                h2_lines, h2_y, fontcolor="white", borderw=3))
            dt_filters.extend(build_multiline_drawtext(
                sub_lines, sub_y, fontcolor="yellow", borderw=2))
            dt_filters.extend(build_multiline_drawtext(
                cta1_lines, cta1_y, fontcolor="white", borderw=2))
            dt_filters.extend(build_multiline_drawtext(
                cta2_lines, cta2_y, fontcolor="white", borderw=2))

            if dt_filters:
                txt = f"[bg{i}]" + ",".join(dt_filters) + f"[v{i}];"
            else:
                txt = f"[bg{i}]null[v{i}];"
            filter_parts.append(txt)
            concat_inputs += f"[v{i}]"

        filter_parts.append(f"{concat_inputs}concat=n=4:v=1:a=0[outv]")
        filter_complex = "".join(filter_parts)

        out_filename = f"{job_id}.mp4"
        out_path = os.path.join(OUTPUT_DIR, out_filename)

        cmd = (
            ["ffmpeg", "-y", "-threads", "1"]
            + inputs_args
            + [
                "-filter_complex", filter_complex,
                "-map", "[outv]",
                "-c:v", "libx264",
                "-preset", "ultrafast",
                "-crf", "28",
                "-pix_fmt", "yuv420p",
                "-r", "24",
                "-t", "12",
                out_path,
            ]
        )

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            return jsonify({
                "status": "error",
                "message": "FFmpeg failed",
                "stderr": result.stderr[-2000:] if result.stderr else "",
                "font_sizes": {
                    "headline_line1": h1_size,
                    "headline_line2": h2_size,
                    "subheadline": sub_size,
                    "cta_line1": cta1_size,
                    "cta_line2": cta2_size,
                }
            }), 500

        video_url = f"{get_base_url()}/files/{out_filename}"
        return jsonify({
            "status": "success",
            "video_url": video_url,
            "font_sizes": {
                "headline_line1": h1_size,
                "headline_line2": h2_size,
                "subheadline": sub_size,
                "cta_line1": cta1_size,
                "cta_line2": cta2_size,
            },
            "line_counts": {
                "headline_line1": len(h1_lines),
                "headline_line2": len(h2_lines),
                "subheadline": len(sub_lines),
                "cta_line1": len(cta1_lines),
                "cta_line2": len(cta2_lines),
            },
        }), 200

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        import shutil
        if os.path.exists(work_dir):
            shutil.rmtree(work_dir, ignore_errors=True)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
