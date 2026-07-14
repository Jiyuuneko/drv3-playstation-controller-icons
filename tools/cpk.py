#!/usr/bin/env python3
"""Minimal CRI CPK reader and fixed-allocation entry patcher.

Copyright (C) 2026 Danganronpa V3 PlayStation Controller Icons contributors
SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import hashlib
import struct
import time
from dataclasses import dataclass
from pathlib import Path


TYPE_SIZES = {
    0x0: 1,
    0x1: 1,
    0x2: 2,
    0x3: 2,
    0x4: 4,
    0x5: 4,
    0x6: 8,
    0x7: 8,
    0x8: 4,
    0xA: 4,
    0xB: 8,
}


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest().upper()


def open_for_read(path: Path):
    """Retry transient Steam/antivirus sharing locks on large CPK files."""
    for attempt in range(240):
        try:
            return path.open("rb")
        except PermissionError:
            if attempt == 239:
                raise
            time.sleep(0.25)
    raise AssertionError("unreachable")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open_for_read(path) as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def be16(data: bytes, offset: int) -> int:
    return struct.unpack_from(">H", data, offset)[0]


def be32(data: bytes, offset: int) -> int:
    return struct.unpack_from(">I", data, offset)[0]


def cstring(data: bytes, offset: int) -> str:
    end = data.find(b"\0", offset)
    if end < 0:
        end = len(data)
    return data[offset:end].decode("utf-8", errors="replace")


@dataclass
class Column:
    name: str
    kind: int
    row_offset: int | None
    constant: object = None


class UtfTable:
    def __init__(self, data: bytes):
        if data[:4] != b"@UTF":
            raise ValueError("missing @UTF table signature")
        self.data = data
        self.rows_offset = be16(data, 0x0A)
        self.strings_offset = be32(data, 0x0C)
        self.row_length = be16(data, 0x1A)
        self.row_count = be32(data, 0x1C)
        self.rows_base = 8 + self.rows_offset
        self.strings_base = 8 + self.strings_offset
        self.columns: list[Column] = []
        schema_pos = 0x20
        row_pos = 0
        for _ in range(be16(data, 0x18)):
            flags = data[schema_pos]
            schema_pos += 1
            kind = flags & 0x0F
            if kind not in TYPE_SIZES:
                raise ValueError(f"unsupported @UTF type 0x{kind:X}")
            name_offset = be32(data, schema_pos)
            schema_pos += 4
            name = cstring(data, self.strings_base + name_offset)
            storage = flags & 0xF0
            constant = None
            this_row_offset = None
            if storage & 0x20:
                constant, schema_pos = self._decode(schema_pos, kind)
            if storage & 0x40:
                this_row_offset = row_pos
                row_pos += TYPE_SIZES[kind]
            self.columns.append(Column(name, kind, this_row_offset, constant))
        if row_pos != self.row_length:
            raise ValueError("@UTF row schema length mismatch")

    def _decode(self, offset: int, kind: int) -> tuple[object, int]:
        size = TYPE_SIZES[kind]
        if kind == 0x0:
            value = self.data[offset]
        elif kind == 0x1:
            value = struct.unpack_from(">b", self.data, offset)[0]
        elif kind == 0x2:
            value = be16(self.data, offset)
        elif kind == 0x3:
            value = struct.unpack_from(">h", self.data, offset)[0]
        elif kind == 0x4:
            value = be32(self.data, offset)
        elif kind == 0x5:
            value = struct.unpack_from(">i", self.data, offset)[0]
        elif kind == 0x6:
            value = struct.unpack_from(">Q", self.data, offset)[0]
        elif kind == 0x7:
            value = struct.unpack_from(">q", self.data, offset)[0]
        elif kind == 0x8:
            value = struct.unpack_from(">f", self.data, offset)[0]
        elif kind == 0xA:
            value = cstring(self.data, self.strings_base + be32(self.data, offset))
        elif kind == 0xB:
            value = {
                "offset": be32(self.data, offset),
                "size": be32(self.data, offset + 4),
            }
        else:
            raise AssertionError(kind)
        return value, offset + size

    def row(self, index: int) -> dict[str, object]:
        if not 0 <= index < self.row_count:
            raise IndexError(index)
        result: dict[str, object] = {}
        base = self.rows_base + index * self.row_length
        for column in self.columns:
            if column.row_offset is None:
                result[column.name] = column.constant
            else:
                result[column.name] = self._decode(base + column.row_offset, column.kind)[0]
        return result


def read_utf(stream, offset: int) -> UtfTable:
    stream.seek(offset)
    header = stream.read(8)
    if len(header) != 8 or header[:4] != b"@UTF":
        raise ValueError(f"missing @UTF at 0x{offset:X}")
    total_size = be32(header, 4) + 8
    stream.seek(offset)
    return UtfTable(stream.read(total_size))


class CpkArchive:
    def __init__(self, path: Path):
        self.path = path.resolve()
        self.stream = open_for_read(self.path)
        if self.stream.read(4) != b"CPK ":
            raise ValueError(f"{path} is not a CRI CPK archive")
        self.header = read_utf(self.stream, 0x10).row(0)
        toc_offset = self.header.get("TocOffset")
        if not isinstance(toc_offset, int) or toc_offset <= 0:
            raise ValueError("CPK has no usable TOC")
        self.stream.seek(toc_offset)
        if self.stream.read(4) != b"TOC ":
            raise ValueError(f"missing TOC chunk at 0x{toc_offset:X}")
        self.toc_offset = toc_offset
        self.toc = read_utf(self.stream, toc_offset + 0x10)
        content_offset = int(self.header.get("ContentOffset") or 0)
        self.offset_base = min(content_offset, toc_offset)

    def close(self) -> None:
        self.stream.close()

    def __enter__(self) -> "CpkArchive":
        return self

    def __exit__(self, *_args) -> None:
        self.close()

    def files(self) -> list[dict[str, object]]:
        result = []
        for index in range(self.toc.row_count):
            row = self.toc.row(index)
            directory = str(row.get("DirName") or "").replace("\\", "/").strip("/")
            name = str(row.get("FileName") or "")
            internal = f"{directory}/{name}" if directory else name
            result.append(
                {
                    "index": index,
                    "path": internal,
                    "stored_size": int(row.get("FileSize") or 0),
                    "extract_size": int(row.get("ExtractSize") or 0),
                    "offset": self.offset_base + int(row.get("FileOffset") or 0),
                }
            )
        return result

    def find(self, internal_path: str) -> dict[str, object]:
        wanted = internal_path.replace("\\", "/").casefold()
        matches = [entry for entry in self.files() if str(entry["path"]).casefold() == wanted]
        if len(matches) != 1:
            raise ValueError(f"expected one {internal_path!r} entry, found {len(matches)}")
        return matches[0]

    def read_entry(self, internal_path: str) -> bytes:
        entry = self.find(internal_path)
        self.stream.seek(int(entry["offset"]))
        payload = self.stream.read(int(entry["stored_size"]))
        if len(payload) != int(entry["stored_size"]):
            raise ValueError(f"short read for {internal_path}")
        if payload.startswith(b"CRILAYLA"):
            raise ValueError(f"compressed CPK entry is unsupported: {internal_path}")
        return payload

    def toc_cell(self, row_index: int, name: str) -> tuple[int, int]:
        column = next((item for item in self.toc.columns if item.name == name), None)
        if column is None or column.row_offset is None or column.kind not in (0x4, 0x6):
            raise ValueError(f"unsupported TOC column: {name}")
        position = (
            self.toc_offset
            + 0x10
            + self.toc.rows_base
            + row_index * self.toc.row_length
            + column.row_offset
        )
        return position, column.kind


def inspect_entry(
    archive_path: Path,
    internal_path: str,
    original_size: int,
    original_hash: str,
    replacement_size: int,
    replacement_hash: str,
) -> dict[str, object]:
    with CpkArchive(archive_path) as archive:
        if int(archive.header.get("Codec") or 0) != 0:
            raise ValueError("archive codec is not the supported uncompressed mode")
        if int(archive.header.get("EnableFileCrc") or 0) != 0:
            raise ValueError("archive unexpectedly enables per-file CRCs")
        files = archive.files()
        entry = archive.find(internal_path)
        payload = archive.read_entry(internal_path)
        payload_hash = sha256(payload)
        if (
            int(entry["stored_size"]) == original_size
            and int(entry["extract_size"]) == original_size
            and payload_hash == original_hash.upper()
        ):
            state = "original"
        elif (
            int(entry["stored_size"]) == replacement_size
            and int(entry["extract_size"]) == replacement_size
            and payload_hash == replacement_hash.upper()
        ):
            state = "patched"
        else:
            state = "unsupported"
        offset = int(entry["offset"])
        later = sorted(int(item["offset"]) for item in files if int(item["offset"]) > offset)
        allocation_end = later[0] if later else archive_path.stat().st_size
        file_cell, file_kind = archive.toc_cell(int(entry["index"]), "FileSize")
        extract_cell, extract_kind = archive.toc_cell(int(entry["index"]), "ExtractSize")
        return {
            "state": state,
            "entry": {**entry, "sha256": payload_hash},
            "allocation_size": allocation_end - offset,
            "file_size_cell": file_cell,
            "file_size_kind": file_kind,
            "extract_size_cell": extract_cell,
            "extract_size_kind": extract_kind,
        }


def encode_integer(value: int, kind: int) -> bytes:
    return struct.pack(">I" if kind == 0x4 else ">Q", value)


def open_for_update(path: Path):
    """Retry transient Steam/antivirus sharing locks on newly selected language CPKs."""
    for attempt in range(240):
        try:
            return path.open("r+b", buffering=0)
        except PermissionError:
            if attempt == 239:
                raise
            time.sleep(0.25)
    raise AssertionError("unreachable")


def patch_entry(
    archive_path: Path,
    internal_path: str,
    original_size: int,
    original_hash: str,
    replacement: bytes,
    expected_original_archive_hash: str | None = None,
) -> dict[str, object]:
    replacement_hash = sha256(replacement)
    result = inspect_entry(
        archive_path,
        internal_path,
        original_size,
        original_hash,
        len(replacement),
        replacement_hash,
    )
    if result["state"] == "patched":
        return result
    if result["state"] != "original":
        raise ValueError("entry is neither the supported original nor the expected patch")
    if len(replacement) > int(result["allocation_size"]):
        raise ValueError("replacement does not fit the existing CPK allocation")
    offset = int(result["entry"]["offset"])
    remaining = int(result["allocation_size"]) - len(replacement)
    with open_for_update(archive_path) as stream:
        if expected_original_archive_hash is not None:
            stream.seek(0)
            digest = hashlib.sha256()
            for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
                digest.update(block)
            actual = digest.hexdigest().upper()
            if actual != expected_original_archive_hash.upper():
                raise ValueError(f"original archive checksum mismatch: {archive_path}")
        stream.seek(offset)
        stream.write(replacement)
        zeroes = b"\0" * min(1024 * 1024, max(remaining, 1))
        while remaining:
            count = min(remaining, len(zeroes))
            stream.write(zeroes[:count])
            remaining -= count
        for cell, kind in (
            (result["file_size_cell"], result["file_size_kind"]),
            (result["extract_size_cell"], result["extract_size_kind"]),
        ):
            stream.seek(int(cell))
            stream.write(encode_integer(len(replacement), int(kind)))
        stream.flush()
    verified = inspect_entry(
        archive_path,
        internal_path,
        original_size,
        original_hash,
        len(replacement),
        replacement_hash,
    )
    if verified["state"] != "patched":
        raise ValueError("post-write structural verification failed")
    return verified
