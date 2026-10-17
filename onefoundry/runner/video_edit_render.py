"""Networkless FFmpeg recipe execution; no DB, credentials, host paths or shell arguments."""

from __future__ import annotations

import hashlib
import json
import math
import struct
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path
from typing import Any

from onefoundry.video_edit_contract import EditRequest, canonical, digest


class RenderFailure(ValueError):
    """A known bounded render failure with a safe reason code."""


def _run(args: list[str], root: Path, timeout: int, *, capture: bool = False) -> bytes:
    result = subprocess.run(  # noqa: S603 - fixed executable/argv assembled from validated numeric recipe
        args,
        cwd=root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=timeout,
        check=False,
        env={"PATH": "/usr/bin:/bin", "HOME": str(root), "LANG": "C.UTF-8"},
    )
    if result.returncode:
        raise RenderFailure("ffmpeg_dependency_failure")
    return result.stdout if capture else b""


def _ffmpeg(args: list[str], root: Path, policy: dict[str, Any]) -> bytes:
    return _run(["/usr/bin/ffmpeg", "-nostdin", "-y", "-v", "error", *args], root, policy["wall_seconds"])


def probe(path: Path, root: Path, policy: dict[str, Any]) -> dict[str, Any]:
    raw = _run(
        [
            "/usr/bin/ffprobe",
            "-v",
            "error",
            "-protocol_whitelist",
            "file,pipe",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            path.name,
        ],
        root,
        policy["wall_seconds"],
        capture=True,
    )
    if len(raw) > policy["max_receipt_bytes"]:
        raise RenderFailure("probe_output_exceeds_bound")
    value: dict[str, Any] = json.loads(raw)
    return value


def _duration(info: dict[str, Any]) -> float:
    value = float(info["format"]["duration"])
    if not math.isfinite(value) or value <= 0:
        raise RenderFailure("source_duration_invalid")
    return value


def _time(seconds: float) -> str:
    ticks = round(seconds * 100)
    minutes, rest = divmod(ticks, 6000)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02}:{rest // 100:02}.{rest % 100:02}"


def write_captions(recipe: EditRequest, root: Path) -> list[dict[str, Any]]:
    font = max(14, round(recipe.height * 0.045))
    header = (
        "[Script Info]\nScriptType: v4.00+\nWrapStyle: 2\n"
        f"PlayResX: {recipe.width}\nPlayResY: {recipe.height}\n"
        "[V4+ Styles]\nFormat: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,"
        "BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,"
        "Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding\n"
        f"Style: Speech,DejaVu Sans,{font},&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,"
        f"0,0,0,0,100,100,0,0,1,1,0,2,{font},{font},{font // 2},1\n"
        "[Events]\nFormat: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text\n"
    )
    ledger = []
    for item in recipe.captions:
        # A bounded conservative line width also handles portrait exports.
        lines = textwrap.wrap(item.text, width=max(16, int(recipe.width / (font * 0.65)) - 4))
        if len(lines) > 2:
            raise RenderFailure("caption_exceeds_reserved_area")
        header += (
            f"Dialogue: 0,{_time(item.start)},{_time(item.end)},Speech,,0,0,0,," + "\\N".join(lines) + "\n"
        )
        ledger.append({"start": item.start, "end": item.end, "text": item.text, "lines": len(lines)})
    (root / "captions.ass").write_text(header)
    return ledger


def _validate_source(path: Path, kind: str, root: Path, policy: dict[str, Any]) -> dict[str, Any]:
    info = probe(path, root, policy)
    streams = info.get("streams", [])
    allowed = {"audio"} if kind == "audio" else {"video", "audio"}
    if not streams or any(s.get("codec_type") not in allowed for s in streams):
        raise RenderFailure("source_streams_invalid")
    if kind != "audio":
        videos = [s for s in streams if s["codec_type"] == "video"]
        if len(videos) != 1:
            raise RenderFailure("source_video_streams_invalid")
        video = videos[0]
        if not (0 < video["width"] <= policy["max_width"] and 0 < video["height"] <= policy["max_height"]):
            raise RenderFailure("source_dimensions_exceed_bounds")
    if kind != "image" and _duration(info) > policy["max_duration_seconds"]:
        raise RenderFailure("source_duration_exceeds_bounds")
    return info


def render(
    recipe: EditRequest, assets: list[dict[str, Any]], policy: dict[str, Any], root: Path
) -> tuple[bytes, dict[str, Any]]:
    if (
        recipe.duration > policy["max_duration_seconds"]
        or recipe.width > policy["max_width"]
        or recipe.height > policy["max_height"]
        or len(recipe.scenes) > policy["max_scenes"]
        or len(recipe.captions) > policy["max_captions"]
    ):
        raise RenderFailure("recipe_exceeds_bounds")
    sources = {}
    for index, asset in enumerate(assets):
        path = root / f"asset_{index}"
        raw = path.read_bytes()
        if len(raw) != asset["byte_size"] or hashlib.sha256(raw).hexdigest() != asset["sha256"]:
            raise RenderFailure("source_integrity_failure")
        info = _validate_source(path, asset["kind"], root, policy)
        sources[asset["id"]] = (path, info, asset["kind"])
    fps = policy["frame_rate"]
    picture_height = recipe.height if not recipe.captions else int(recipe.height * 0.82) // 2 * 2
    scenes, cursor = [], 0.0
    for index, scene in enumerate(recipe.scenes):
        path, info, kind = sources[str(scene.asset_id)]
        if kind != scene.kind:
            raise RenderFailure("source_kind_mismatch")
        if kind == "video" and scene.start + scene.seconds > _duration(info) + 1 / fps:
            raise RenderFailure("source_trim_exceeds_duration")
        if abs(scene.seconds * fps - round(scene.seconds * fps)) > 0.001:
            raise RenderFailure("scene_duration_must_align_to_frames")
        if scene.fit == "contain":
            fit = (
                f"scale={recipe.width}:{picture_height}:force_original_aspect_ratio=decrease,"
                f"pad={recipe.width}:{picture_height}:(ow-iw)*{scene.position_x}:(oh-ih)*{scene.position_y}"
            )
        else:
            fit = (
                f"scale={recipe.width}:{picture_height}:force_original_aspect_ratio=increase,"
                f"crop={recipe.width}:{picture_height}:(iw-ow)*{scene.position_x}:(ih-oh)*{scene.position_y}"
            )
        graph = (
            f"trim=start={scene.start}:duration={scene.seconds},setpts=PTS-STARTPTS,{fit},"
            f"pad={recipe.width}:{recipe.height}:0:0:color=black,setsar=1,fps={fps},format=yuv420p"
        )
        target = f"scene_{index}.mp4"
        flags = ["-loop", "1"] if kind == "image" else []
        _ffmpeg(
            [
                "-protocol_whitelist",
                "file,pipe",
                *flags,
                "-i",
                path.name,
                "-vf",
                graph,
                "-an",
                "-frames:v",
                str(round(scene.seconds * fps)),
                "-c:v",
                "libx264",
                "-threads",
                "1",
                "-preset",
                "fast",
                "-crf",
                "20",
                target,
            ],
            root,
            policy,
        )
        scenes.append(
            {
                "asset_id": str(scene.asset_id),
                "source_start": scene.start,
                "start": cursor,
                "end": cursor + scene.seconds,
                "fit": scene.fit,
                "position_x": scene.position_x,
                "position_y": scene.position_y,
            }
        )
        cursor += scene.seconds
    (root / "concat.txt").write_text("".join(f"file 'scene_{i}.mp4'\n" for i in range(len(scenes))))
    _ffmpeg(
        [
            "-protocol_whitelist",
            "file,pipe",
            "-f",
            "concat",
            "-safe",
            "1",
            "-i",
            "concat.txt",
            "-c",
            "copy",
            "silent.mp4",
        ],
        root,
        policy,
    )
    captions = write_captions(recipe, root)
    # Normalize each complete selected track to PCM before the final mix. This
    # preserves the production fix for lost ending speech during in-graph normalization.
    tracks = []
    for index, track in enumerate(recipe.audio):
        path, info, kind = sources[str(track.asset_id)]
        if kind != "audio" or track.source_start + track.seconds > _duration(info) + 0.001:
            raise RenderFailure("audio_trim_exceeds_duration")
        selected = f"audio_{index}.wav"
        loudness = -16 if track.kind == "narration" else -24
        _ffmpeg(
            [
                "-protocol_whitelist",
                "file,pipe",
                "-i",
                path.name,
                "-af",
                f"atrim=start={track.source_start}:duration={track.seconds},asetpts=PTS-STARTPTS,"
                f"loudnorm=I={loudness}:TP=-2:LRA=7,aresample=48000",
                "-c:a",
                "pcm_s16le",
                selected,
            ],
            root,
            policy,
        )
        tracks.append(selected)
    inputs = ["-i", "silent.mp4"]
    for selected in tracks:
        inputs.extend(["-i", selected])
    graphs = ["[0:v]ass=captions.ass[v]"]
    for index, track in enumerate(recipe.audio, 1):
        graphs.append(
            f"[{index}:a]asetpts=PTS-STARTPTS,volume={track.gain},"
            f"adelay={round(track.start * 1000)}:all=1,apad=whole_dur={recipe.duration},"
            f"atrim=duration={recipe.duration}[a{index}]"
        )
    if tracks:
        labels = "".join(f"[a{i}]" for i in range(1, len(tracks) + 1))
        graphs.append(
            f"{labels}amix=inputs={len(tracks)}:normalize=0:duration=longest,"
            f"alimiter=limit=0.89:level=false,aresample=48000,atrim=duration={recipe.duration}[a]"
        )
    else:
        graphs.append(f"anullsrc=r=48000:cl=mono,atrim=duration={recipe.duration}[a]")
    # Preserve the complete lossless mix for objective comparison of the encoded ending.
    audio_graph = ";".join(graphs[1:])
    _ffmpeg(
        [*inputs, "-filter_complex", audio_graph, "-map", "[a]", "-c:a", "pcm_s16le", "mix.wav"], root, policy
    )
    _ffmpeg(
        [
            "-i",
            "silent.mp4",
            "-i",
            "mix.wav",
            "-vf",
            "ass=captions.ass",
            "-map",
            "0:v",
            "-map",
            "1:a",
            "-c:v",
            "libvpx-vp9",
            "-threads",
            "1",
            "-b:v",
            "0",
            "-crf",
            "30",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "libopus",
            "-b:a",
            "96k",
            "-t",
            str(recipe.duration),
            "output.webm",
        ],
        root,
        policy,
    )
    output = root / "output.webm"
    if output.stat().st_size > policy["max_bytes"]:
        raise RenderFailure("export_exceeds_byte_bound")
    info = probe(output, root, policy)
    video = next(s for s in info["streams"] if s["codec_type"] == "video")
    if (video["width"], video["height"]) != (recipe.width, recipe.height):
        raise RenderFailure("export_dimensions_mismatch")
    _ffmpeg(["-i", "output.webm", "-f", "null", "-"], root, policy)
    _ffmpeg(
        ["-i", "output.webm", "-vn", "-ac", "1", "-ar", "48000", "-f", "f32le", "decoded.raw"], root, policy
    )
    _ffmpeg(["-i", "mix.wav", "-ac", "1", "-ar", "48000", "-f", "f32le", "reference.raw"], root, policy)
    decoded = (root / "decoded.raw").read_bytes()
    reference = (root / "reference.raw").read_bytes()
    audio_seconds = len(decoded) / (4 * 48000)
    if abs(audio_seconds - recipe.duration) > 1 / fps:
        raise RenderFailure("export_audio_end_mismatch")
    last_narration = next((t for t in recipe.audio if t.kind == "narration"), None)
    similarity = None
    if last_narration:
        # Compare the last second of the requested speech interval with the full
        # PCM mix. This tests encoding preservation, not intelligibility/listening.
        end = round((last_narration.start + last_narration.seconds) * 48000)
        start = max(round(last_narration.start * 48000), end - 48000)
        a = [v[0] for v in struct.iter_unpack("<f", reference[start * 4 : end * 4])]
        b = [v[0] for v in struct.iter_unpack("<f", decoded[start * 4 : end * 4])]
        if len(a) != len(b) or not a:
            raise RenderFailure("audio_tail_samples_missing")
        norm = math.sqrt(sum(x * x for x in a) * sum(x * x for x in b))
        if norm == 0:
            raise RenderFailure("audio_tail_silent")
        similarity = sum(x * y for x, y in zip(a, b, strict=True)) / norm
        if not math.isfinite(similarity) or similarity < 0.8:
            raise RenderFailure("audio_tail_encoding_mismatch")
    raw = output.read_bytes()
    receipt = {
        "sha256": hashlib.sha256(raw).hexdigest(),
        "byte_size": len(raw),
        "width": recipe.width,
        "height": recipe.height,
        "duration_seconds": _duration(info),
        "decoded_audio_seconds": audio_seconds,
        "full_decode_verified": True,
        "ending_mix_similarity": similarity,
        "scenes": scenes,
        "captions": captions,
        "subjective_listening": "unverified",
        "caption_readability": "requires operator review",
        "recipe_sha256": digest(recipe.model_dump(mode="json")),
        "assets": assets,
    }
    return raw, receipt


def main() -> None:
    header = sys.stdin.buffer.read(4)
    if len(header) != 4:
        raise RenderFailure("render_input_missing")
    size = struct.unpack(">I", header)[0]
    if not 1 <= size <= 65536:
        raise RenderFailure("render_manifest_exceeds_bound")
    manifest = json.loads(sys.stdin.buffer.read(size))
    policy = manifest["policy"]
    recipe = EditRequest.model_validate(manifest["recipe"])
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        for index, asset in enumerate(manifest["assets"]):
            if not 1 <= asset["byte_size"] <= policy["max_bytes"]:
                raise RenderFailure("source_bytes_exceed_bound")
            data = sys.stdin.buffer.read(asset["byte_size"])
            if len(data) != asset["byte_size"]:
                raise RenderFailure("render_input_incomplete")
            (root / f"asset_{index}").write_bytes(data)
        if sys.stdin.buffer.read(1):
            raise RenderFailure("render_input_trailing_bytes")
        raw, receipt = render(recipe, manifest["assets"], policy, root)
        encoded = canonical(receipt)
        if len(encoded) > policy["max_receipt_bytes"]:
            raise RenderFailure("render_receipt_exceeds_bound")
        sys.stdout.buffer.write(struct.pack(">I", len(encoded)) + encoded + raw)


if __name__ == "__main__":
    try:
        main()
    except (RenderFailure, ValueError, KeyError, OSError, subprocess.TimeoutExpired) as error:
        reason = str(error) if isinstance(error, RenderFailure) else "render_input_or_dependency_failure"
        sys.stderr.write(reason + "\n")
        sys.exit(1)
