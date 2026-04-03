#!/usr/bin/env python3
"""
SHITMAN - Atari Jaguar CD image to BigPImage converter

Converts BIN/CUE disc images into BigPEmu's .bigpimg format.
"""

import argparse
import os
import struct
import sys
import zlib
from dataclasses import dataclass, field
from pathlib import Path


# ── Version ───────────────────────────────────────────────────────────────────

VERSION = '0.69.420'

# ── Constants ────────────────────────────────────────────────────────────────

BIGPIMG_MAGIC = b'\x35\x71\x17\xd7\x7c\xf4\x19\x58'
BIGPIMG_VERSION = 1
SECTOR_SIZE_RAW = 2352        # Standard CD sector
SECTOR_SIZE_SUB = 2448        # With 96-byte subchannel
SUBCHANNEL_SIZE = 96           # Subchannel data per sector
PREGAP_SECTORS = 150           # 2-second pregap before first track
INTERSESSION_GAP = 11400       # Lead-out (6750) + lead-in (4500) + pregap (150)
LEAD_OUT_SECTORS = 2250        # Final session lead-out (30 seconds at 75 fps)
HEADER_SIZE = 80               # 0x50
SESSION_ENTRY_SIZE = 16
TRACK_ENTRY_SIZE = 28
FLAGS_NORMAL = 2
FLAGS_SUBCHANNEL = 3
# Per-track sector_size in subchannel mode: main_size | (sub_size << 16)
TRACK_SECTOR_SIZE_SUB = SECTOR_SIZE_RAW | (SUBCHANNEL_SIZE << 16)  # 0x00600930
# Subchannel fill patterns
SUB_FILL_GAP = b'\x80' * SUBCHANNEL_SIZE       # Gap/lead-in sectors
SUB_FILL_DATA = (b'\x00' * 6 + b'\x40' + b'\x00' * 5) * 8  # Data sectors
DEFLATE_LEVEL = 9              # Best compression (closest to BigPEmu output)


# ── CUE/BIN Parsing ─────────────────────────────────────────────────────────

@dataclass
class CueTrack:
    number: int
    track_type: str            # AUDIO, MODE1/2352, MODE2/2352, etc.
    filename: str
    flags: list
    index00_offset: int        # Sectors from start of file to INDEX 00 (-1 if absent)
    index01_offset: int        # Sectors from start of file to INDEX 01
    session: int               # 1-based session number
    file_size: int = 0         # Populated after parsing
    sector_count: int = 0      # Total sectors in the BIN file


@dataclass
class CueSession:
    number: int                # 1-based
    first_track: int           # 1-based track number
    last_track: int            # 1-based track number


@dataclass
class CueSheet:
    sessions: list
    tracks: list
    base_dir: str              # Directory containing the CUE file


def parse_msf(msf: str) -> int:
    """Parse MM:SS:FF timestamp to sector count."""
    parts = msf.strip().split(':')
    mm, ss, ff = int(parts[0]), int(parts[1]), int(parts[2])
    return mm * 60 * 75 + ss * 75 + ff


def parse_cue(cue_path: str) -> CueSheet:
    """Parse a CUE sheet file."""
    base_dir = os.path.dirname(os.path.abspath(cue_path))
    sessions = []
    tracks = []
    current_session = 1
    current_file = None
    current_track = None

    with open(cue_path, 'r') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue

            if line.startswith('REM SESSION'):
                current_session = int(line.split()[-1])
                continue

            if line.startswith('FILE'):
                # Extract filename between quotes
                start = line.index('"') + 1
                end = line.index('"', start)
                current_file = line[start:end]
                continue

            if line.startswith('TRACK'):
                parts = line.split()
                track_num = int(parts[1])
                track_type = parts[2]
                current_track = CueTrack(
                    number=track_num,
                    track_type=track_type,
                    filename=current_file,
                    flags=[],
                    index00_offset=-1,
                    index01_offset=0,
                    session=current_session,
                )
                tracks.append(current_track)
                continue

            if line.startswith('FLAGS') and current_track:
                current_track.flags = line.split()[1:]
                continue

            if line.startswith('INDEX') and current_track:
                parts = line.split()
                idx_num = int(parts[1])
                offset = parse_msf(parts[2])
                if idx_num == 0:
                    current_track.index00_offset = offset
                elif idx_num == 1:
                    current_track.index01_offset = offset
                continue

    # Populate file sizes and sector counts
    for track in tracks:
        file_path = os.path.join(base_dir, track.filename)
        track.file_size = os.path.getsize(file_path)
        track.sector_count = track.file_size // SECTOR_SIZE_RAW

    # Build session list
    session_nums = sorted(set(t.session for t in tracks))
    for sn in session_nums:
        session_tracks = [t for t in tracks if t.session == sn]
        sessions.append(CueSession(
            number=sn,
            first_track=session_tracks[0].number,
            last_track=session_tracks[-1].number,
        ))

    return CueSheet(sessions=sessions, tracks=tracks, base_dir=base_dir)


# ── Disc Layout ──────────────────────────────────────────────────────────────

@dataclass
class DiscTrack:
    """A track with its absolute position on the virtual disc."""
    number: int
    session_index: int         # 0-based
    start_lba: int             # Absolute LBA at INDEX 01 position
    sector_count: int          # Total sectors in the BIN file
    filename: str              # Path to BIN file
    file_offset: int           # Byte offset into the BIN file where data starts
    sector_size: int
    index01_offset: int = 0    # Sectors of pregap before INDEX 01 in BIN file
    data_start_lba: int = 0    # LBA where BIN file sector 0 maps to


@dataclass
class DiscLayout:
    sessions: list             # List of (first_track, last_track, end_lba)
    tracks: list               # List of DiscTrack
    total_sectors: int         # Full disc extent
    sector_size: int


def compute_disc_layout(cue: CueSheet, sector_size: int = SECTOR_SIZE_RAW) -> DiscLayout:
    """Compute absolute LBA positions for all tracks."""
    disc_tracks = []
    current_lba = PREGAP_SECTORS  # First track starts after 150-sector pregap

    for si, session in enumerate(cue.sessions):
        session_tracks = [t for t in cue.tracks if t.session == session.number]

        if si > 0:
            # Inter-session gap before this session
            current_lba += INTERSESSION_GAP

        for ti, track in enumerate(session_tracks):
            # The BIN file data occupies disc space starting at current_lba.
            # The track's reported start_lba is at the INDEX 01 position
            # within the BIN file (i.e., past any pregap/INDEX 00 data).
            data_sectors = track.sector_count
            file_offset = 0  # We include the entire BIN file
            index01_offset = track.index01_offset  # Sectors into file where INDEX 01 is

            dt = DiscTrack(
                number=track.number,
                session_index=si,
                start_lba=current_lba + index01_offset,
                sector_count=data_sectors,
                filename=os.path.join(cue.base_dir, track.filename),
                file_offset=file_offset,
                sector_size=sector_size,
                index01_offset=index01_offset,
                data_start_lba=current_lba,
            )
            disc_tracks.append(dt)
            current_lba += data_sectors

    # Build session end LBAs
    disc_sessions = []
    for si, session in enumerate(cue.sessions):
        session_disc_tracks = [t for t in disc_tracks if t.session_index == si]
        last = session_disc_tracks[-1]
        end_lba = last.data_start_lba + last.sector_count
        disc_sessions.append((session.first_track, session.last_track, end_lba))

    total_sectors = current_lba + LEAD_OUT_SECTORS

    return DiscLayout(
        sessions=disc_sessions,
        tracks=disc_tracks,
        total_sectors=total_sectors,
        sector_size=sector_size,
    )


# ── Sector Reader ────────────────────────────────────────────────────────────

class SectorReader:
    """Read sectors from a disc layout by LBA."""

    def __init__(self, layout: DiscLayout):
        self.layout = layout
        self._file_handles = {}
        # Build LBA -> track lookup
        # BIN file data starts at data_start_lba (= start_lba - index01_offset)
        self._track_map = {}
        for track in layout.tracks:
            for i in range(track.sector_count):
                self._track_map[track.data_start_lba + i] = (track, i)

    def read_sector(self, lba: int) -> bytes:
        """Read a single sector at the given LBA. Returns None for gap sectors."""
        if lba not in self._track_map:
            return None
        track, sector_idx = self._track_map[lba]
        if track.filename not in self._file_handles:
            self._file_handles[track.filename] = open(track.filename, 'rb')
        fh = self._file_handles[track.filename]
        offset = track.file_offset + sector_idx * SECTOR_SIZE_RAW
        fh.seek(offset)
        return fh.read(SECTOR_SIZE_RAW)

    def close(self):
        for fh in self._file_handles.values():
            fh.close()
        self._file_handles.clear()


# ── Disc ID / Hash ───────────────────────────────────────────────────────────

def compute_disc_id(layout: DiscLayout, reader: SectorReader) -> bytes:
    """
    Compute the 8-byte disc ID using BigPEmu's FNV-1a 64-bit hash algorithm.

    The hash input consists of:
    1. Trimmed sector data from the first track of the last session (full BIN file
       with leading/trailing zero bytes removed)
    2. Session metadata: num_sessions × 12 bytes (first_track, last_track, end_lba)
    3. Track metadata: num_tracks × 20 bytes (start_lba, track_num, sector_size,
       session_index, flag=1)
    """
    FNV_OFFSET = 0xcbf29ce484222325
    FNV_PRIME = 0x100000001b3
    MASK64 = (1 << 64) - 1

    # Find the first track of the last session
    last_session = layout.sessions[-1]
    target_track_num = last_session[0]  # first_track of last session
    target_track = next(t for t in layout.tracks if t.number == target_track_num)

    # Read the full BIN file for this track
    with open(target_track.filename, 'rb') as f:
        track_data = f.read()

    # Trim leading/trailing zero bytes
    total = len(track_data)
    if total > 0:
        # Forward scan: find first non-zero byte, up to total - 0x400
        fwd_limit = total - 0x400 if total > 0x400 else 0
        fwd = 0
        for i in range(fwd_limit):
            if track_data[i] != 0:
                fwd = i
                break
        else:
            if fwd_limit > 0:
                fwd = fwd_limit

        # Backward scan: find last non-zero byte, down to max(0x800, fwd)
        lower = max(0x800, fwd)
        bwd = total - 1
        if bwd > lower:
            for i in range(bwd, lower, -1):
                if track_data[i] != 0:
                    bwd = i
                    break
            else:
                bwd = lower

        trimmed = track_data[fwd:bwd + 1]
    else:
        trimmed = track_data

    # Build metadata
    meta = b''
    # Session entries: 12 bytes each (first_track_u32, last_track_u32, end_lba_u32)
    for first_track, last_track, end_lba in layout.sessions:
        meta += struct.pack('<III', first_track, last_track, end_lba)
    # Track entries: 20 bytes each (start_lba, track_num, sector_size, session_idx, flag)
    for track in layout.tracks:
        meta += struct.pack('<IIIII',
                            track.start_lba,
                            track.number,
                            SECTOR_SIZE_RAW,  # Always use raw sector size (2352)
                            track.session_index,
                            1)  # Flag: always 1

    # FNV-1a 64-bit hash over trimmed data + metadata
    hash_input = trimmed + meta
    h = FNV_OFFSET
    for b in hash_input:
        h = ((h ^ b) * FNV_PRIME) & MASK64

    return struct.pack('<Q', h)


# ── Encoder ──────────────────────────────────────────────────────────────────

def compress_sector(data: bytes, level: int = DEFLATE_LEVEL) -> bytes:
    """Compress a sector with raw DEFLATE."""
    obj = zlib.compressobj(level, zlib.DEFLATED, -15)
    compressed = obj.compress(data)
    compressed += obj.flush()
    return compressed


def encode_shitman(cue_path: str, output_path: str,
                   subchannel: bool = False,
                   prepass: bool = False,
                   compression_level: int = DEFLATE_LEVEL,
                   verbose: bool = False):
    """Encode a BIN/CUE disc image into BigPImage format."""

    if verbose:
        print(f"Parsing CUE: {cue_path}")

    cue = parse_cue(cue_path)
    sector_size = SECTOR_SIZE_SUB if subchannel else SECTOR_SIZE_RAW
    flags = FLAGS_SUBCHANNEL if subchannel else FLAGS_NORMAL

    layout = compute_disc_layout(cue, sector_size)
    reader = SectorReader(layout)

    if verbose:
        print(f"Sessions: {len(layout.sessions)}")
        print(f"Tracks: {len(layout.tracks)}")
        print(f"Total sectors: {layout.total_sectors}")
        print(f"Sector size: {sector_size}")

    # ── Phase 1: Compress all sectors and build the index ────────────────

    if verbose:
        print("Compressing sectors...")

    sector_index = []      # u16 per sector
    compressed_data = []   # List of bytes objects for stored sectors
    zero_sector = b'\x00' * SECTOR_SIZE_RAW

    # Dictionary for pre-pass deduplication
    dict_entries = []       # List of raw sector bytes
    dict_map = {}           # sector_content_hash -> dict_index
    dict_entry_count = 0
    dict_compressed = b''

    if prepass:
        # First pass: identify duplicate non-zero sectors
        if verbose:
            print("Pre-pass: building sector dictionary...")
        sector_hashes = {}   # hash -> (content, count, [lbas])
        for lba in range(layout.total_sectors):
            raw = reader.read_sector(lba)
            if raw is None or raw == zero_sector:
                continue
            h = hash(raw)
            if h in sector_hashes:
                sector_hashes[h][1] += 1
                sector_hashes[h][2].append(lba)
            else:
                sector_hashes[h] = [raw, 1, [lba]]

        # Collect duplicates — only sectors appearing 3+ times are worth
        # the dictionary overhead (matches BigPEmu's observed behavior)
        for h, (content, count, lbas) in sector_hashes.items():
            if count >= 3:
                idx = len(dict_entries)
                dict_entries.append(content)
                dict_map[h] = idx

        dict_entry_count = len(dict_entries)
        if dict_entry_count > 0:
            # Compress dictionary as single deflate stream
            dict_raw = b''.join(dict_entries)
            dict_compressed = compress_sector(dict_raw, compression_level)
            if verbose:
                print(f"  Dictionary: {dict_entry_count} entries, "
                      f"{len(dict_raw)} -> {len(dict_compressed)} bytes")

    # Main compression pass
    for lba in range(layout.total_sectors):
        raw = reader.read_sector(lba)

        if raw is None:
            # Gap sector
            if subchannel:
                # Store gap sector: zeros + gap subchannel pattern (0x80)
                sub_sector = b'\x00' * SECTOR_SIZE_RAW + SUB_FILL_GAP
                compressed = compress_sector(sub_sector, compression_level)
                sector_index.append(len(compressed))
                compressed_data.append(compressed)
            else:
                sector_index.append(0xFFFF)
            continue

        if raw == zero_sector:
            # All-zero data sector
            if subchannel:
                sub_sector = raw + SUB_FILL_DATA
                compressed = compress_sector(sub_sector, compression_level)
                sector_index.append(len(compressed))
                compressed_data.append(compressed)
            else:
                sector_index.append(0xFFFF)
            continue

        # Check dictionary
        if prepass:
            h = hash(raw)
            if h in dict_map:
                dict_idx = dict_map[h]
                ref = 0x8000 | (dict_idx << 7)
                sector_index.append(ref)
                continue

        # Regular compression
        if subchannel:
            # Append synthetic subchannel data pattern
            raw = raw + SUB_FILL_DATA

        compressed = compress_sector(raw, compression_level)

        # If compressed is >= sector_size, store raw with entry = 0
        if len(compressed) >= sector_size:
            sector_index.append(0)
            compressed_data.append(raw)
        else:
            sector_index.append(len(compressed))
            compressed_data.append(compressed)

        if verbose and lba % 10000 == 0 and lba > 0:
            print(f"  Processed {lba}/{layout.total_sectors} sectors...")

    reader.close()

    # ── Phase 2: Compute disc ID ────────────────────────────────────────

    reader2 = SectorReader(layout)
    disc_id = compute_disc_id(layout, reader2)
    reader2.close()

    # ── Phase 3: Write the file ─────────────────────────────────────────

    if verbose:
        print(f"Writing: {output_path}")

    num_sessions = len(layout.sessions)
    num_tracks = len(layout.tracks)
    toc_offset = HEADER_SIZE
    index_offset = toc_offset + num_sessions * SESSION_ENTRY_SIZE + num_tracks * TRACK_ENTRY_SIZE

    with open(output_path, 'wb') as f:
        # ── Header ──
        f.write(BIGPIMG_MAGIC)                              # 0x00: magic
        f.write(struct.pack('<I', BIGPIMG_VERSION))         # 0x08: version
        f.write(struct.pack('<I', num_sessions))            # 0x0C: session count
        f.write(struct.pack('<I', num_tracks))              # 0x10: track count
        f.write(struct.pack('<I', toc_offset))              # 0x14: TOC offset
        f.write(struct.pack('<I', index_offset))            # 0x18: index offset
        f.write(struct.pack('<I', 1))                       # 0x1C: unknown (always 1)
        f.write(struct.pack('<I', sector_size))             # 0x20: sector size
        f.write(struct.pack('<I', flags))                   # 0x24: flags
        f.write(struct.pack('<I', layout.total_sectors))    # 0x28: total sectors
        f.write(struct.pack('<I', 0))                       # 0x2C: reserved
        f.write(disc_id)                                    # 0x30: disc ID (8 bytes)
        f.write(struct.pack('<I', dict_entry_count))        # 0x38: dict count
        f.write(struct.pack('<I', len(dict_compressed)))    # 0x3C: dict compressed size
        f.write(b'\x00' * 16)                               # 0x40: reserved

        # ── Session table ──
        for first_track, last_track, end_lba in layout.sessions:
            f.write(struct.pack('<IIII', first_track, last_track, end_lba, 0))

        # ── Track table ──
        # Per-track sector_size: raw value for normal, composite for subchannel
        track_sector_field = TRACK_SECTOR_SIZE_SUB if subchannel else SECTOR_SIZE_RAW
        for dt in layout.tracks:
            f.write(struct.pack('<II', dt.session_index, track_sector_field))
            f.write(struct.pack('<I', dt.start_lba))
            f.write(b'\x00' * 16)  # Reserved fields

        # ── Sector index ──
        for entry in sector_index:
            f.write(struct.pack('<H', entry))

        # ── Dictionary (if any) ──
        if dict_compressed:
            f.write(dict_compressed)

        # ── Compressed sector data ──
        for chunk in compressed_data:
            f.write(chunk)

    file_size = os.path.getsize(output_path)
    if verbose:
        compressed_count = sum(1 for e in sector_index if e != 0xFFFF and e != 0 and not (e & 0x8000))
        raw_count = sum(1 for e in sector_index if e == 0)
        skipped = sum(1 for e in sector_index if e == 0xFFFF)
        dict_refs = sum(1 for e in sector_index if e != 0xFFFF and (e & 0x8000))
        print(f"Done: {file_size:,} bytes")
        print(f"  Compressed: {compressed_count}, Raw: {raw_count}, "
              f"Skipped: {skipped}, Dict refs: {dict_refs}")


# ── Verification ─────────────────────────────────────────────────────────────

def verify_shitman(bigpimg_path: str, cue_path: str, verbose: bool = False):
    """Verify a .bigpimg file decompresses correctly against source BIN/CUE."""
    cue = parse_cue(cue_path)
    layout = compute_disc_layout(cue)
    reader = SectorReader(layout)

    with open(bigpimg_path, 'rb') as f:
        img = f.read()

    # Parse header
    total_sectors = struct.unpack_from('<I', img, 0x28)[0]
    sector_size = struct.unpack_from('<I', img, 0x20)[0]
    dict_count = struct.unpack_from('<I', img, 0x38)[0]
    dict_comp_size = struct.unpack_from('<I', img, 0x3C)[0]
    index_offset = struct.unpack_from('<I', img, 0x18)[0]

    # Read sector index
    entries = [struct.unpack_from('<H', img, index_offset + i * 2)[0]
               for i in range(total_sectors)]
    table_end = index_offset + total_sectors * 2

    # Load dictionary if present
    dict_sectors = []
    data_start = table_end
    if dict_count > 0 and dict_comp_size > 0:
        dict_compressed = img[table_end:table_end + dict_comp_size]
        dict_raw = zlib.decompress(dict_compressed, -15)
        for i in range(dict_count):
            dict_sectors.append(dict_raw[i * sector_size:(i + 1) * sector_size])
        data_start = table_end + dict_comp_size

    # Verify each stored sector
    offset = data_start
    errors = 0
    verified = 0
    zero_sector = b'\x00' * SECTOR_SIZE_RAW

    for lba in range(total_sectors):
        entry = entries[lba]
        raw = reader.read_sector(lba)

        if entry == 0xFFFF:
            # Should be a gap or zero sector
            if raw is not None and raw != zero_sector:
                if verbose:
                    print(f"  ERROR: LBA {lba} marked FFFF but has non-zero data")
                errors += 1
            continue

        if entry & 0x8000:
            # Dictionary reference
            dict_idx = (entry & 0x7F80) >> 7
            expected = dict_sectors[dict_idx][:SECTOR_SIZE_RAW]
            if raw != expected:
                if verbose:
                    print(f"  ERROR: LBA {lba} dict ref {dict_idx} mismatch")
                errors += 1
            else:
                verified += 1
            continue

        # Stored sector (compressed or raw)
        if entry == 0:
            # Raw uncompressed sector
            stored = img[offset:offset + sector_size]
            offset += sector_size
            if stored[:SECTOR_SIZE_RAW] != (raw or zero_sector):
                if verbose:
                    print(f"  ERROR: LBA {lba} raw content mismatch")
                errors += 1
            else:
                verified += 1
            continue

        # Compressed sector
        compressed = img[offset:offset + entry]
        offset += entry
        try:
            decompressed = zlib.decompress(compressed, -15)
        except Exception as e:
            if verbose:
                print(f"  ERROR: LBA {lba} decompression failed: {e}")
            errors += 1
            continue

        if decompressed[:SECTOR_SIZE_RAW] != (raw or zero_sector):
            if verbose:
                print(f"  ERROR: LBA {lba} content mismatch")
            errors += 1
        else:
            verified += 1

    reader.close()

    print(f"Verification: {verified} sectors OK, {errors} errors")
    return errors == 0


# ── Binary Comparison ────────────────────────────────────────────────────────

def compare_shitman(our_path: str, ref_path: str, verbose: bool = False):
    """Compare our output against a reference .bigpimg file."""
    with open(our_path, 'rb') as f:
        ours = f.read()
    with open(ref_path, 'rb') as f:
        ref = f.read()

    print(f"Our file:  {len(ours):,} bytes")
    print(f"Reference: {len(ref):,} bytes")

    # Compare headers (skip disc ID at 0x30-0x37)
    header_match = True
    for i in range(HEADER_SIZE):
        if 0x30 <= i < 0x38:
            continue  # Skip disc ID
        if i < len(ours) and i < len(ref) and ours[i] != ref[i]:
            if verbose:
                print(f"  Header diff at 0x{i:02X}: ours=0x{ours[i]:02X} ref=0x{ref[i]:02X}")
            header_match = False

    if header_match:
        print("Header: MATCH (excluding disc ID)")
    else:
        print("Header: MISMATCH")

    # Compare session + track tables
    our_idx_off = struct.unpack_from('<I', ours, 0x18)[0]
    ref_idx_off = struct.unpack_from('<I', ref, 0x18)[0]
    toc_match = ours[HEADER_SIZE:our_idx_off] == ref[HEADER_SIZE:ref_idx_off]
    print(f"TOC (sessions+tracks): {'MATCH' if toc_match else 'MISMATCH'}")

    # Compare sector index
    our_total = struct.unpack_from('<I', ours, 0x28)[0]
    ref_total = struct.unpack_from('<I', ref, 0x28)[0]
    if our_total == ref_total:
        our_idx = ours[our_idx_off:our_idx_off + our_total * 2]
        ref_idx = ref[ref_idx_off:ref_idx_off + ref_total * 2]
        idx_match = our_idx == ref_idx
        if not idx_match and verbose:
            # Find first difference
            for i in range(0, len(our_idx), 2):
                o = struct.unpack_from('<H', our_idx, i)[0]
                r = struct.unpack_from('<H', ref_idx, i)[0]
                if o != r:
                    print(f"  Index diff at sector {i//2}: ours=0x{o:04X} ref=0x{r:04X}")
                    break
        print(f"Sector index: {'MATCH' if idx_match else 'MISMATCH'}")
    else:
        print(f"Sector index: MISMATCH (different total sectors: {our_total} vs {ref_total})")


# ── CLI ──────────────────────────────────────────────────────────────────────

def main():
    print(f'SHITMAN - Convert Atari Jaguar CD images to BigPImage format - {VERSION}')

    parser = argparse.ArgumentParser(
        description='SHITMAN - Convert Atari Jaguar CD images to BigPImage format',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s game.cue                        Convert to game.bigpimg
  %(prog)s game.cue -o output.bigpimg      Specify output path
  %(prog)s game.cue --subchannel           Preserve subchannel data
  %(prog)s game.cue --prepass              Enable compression pre-pass
  %(prog)s --verify game.bigpimg game.cue  Verify against source
  %(prog)s --compare ours.bigpimg ref.bigpimg  Compare two files
  %(prog)s --batch /path/to/images/        Convert all CUE files in directory
""")
    parser.add_argument('input', nargs='?', help='Input CUE file or directory')
    parser.add_argument('-o', '--output', help='Output .bigpimg file path')
    parser.add_argument('--subchannel', action='store_true',
                        help='Preserve subchannel data')
    parser.add_argument('--prepass', action='store_true',
                        help='Enable compression pre-pass (sector dictionary)')
    parser.add_argument('--level', type=int, default=DEFLATE_LEVEL,
                        help=f'Compression level 1-9 (default: {DEFLATE_LEVEL})')
    parser.add_argument('-v', '--verbose', action='store_true',
                        help='Verbose output')
    parser.add_argument('--verify', nargs=2, metavar=('BIGPIMG', 'CUE'),
                        help='Verify a .bigpimg against its source CUE')
    parser.add_argument('--compare', nargs=2, metavar=('OURS', 'REF'),
                        help='Compare two .bigpimg files')
    parser.add_argument('--batch', action='store_true',
                        help='Convert all CUE files in input directory')

    args = parser.parse_args()

    if args.verify:
        success = verify_shitman(args.verify[0], args.verify[1], verbose=True)
        sys.exit(0 if success else 1)

    if args.compare:
        compare_shitman(args.compare[0], args.compare[1], verbose=True)
        sys.exit(0)

    if not args.input:
        parser.print_help()
        sys.exit(1)

    if args.batch:
        # Find all CUE files in subdirectories
        input_dir = Path(args.input)
        cue_files = sorted(input_dir.rglob('*.cue'))
        if not cue_files:
            print(f"No CUE files found in {input_dir}")
            sys.exit(1)
        print(f"Found {len(cue_files)} CUE files")
        output_dir = Path(args.output) if args.output else input_dir
        output_dir.mkdir(parents=True, exist_ok=True)
        for cue_file in cue_files:
            name = cue_file.stem + '.bigpimg'
            out_path = output_dir / name
            print(f"\n{'='*60}")
            print(f"Converting: {cue_file.name}")
            try:
                encode_shitman(
                    str(cue_file), str(out_path),
                    subchannel=args.subchannel,
                    prepass=args.prepass,
                    compression_level=args.level,
                    verbose=args.verbose,
                )
            except Exception as e:
                print(f"  ERROR: {e}")
    else:
        cue_path = args.input
        if args.output:
            output_path = args.output
        else:
            output_path = os.path.splitext(cue_path)[0] + '.bigpimg'

        encode_shitman(
            cue_path, output_path,
            subchannel=args.subchannel,
            prepass=args.prepass,
            compression_level=args.level,
            verbose=args.verbose,
        )


if __name__ == '__main__':
    main()
