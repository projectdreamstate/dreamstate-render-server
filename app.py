import os
import uuid
import subprocess
import tempfile
import requests
from flask import Flask, request, jsonify
import cloudinary
import cloudinary.uploader

app = Flask(__name__)

# Cloudinary config — uses env vars set in Railway
cloudinary.config(
    cloud_name="dk8bnnf1b",
    api_key=os.environ["CLOUDINARY_API_KEY"],
    api_secret=os.environ["CLOUDINARY_API_SECRET"]
)

# Volume levels per content mode
VOLUME_MODES = {
    "meditation":  {"voice": 1.0,  "music": 0.15},
    "frequency":   {"voice": 0.0,  "music": 1.0},
    "affirmation": {"voice": 1.0,  "music": 0.15},
    "subliminal":  {"voice": 0.05, "music": 0.85},
}


def download(url, dest_path):
    """Download a file from URL to dest_path."""
    r = requests.get(url, stream=True, timeout=120)
    r.raise_for_status()
    with open(dest_path, "wb") as f:
        for chunk in r.iter_content(chunk_size=8192):
            f.write(chunk)


def probe_streams(path):
    """Return a list of stream codec_types ('video'/'audio') ffprobe finds in a file."""
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "stream=codec_type",
            "-of", "default=noprint_wrappers=1:nokey=1",
            path
        ],
        capture_output=True, text=True, timeout=30
    )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def validate_asset(path, url, kind):
    """Make sure a downloaded asset is real media, not an empty file or an HTML/JSON
    error page that came back with a 200. Raises ValueError with a clear message."""
    size = os.path.getsize(path) if os.path.exists(path) else 0
    if size < 1024:
        # Show the first bytes so we can see if it's an error page rather than media.
        head = ""
        try:
            with open(path, "rb") as f:
                head = f.read(200).decode("utf-8", "replace")
        except Exception:
            pass
        raise ValueError(
            f"{kind} asset is only {size} bytes (likely a 404/placeholder, not media). "
            f"URL: {url} | first bytes: {head!r}"
        )

    streams = probe_streams(path)
    if kind == "visual":
        if "video" not in streams:
            raise ValueError(
                f"visual is not a decodable image/video (ffprobe streams={streams}). "
                f"Check that this Cloudinary URL points at a real image: {url}"
            )
    else:  # audio / music / voice
        if "audio" not in streams:
            raise ValueError(
                f"{kind} has no audio stream (ffprobe streams={streams}). URL: {url}"
            )


def get_duration(path):
    """Get media duration in seconds using ffprobe."""
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            path
        ],
        capture_output=True, text=True, timeout=30
    )
    raw = result.stdout.strip()
    try:
        return float(raw)
    except ValueError:
        raise ValueError(f"could not read a duration from {path} (ffprobe returned {raw!r})")


def build_ffmpeg_cmd(mode, visual_path, voice_path, music_path, output_path, duration):
    """Build the ffmpeg command for the given mode."""
    volumes = VOLUME_MODES.get(mode, VOLUME_MODES["meditation"])

    # Scale + pad visual to 1920x1080 with black bars if needed
    scale_filter = (
        "scale=1280:720:force_original_aspect_ratio=decrease,"
        "pad=1280:720:(ow-iw)/2:(oh-ih)/2,setsar=1[vout]"
    )

    # -nostats + -loglevel error: keep stderr to the *real* error instead of
    # thousands of "frame=0" progress lines that bury it.
    base = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostats", "-y"]

    if mode == "frequency":
        # Music only — no voiceover
        return base + [
            "-loop", "1", "-framerate", "2", "-i", visual_path,
            "-i", music_path,
            "-filter_complex",
            (
                f"[0:v]{scale_filter};"
                f"[1:a]volume={volumes['music']}[aout]"
            ),
            "-map", "[vout]", "-map", "[aout]",
            "-c:v", "libx264", "-tune", "stillimage", "-preset", "ultrafast", "-crf", "23",
            "-c:a", "aac", "-b:a", "192k",
            "-pix_fmt", "yuv420p",
            "-r", "2",
            "-t", str(duration),
            output_path
        ]
    else:
        # Voiceover + background music mixed
        return base + [
            "-loop", "1", "-framerate", "2", "-i", visual_path,
            "-i", voice_path,
            "-i", music_path,
            "-filter_complex",
            (
                f"[0:v]{scale_filter};"
                f"[1:a]dynaudnorm=f=200:g=8,volume={volumes['voice']}[v];"
                f"[2:a]volume={volumes['music']}[m];"
                f"[v][m]amix=inputs=2:duration=first:normalize=0[aout]"
            ),
            "-map", "[vout]", "-map", "[aout]",
            "-c:v", "libx264", "-tune", "stillimage", "-preset", "ultrafast", "-crf", "23",
            "-c:a", "aac", "-b:a", "192k",
            "-pix_fmt", "yuv420p",
            "-r", "2",
            "-t", str(duration),
            output_path
        ]


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@app.route("/render", methods=["POST"])
def render():
    data = request.json
    if not data:
        return jsonify({"error": "No JSON body"}), 400

    mode       = data.get("mode", "meditation")
    audio_url  = data.get("audio_url")   # voiceover (None for frequency)
    video_url  = data.get("video_url")   # visual image from Cloudinary
    music_url  = data.get("music_url")   # background music
    job_id     = data.get("job_id", str(uuid.uuid4()))

    if not video_url or not music_url:
        return jsonify({"error": "video_url and music_url are required"}), 400
    if mode != "frequency" and not audio_url:
        return jsonify({"error": f"audio_url required for mode '{mode}'"}), 400

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            visual_path = os.path.join(tmpdir, "visual.jpg")
            music_path  = os.path.join(tmpdir, "music.mp3")
            output_path = os.path.join(tmpdir, "output.mp4")

            # Download + validate assets (clear errors instead of an opaque ffmpeg failure)
            download(video_url, visual_path)
            validate_asset(visual_path, video_url, "visual")
            download(music_url, music_path)
            validate_asset(music_path, music_url, "music")

            if mode == "frequency":
                duration = get_duration(music_path)
                voice_path = None
            else:
                voice_path = os.path.join(tmpdir, "voice.mp3")
                download(audio_url, voice_path)
                validate_asset(voice_path, audio_url, "voice")
                duration = get_duration(voice_path)

            if not duration or duration < 0.5:
                return jsonify({
                    "error": f"render aborted: computed duration is {duration!r} "
                             f"(audio is empty or unreadable), which would produce 0 frames."
                }), 500

            # Build and run ffmpeg
            cmd = build_ffmpeg_cmd(mode, visual_path, voice_path, music_path, output_path, duration)
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)

            if result.returncode != 0:
                return jsonify({
                    "error": "ffmpeg failed",
                    "returncode": result.returncode,
                    "details": result.stderr[-4000:] or "(ffmpeg printed no error. A negative returncode such as -9 means the OS killed the process, almost always out-of-memory on the host.)"
                }), 500

            # Upload rendered video to Cloudinary
            upload_result = cloudinary.uploader.upload(
                output_path,
                resource_type="video",
                folder="dreamstate/rendered",
                public_id=job_id,
                overwrite=True
            )

            return jsonify({"video_url": upload_result["secure_url"]})

    except requests.exceptions.RequestException as e:
        return jsonify({"error": f"Download failed: {str(e)}"}), 500
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Render timed out (30 min limit)"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
