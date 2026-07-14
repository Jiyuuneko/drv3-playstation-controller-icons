#!/usr/bin/env python3
"""Read, decompress, recompress, and rebuild DRV3 CPS./SPC archives.

The SPC LZSS format and compression behavior are based on V3Lib's
SpcSubfile implementation by CaptainSwag101, as distributed with the
GPL-3.0 Harmony-Tools project. This Python implementation is rewritten
and optimized for this mod's source-only build pipeline.

Copyright (C) 2026 Danganronpa V3 PlayStation Controller Icons contributors
SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import dataclasses
import struct
from collections import defaultdict, deque


WINDOW_SIZE = 1024
MAX_SEQUENCE = 65


def align16(value: int) -> int:
    return (value + 15) & ~15


def reverse_bits(value: int) -> int:
    return int(f"{value:08b}"[::-1], 2)


def decompress_lzss(data: bytes, expected_size: int) -> bytes:
    output = bytearray()
    flag = 1
    pos = 0
    while pos < len(data):
        if flag == 1:
            flag = 0x100 | reverse_bits(data[pos])
            pos += 1
        if pos >= len(data):
            break
        if flag & 1:
            output.append(data[pos])
            pos += 1
        else:
            if pos + 2 > len(data):
                raise ValueError("truncated SPC LZSS reference")
            value = int.from_bytes(data[pos : pos + 2], "little")
            pos += 2
            count = (value >> 10) + 2
            offset = value & (WINDOW_SIZE - 1)
            for _ in range(count):
                source = len(output) - WINDOW_SIZE + offset
                if source < 0 or source >= len(output):
                    raise ValueError("invalid SPC LZSS window reference")
                output.append(output[source])
        flag >>= 1
    if len(output) != expected_size:
        raise ValueError(
            f"SPC decompression produced {len(output)} bytes; expected {expected_size}"
        )
    return bytes(output)


def compress_lzss(data: bytes) -> bytes:
    """Greedy V3Lib-compatible compressor with an indexed 1024-byte window."""
    output = bytearray()
    positions: dict[int, deque[int]] = defaultdict(deque)
    indexed_until = 0
    pos = 0

    while pos < len(data):
        flag = 0
        block = bytearray()
        for bit in range(8):
            if pos >= len(data):
                break

            while indexed_until < pos:
                if indexed_until + 1 < len(data):
                    key = (data[indexed_until] << 8) | data[indexed_until + 1]
                    positions[key].append(indexed_until)
                indexed_until += 1

            best_length = 0
            best_distance = 0
            if pos + 1 < len(data):
                key = (data[pos] << 8) | data[pos + 1]
                candidates = positions.get(key)
                if candidates:
                    minimum = pos - WINDOW_SIZE
                    while candidates and candidates[0] < minimum:
                        candidates.popleft()
                    for candidate in reversed(candidates):
                        distance = pos - candidate
                        limit = min(MAX_SEQUENCE, len(data) - pos)
                        length = 2
                        while (
                            length < limit
                            and data[candidate + length] == data[pos + length]
                        ):
                            length += 1
                        if length > best_length:
                            best_length = length
                            best_distance = distance
                            if length == limit:
                                break

            if best_length >= 2:
                encoded = (WINDOW_SIZE - best_distance) | ((best_length - 2) << 10)
                block.extend(encoded.to_bytes(2, "little"))
                pos += best_length
            else:
                flag |= 1 << bit
                block.append(data[pos])
                pos += 1

        output.append(reverse_bits(flag))
        output.extend(block)
    return bytes(output)


@dataclasses.dataclass
class Member:
    compression: int
    unknown: int
    original_size: int
    prefix: bytes
    name_and_padding: bytes
    name_length: int
    stored_data: bytes

    @property
    def name(self) -> str:
        return self.name_and_padding[: self.name_length].decode("shift_jis")

    def unpacked(self) -> bytes:
        if self.compression == 2:
            return decompress_lzss(self.stored_data, self.original_size)
        if self.compression in (0, 1):
            if len(self.stored_data) != self.original_size:
                raise ValueError(f"unexpected uncompressed member size: {self.name}")
            return self.stored_data
        raise ValueError(f"unsupported SPC compression flag {self.compression}: {self.name}")

    def with_unpacked(self, data: bytes) -> "Member":
        if len(data) != self.original_size:
            raise ValueError(
                f"refusing original-size change for {self.name}: "
                f"{self.original_size} -> {len(data)}"
            )
        if self.compression == 2:
            stored = compress_lzss(data)
        elif self.compression in (0, 1):
            stored = data
        else:
            raise ValueError(f"unsupported SPC compression flag {self.compression}: {self.name}")
        return dataclasses.replace(self, stored_data=stored)


@dataclasses.dataclass
class Archive:
    header: bytes
    members: list[Member]

    def one(self, name: str) -> Member:
        matches = [member for member in self.members if member.name == name]
        if len(matches) != 1:
            raise ValueError(f"expected one {name!r} member, found {len(matches)}")
        return matches[0]

    def replace(self, name: str, unpacked: bytes) -> None:
        old = self.one(name)
        self.members[self.members.index(old)] = old.with_unpacked(unpacked)

    def rebuild(self) -> bytes:
        result = bytearray(self.header)
        for member in self.members:
            prefix = bytearray(member.prefix)
            struct.pack_into(
                "<hhiii",
                prefix,
                0,
                member.compression,
                member.unknown,
                len(member.stored_data),
                member.original_size,
                member.name_length,
            )
            result.extend(prefix)
            result.extend(member.name_and_padding)
            result.extend(member.stored_data)
            result.extend(b"\0" * (-len(member.stored_data) % 16))
        return bytes(result)


def parse(data: bytes) -> Archive:
    if data[:4] != b"CPS.":
        raise ValueError("not a CPS./SPC archive")
    count = struct.unpack_from("<I", data, 40)[0]
    pos = 80
    members: list[Member] = []
    for _ in range(count):
        entry_start = pos
        compression, unknown, stored_size, original_size, name_length = struct.unpack_from(
            "<hhiii", data, pos
        )
        name_start = pos + 32
        data_start = align16(name_start + name_length + 1)
        data_end = data_start + stored_size
        if data_end > len(data):
            raise ValueError("SPC member extends beyond archive")
        members.append(
            Member(
                compression=compression,
                unknown=unknown,
                original_size=original_size,
                prefix=data[entry_start:name_start],
                name_and_padding=data[name_start:data_start],
                name_length=name_length,
                stored_data=data[data_start:data_end],
            )
        )
        pos = align16(data_end)
    if pos != len(data):
        raise ValueError(f"unexpected SPC trailing bytes: {len(data) - pos}")
    return Archive(data[:80], members)
