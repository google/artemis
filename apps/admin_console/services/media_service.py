# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
from typing import Any
import urllib.parse
from fastapi import HTTPException

from artemis.config import IMAGES_DIR, TRACES_PATH, WORKSPACE_ROOT


class MediaService:
    """Service handling media indexing, local file security, payload unwrapping and notes/plans."""

    @classmethod
    def ensure_browser_playable_video(cls, p: Path) -> Path:
        """Ensure the video file is in a browser-supported container (MP4).

        If given an MKV, check if a converted MP4 already exists or convert it.
        """
        try:
            if not p.exists():
                return p
            suffix = p.suffix.lower()
            if suffix == ".mp4":
                return p
            if suffix in (".mkv", ".webm"):
                mp4_cand = p.with_suffix(".mp4")
                if mp4_cand.exists() and mp4_cand.stat().st_size > 0:
                    return mp4_cand
                from artemis.utils.video import get_ffmpeg_path
                ffmpeg = get_ffmpeg_path()
                # 1. Attempt fast copy remux
                subprocess.run(
                    [
                        ffmpeg,
                        "-y",
                        "-fflags",
                        "+genpts",
                        "-i",
                        str(p),
                        "-c",
                        "copy",
                        "-movflags",
                        "+faststart",
                        str(mp4_cand),
                    ],
                    capture_output=True,
                    timeout=15,
                )
                if mp4_cand.exists() and mp4_cand.stat().st_size > 0:
                    return mp4_cand
                # 2. If copy remux failed, fallback to ultrafast re-encode
                subprocess.run(
                    [
                        ffmpeg,
                        "-y",
                        "-fflags",
                        "+genpts+discardcorrupt",
                        "-i",
                        str(p),
                        "-c:v",
                        "libx264",
                        "-preset",
                        "ultrafast",
                        "-pix_fmt",
                        "yuv420p",
                        "-movflags",
                        "+faststart",
                        str(mp4_cand),
                    ],
                    capture_output=True,
                    timeout=30,
                )
                if mp4_cand.exists() and mp4_cand.stat().st_size > 0:
                    return mp4_cand
        except Exception:
            pass
        return p

    @staticmethod
    def path_to_video_url(p: Path) -> str:
        resolved = p.resolve()
        try:
            rel = resolved.relative_to(WORKSPACE_ROOT)
            return f"/videos/{urllib.parse.quote(rel.as_posix(), safe='/')}"
        except ValueError:
            return f"/videos/{urllib.parse.quote(resolved.as_posix().lstrip('/'), safe='/:')}"

    @classmethod
    def build_video_index(cls) -> dict[str, str]:
        idx = {}
        dirs = [
            TRACES_PATH,
            WORKSPACE_ROOT / "artemis-traces",
            WORKSPACE_ROOT / ".benchmarks" / "diagnoser" / "outputs" / "artifacts",
        ]
        for d in dirs:
            if not d.exists():
                continue
            try:
                for item in d.iterdir():
                    if item.is_dir():
                        for ext in [".mp4", ".mkv", ".webm"]:
                            vfile = item / f"recording{ext}"
                            if vfile.exists():
                                vfile = cls.ensure_browser_playable_video(vfile)
                                url = cls.path_to_video_url(vfile)
                                idx[item.name] = url
                                prefix = item.name.split("_PASS_")[0].split("_FAIL_")[0]
                                idx[prefix] = url
                                break
                    elif item.suffix in [".mp4", ".mkv", ".webm"]:
                        item = cls.ensure_browser_playable_video(item)
                        url = cls.path_to_video_url(item)
                        idx[item.stem] = url
                        idx[item.name] = url
            except Exception:
                pass
        return idx

    @classmethod
    def resolve_video_url(
        cls,
        row_dict: dict[str, Any],
        video_rec_map: dict[str, str],
        video_idx: dict[str, str],
    ) -> str | None:
        raw_sid = row_dict.get("session_id")
        s_id = (
            str(raw_sid).strip()
            if raw_sid is not None and str(raw_sid).strip().lower() != "none"
            else ""
        )
        v_url = None
        v_fp = row_dict.get("video_filepath")
        if v_fp and os.path.exists(v_fp):
            p = cls.ensure_browser_playable_video(Path(v_fp))
            v_url = cls.path_to_video_url(p)

        orig_rec = video_rec_map.get(s_id)
        if not v_url and orig_rec:
            p = Path(orig_rec)
            if p.exists():
                p = cls.ensure_browser_playable_video(p)
                v_url = cls.path_to_video_url(p)
            else:
                folder_name = p.parent.name
                v_url = video_idx.get(folder_name) or video_idx.get(folder_name.split("_")[0])

        if not v_url:
            v_url = video_idx.get(s_id)

        if not v_url and s_id:
            sess_dir = TRACES_PATH / s_id
            if sess_dir.is_dir():
                for ext in [".mp4", ".webm", ".mkv"]:
                    vfile = sess_dir / f"recording{ext}"
                    if vfile.exists():
                        vfile = cls.ensure_browser_playable_video(vfile)
                        v_url = cls.path_to_video_url(vfile)
                        break
                if not v_url:
                    try:
                        for item in sess_dir.iterdir():
                            if item.is_file() and item.suffix.lower() in [".mp4", ".webm", ".mkv"]:
                                item = cls.ensure_browser_playable_video(item)
                                v_url = cls.path_to_video_url(item)
                                break
                    except Exception:
                        pass

        if not v_url and row_dict.get("initial_goal"):
            goal_str = row_dict["initial_goal"]
            task_hint = (
                goal_str.split("Task: ")[1].split()[0].strip()
                if "Task: " in goal_str
                else goal_str.split("\n")[0].strip()
            )
            v_url = video_idx.get(task_hint)

        return v_url

    @classmethod
    def resolve_video_segments(cls, video_url: str | None) -> list[dict[str, Any]]:
        """Resolve a recording sidecar manifest into browser-safe segment URLs."""
        if not video_url or not video_url.startswith("/videos/"):
            return []
        relative = urllib.parse.unquote(video_url.removeprefix("/videos/"))
        video_path = (WORKSPACE_ROOT / relative).resolve()
        manifest_path = video_path.parent / "recording.json"
        if not manifest_path.is_file():
            return []
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            segments = []
            for item in payload.get("segments", []):
                segment_path = (manifest_path.parent / str(item["file"])).resolve()
                if segment_path.parent != manifest_path.parent.resolve() or not segment_path.is_file():
                    continue
                segments.append(
                    {
                        "url": cls.path_to_video_url(segment_path),
                        "start": float(item.get("start", 0)),
                        "duration": float(item.get("duration", 0)),
                        "width": int(item.get("width", 0)),
                        "height": int(item.get("height", 0)),
                    }
                )
            return segments
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return []

    @staticmethod
    def unwrap_payload(obj: Any) -> Any:
        """Unwraps trace payloads, storing base64 images as disk files and
        truncating large dumps.
        """
        if isinstance(obj, str):
            try:
                if (obj.startswith("{") and obj.endswith("}")) or (
                    obj.startswith("[") and obj.endswith("]")
                ):
                    return MediaService.unwrap_payload(json.loads(obj))
            except Exception:
                pass

            # Check if the string is a Base64 image
            is_base64_img = False
            base64_data = None
            if obj.startswith("data:image/") and ";base64," in obj:
                try:
                    _, base64_data = obj.split(";base64,", 1)
                    is_base64_img = True
                except Exception:
                    pass
            elif obj.startswith("iVBORw0KGgo") or obj.startswith("/9j/"):
                is_base64_img = True
                base64_data = obj

            if is_base64_img and base64_data:
                try:
                    image_bytes = base64.b64decode(base64_data)
                    image_hash = hashlib.sha256(image_bytes).hexdigest()

                    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
                    image_path = IMAGES_DIR / f"{image_hash}.jpg"
                    if not image_path.exists():
                        image_path.write_bytes(image_bytes)

                    return f"image://{image_hash}"
                except Exception:
                    pass

            if len(obj) > 2000:
                preview_start = obj[:100].replace("\n", " ")
                preview_end = obj[-100:].replace("\n", " ")
                return (
                    f"<Massive String: {preview_start}...[truncated"
                    f" {len(obj) - 200} characters for UI performance]...{preview_end}"
                    f" (length={len(obj)})>"
                )
            return obj
        elif isinstance(obj, dict):
            return {k: MediaService.unwrap_payload(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            if len(obj) > 20 and any(
                isinstance(x, dict) and ("bounds" in x or "resource-id" in x) for x in obj
            ):
                return [f"<XML UI List with {len(obj)} elements truncated for UI performance>"]
            return [MediaService.unwrap_payload(x) for x in obj]
        return obj

    @staticmethod
    def get_safe_local_file(path_str: str) -> tuple[Path, str]:
        decoded_path = urllib.parse.unquote(path_str)
        if decoded_path.startswith("file://"):
            decoded_path = decoded_path[len("file://") :]
        p = Path(decoded_path).resolve()

        if not p.is_file():
            raise HTTPException(status_code=404, detail="File not found")

        if not str(p).startswith(str(WORKSPACE_ROOT)):
            raise HTTPException(
                status_code=403,
                detail="Access denied. Path is outside workspace root.",
            )

        ext = p.suffix.lower()
        media_type = "application/octet-stream"
        if ext in (".jpg", ".jpeg"):
            media_type = "image/jpeg"
        elif ext == ".png":
            media_type = "image/png"
        elif ext == ".mp4":
            media_type = "video/mp4"

        return p, media_type

    @staticmethod
    def get_task_plan_content(session_id: str) -> str:
        plan_path = TRACES_PATH / session_id / "notes" / "task_plan.md"
        if not plan_path.exists():
            plan_path = TRACES_PATH / "notes" / "task_plan.md"

        if plan_path.exists():
            try:
                return plan_path.read_text(encoding="utf-8")
            except Exception as e:
                return f"Error reading task plan: {e}"
        return "No task plan created yet."

    @staticmethod
    def get_session_notes_content(session_id: str) -> dict[str, str]:
        notes_dir = TRACES_PATH / session_id / "notes"
        notes_content = {}

        if notes_dir.exists() and notes_dir.is_dir():
            for file in notes_dir.iterdir():
                if file.is_file() and file.suffix in [".md", ".txt", ".json", ".yaml"]:
                    try:
                        notes_content[file.name] = file.read_text(encoding="utf-8")
                    except Exception as e:
                        notes_content[file.name] = f"Error reading file: {e}"

        if not notes_content:
            global_notes_dir = TRACES_PATH / "notes"
            if global_notes_dir.exists() and global_notes_dir.is_dir():
                for file in global_notes_dir.iterdir():
                    if file.is_file() and file.suffix in [
                        ".md",
                        ".txt",
                        ".json",
                        ".yaml",
                    ]:
                        try:
                            notes_content[file.name] = file.read_text(encoding="utf-8")
                        except Exception as e:
                            notes_content[file.name] = f"Error reading file: {e}"

        return notes_content


media_service = MediaService()
