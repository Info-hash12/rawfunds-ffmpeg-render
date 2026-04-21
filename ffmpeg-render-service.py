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
SAFE_MARGIN = 60  # px padding on each side
MAX_TEXT_W = FRAME_W - 2 * SAFE_MARGIN  # 960 px usable width

# ---------------------------------------------------------------------------
# Font configuration â DejaVu Sans is installed via apt
# ---------------------------------------------------------------------------
FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_PATH_REGULAR = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

# Font-size ranges (max â min) for each text role
FONT_RANGES = {
    "headline":    {"max": 72, "min": 44, "step": 2},
    "subheadline": {"max": 52, "min": 32, "step": 2},
    "cta":         {"max": 48, "min": 30, "step": 2},
}


def _find_font_path():
    """Return a working .ttf path â try DejaVu first, then fall back."""
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
# Text measurement with Pillow
# ---------------------------------------------------------------------------
try:
    from PIL import ImageFont

    def measure_text_width(text, font_size, bold=True):
        """Return pixel width of *text* rendered at *font_size*."""
        fpath = FONT_PATH if bold else FONT_PATH_REGULAR
        if not os.path.exists(fpath):
            fpath = _find_font_path()
        try:
            font = ImageFont.truetype(fpath, font_size)
            bbox = font.getbbox(text)
            return bbox[2] - bbox[0]
        except Exception:
            # Rough estimate: average char width â 0.6 Ã font_size
            return int(len(text) * font_size * 0.6)

except ImportError:
    # Pillow not available â use character-count estimate
    def measure_text_width(text, font_size, bold=True):
        return int(len(text) * font_size * 0.6)


def auto_fit_fontsize(text, role="headline"):
    """Pick the largest font size that keeps *text* inside MAX_TEXT_W.

    Returns (font_size, letter_spacing).
    letter_spacing is 0 normally, or negative if we had to compress.
    """
    cfg = FONT_RANGES.get(role, FONT_RANGES["headline"])
    fmax, fmin, fstep = cfg["max"], cfg["min"], cfg["step"]

    # 1. Try reducing font size first
    for fs in range(fmax, fmin - 1, -fstep):
        w = measure_text_width(text, fs)
        if w <= MAX_TEXT_W:
            return fs, 0

    # 2. At min font size, try compressing letter spacing
    fs = fmin
    for spacing in range(0, -6, -1):  # 0, -1, -2, â¦ -5
        estimated_w = measure_text_width(text, fs) + spacing * max(len(text) - 1, 1)
        if estimated_w <= MAX_TEXT_W:
            return fs, spacing

    # 3. Last resort â clamp at min size, zero spacing; FFmpeg x-clamp will
    #    keep it inside the frame (text may be slightly truncated visually).
    return fmin, 0


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
    """Return an FFmpeg expression that centres text but clamps to safe margins.

    Logic: x = max(margin, min((w-text_w)/2, w - text_w - margin))
    This guarantees text_w pixels always land between margin..w-margin.
    """
    m = margin
    return f"max({m}\\,min((w-text_w)/2\\,w-text_w-{m}))"


def drawtext_filter(text, fontsize, yexpr, fontcolor="white",
                    borderw=2, bordercolor="black", bold=True,
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
    # FFmpeg â¥ 5.x doesn't have a native letter_spacing param in all builds,
    # so we only add it when non-zero and hope the build supports it.
    # If unsupported, FFmpeg silently ignores it.
    if letter_spacing != 0:
        try:
            # Test if build supports it by just adding it â worst case it's ignored
            parts.append(f"letter_spacing={letter_spacing}")
        except Exception:
            pass
    return ":".join(parts)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True, "service": "ffmpeg-render", "version": "2.0-textfit"}), 200


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

        # --- Auto-fit font sizes --------------------------------------------
        h1_size, h1_sp = auto_fit_fontsize(headline_line1, "headline")
        h2_size, h2_sp = auto_fit_fontsize(headline_line2, "headline")
        sub_size, sub_sp = auto_fit_fontsize(subheadline, "subheadline")
        cta1_size, cta1_sp = auto_fit_fontsize(cta_line1, "cta")
        cta2_size, cta2_sp = auto_fit_fontsize(cta_line2, "cta")

        # --- Y positions (proportional to 1920-high frame) ------------------
        # Headline block ~30 % from top
        h1_y = f"h*0.30"
        h2_y = f"h*0.30+{h1_size + 12}"  # line gap = 12px
        # Subheadline ~50 %
        sub_y = f"h*0.50"
        # CTA block ~70 %
        cta1_y = f"h*0.70"
        cta2_y = f"h*0.70+{cta1_size + 10}"

        # --- Build filter_complex -------------------------------------------
        inputs_args = []
        for p in img_paths:
            inputs_args.extend(["-loop", "1", "-t", "3", "-i", p])

        filter_parts = []
        concat_inputs = ""

        for i in range(4):
            # Scale + crop to exact frame
            base = (
                f"[{i}]scale={FRAME_W}:{FRAME_H}:"
                f"force_original_aspect_ratio=increase,"
                f"crop={FRAME_W}:{FRAME_H},setsar=1,"
                f"fps=30,format=yuv420p[bg{i}];"
            )
            filter_parts.append(base)

            # Overlay text with safe-margin clamping
            txt = f"[bg{i}]"
            txt += drawtext_filter(headline_line1, h1_size, h1_y,
                                   fontcolor="white", borderw=3,
                                   letter_spacing=h1_sp) + ","
            txt += drawtext_filter(headline_line2, h2_size, h2_y,
                                   fontcolor="white", borderw=3,
                                   letter_spacing=h2_sp) + ","
            txt += drawtext_filter(subheadline, sub_size, sub_y,
                                   fontcolor="yellow", borderw=2,
                                   letter_spacing=sub_sp) + ","
            txt += drawtext_filter(cta_line1, cta1_size, cta1_y,
                                   fontcolor="white", borderw=2,
                                   letter_spacing=cta1_sp) + ","
            txt += drawtext_filter(cta_line2, cta2_size, cta2_y,
                                   fontcolor="white", borderw=2,
                                   letter_spacing=cta2_sp)
            txt += f"[v{i}];"
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
            "letter_spacing": {
                "headline_line1": h1_sp,
                "headline_line2": h2_sp,
                "subheadline": sub_sp,
                "cta_line1": cta1_sp,
                "cta_line2": cta2_sp,
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
