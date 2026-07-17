#!/usr/bin/env python3
"""Install, verify, switch, and remove the DRV3 PlayStation icon mod.

Copyright (C) 2026 Danganronpa V3 PlayStation Controller Icons contributors
SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import re
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

import modtool


PROJECT_ROOT = Path(__file__).resolve().parent.parent
GAME_ROOT = PROJECT_ROOT.parent
BASE_MANIFEST_PATH = PROJECT_ROOT / "manifest.json"
ASSET_ROOT = PROJECT_ROOT / "assets"
LOCAL_ROOT = GAME_ROOT / "DualSense_UI_Mod_Data"
ORIGINAL_ENTRY_ROOT = LOCAL_ROOT / "original_entries"
RUNTIME_MANIFEST_ROOT = LOCAL_ROOT / "manifests"
STATE_PATH = LOCAL_ROOT / "installed-state.json"


class ModError(RuntimeError):
    """An expected, user-facing mod operation failure."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2), encoding="utf-8")
    temporary.replace(path)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def namespace(**values: object) -> argparse.Namespace:
    return argparse.Namespace(**values)


def resolve_relative(root: Path, relative: str) -> Path:
    candidate = (root / Path(relative.replace("/", os.sep))).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise ModError(f"Unsafe relative path in saved metadata: {relative}") from exc
    return candidate


def process_is_running(executable_name: str) -> bool:
    if os.name != "nt":
        return False

    from ctypes import wintypes

    class ProcessEntry32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_snapshot = kernel32.CreateToolhelp32Snapshot
    create_snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    create_snapshot.restype = wintypes.HANDLE
    process_first = kernel32.Process32FirstW
    process_first.argtypes = (wintypes.HANDLE, ctypes.POINTER(ProcessEntry32W))
    process_first.restype = wintypes.BOOL
    process_next = kernel32.Process32NextW
    process_next.argtypes = (wintypes.HANDLE, ctypes.POINTER(ProcessEntry32W))
    process_next.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL

    snapshot = create_snapshot(0x00000002, 0)
    if snapshot == wintypes.HANDLE(-1).value:
        raise ModError("Could not check whether Danganronpa V3 is running.")
    try:
        entry = ProcessEntry32W()
        entry.dwSize = ctypes.sizeof(entry)
        if not process_first(snapshot, ctypes.byref(entry)):
            return False
        expected = executable_name.casefold()
        while True:
            if entry.szExeFile.casefold() == expected:
                return True
            if not process_next(snapshot, ctypes.byref(entry)):
                return False
    finally:
        close_handle(snapshot)


class ModManager:
    def __init__(self) -> None:
        if sys.version_info < (3, 10):
            raise ModError("Python 3.10 or newer is required.")
        if not BASE_MANIFEST_PATH.is_file():
            raise ModError("Missing manifest.json next to the mod manager.")
        self.base_manifest = read_json(BASE_MANIFEST_PATH)

    @property
    def version(self) -> str:
        return str(self.base_manifest["version"])

    def validate_game(self, require_closed: bool = False) -> Path:
        executable = self.base_manifest["supported_game"]["executable"]
        game_exe = resolve_relative(GAME_ROOT, str(executable["path"]))
        if not game_exe.is_file():
            raise ModError(
                "Place this mod folder directly inside the supported "
                "Danganronpa V3 game directory."
            )
        if (
            game_exe.stat().st_size != int(executable["size"])
            or sha256_file(game_exe) != str(executable["sha256"]).upper()
        ):
            raise ModError("Unsupported or changed Dangan3Win.exe. No archive was modified.")
        if require_closed and process_is_running("Dangan3Win.exe"):
            raise ModError("Danganronpa V3 is running. Close the game and retry.")
        return game_exe

    def validate_assets(self) -> None:
        for asset in self.base_manifest["redistributable_assets"]:
            path = resolve_relative(PROJECT_ROOT, str(asset["path"]))
            expected_size = asset.get("size")
            if (
                not path.is_file()
                or (expected_size is not None and path.stat().st_size != int(expected_size))
                or sha256_file(path) != str(asset["sha256"]).upper()
            ):
                raise ModError(f"Project asset verification failed: {asset['path']}")

    def active_language(self, required: bool = True) -> str:
        language_path = GAME_ROOT / "language.txt"
        language = (
            language_path.read_text(encoding="utf-8-sig").strip().upper()
            if language_path.is_file()
            else ""
        )
        if required and (not language or re.fullmatch(r"[A-Z0-9_]+", language) is None):
            raise ModError(f"Could not determine a valid language code from language.txt: {language!r}")
        return language or "unknown"

    def state(self) -> dict[str, object] | None:
        return read_json(STATE_PATH) if STATE_PATH.is_file() else None

    def preflight_archive_writes(self, manifest: dict[str, object]) -> None:
        """Confirm every target archive is writable before changing the first one."""
        checked: set[Path] = set()
        for patch in manifest["archive_patches"]:
            archive_path = resolve_relative(GAME_ROOT, str(patch["game_path"]))
            if archive_path in checked:
                continue
            checked.add(archive_path)
            try:
                with archive_path.open("r+b", buffering=0):
                    pass
            except OSError as exc:
                raise ModError(
                    f"Cannot open a required game archive for writing: {archive_path}. "
                    "Close the game, Steam file verification, and other programs using it."
                ) from exc

    def manifest_patch(self, manifest: dict[str, object], patch_id: str) -> dict[str, object] | None:
        matches = [patch for patch in manifest["archive_patches"] if patch["id"] == patch_id]
        return matches[0] if len(matches) == 1 else None

    def runtime_manifest(
        self, language: str, state: dict[str, object] | None
    ) -> tuple[Path, dict[str, object]]:
        values = {"language": language, "language_lower": language.lower()}
        template = self.base_manifest["language_archive"]
        active_game_path = str(template["game_path_pattern"]).format(**values)
        active_archive_path = resolve_relative(GAME_ROOT, active_game_path)
        if not active_archive_path.is_file():
            raise ModError(f"The active {language} language archive is missing: {active_game_path}")

        runtime_path = RUNTIME_MANIFEST_ROOT / f"runtime-manifest-{language}.json"
        reuse = False
        candidate: dict[str, object] | None = None
        if runtime_path.is_file():
            try:
                candidate = read_json(runtime_path)
                required_ids = (
                    "language_controller_help",
                    "language_scrum_prompts",
                    "language_argument_armament_prompts",
                )
                correct_paths = all(
                    (patch := self.manifest_patch(candidate, patch_id)) is not None
                    and patch["game_path"] == active_game_path
                    for patch_id in required_ids
                )
                base_ids = {str(patch["id"]) for patch in self.base_manifest["archive_patches"]}
                candidate_ids = {str(patch["id"]) for patch in candidate["archive_patches"]}
                if correct_paths and base_ids.issubset(candidate_ids):
                    modtool.classify_game(
                        namespace(
                            manifest=runtime_path,
                            game_root=GAME_ROOT,
                            state=STATE_PATH if state is not None else None,
                            entries_only=True,
                        )
                    )
                    reuse = True
            except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError):
                reuse = False

        if not reuse:
            print(f"Discovering the active {language} controller-help and minigame resources...")
            modtool.prepare_language_manifest(
                namespace(
                    manifest=BASE_MANIFEST_PATH,
                    game_root=GAME_ROOT,
                    language=language,
                    output=runtime_path,
                    existing_manifest=runtime_path if runtime_path.is_file() else None,
                    state=STATE_PATH if state is not None else None,
                )
            )
            candidate = read_json(runtime_path)
        elif candidate is not None:
            candidate["project"] = self.base_manifest["project"]
            candidate["version"] = self.base_manifest["version"]
            candidate["distribution_model"] = self.base_manifest["distribution_model"]
            write_json(runtime_path, candidate)

        manifest = read_json(runtime_path)
        supported = manifest.get("supported_game", {})
        if supported.get("language_code") != language:
            raise ModError("Runtime language manifest mismatch.")
        return runtime_path, manifest

    def build_payload(
        self,
        manifest_path: Path,
        language: str,
        variant_key: str,
    ) -> Path:
        build_root = LOCAL_ROOT / "build" / language / variant_key
        modtool.build(
            namespace(
                manifest=manifest_path,
                original_entry_dir=ORIGINAL_ENTRY_ROOT,
                asset_dir=ASSET_ROOT,
                variant=variant_key,
                output_dir=build_root,
            )
        )
        return build_root

    def install(self, variant: str) -> None:
        self.validate_game(require_closed=True)
        self.validate_assets()
        language = self.active_language()
        state = self.state()
        manifest_path, manifest = self.runtime_manifest(language, state)

        print(f"Classifying supported resident and {language} language archives...")
        classification = modtool.classify_game(
            namespace(
                manifest=manifest_path,
                game_root=GAME_ROOT,
                state=STATE_PATH if state is not None else None,
                entries_only=True,
            )
        )
        any_patched = bool(classification["any_patched"])
        all_patched = bool(classification["all_patched"])

        ORIGINAL_ENTRY_ROOT.mkdir(parents=True, exist_ok=True)
        RUNTIME_MANIFEST_ROOT.mkdir(parents=True, exist_ok=True)
        print("Saving or verifying only the required compact original entries...")
        modtool.capture_original_entries(
            namespace(
                manifest=manifest_path,
                game_root=GAME_ROOT,
                output_dir=ORIGINAL_ENTRY_ROOT,
            )
        )

        variant_key = "dualsense" if variant == "DualSense" else "dualshock4"
        print(f"Building the {variant} payload for language {language}...")
        build_root = self.build_payload(manifest_path, language, variant_key)

        if (
            all_patched
            and state is not None
            and state.get("variant") == variant
            and state.get("language_code") == language
        ):
            full_classification = modtool.classify_game(
                namespace(
                    manifest=manifest_path,
                    game_root=GAME_ROOT,
                    state=STATE_PATH,
                    entries_only=False,
                )
            )
            if not bool(full_classification["all_patched"]):
                raise ModError("Installed archive state is incomplete.")
            modtool.verify_game(
                namespace(
                    manifest=manifest_path,
                    game_root=GAME_ROOT,
                    payload_dir=build_root,
                    expect="patched",
                )
            )
            state["project"] = manifest["project"]
            state["version"] = manifest["version"]
            write_json(STATE_PATH, state)
            print(f"{variant} is already installed and verified for {language}.")
            return

        switching = any_patched and state is not None and state.get("variant") != variant
        current_build_root: Path | None = None
        if switching:
            current_variant_key = (
                "dualsense" if state.get("variant") == "DualSense" else "dualshock4"
            )
            current_build_root = self.build_payload(
                manifest_path, language, current_variant_key
            )

        self.preflight_archive_writes(manifest)

        patch_attempted = False
        try:
            if switching and current_build_root is not None:
                print(f"Restoring compact originals before switching to {variant}...")
                modtool.restore_game(
                    namespace(
                        manifest=manifest_path,
                        game_root=GAME_ROOT,
                        payload_dir=current_build_root,
                        original_entry_dir=ORIGINAL_ENTRY_ROOT,
                    )
                )

            patch_attempted = True
            print(
                f"Applying the resident, {language} controller-help, "
                "and minigame replacements..."
            )
            patch_result = modtool.patch_game(
                namespace(
                    manifest=manifest_path,
                    game_root=GAME_ROOT,
                    payload_dir=build_root,
                )
            )
            modtool.verify_game(
                namespace(
                    manifest=manifest_path,
                    game_root=GAME_ROOT,
                    payload_dir=build_root,
                    expect="patched",
                )
            )

            records = []
            for patch in manifest["archive_patches"]:
                payload_path = build_root / str(patch["payload_name"])
                matches = [
                    record
                    for record in patch_result["archives"]
                    if record["game_path"] == patch["game_path"]
                    and record["target_entry"] == patch["target_entry"]["path"]
                ]
                if len(matches) != 1:
                    raise ModError(f"Patcher did not report {patch['game_path']}.")
                patch_record = matches[0]
                records.append(
                    {
                        "game_path": patch["game_path"],
                        "original_sha256": patch["original_archive_sha256"],
                        "patched_sha256": patch_record["archive_sha256"],
                        "target_entry": patch["target_entry"]["path"],
                        "original_entry_backup": patch["target_entry"]["backup_name"],
                        "patched_entry_size": payload_path.stat().st_size,
                        "patched_entry_sha256": sha256_file(payload_path),
                    }
                )
            new_state: dict[str, object] = {
                "project": manifest["project"],
                "version": manifest["version"],
                "variant": variant,
                "language_code": language,
                "runtime_manifest": f"manifests/runtime-manifest-{language}.json",
                "installed": True,
                "installed_utc": utc_now(),
                "archives": records,
            }
            write_json(STATE_PATH, new_state)
        except Exception:
            recovery_root = build_root if patch_attempted else current_build_root
            if recovery_root is not None:
                print("WARNING: Installation failed; restoring compact original entries.")
                try:
                    modtool.restore_game(
                        namespace(
                            manifest=manifest_path,
                            game_root=GAME_ROOT,
                            payload_dir=recovery_root,
                            original_entry_dir=ORIGINAL_ENTRY_ROOT,
                        )
                    )
                    if state is not None:
                        state["installed"] = False
                        state["recovery_utc"] = utc_now()
                        write_json(STATE_PATH, state)
                except Exception as recovery_error:
                    print(f"WARNING: Automatic recovery also failed: {recovery_error}")
            raise

        print(
            f"{variant} UI variant installed successfully for language {language}. "
            "Controller input was not changed."
        )

    def uninstall(self) -> None:
        self.validate_game(require_closed=True)
        state = self.state()
        if state is None:
            raise ModError(
                "No installation record exists. Use Steam Verify if game files need restoration."
            )
        if not bool(state.get("installed")):
            print("The mod is already marked as uninstalled.")
            return
        runtime_relative = str(state.get("runtime_manifest", ""))
        if not runtime_relative:
            raise ModError(
                "This is a legacy installation record. Run the installer or use Steam Verify."
            )
        manifest_path = resolve_relative(LOCAL_ROOT, runtime_relative)
        if not manifest_path.is_file():
            raise ModError("The saved runtime manifest is missing. Use Steam Verify.")
        manifest = read_json(manifest_path)

        print(f"Classifying installed archives for language {state.get('language_code')}...")
        classification = modtool.classify_game(
            namespace(
                manifest=manifest_path,
                game_root=GAME_ROOT,
                state=STATE_PATH,
                entries_only=True,
            )
        )
        if bool(classification["any_patched"]):
            self.preflight_archive_writes(manifest)
            modtool.verify_original_entries(
                namespace(
                    manifest=manifest_path,
                    original_entry_dir=ORIGINAL_ENTRY_ROOT,
                )
            )
            variant_key = (
                "dualsense" if state.get("variant") == "DualSense" else "dualshock4"
            )
            build_root = self.build_payload(
                manifest_path, str(state["language_code"]), variant_key
            )
            print("Restoring only the modified CPK entries...")
            modtool.restore_game(
                namespace(
                    manifest=manifest_path,
                    game_root=GAME_ROOT,
                    payload_dir=build_root,
                    original_entry_dir=ORIGINAL_ENTRY_ROOT,
                )
            )
        else:
            print("All currently installed supported archives are already original.")

        state["installed"] = False
        state["uninstalled_utc"] = utc_now()
        write_json(STATE_PATH, state)
        print("Uninstall completed successfully. Compact original entries were retained.")

    def verify_installed(self) -> None:
        self.validate_game()
        self.validate_assets()
        state = self.state()
        if state is None or not bool(state.get("installed")):
            raise ModError("No installed-state record exists.")
        active_language = self.active_language()
        if active_language != state.get("language_code"):
            raise ModError(
                f"The game language changed from {state.get('language_code')} to "
                f"{active_language}. Run the selected installer again."
            )
        runtime_relative = str(state.get("runtime_manifest", ""))
        if not runtime_relative:
            raise ModError("Legacy installation record detected. Run the selected installer.")
        manifest_path = resolve_relative(LOCAL_ROOT, runtime_relative)
        if not manifest_path.is_file():
            raise ModError("The saved runtime manifest is missing.")
        variant_key = (
            "dualsense" if state.get("variant") == "DualSense" else "dualshock4"
        )
        build_root = LOCAL_ROOT / "build" / str(state["language_code"]) / variant_key

        classification = modtool.classify_game(
            namespace(
                manifest=manifest_path,
                game_root=GAME_ROOT,
                state=STATE_PATH,
                entries_only=False,
            )
        )
        if not bool(classification["all_patched"]):
            raise ModError("One or more installed archives are not patched.")
        modtool.verify_original_entries(
            namespace(
                manifest=manifest_path,
                original_entry_dir=ORIGINAL_ENTRY_ROOT,
            )
        )
        self.build_payload(manifest_path, str(state["language_code"]), variant_key)
        modtool.verify_game(
            namespace(
                manifest=manifest_path,
                game_root=GAME_ROOT,
                payload_dir=build_root,
                expect="patched",
            )
        )
        print(
            f"Installed {state.get('variant')} state for {state.get('language_code')} "
            "and compact rollback entries: OK"
        )

    def verify_original(self) -> None:
        self.validate_game()
        self.validate_assets()
        state = self.state()
        manifest_path = BASE_MANIFEST_PATH
        if state is not None and state.get("runtime_manifest"):
            saved = resolve_relative(LOCAL_ROOT, str(state["runtime_manifest"]))
            if saved.is_file():
                manifest_path = saved
        classification = modtool.classify_game(
            namespace(
                manifest=manifest_path,
                game_root=GAME_ROOT,
                state=STATE_PATH if state is not None else None,
                entries_only=False,
            )
        )
        if not bool(classification["all_original"]):
            raise ModError("One or more present archives are not original.")
        print("Original archives recorded by the active or saved manifest: OK")

    def run_action(self, action: str) -> None:
        actions = {
            "dualsense": lambda: self.install("DualSense"),
            "dualshock4": lambda: self.install("DualShock4"),
            "verify-installed": self.verify_installed,
            "uninstall": self.uninstall,
            "verify-original": self.verify_original,
        }
        actions[action]()

    def interactive(self) -> int:
        actions = {
            "1": "dualsense",
            "2": "dualshock4",
            "3": "verify-installed",
            "4": "uninstall",
            "5": "verify-original",
        }
        while True:
            language = self.active_language(required=False)
            print("\n" + "=" * 60)
            print(f" DRV3 PlayStation Controller Icons Manager v{self.version}")
            print("=" * 60)
            print(f"\nActive game language: {language}\n")
            print("  1. Install DualSense icons (Create and Options icons)")
            print("  2. Install DualShock 4 icons (SHARE and OPTIONS)")
            print("  3. Verify the installed mod")
            print("  4. Uninstall and restore original files")
            print("  5. Verify original game files")
            print("  Q. Quit\n")
            print("The game must be closed before installing or uninstalling.")
            print("Only compact rollback entries are saved; no full CPK backup is made.\n")
            try:
                choice = input("Choose an option: ").strip().upper()
            except (EOFError, KeyboardInterrupt):
                print()
                return 0
            if choice == "Q":
                return 0
            action = actions.get(choice)
            if action is None:
                print("Unknown option.")
                continue
            try:
                self.run_action(action)
                print("\nOperation completed successfully.")
            except Exception as exc:
                if os.environ.get("DRV3_MOD_DEBUG") == "1":
                    traceback.print_exc()
                print(f"\nERROR: {exc}")
                print("No unsupported file should have been patched.")
            try:
                input("\nPress Enter to return to the menu...")
            except (EOFError, KeyboardInterrupt):
                print()
                return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument(
        "action",
        nargs="?",
        choices=(
            "dualsense",
            "dualshock4",
            "verify-installed",
            "uninstall",
            "verify-original",
        ),
        help="run one operation non-interactively; omit for the menu",
    )
    result.add_argument("--version", action="store_true", help="show the mod version")
    return result


def main() -> int:
    try:
        args = parser().parse_args()
        manager = ModManager()
        if args.version:
            print(manager.version)
            return 0
        if args.action is None:
            return manager.interactive()
        manager.run_action(args.action)
        return 0
    except Exception as exc:
        if os.environ.get("DRV3_MOD_DEBUG") == "1":
            traceback.print_exc()
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
