import asyncio
import json
import os
import shutil
from pathlib import Path

from fonemago.content import Catalog, prepared_path, sha256
from fonemago.wiki import fetch


async def process(*args: str) -> bytes:
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, _stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
    except BaseException:
        if proc.returncode is None:
            proc.kill()
        await proc.communicate()
        raise
    if proc.returncode:
        raise ValueError(f"Audio processing failed ({Path(args[0]).name})")
    return stdout


async def prepare_audio(catalog: Catalog, root: Path):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise ValueError("Install ffmpeg (including ffprobe) before preparing audio")
    (root / "originals").mkdir(parents=True, exist_ok=True)
    (root / "prepared").mkdir(parents=True, exist_ok=True)
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    for asset in catalog.assets:
        original = root / "originals" / f"{asset.id}{asset.original_suffix}"
        if not original.is_file() or sha256(original) != asset.original_sha256:
            raw = await fetch(asset.download_url)
            import hashlib

            if hashlib.sha256(raw).hexdigest() != asset.original_sha256:
                raise ValueError(f"Source changed: {asset.id}; review before updating checksum")
            temp = original.with_suffix(original.suffix + ".part")
            temp.write_bytes(raw)
            os.replace(temp, original)
        target = prepared_path(root, asset.id)
        entry = manifest.get(asset.id, {})
        if (target.is_file() and entry.get("original_sha256") == asset.original_sha256
                and entry.get("prepared_sha256") == sha256(target)):
            continue
        # Local downloaded files only; protocols other than file/pipe are disabled.
        info = json.loads(await process(
            "ffprobe", "-v", "error", "-protocol_whitelist", "file,pipe", "-show_format",
            "-show_streams", "-of", "json", str(original),
        ))
        duration = float(info["format"]["duration"])
        if not 0 < duration <= 30 or not any(
            s.get("codec_type") == "audio" for s in info["streams"]
        ):
            raise ValueError(f"Invalid reference audio: {asset.id}")
        temp = target.with_suffix(".part.mp3")
        try:
            await process(
                "ffmpeg", "-nostdin", "-v", "error", "-y", "-protocol_whitelist", "file,pipe",
                "-i", str(original), "-map", "0:a:0", "-vn", "-map_metadata", "-1",
                "-codec:a", "libmp3lame", "-q:a", "2", str(temp),
            )
            os.replace(temp, target)
        finally:
            temp.unlink(missing_ok=True)
        manifest[asset.id] = {
            "original_sha256": asset.original_sha256, "prepared_sha256": sha256(target),
            "duration_sec": duration, "source_url": asset.source_url,
        }
        temp_manifest = manifest_path.with_suffix(".tmp")
        temp_manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
        os.replace(temp_manifest, manifest_path)
        print(f"Prepared: {asset.id}")
