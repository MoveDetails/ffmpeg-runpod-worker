import os
import re
import tempfile
import subprocess
import runpod
from supabase import create_client, Client
from datetime import datetime, timezone, timedelta

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
BUCKET = "estimate-media"
SIGNED_URL_TTL = 7 * 24 * 3600  # 7 days in seconds

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


def _update_file_entry(estimate_id: str, org_id: str, file_id: str, patch: dict):
    row = (
        supabase.table("estimate_media")
        .select("data")
        .eq("estimate_id", estimate_id)
        .maybe_single()
        .execute()
    )
    if not row.data:
        raise RuntimeError(f"estimate_media row not found for estimate {estimate_id}")

    files = row.data["data"].get("files", [])
    updated = [
        {**f, **patch} if f.get("id") == file_id else f
        for f in files
    ]

    supabase.table("estimate_media").update(
        {"data": {"files": updated}, "updated_at": "now()"}
    ).eq("estimate_id", estimate_id).execute()


def _sign(storage_path: str) -> str:
    res = supabase.storage.from_(BUCKET).create_signed_url(storage_path, SIGNED_URL_TTL)
    if "signedURL" in res:
        return res["signedURL"]
    if "signedUrl" in res:
        return res["signedUrl"]
    raise RuntimeError(f"Failed to sign {storage_path}: {res}")


def _upload_file(local_path: str, storage_path: str, content_type: str):
    with open(local_path, "rb") as f:
        data = f.read()
    supabase.storage.from_(BUCKET).upload(
        storage_path,
        data,
        {"content-type": content_type, "upsert": "true"},
    )


def _run_ffmpeg(args: list[str], label: str):
    result = subprocess.run(
        ["ffmpeg"] + args,
        capture_output=True,
    )
    if result.returncode != 0:
        stdout = result.stdout.decode("utf-8", errors="replace")[-3000:]
        stderr = result.stderr.decode("utf-8", errors="replace")[-3000:]
        raise RuntimeError(
            f"ffmpeg {label} failed (exit {result.returncode}):\n"
            f"STDOUT: {stdout}\nSTDERR: {stderr}"
        )


def _rewrite_m3u8_with_signed_urls(m3u8_path: str, ts_signed: dict[str, str]) -> str:
    with open(m3u8_path, "r") as f:
        content = f.read()
    for filename, signed_url in ts_signed.items():
        content = re.sub(rf"^{re.escape(filename)}$", signed_url, content, flags=re.MULTILINE)
    return content


def handler(job: dict) -> dict:
    inp = job.get("input", {})
    storage_path: str = inp["storage_path"]
    org_id: str = inp["org_id"]
    estimate_id: str = inp["estimate_id"]
    file_id: str = inp["file_id"]

    _update_file_entry(estimate_id, org_id, file_id, {"transcoding_status": "processing"})

    with tempfile.TemporaryDirectory() as tmp:
        raw_path = os.path.join(tmp, "input_video")

        response = supabase.storage.from_(BUCKET).download(storage_path)
        if not isinstance(response, (bytes, bytearray)):
            raise RuntimeError(f"Download failed, got: {response}")
        if len(response) == 0:
            raise RuntimeError(f"Downloaded file is empty: {storage_path}")
        with open(raw_path, "wb") as f:
            f.write(response)

        hls_dir = os.path.join(tmp, "hls")
        os.makedirs(hls_dir)

        playlist_local = os.path.join(hls_dir, "playlist.m3u8")
        segment_pattern = os.path.join(hls_dir, "segment%03d.ts")
        thumbnail_local = os.path.join(hls_dir, "thumbnail.jpg")

        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=codec_type", "-of", "csv=p=0", raw_path],
            capture_output=True,
        )
        has_audio = probe.stdout.strip() != b""

        transcode_args = [
            "-y", "-i", raw_path,
            "-map", "0:v:0",
            "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
        ]
        if has_audio:
            transcode_args += ["-map", "0:a:0", "-c:a", "aac", "-b:a", "128k"]
        else:
            transcode_args += ["-an"]
        transcode_args += [
            "-hls_time", "10",
            "-hls_playlist_type", "vod",
            "-hls_segment_filename", segment_pattern,
            playlist_local,
        ]

        _run_ffmpeg(transcode_args, label="transcode")

        _run_ffmpeg(
            [
                "-y", "-i", raw_path,
                "-frames:v", "1",
                "-update", "1",
                thumbnail_local,
            ],
            label="thumbnail",
        )

        ts_files = sorted(f for f in os.listdir(hls_dir) if f.endswith(".ts"))
        hls_folder = f"{org_id}/{estimate_id}/{file_id}"
        ts_signed: dict[str, str] = {}

        for ts_name in ts_files:
            ts_storage_path = f"{hls_folder}/{ts_name}"
            _upload_file(os.path.join(hls_dir, ts_name), ts_storage_path, "application/octet-stream")
            ts_signed[ts_name] = _sign(ts_storage_path)

        thumbnail_storage_path = f"{hls_folder}/thumbnail.jpg"
        _upload_file(thumbnail_local, thumbnail_storage_path, "image/jpeg")
        thumbnail_signed_url = _sign(thumbnail_storage_path)

        rewritten = _rewrite_m3u8_with_signed_urls(playlist_local, ts_signed)
        rewritten_path = os.path.join(hls_dir, "playlist_signed.m3u8")
        with open(rewritten_path, "w") as f:
            f.write(rewritten)

        m3u8_storage_path = f"{hls_folder}/playlist.m3u8"
        _upload_file(rewritten_path, m3u8_storage_path, "application/vnd.apple.mpegurl")
        m3u8_signed_url = _sign(m3u8_storage_path)

    supabase.storage.from_(BUCKET).remove([storage_path])

    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=SIGNED_URL_TTL)).isoformat()

    _update_file_entry(
        estimate_id,
        org_id,
        file_id,
        {
            "transcoding_status": "ready",
            "hls_folder_path": hls_folder,
            "hls_m3u8_signed_url": m3u8_signed_url,
            "hls_m3u8_signed_url_expires_at": expires_at,
            "thumbnail_path": thumbnail_storage_path,
            "thumbnail_signed_url": thumbnail_signed_url,
            "thumbnail_signed_url_expires_at": expires_at,
            "storage_path": None,
            "signed_url": None,
            "signed_url_expires_at": None,
        },
    )

    return {
        "file_id": file_id,
        "hls_folder_path": hls_folder,
        "m3u8_signed_url": m3u8_signed_url,
        "thumbnail_signed_url": thumbnail_signed_url,
    }


runpod.serverless.start({"handler": handler})
