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


def get_base_url():
    """Return the public base URL (Railway sets RAILWAY_PUBLIC_DOMAIN)."""
    domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN")
    if domain:
        return f"https://{domain}"
    return f"http://0.0.0.0:{PORT}"


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True, "service": "ffmpeg-render"}), 200


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
        img_paths = []
        for i, url in enumerate(image_urls[:4]):
            resp = req_lib.get(url, timeout=30, allow_redirects=True)
            resp.raise_for_status()
            img_path = os.path.join(work_dir, f"img{i}.jpg")
            with open(img_path, "wb") as f:
                f.write(resp.content)
            img_paths.append(img_path)

        inputs_args = []
        for i, p in enumerate(img_paths):
            inputs_args.extend(["-loop", "1", "-t", "3", "-i", p])

        def esc(t):
            return t.replace("\\", "\\\\").replace("'", "\u2019").replace(":", "\\:").replace("%", "%%")

        filter_parts = []
        concat_inputs = ""
        for i in range(4):
            base = f"[{i}]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1,fps=30,format=yuv420p[bg{i}];"
            filter_parts.append(base)
            txt = f"[bg{i}]"
            txt += f"drawtext=text='{esc(headline_line1)}':fontsize=52:fontcolor=white:borderw=3:bordercolor=black:x=(w-text_w)/2:y=h*0.30:font=Sans,"
            txt += f"drawtext=text='{esc(headline_line2)}':fontsize=48:fontcolor=white:borderw=3:bordercolor=black:x=(w-text_w)/2:y=h*0.30+70:font=Sans,"
            txt += f"drawtext=text='{esc(subheadline)}':fontsize=40:fontcolor=yellow:borderw=2:bordercolor=black:x=(w-text_w)/2:y=h*0.50:font=Sans,"
            txt += f"drawtext=text='{esc(cta_line1)}':fontsize=34:fontcolor=white:borderw=2:bordercolor=black:x=(w-text_w)/2:y=h*0.70:font=Sans,"
            txt += f"drawtext=text='{esc(cta_line2)}':fontsize=34:fontcolor=white:borderw=2:bordercolor=black:x=(w-text_w)/2:y=h*0.70+50:font=Sans"
            txt += f"[v{i}];"
            filter_parts.append(txt)
            concat_inputs += f"[v{i}]"

        filter_parts.append(f"{concat_inputs}concat=n=4:v=1:a=0[outv]")
        filter_complex = "".join(filter_parts)

        out_filename = f"{job_id}.mp4"
        out_path = os.path.join(OUTPUT_DIR, out_filename)

        cmd = ["ffmpeg", "-y"] + inputs_args + ["-filter_complex", filter_complex, "-map", "[outv]", "-c:v", "libx264", "-preset", "fast", "-crf", "23", "-pix_fmt", "yuv420p", "-r", "30", "-t", "12", out_path]

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            return jsonify({"status": "error", "message": "FFmpeg failed", "stderr": result.stderr[-2000:] if result.stderr else ""}), 500

        video_url = f"{get_base_url()}/files/{out_filename}"
        return jsonify({"status": "success", "video_url": video_url}), 200

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        import shutil
        if os.path.exists(work_dir):
            shutil.rmtree(work_dir, ignore_errors=True)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
