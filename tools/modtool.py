#!/usr/bin/env python3
"""Build and apply DRV3 PlayStation prompt payloads from a user's own game files.

Copyright (C) 2026 Danganronpa V3 PlayStation Controller Icons contributors
SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import argparse
import json
import os
import struct
import sys
import traceback
from pathlib import Path

from cpk import CpkArchive, file_sha256, inspect_entry, patch_entry, sha256
from spc import parse as parse_spc


OFFSET_MASK = 0x3FFFFFFF


def load_manifest(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def descriptors(srd: bytes) -> list[dict[str, object]]:
    result = []
    cursor = 0
    while True:
        cursor = srd.find(b"$RSI", cursor)
        if cursor < 0:
            return result
        if cursor + 44 > len(srd):
            raise ValueError(f"truncated RSI descriptor at 0x{cursor:X}")
        packed_offset, stored_size, width = struct.unpack_from("<III", srd, cursor + 32)
        name_start = cursor + 48
        name_end = srd.find(b"\0", name_start)
        if name_end < 0:
            raise ValueError(f"unterminated RSI texture name at 0x{cursor:X}")
        result.append(
            {
                "index": len(result),
                "data_offset": packed_offset & OFFSET_MASK,
                "stored_size": stored_size,
                "width": width,
                "name": srd[name_start:name_end].decode("utf-8"),
            }
        )
        cursor += 4


def dds_payload(path: Path) -> tuple[bytes, int]:
    data = path.read_bytes()
    if len(data) < 148 or data[:4] != b"DDS ":
        raise ValueError(f"not a DX10 DDS file: {path}")
    height, width = struct.unpack_from("<II", data, 12)
    mip_count = struct.unpack_from("<I", data, 28)[0]
    if data[84:88] != b"DX10":
        raise ValueError(f"DDS does not use a DX10 header: {path}")
    dxgi_format, dimension, _misc, array_size, _alpha = struct.unpack_from(
        "<IIIII", data, 128
    )
    if dxgi_format not in (98, 99) or dimension != 3 or array_size != 1 or mip_count not in (0, 1):
        raise ValueError(f"DDS is not a one-level 2D BC7 texture: {path}")
    payload = data[148:]
    expected = ((width + 3) // 4) * ((height + 3) // 4) * 16
    if len(payload) != expected:
        raise ValueError(f"unexpected BC7 payload size: {path}")
    return payload, width


def copy_texture(blob: bytearray, items: list[dict[str, object]], source: int, target: int) -> None:
    src = items[source]
    dst = items[target]
    if src["stored_size"] != dst["stored_size"] or src["width"] != dst["width"]:
        raise ValueError(f"incompatible texture descriptors {source} -> {target}")
    src_start = int(src["data_offset"])
    dst_start = int(dst["data_offset"])
    size = int(src["stored_size"])
    if src_start + size > len(blob) or dst_start + size > len(blob):
        raise ValueError("texture block extends beyond SRDV")
    blob[dst_start : dst_start + size] = blob[src_start : src_start + size]


def replace_texture(
    blob: bytearray,
    items: list[dict[str, object]],
    target: int,
    dds_path: Path,
) -> None:
    payload, width = dds_payload(dds_path)
    item = items[target]
    if width != item["width"] or len(payload) != item["stored_size"]:
        raise ValueError(f"DDS does not match texture descriptor {target}: {dds_path}")
    start = int(item["data_offset"])
    blob[start : start + len(payload)] = payload


SCRUM_PROMPT_TEXTURE_PAIRS = (
    ("t_scrum_push_button_circle.png", "t_scrum_push_button_circle_xone.png"),
    ("t_scrum_push_button_circle_miss.png", "t_scrum_push_button_circle_miss_xone.png"),
    ("t_scrum_push_button_cross.png", "t_scrum_push_button_cross_xone.png"),
    ("t_scrum_push_button_cross_miss.png", "t_scrum_push_button_cross_miss_xone.png"),
    ("t_scrum_push_button_square.png", "t_scrum_push_button_square_xone.png"),
    ("t_scrum_push_button_square_miss.png", "t_scrum_push_button_square_miss_xone.png"),
    ("t_scrum_push_button_triangle.png", "t_scrum_push_button_triangle_xone.png"),
    ("t_scrum_push_button_triangle_miss.png", "t_scrum_push_button_triangle_miss_xone.png"),
)

ARGUMENT_ARMAMENT_TEXTURE_PAIRS = (
    ("t_riron_rythm_button_circle.png", "t_riron_rythm_button_circle_xone.png"),
    ("t_riron_rythm_button_cross.png", "t_riron_rythm_button_cross_xone.png"),
    ("t_riron_rythm_button_square.png", "t_riron_rythm_button_square_xone.png"),
    ("t_riron_rythm_button_triangle.png", "t_riron_rythm_button_triangle_xone.png"),
    ("t_riron_rythm_renda_button_all.png", "t_riron_rythm_renda_button_all_xone.png"),
)

EXTRA_LANGUAGE_PROMPT_PACKAGES = (
    ("language_carddeath_get_prompts", "Card Death reward", "carddeath_get_entry_pattern", 2),
    ("language_carddeath_guide_prompts", "Card Death guide", "carddeath_guide_entry_pattern", 5),
    (
        "language_chara_card_check_prompts",
        "character card guide",
        "chara_card_check_entry_pattern",
        1,
    ),
    (
        "language_course_check_prompts",
        "course check guide",
        "course_check_entry_pattern",
        1,
    ),
    ("language_step_guide_prompts", "step guide", "step_guide_entry_pattern", 1),
)

BASE_PROMPT_PACKAGES = (
    ("base_rpg_create_tab_prompts", "RPG creation tabs", 2, False),
    ("base_success_skill_prompts", "skill screen", 2, False),
    ("base_rebuttal_showdown_prompts", "Rebuttal Showdown", 2, False),
    ("base_argument_finish_circle", "Argument Armament Circle finish", 1, False),
    ("base_argument_finish_cross", "Argument Armament Cross finish", 1, True),
    ("base_argument_finish_square", "Argument Armament Square finish", 1, False),
    ("base_argument_finish_triangle", "Argument Armament Triangle finish", 1, False),
)


def copy_named_texture(
    blob: bytearray,
    items: list[dict[str, object]],
    source_name: str,
    target_name: str,
) -> None:
    by_name = {str(item["name"]): int(item["index"]) for item in items}
    if source_name not in by_name or target_name not in by_name:
        raise ValueError(f"missing texture mapping: {source_name} -> {target_name}")
    copy_texture(blob, items, by_name[source_name], by_name[target_name])


def swap_named_texture(
    blob: bytearray,
    items: list[dict[str, object]],
    source_name: str,
    target_name: str,
) -> None:
    by_name = {str(item["name"]): int(item["index"]) for item in items}
    if source_name not in by_name or target_name not in by_name:
        raise ValueError(f"missing texture mapping: {source_name} -> {target_name}")
    source = items[by_name[source_name]]
    target = items[by_name[target_name]]
    if source["stored_size"] != target["stored_size"] or source["width"] != target["width"]:
        raise ValueError(f"incompatible texture descriptors: {source_name} -> {target_name}")
    source_start = int(source["data_offset"])
    target_start = int(target["data_offset"])
    size = int(source["stored_size"])
    source_payload = bytes(blob[source_start : source_start + size])
    target_payload = bytes(blob[target_start : target_start + size])
    blob[source_start : source_start + size] = target_payload
    blob[target_start : target_start + size] = source_payload


def build_named_prompt_payload(
    original_spc: bytes,
    texture_pairs: tuple[tuple[str, str], ...],
    swap: bool = False,
) -> tuple[bytes, str]:
    archive = parse_spc(original_spc)
    srd = archive.one("texture.srd").unpacked()
    srdv = archive.one("texture.srdv").unpacked()
    items = descriptors(srd)
    modified = bytearray(srdv)
    for source_name, target_name in texture_pairs:
        if swap:
            swap_named_texture(modified, items, source_name, target_name)
        else:
            copy_named_texture(modified, items, source_name, target_name)
    archive.replace("texture.srdv", bytes(modified))
    return archive.rebuild(), sha256(bytes(modified))


def build_scrum_prompt_payload(original_spc: bytes) -> tuple[bytes, str]:
    return build_named_prompt_payload(original_spc, SCRUM_PROMPT_TEXTURE_PAIRS)


def build_argument_armament_payload(original_spc: bytes) -> tuple[bytes, str]:
    return build_named_prompt_payload(original_spc, ARGUMENT_ARMAMENT_TEXTURE_PAIRS)


def build_xone_redirect_payload(
    original_spc: bytes,
    expected_xone_textures: int,
    swap: bool = False,
) -> tuple[bytes, str]:
    archive = parse_spc(original_spc)
    items = descriptors(archive.one("texture.srd").unpacked())
    names = {str(item["name"]) for item in items}
    targets = sorted(name for name in names if "_xone" in name)
    if len(targets) != expected_xone_textures:
        raise ValueError(
            f"expected {expected_xone_textures} XOne textures, found {len(targets)}"
        )
    pairs = tuple((target.replace("_xone", "", 1), target) for target in targets)
    missing_sources = [source for source, _target in pairs if source not in names]
    if missing_sources:
        raise ValueError(f"missing shipped PlayStation texture: {missing_sources[0]}")
    return build_named_prompt_payload(original_spc, pairs, swap=swap)


def original_archive(manifest: dict[str, object], patch_id: str) -> dict[str, object]:
    matches = [item for item in manifest["archive_patches"] if item["id"] == patch_id]
    if len(matches) != 1:
        raise ValueError(f"manifest must define one {patch_id!r} patch")
    return matches[0]


def prepare_language_manifest(args: argparse.Namespace) -> dict[str, object]:
    manifest = load_manifest(args.manifest)
    language = args.language.strip().upper()
    if not language or not language.replace("_", "").isalnum():
        raise ValueError(f"invalid language code: {language!r}")
    template = manifest["language_archive"]
    values = {"language": language, "language_lower": language.lower()}
    game_path = str(template["game_path_pattern"]).format(**values)
    target_path = str(template["target_entry_pattern"]).format(**values)
    source_path = str(template["source_entry_pattern"]).format(**values)
    scrum_path = str(template["scrum_entry_pattern"]).format(**values)
    argument_armament_path = str(template["argument_armament_entry_pattern"]).format(**values)
    extra_prompt_paths = {
        patch_id: str(template[template_key]).format(**values)
        for patch_id, _label, template_key, _expected_count in EXTRA_LANGUAGE_PROMPT_PACKAGES
    }
    archive_path = args.game_root / Path(game_path)
    if not archive_path.is_file():
        raise ValueError(f"active language archive is missing: {archive_path}")
    with CpkArchive(archive_path) as archive:
        target = archive.read_entry(target_path)
        source = archive.read_entry(source_path)
        scrum = archive.read_entry(scrum_path)
        argument_armament = archive.read_entry(argument_armament_path)
        extra_prompts = {
            patch_id: archive.read_entry(path)
            for patch_id, path in extra_prompt_paths.items()
        }
    target_hash = sha256(target)
    source_hash = sha256(source)
    current_archive_hash = file_sha256(archive_path)

    existing_help = None
    if args.existing_manifest is not None and args.existing_manifest.is_file():
        existing = load_manifest(args.existing_manifest)
        if str(existing.get("supported_game", {}).get("language_code", "")).upper() == language:
            existing_help = next(
                (
                    item
                    for item in existing.get("archive_patches", [])
                    if item.get("id") == "language_controller_help" and item.get("game_path") == game_path
                ),
                None,
            )

    if target_hash == source_hash:
        if existing_help is None:
            raise ValueError(
                "active language help entry already matches the PlayStation source; use saved state or Steam Verify"
            )
        old_source = existing_help["source_entry"]
        if len(source) != int(old_source["size"]) or source_hash != str(old_source["sha256"]).upper():
            raise ValueError("saved PlayStation help source no longer matches the active language archive")
        if args.state is None or not args.state.is_file():
            raise ValueError("installed state is required to upgrade an existing language patch")
        state = json.loads(args.state.read_text(encoding="utf-8-sig"))
        record = next(
            (
                item
                for item in state.get("archives", [])
                if item.get("game_path") == game_path and item.get("target_entry") == target_path
            ),
            None,
        )
        if record is None or current_archive_hash != str(record.get("patched_sha256", "")).upper():
            raise ValueError("active language archive changed after the existing mod was installed")
        help_patch = json.loads(json.dumps(existing_help))
        original_archive_hash = str(help_patch["original_archive_sha256"]).upper()
    else:
        details = inspect_entry(
            archive_path,
            target_path,
            len(target),
            target_hash,
            len(source),
            source_hash,
        )
        if details["state"] != "original" or len(source) > int(details["allocation_size"]):
            raise ValueError("active language help entries do not fit the expected fixed-allocation layout")
        original_archive_hash = current_archive_hash
        help_patch = {
            "id": "language_controller_help",
            "game_path": game_path,
            "archive_size": archive_path.stat().st_size,
            "original_archive_sha256": original_archive_hash,
            "payload_name": f"help_tutorial_xone_{language}.spc",
            "target_entry": {
                "path": target_path,
                "backup_name": f"help_tutorial_xone_{language}.spc",
                "allocation_size": int(details["allocation_size"]),
                "original_size": len(target),
                "original_sha256": target_hash,
            },
            "source_entry": {
                "path": source_path,
                "backup_name": f"help_tutorial_ps4_{language}.spc",
                "size": len(source),
                "sha256": source_hash,
            },
        }

    scrum_hash = sha256(scrum)
    scrum_details = inspect_entry(
        archive_path,
        scrum_path,
        len(scrum),
        scrum_hash,
        0,
        "",
    )
    scrum_payload, _scrum_srdv_hash = build_scrum_prompt_payload(scrum)
    if scrum_details["state"] != "original" or len(scrum_payload) > int(scrum_details["allocation_size"]):
        raise ValueError("active language Scrum Debate prompts do not fit the expected fixed-allocation layout")
    scrum_patch = {
        "id": "language_scrum_prompts",
        "game_path": game_path,
        "archive_size": archive_path.stat().st_size,
        "original_archive_sha256": original_archive_hash,
        "payload_name": f"t_scrum_push_{language}.spc",
        "target_entry": {
            "path": scrum_path,
            "backup_name": f"t_scrum_push_{language}.spc",
            "allocation_size": int(scrum_details["allocation_size"]),
            "original_size": len(scrum),
            "original_sha256": scrum_hash,
        },
    }

    argument_armament_hash = sha256(argument_armament)
    argument_armament_details = inspect_entry(
        archive_path,
        argument_armament_path,
        len(argument_armament),
        argument_armament_hash,
        0,
        "",
    )
    argument_armament_payload, _argument_armament_srdv_hash = build_argument_armament_payload(
        argument_armament
    )
    if (
        argument_armament_details["state"] != "original"
        or len(argument_armament_payload) > int(argument_armament_details["allocation_size"])
    ):
        raise ValueError(
            "active language Argument Armament prompts do not fit the expected fixed-allocation layout"
        )
    argument_armament_patch = {
        "id": "language_argument_armament_prompts",
        "game_path": game_path,
        "archive_size": archive_path.stat().st_size,
        "original_archive_sha256": original_archive_hash,
        "payload_name": f"t_riron_rythm_{language}.spc",
        "target_entry": {
            "path": argument_armament_path,
            "backup_name": f"t_riron_rythm_{language}.spc",
            "allocation_size": int(argument_armament_details["allocation_size"]),
            "original_size": len(argument_armament),
            "original_sha256": argument_armament_hash,
        },
    }

    extra_prompt_patches = []
    for patch_id, label, _template_key, expected_count in EXTRA_LANGUAGE_PROMPT_PACKAGES:
        prompt_path = extra_prompt_paths[patch_id]
        prompt = extra_prompts[patch_id]
        prompt_hash = sha256(prompt)
        prompt_details = inspect_entry(
            archive_path,
            prompt_path,
            len(prompt),
            prompt_hash,
            0,
            "",
        )
        prompt_payload, _prompt_srdv_hash = build_xone_redirect_payload(prompt, expected_count)
        if prompt_details["state"] != "original" or len(prompt_payload) > int(
            prompt_details["allocation_size"]
        ):
            raise ValueError(
                f"active language {label} prompts do not fit the expected fixed-allocation layout"
            )
        extra_prompt_patches.append(
            {
                "id": patch_id,
                "game_path": game_path,
                "archive_size": archive_path.stat().st_size,
                "original_archive_sha256": original_archive_hash,
                "payload_name": Path(prompt_path).name,
                "target_entry": {
                    "path": prompt_path,
                    "backup_name": Path(prompt_path).name,
                    "allocation_size": int(prompt_details["allocation_size"]),
                    "original_size": len(prompt),
                    "original_sha256": prompt_hash,
                },
            }
        )

    runtime = json.loads(json.dumps(manifest))
    runtime["supported_game"]["language_code"] = language
    runtime["archive_patches"].extend(
        (help_patch, scrum_patch, argument_armament_patch, *extra_prompt_patches)
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(json.dumps(runtime, indent=2), encoding="utf-8")
    temporary.replace(args.output)
    return {
        "language": language,
        "runtime_manifest": str(args.output.resolve()),
        "language_archive": game_path,
        "archive_sha256": original_archive_hash,
        "target_entry": help_patch["target_entry"],
        "source_entry": help_patch["source_entry"],
        "scrum_target_entry": scrum_patch["target_entry"],
        "argument_armament_target_entry": argument_armament_patch["target_entry"],
        "extra_prompt_target_entries": {
            patch["id"]: patch["target_entry"] for patch in extra_prompt_patches
        },
    }


def checked_file(path: Path, expected_size: int, expected_hash: str, label: str) -> bytes:
    if not path.is_file():
        raise ValueError(f"missing {label}: {path}")
    payload = path.read_bytes()
    if len(payload) != expected_size or sha256(payload) != expected_hash.upper():
        raise ValueError(f"invalid {label}: {path}")
    return payload


def target_backup_path(patch: dict[str, object], root: Path) -> Path:
    return root / str(patch["target_entry"]["backup_name"])


def source_backup_path(patch: dict[str, object], root: Path) -> Path:
    return root / str(patch["source_entry"]["backup_name"])


def preserve_file(path: Path, payload: bytes, label: str) -> None:
    if path.exists():
        existing = path.read_bytes()
        if existing != payload:
            raise ValueError(f"existing {label} does not match the supported original: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(payload)
    temporary.replace(path)


def capture_original_entries(args: argparse.Namespace) -> dict[str, object]:
    manifest = load_manifest(args.manifest)
    records = []
    for patch in manifest["archive_patches"]:
        entry = patch["target_entry"]
        destination = target_backup_path(patch, args.output_dir)
        if destination.is_file():
            payload = checked_file(
                destination,
                int(entry["original_size"]),
                str(entry["original_sha256"]),
                "original entry backup",
            )
        else:
            archive_path = args.game_root / Path(str(patch["game_path"]))
            if not archive_path.is_file() or archive_path.stat().st_size != int(patch["archive_size"]):
                raise ValueError(f"archive is missing or has an unexpected size: {archive_path}")
            with CpkArchive(archive_path) as archive:
                payload = archive.read_entry(str(entry["path"]))
            if len(payload) != int(entry["original_size"]) or sha256(payload) != str(entry["original_sha256"]).upper():
                raise ValueError(f"unexpected original CPK entry: {entry['path']}")
            preserve_file(destination, payload, "original entry backup")
        records.append({"path": str(destination.resolve()), "size": len(payload), "sha256": sha256(payload)})

    help_patch = original_archive(manifest, "language_controller_help")
    source = help_patch["source_entry"]
    destination = source_backup_path(help_patch, args.output_dir)
    if destination.is_file():
        payload = checked_file(destination, int(source["size"]), str(source["sha256"]), "PlayStation help source")
    else:
        language_path = args.game_root / Path(str(help_patch["game_path"]))
        if not language_path.is_file() or language_path.stat().st_size != int(help_patch["archive_size"]):
            raise ValueError(f"language archive is missing or has an unexpected size: {language_path}")
        with CpkArchive(language_path) as archive:
            payload = archive.read_entry(str(source["path"]))
        if len(payload) != int(source["size"]) or sha256(payload) != str(source["sha256"]).upper():
            raise ValueError(f"unexpected shipped PlayStation help entry: {source['path']}")
        preserve_file(destination, payload, "PlayStation help source")
    records.append({"path": str(destination.resolve()), "size": len(payload), "sha256": sha256(payload)})
    return {"original_entries": records, "total_size": sum(int(item["size"]) for item in records)}


def checked_original_entries(manifest: dict[str, object], root: Path) -> list[dict[str, object]]:
    records = []
    for patch in manifest["archive_patches"]:
        entry = patch["target_entry"]
        path = target_backup_path(patch, root)
        payload = checked_file(
            path,
            int(entry["original_size"]),
            str(entry["original_sha256"]),
            "original entry backup",
        )
        records.append({"path": str(path.resolve()), "size": len(payload), "sha256": sha256(payload)})
    help_patch = original_archive(manifest, "language_controller_help")
    source = help_patch["source_entry"]
    path = source_backup_path(help_patch, root)
    payload = checked_file(path, int(source["size"]), str(source["sha256"]), "PlayStation help source")
    records.append({"path": str(path.resolve()), "size": len(payload), "sha256": sha256(payload)})
    return records


def verify_original_entries(args: argparse.Namespace) -> dict[str, object]:
    records = checked_original_entries(load_manifest(args.manifest), args.original_entry_dir)
    return {"original_entries": records, "total_size": sum(int(item["size"]) for item in records)}


def build(args: argparse.Namespace) -> dict[str, object]:
    manifest = load_manifest(args.manifest)
    resident_patch = original_archive(manifest, "resident_runtime_glyphs")
    help_patch = original_archive(manifest, "language_controller_help")
    resident_entry = resident_patch["target_entry"]
    original_spc = checked_file(
        target_backup_path(resident_patch, args.original_entry_dir),
        int(resident_entry["original_size"]),
        str(resident_entry["original_sha256"]),
        "original resident entry",
    )
    archive = parse_spc(original_spc)
    srd = archive.one("pad_texture.srd").unpacked()
    srdv = archive.one("pad_texture.srdv").unpacked()
    nested = resident_patch["nested_original"]
    if len(srd) != int(nested["srd_size"]) or sha256(srd) != str(nested["srd_sha256"]).upper():
        raise ValueError("unexpected original pad_texture.srd")
    if len(srdv) != int(nested["srdv_size"]) or sha256(srdv) != str(nested["srdv_sha256"]).upper():
        raise ValueError("unexpected original pad_texture.srdv")

    items = descriptors(srd)
    if len(items) != 160:
        raise ValueError(f"expected 160 runtime prompt descriptors, found {len(items)}")
    modified = bytearray(srdv)
    for source in range(0, 26):
        copy_texture(modified, items, source, source + 40)
    for source in range(28, 40):
        copy_texture(modified, items, source, source + 40)
    copy_texture(modified, items, 27, 40)

    if args.variant == "dualsense":
        asset_root = args.asset_dir
        replace_texture(modified, items, 40, asset_root / "create.dds")
        replace_texture(modified, items, 41, asset_root / "options.dds")

    archive.replace("pad_texture.srdv", bytes(modified))
    resident_payload = archive.rebuild()
    allocation = int(resident_patch["target_entry"]["allocation_size"])
    if len(resident_payload) > allocation:
        raise ValueError(
            f"rebuilt resident SPC is {len(resident_payload)} bytes; allocation is {allocation}"
        )

    source_info = help_patch["source_entry"]
    help_payload = checked_file(
        source_backup_path(help_patch, args.original_entry_dir),
        int(source_info["size"]),
        str(source_info["sha256"]),
        "PlayStation help source",
    )

    base_prompt_payloads = []
    for patch_id, label, expected_count, swap in BASE_PROMPT_PACKAGES:
        matches = [item for item in manifest["archive_patches"] if item["id"] == patch_id]
        if len(matches) > 1:
            raise ValueError(f"manifest defines more than one {label} patch")
        if not matches:
            continue
        patch = matches[0]
        entry = patch["target_entry"]
        original = checked_file(
            target_backup_path(patch, args.original_entry_dir),
            int(entry["original_size"]),
            str(entry["original_sha256"]),
            f"original {label} entry",
        )
        payload, srdv_hash = build_xone_redirect_payload(original, expected_count, swap=swap)
        if len(payload) > int(entry["allocation_size"]):
            raise ValueError(
                f"rebuilt {label} SPC is {len(payload)} bytes; "
                f"allocation is {entry['allocation_size']}"
            )
        base_prompt_payloads.append((patch, label, payload, srdv_hash, swap))

    scrum_matches = [
        item for item in manifest["archive_patches"] if item["id"] == "language_scrum_prompts"
    ]
    if len(scrum_matches) > 1:
        raise ValueError("manifest defines more than one language Scrum Debate patch")
    scrum_patch = scrum_matches[0] if len(scrum_matches) == 1 else None
    scrum_payload = None
    scrum_srdv_hash = None
    if scrum_patch is not None:
        scrum_entry = scrum_patch["target_entry"]
        original_scrum = checked_file(
            target_backup_path(scrum_patch, args.original_entry_dir),
            int(scrum_entry["original_size"]),
            str(scrum_entry["original_sha256"]),
            "original Scrum Debate entry",
        )
        scrum_payload, scrum_srdv_hash = build_scrum_prompt_payload(original_scrum)
        if len(scrum_payload) > int(scrum_entry["allocation_size"]):
            raise ValueError(
                f"rebuilt Scrum Debate SPC is {len(scrum_payload)} bytes; "
                f"allocation is {scrum_entry['allocation_size']}"
            )

    argument_armament_matches = [
        item
        for item in manifest["archive_patches"]
        if item["id"] == "language_argument_armament_prompts"
    ]
    if len(argument_armament_matches) > 1:
        raise ValueError("manifest defines more than one Argument Armament patch")
    argument_armament_patch = (
        argument_armament_matches[0] if len(argument_armament_matches) == 1 else None
    )
    argument_armament_payload = None
    argument_armament_srdv_hash = None
    if argument_armament_patch is not None:
        argument_armament_entry = argument_armament_patch["target_entry"]
        original_argument_armament = checked_file(
            target_backup_path(argument_armament_patch, args.original_entry_dir),
            int(argument_armament_entry["original_size"]),
            str(argument_armament_entry["original_sha256"]),
            "original Argument Armament entry",
        )
        argument_armament_payload, argument_armament_srdv_hash = build_argument_armament_payload(
            original_argument_armament
        )
        if len(argument_armament_payload) > int(argument_armament_entry["allocation_size"]):
            raise ValueError(
                f"rebuilt Argument Armament SPC is {len(argument_armament_payload)} bytes; "
                f"allocation is {argument_armament_entry['allocation_size']}"
            )

    extra_prompt_payloads = []
    for patch_id, label, _template_key, expected_count in EXTRA_LANGUAGE_PROMPT_PACKAGES:
        matches = [item for item in manifest["archive_patches"] if item["id"] == patch_id]
        if len(matches) > 1:
            raise ValueError(f"manifest defines more than one {label} patch")
        if not matches:
            continue
        patch = matches[0]
        entry = patch["target_entry"]
        original = checked_file(
            target_backup_path(patch, args.original_entry_dir),
            int(entry["original_size"]),
            str(entry["original_sha256"]),
            f"original {label} entry",
        )
        payload, srdv_hash = build_xone_redirect_payload(original, expected_count)
        if len(payload) > int(entry["allocation_size"]):
            raise ValueError(
                f"rebuilt {label} SPC is {len(payload)} bytes; "
                f"allocation is {entry['allocation_size']}"
            )
        extra_prompt_payloads.append((patch, label, payload, srdv_hash))

    args.output_dir.mkdir(parents=True, exist_ok=True)
    resident_path = payload_for(resident_patch, args.output_dir)
    help_path = payload_for(help_patch, args.output_dir)
    report_path = args.output_dir / "build-report.json"
    resident_path.write_bytes(resident_payload)
    help_path.write_bytes(help_payload)
    for patch, _label, payload, _srdv_hash, _swap in base_prompt_payloads:
        payload_for(patch, args.output_dir).write_bytes(payload)
    if scrum_patch is not None and scrum_payload is not None:
        payload_for(scrum_patch, args.output_dir).write_bytes(scrum_payload)
    if argument_armament_patch is not None and argument_armament_payload is not None:
        payload_for(argument_armament_patch, args.output_dir).write_bytes(argument_armament_payload)
    for patch, _label, payload, _srdv_hash in extra_prompt_payloads:
        payload_for(patch, args.output_dir).write_bytes(payload)
    report = {
        "variant": args.variant,
        "resident_payload": {
            "path": str(resident_path.resolve()),
            "size": len(resident_payload),
            "sha256": sha256(resident_payload),
            "modified_srdv_sha256": sha256(bytes(modified)),
        },
        "help_payload": {
            "path": str(help_path.resolve()),
            "size": len(help_payload),
            "sha256": sha256(help_payload),
        },
    }
    if base_prompt_payloads:
        report["base_prompt_payloads"] = []
        for patch, label, payload, srdv_hash, swap in base_prompt_payloads:
            path = payload_for(patch, args.output_dir)
            report["base_prompt_payloads"].append(
                {
                    "id": patch["id"],
                    "label": label,
                    "path": str(path.resolve()),
                    "size": len(payload),
                    "sha256": sha256(payload),
                    "modified_srdv_sha256": srdv_hash,
                    "texture_operation": "swap" if swap else "copy",
                }
            )
    if scrum_patch is not None and scrum_payload is not None:
        scrum_path = payload_for(scrum_patch, args.output_dir)
        report["scrum_payload"] = {
            "path": str(scrum_path.resolve()),
            "size": len(scrum_payload),
            "sha256": sha256(scrum_payload),
            "modified_srdv_sha256": scrum_srdv_hash,
        }
    if argument_armament_patch is not None and argument_armament_payload is not None:
        argument_armament_path = payload_for(argument_armament_patch, args.output_dir)
        report["argument_armament_payload"] = {
            "path": str(argument_armament_path.resolve()),
            "size": len(argument_armament_payload),
            "sha256": sha256(argument_armament_payload),
            "modified_srdv_sha256": argument_armament_srdv_hash,
        }
    if extra_prompt_payloads:
        report["extra_prompt_payloads"] = []
        for patch, label, payload, srdv_hash in extra_prompt_payloads:
            path = payload_for(patch, args.output_dir)
            report["extra_prompt_payloads"].append(
                {
                    "id": patch["id"],
                    "label": label,
                    "path": str(path.resolve()),
                    "size": len(payload),
                    "sha256": sha256(payload),
                    "modified_srdv_sha256": srdv_hash,
                }
            )
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def payload_for(patch: dict[str, object], payload_dir: Path) -> Path:
    return payload_dir / str(patch["payload_name"])


def patch_game(args: argparse.Namespace) -> dict[str, object]:
    manifest = load_manifest(args.manifest)
    results = []
    seen_archives: set[str] = set()
    for patch in manifest["archive_patches"]:
        game_path = str(patch["game_path"])
        archive_path = args.game_root / Path(game_path)
        replacement_path = payload_for(patch, args.payload_dir)
        replacement = replacement_path.read_bytes()
        entry = patch["target_entry"]
        expected_archive_hash = (
            str(patch["original_archive_sha256"]) if game_path not in seen_archives else None
        )
        result = patch_entry(
            archive_path,
            str(entry["path"]),
            int(entry["original_size"]),
            str(entry["original_sha256"]),
            replacement,
            expected_archive_hash,
        )
        seen_archives.add(game_path)
        results.append(
            {
                "game_path": game_path,
                "target_entry": str(entry["path"]),
                "archive_path": archive_path,
                "entry_size": len(replacement),
                "entry_sha256": sha256(replacement),
                "state": result["state"],
            }
        )
    archive_hashes: dict[Path, str] = {}
    for result in results:
        archive_path = result.pop("archive_path")
        if archive_path not in archive_hashes:
            archive_hashes[archive_path] = file_sha256(archive_path)
        result["archive_sha256"] = archive_hashes[archive_path]
    return {"archives": results}


def classify_game(args: argparse.Namespace) -> dict[str, object]:
    manifest = load_manifest(args.manifest)
    state = None
    if args.state is not None and args.state.is_file():
        state = json.loads(args.state.read_text(encoding="utf-8-sig"))
    results = []
    archive_hashes: dict[Path, str] = {}
    for patch in manifest["archive_patches"]:
        archive_path = args.game_root / Path(str(patch["game_path"]))
        if not archive_path.is_file() and str(patch["id"]).startswith("language_"):
            results.append({"game_path": str(patch["game_path"]), "state": "missing"})
            continue
        if not archive_path.is_file() or archive_path.stat().st_size != int(patch["archive_size"]):
            raise ValueError(f"missing or unexpected archive: {archive_path}")
        entry = patch["target_entry"]
        record = None
        if state is not None and state.get("installed"):
            record = next(
                (
                    item
                    for item in state.get("archives", [])
                    if item.get("game_path") == patch["game_path"]
                    and item.get("target_entry") == entry["path"]
                ),
                None,
            )
        if args.entries_only:
            inspected = inspect_entry(
                archive_path,
                str(entry["path"]),
                int(entry["original_size"]),
                str(entry["original_sha256"]),
                int(record.get("patched_entry_size", 0)) if record is not None else 0,
                str(record.get("patched_entry_sha256", "")) if record is not None else "",
            )
            status = str(inspected["state"])
            if status == "unsupported":
                raise ValueError(f"unsupported archive entry state: {archive_path}")
            archive_hash = None
        else:
            if archive_path not in archive_hashes:
                archive_hashes[archive_path] = file_sha256(archive_path)
            archive_hash = archive_hashes[archive_path]
            if archive_hash == str(patch["original_archive_sha256"]).upper():
                status = "original"
            else:
                if record is None or archive_hash != str(record.get("patched_sha256", "")).upper():
                    raise ValueError(f"unsupported archive state: {archive_path}")
                status = "patched"
        result = {
            "game_path": str(patch["game_path"]),
            "target_entry": str(entry["path"]),
            "state": status,
        }
        if archive_hash is not None:
            result["archive_sha256"] = archive_hash
        results.append(result)
    present = [item for item in results if item["state"] != "missing"]
    return {
        "archives": results,
        "any_patched": any(item["state"] == "patched" for item in present),
        "all_patched": bool(present) and all(item["state"] == "patched" for item in present),
        "all_original": bool(present) and all(item["state"] == "original" for item in present),
    }


def restore_game(args: argparse.Namespace) -> dict[str, object]:
    manifest = load_manifest(args.manifest)
    checked_original_entries(manifest, args.original_entry_dir)
    results = []
    for patch in manifest["archive_patches"]:
        archive_path = args.game_root / Path(str(patch["game_path"]))
        if not archive_path.is_file() and str(patch["id"]).startswith("language_"):
            results.append({"game_path": str(patch["game_path"]), "state": "missing-language-archive-skipped"})
            continue
        patched = payload_for(patch, args.payload_dir).read_bytes()
        entry = patch["target_entry"]
        original = checked_file(
            target_backup_path(patch, args.original_entry_dir),
            int(entry["original_size"]),
            str(entry["original_sha256"]),
            "original entry backup",
        )
        patch_entry(
            archive_path,
            str(entry["path"]),
            len(patched),
            sha256(patched),
            original,
        )
        restored = inspect_entry(
            archive_path,
            str(entry["path"]),
            int(entry["original_size"]),
            str(entry["original_sha256"]),
            len(patched),
            sha256(patched),
        )
        if restored["state"] != "original":
            raise ValueError(f"entry restore verification failed: {entry['path']}")
        results.append(
            {
                "game_path": str(patch["game_path"]),
                "archive_path": archive_path,
                "expected_archive_sha256": str(patch["original_archive_sha256"]).upper(),
                "entry_size": len(original),
                "entry_sha256": sha256(original),
                "state": "original",
            }
        )
    archive_hashes: dict[Path, str] = {}
    for result in results:
        if result["state"] == "missing-language-archive-skipped":
            continue
        archive_path = result.pop("archive_path")
        expected_hash = result.pop("expected_archive_sha256")
        if archive_path not in archive_hashes:
            archive_hashes[archive_path] = file_sha256(archive_path)
        archive_hash = archive_hashes[archive_path]
        if archive_hash != expected_hash:
            raise ValueError(
                f"restored entry is exact but the surrounding archive differs; use Steam Verify: {archive_path}"
            )
        result["archive_sha256"] = archive_hash
    return {"archives": results}


def verify_game(args: argparse.Namespace) -> dict[str, object]:
    manifest = load_manifest(args.manifest)
    results = []
    for patch in manifest["archive_patches"]:
        archive_path = args.game_root / Path(str(patch["game_path"]))
        if not archive_path.is_file() and str(patch["id"]).startswith("language_"):
            results.append({"game_path": str(patch["game_path"]), "entry_state": "missing-language-archive-skipped"})
            continue
        replacement = payload_for(patch, args.payload_dir).read_bytes()
        entry = patch["target_entry"]
        result = inspect_entry(
            archive_path,
            str(entry["path"]),
            int(entry["original_size"]),
            str(entry["original_sha256"]),
            len(replacement),
            sha256(replacement),
        )
        if result["state"] != args.expect:
            raise ValueError(
                f"{patch['game_path']} entry state is {result['state']}; expected {args.expect}"
            )
        results.append(
            {
                "game_path": str(patch["game_path"]),
                "target_entry": str(entry["path"]),
                "entry_state": result["state"],
                "entry_sha256": result["entry"]["sha256"],
            }
        )
    return {"archives": results}


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    build_command = commands.add_parser("build")
    build_command.add_argument("--manifest", required=True, type=Path)
    build_command.add_argument("--original-entry-dir", required=True, type=Path)
    build_command.add_argument("--asset-dir", required=True, type=Path)
    build_command.add_argument("--variant", choices=("dualsense", "dualshock4"), required=True)
    build_command.add_argument("--output-dir", required=True, type=Path)
    build_command.set_defaults(function=build)
    prepare_command = commands.add_parser("prepare-language")
    prepare_command.add_argument("--manifest", required=True, type=Path)
    prepare_command.add_argument("--game-root", required=True, type=Path)
    prepare_command.add_argument("--language", required=True)
    prepare_command.add_argument("--output", required=True, type=Path)
    prepare_command.add_argument("--existing-manifest", type=Path)
    prepare_command.add_argument("--state", type=Path)
    prepare_command.set_defaults(function=prepare_language_manifest)
    classify_command = commands.add_parser("classify")
    classify_command.add_argument("--manifest", required=True, type=Path)
    classify_command.add_argument("--game-root", required=True, type=Path)
    classify_command.add_argument("--state", type=Path)
    classify_command.add_argument("--entries-only", action="store_true")
    classify_command.set_defaults(function=classify_game)
    capture_command = commands.add_parser("capture-originals")
    capture_command.add_argument("--manifest", required=True, type=Path)
    capture_command.add_argument("--game-root", required=True, type=Path)
    capture_command.add_argument("--output-dir", required=True, type=Path)
    capture_command.set_defaults(function=capture_original_entries)
    check_command = commands.add_parser("verify-originals")
    check_command.add_argument("--manifest", required=True, type=Path)
    check_command.add_argument("--original-entry-dir", required=True, type=Path)
    check_command.set_defaults(function=verify_original_entries)
    for name, function in (("patch", patch_game), ("restore", restore_game), ("verify", verify_game)):
        command = commands.add_parser(name)
        command.add_argument("--manifest", required=True, type=Path)
        command.add_argument("--game-root", required=True, type=Path)
        command.add_argument("--payload-dir", required=True, type=Path)
        if name == "restore":
            command.add_argument("--original-entry-dir", required=True, type=Path)
        if name == "verify":
            command.add_argument("--expect", choices=("original", "patched"), required=True)
        command.set_defaults(function=function)
    return result


def main() -> int:
    try:
        args = parser().parse_args()
        output = args.function(args)
        print(json.dumps(output, indent=2))
        return 0
    except Exception as exc:
        if os.environ.get("DRV3_MOD_DEBUG") == "1":
            traceback.print_exc()
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
