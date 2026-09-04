from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    flash,
    send_from_directory,
)
from pathlib import Path
from datetime import datetime
import json
import os
import re
import subprocess

app = Flask(__name__)
app.secret_key = "change-this-later"

BASE_DIR = Path("/app")
DRAFT_DIR = BASE_DIR / "drafts"
IMAGE_DIR = BASE_DIR / "images"
BUILD_DIR = BASE_DIR / "jenkins-builds"
PUBLISHED_DIR = BASE_DIR / "published"
PUBLISHED_DIR.mkdir(parents=True, exist_ok=True)

def published_marker(filename):
    return PUBLISHED_DIR / f"{Path(filename).stem}.published"


def publishing_lock(filename):
    return PUBLISHED_DIR / f"{Path(filename).stem}.publishing"


def find_generate_build(draft_filename):
    for manifest in BUILD_DIR.glob("[0-9]*.env"):
        try:
            values = {}

            for line in manifest.read_text(
                encoding="utf-8"
            ).splitlines():
                if "=" in line:
                    key, value = line.split("=", 1)
                    values[key] = value

            if values.get("DRAFT_FILE") == draft_filename:
                return values.get(
                    "GENERATE_BUILD",
                    manifest.stem
                )

        except Exception:
            continue

    return None

def get_drafts():
    drafts = []

    for file in sorted(
        DRAFT_DIR.glob("*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    ):
        try:
            data = json.loads(
                file.read_text(encoding="utf-8")
            )
        except Exception:
            continue

        prefix = file.stem
        image = IMAGE_DIR / f"{prefix}.png"

        drafts.append(
            {
                "filename": file.name,
                "prefix": prefix,
                "post_text": data.get("post_text", ""),
                "research": data.get("research"),
                "has_image": image.exists(),
                "published": published_marker(file.name).exists(),
            }
        )

    return drafts

def published_marker(filename):
    return PUBLISHED_DIR / f"{Path(filename).stem}.published"


def find_generate_build(draft_filename):
    for manifest in BUILD_DIR.glob("[0-9]*.env"):
        try:
            values = {}

            for line in manifest.read_text(
                encoding="utf-8"
            ).splitlines():
                if "=" in line:
                    key, value = line.split("=", 1)
                    values[key] = value

            if values.get("DRAFT_FILE") == draft_filename:
                return (
                    values.get("GENERATE_BUILD")
                    or manifest.stem
                )

        except Exception:
            continue

    return None

@app.route("/")
def dashboard():
    return render_template(
        "dashboard.html",
        drafts=get_drafts(),
    )


@app.route("/generate", methods=["GET", "POST"])
def generate():
    if request.method == "POST":
        topic = request.form.get("topic", "").strip()
        auto_topic = request.form.get("auto_topic")

        cmd = [
            "python",
            "/app/linkedin_auto.py",
            "--base-dir",
            "/app",
        ]

        if auto_topic:
            cmd.append("--auto-topic")
        elif topic:
            cmd.extend(["--topic", topic])
        else:
            flash("Enter a topic or select automatic topic.")
            return redirect(url_for("generate"))

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:
            flash(result.stderr or "Generation failed.")
        else:
            flash("Draft generated successfully.")

        return redirect(url_for("dashboard"))

    return render_template("generate.html")

@app.route("/preview/<filename>")
def preview(filename):

    if Path(filename).name != filename:
        return "Invalid filename", 400

    path = DRAFT_DIR / filename

    if not path.exists():
        return "Draft not found", 404

    data = json.loads(
        path.read_text(encoding="utf-8")
    )

    image_file = IMAGE_DIR / f"{path.stem}.png"

    return render_template(
        "preview.html",
        draft=data,
        filename=filename,
        image_name=(
            image_file.name
            if image_file.exists()
            else None
        ),
        published=published_marker(filename).exists(),
        publishing=publishing_lock(filename).exists(),
    )

@app.route("/publish/<filename>", methods=["POST"])
def publish(filename):

    if Path(filename).name != filename:
        return "Invalid filename", 400

    draft_path = DRAFT_DIR / filename
    image_path = IMAGE_DIR / f"{draft_path.stem}.png"

    if not draft_path.exists():
        return "Draft not found", 404

    if not image_path.exists():
        flash("Image file is missing. Publishing stopped.")
        return redirect(
            url_for("preview", filename=filename)
        )

    PUBLISHED_DIR.mkdir(
        parents=True,
        exist_ok=True
    )

    marker = published_marker(filename)
    lock = publishing_lock(filename)

    generate_build = find_generate_build(filename)

    if generate_build:
        build_marker = (
            BUILD_DIR /
            f"{generate_build}.published"
        )
    else:
        build_marker = None

    # -------------------------------
    # Duplicate checks
    # -------------------------------

    if marker.exists():
        flash("This draft has already been published.")
        return redirect(
            url_for("preview", filename=filename)
        )

    if build_marker and build_marker.exists():
        flash(
            f"Generation build #{generate_build} "
            "has already been published."
        )
        return redirect(
            url_for("preview", filename=filename)
        )

    if lock.exists():
        flash(
            "This draft currently has a publishing lock. "
            "Check LinkedIn before trying again."
        )
        return redirect(
            url_for("preview", filename=filename)
        )

    # -------------------------------
    # Pre-publish write test
    # -------------------------------

    test_file = (
        PUBLISHED_DIR /
        f".flask-write-test-{os.getpid()}"
    )

    try:
        test_file.write_text(
            "test",
            encoding="utf-8"
        )
        test_file.unlink()

    except Exception as exc:
        flash(
            f"Cannot write publish marker: {exc}"
        )
        return redirect(
            url_for("preview", filename=filename)
        )

    # -------------------------------
    # Create atomic publishing lock
    # -------------------------------

    lock_content = (
        f"GENERATE_BUILD={generate_build or 'UI'}\n"
        f"DRAFT_FILE={filename}\n"
        f"STARTED_AT={datetime.now().astimezone().isoformat()}\n"
        f"PUBLISHED_BY=flask-ui\n"
    )

    try:
        fd = os.open(
            lock,
            os.O_WRONLY |
            os.O_CREAT |
            os.O_EXCL,
            0o640
        )

        with os.fdopen(fd, "w") as f:
            f.write(lock_content)

    except FileExistsError:
        flash(
            "This post is already being published."
        )
        return redirect(
            url_for("preview", filename=filename)
        )

    # -------------------------------
    # Publish
    # -------------------------------

    cmd = [
        "python",
        "/app/linkedin_auto.py",
        "--base-dir",
        "/app",
        "--draft-json",
        str(draft_path),
        "--image-file",
        str(image_path),
        "--publish",
    ]

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:

        # Deliberately keep .publishing lock.
        # LinkedIn may have accepted the request before
        # another part of the command failed.
        flash(
            "Publishing returned an error. "
            "The safety lock has been kept. "
            "Check LinkedIn before retrying."
        )

        return redirect(
            url_for("preview", filename=filename)
        )

    # -------------------------------
    # Extract LinkedIn post ID
    # -------------------------------

    post_id = ""

    match = re.search(
        r"Post ID:\s*(\S+)",
        result.stdout
    )

    if match:
        post_id = match.group(1)

    published_at = (
        datetime.now()
        .astimezone()
        .isoformat()
    )

    marker_content = (
        f"GENERATE_BUILD={generate_build or 'UI'}\n"
        f"DRAFT_FILE={filename}\n"
        f"IMAGE_FILE={image_path.name}\n"
        f"PUBLISHED_AT={published_at}\n"
        f"PUBLISHED_BY=flask-ui\n"
        f"LINKEDIN_POST_ID={post_id}\n"
    )

    marker.write_text(
        marker_content,
        encoding="utf-8"
    )

    if build_marker:
        build_marker.write_text(
            marker_content,
            encoding="utf-8"
        )

    lock.unlink(missing_ok=True)

    flash(
        "Post successfully published to LinkedIn."
    )

    return redirect(
        url_for("preview", filename=filename)
    )

@app.route("/images/<filename>")
def images(filename):
    return send_from_directory(
        IMAGE_DIR,
        filename,
    )


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=5000,
        debug=False,
    )
