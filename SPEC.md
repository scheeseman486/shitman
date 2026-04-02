# BigPImage (.bigpimg) Format Specification

Reverse-engineered from BigPEmu v1.21 (Linux x86-64) by analysis of encoded output
and runtime instrumentation of the encoder binary.

## Overview

BigPImage is a compressed CD image container used by BigPEmu, an Atari Jaguar emulator.
It stores multi-session CD layouts with per-sector DEFLATE compression and an optional
deduplication dictionary.

All multi-byte integers are little-endian. Compression uses raw DEFLATE (RFC 1951) with
no zlib or gzip wrapper.

The file is laid out sequentially:

    [Header] [Session Table] [Track Table] [Sector Index] [Dictionary?] [Compressed Sectors]


## 1. File Header (80 bytes)

    Offset  Size  Field                 Description
    ------  ----  --------------------  ------------------------------------------------
    0x00    8     Magic                 35 71 17 D7 7C F4 19 58
    0x08    u32   Version               1
    0x0C    u32   Session count         Number of CD sessions (typically 2 for Jaguar CD)
    0x10    u32   Track count           Total tracks across all sessions
    0x14    u32   TOC offset            Byte offset to session table (always 0x50)
    0x18    u32   Index offset          Byte offset to the sector index table
    0x1C    u32   Sub-version           Always 1
    0x20    u32   Sector size           0x0930 (2352) normal, 0x0990 (2448) with subchannel
    0x24    u32   Flags                 2 = normal, 3 = preserve subchannel data
    0x28    u32   Total sectors         Full disc extent (pregap + data + gaps + lead-out)
    0x2C    u32   Reserved              0
    0x30    8     Disc ID               FNV-1a 64-bit hash (see section 7)
    0x38    u32   Dict entry count      Number of dictionary sectors (0 if no pre-pass)
    0x3C    u32   Dict compressed size  Byte length of compressed dictionary blob (0 if none)
    0x40    16    Reserved              Zeros


## 2. Session Table

Starts at the TOC offset (0x50). Each entry is 16 bytes.

    Offset  Size  Field        Description
    ------  ----  -----------  -----------------------------------------------
    +0x00   u32   First track  1-based track number
    +0x04   u32   Last track   1-based track number (inclusive)
    +0x08   u32   End LBA      One past the last occupied LBA of this session
    +0x0C   u32   Reserved     0


## 3. Track Table

Immediately follows the session table. Each entry is 28 bytes.

    Offset  Size  Field          Description
    ------  ----  -------------  -----------------------------------------------
    +0x00   u32   Session index  0-based session this track belongs to
    +0x04   u32   Sector size    Per-track sector size (see below)
    +0x08   u32   Start LBA      Absolute LBA at the INDEX 01 position
    +0x0C   16    Reserved       Zeros

The per-track sector size is 0x0930 (2352) in normal mode. In subchannel mode the value
is 0x00600930, encoding both the main sector size (low 16 bits = 2352) and the subchannel
size (high 16 bits = 96).


## 4. Sector Index Table

Starts at the index offset. One u16 entry per sector for the full disc extent (LBA 0
through total_sectors - 1).

    Value               Meaning
    ------------------  -----------------------------------------------------------
    0xFFFF              Zero-filled gap. Not stored; reader emits sector_size zeros.
    Bit 15 set,         Dictionary reference. Bits 14-7 encode the 0-based dictionary
    not 0xFFFF          sector index. Bits 6-0 are reserved (zero).
    Other               Compressed size in bytes of the sector's raw DEFLATE payload
                        in the data stream. 0 means stored uncompressed at full
                        sector_size.


## 5. Compression Dictionary (optional)

Present only when dict_entry_count > 0 in the header. Immediately follows the sector
index table.

The dictionary is a single raw DEFLATE stream of dict_compressed_size bytes. It
decompresses to dict_entry_count * sector_size bytes: a flat concatenation of template
sectors.

Template sectors are sectors whose content appears three or more times across the disc.
All duplicate instances reference the dictionary entry via the sector index instead of
storing their own compressed copy. The maximum dictionary index is 255 (8 bits in the
index entry, bits 14-7).


## 6. Compressed Sector Data

Follows the dictionary (or the sector index if no dictionary). Contains concatenated raw
DEFLATE streams in LBA order, one per stored sector. Each decompresses to exactly
sector_size bytes.

Sectors whose index entry is 0xFFFF (gap/zero) or has bit 15 set (dictionary reference)
are not present in this region.

Sectors whose index entry is 0 are stored uncompressed at sector_size bytes.


## 7. Disc ID (FNV-1a 64-bit Hash)

The 8-byte disc ID at header offset 0x30 uniquely identifies the source disc content.
It is invariant across different encoder options (subchannel, dictionary, compression
level). The value is an FNV-1a 64-bit hash (offset basis 0xCBF29CE484222325, prime
0x100000001B3) computed over a byte sequence constructed as follows.

### 7.1 Input Construction

The hash input is the concatenation of three parts:

    [Trimmed sector data] [Session metadata] [Track metadata]

**Sector data.** Read the full BIN file of the first track of the last session. Apply
the trimming algorithm (section 7.2) to remove leading and trailing zero bytes. The
result is the trimmed sector data.

**Session metadata.** For each session in order, append 12 bytes:

    Offset  Size  Field
    ------  ----  -----------
    +0x00   u32   First track (1-based)
    +0x04   u32   Last track  (1-based)
    +0x08   u32   End LBA

**Track metadata.** For each track in order, append 20 bytes:

    Offset  Size  Field
    ------  ----  ---------------
    +0x00   u32   Start LBA (at INDEX 01)
    +0x04   u32   Track number (1-based)
    +0x08   u32   Sector size (always 2352, regardless of subchannel mode)
    +0x0C   u32   Session index (0-based)
    +0x10   u32   Flag (always 1)

Note that the session and track metadata layouts here differ from the on-disk table
formats (section 2 and 3): different field order, no padding, and the track metadata
includes the track number and a flag field not present in the track table.

### 7.2 Trimming Algorithm

Given the raw byte content of the target track's BIN file (length = total):

1. Forward scan: find the offset of the first non-zero byte, scanning from offset 0 up
   to offset total - 0x400. If the entire scanned range is zero, fwd = total - 0x400.
   If total <= 0x400, fwd = 0.

2. Backward scan: find the offset of the last non-zero byte, scanning backward from
   offset total - 1 down to max(0x800, fwd). If the entire scanned range is zero,
   bwd = max(0x800, fwd).

3. The trimmed data is bytes[fwd .. bwd] inclusive.


## 8. Disc Geometry

### Pregap

Track 1 of session 1 starts at LBA 150 (the standard 2-second CD pregap at 75
sectors/second). LBAs 0-149 are gap sectors.

### Track Start LBAs

For multi-file BIN/CUE images, each track's BIN file occupies a contiguous range of
LBAs. The track's start_lba is at the INDEX 01 position within that range. If the BIN
file contains pregap data (INDEX 00 at the start, INDEX 01 at some offset), the pregap
sectors occupy LBAs before the start_lba but are still part of the disc extent.

Within a session, tracks are laid out consecutively: each track's file data begins
immediately after the previous track's file data ends.

### Inter-Session Gap

Between sessions there is a fixed gap of 11400 sectors:

    6750  session lead-out (1.5 minutes)
    4500  next session lead-in (1 minute)
     150  next session pregap (2 seconds)

### Lead-Out

After the last session's data, there are 2250 sectors of lead-out (30 seconds).
Total sectors = last session end LBA + 2250.

### Session End LBA

A session's end LBA is one past the last data-bearing LBA of its last track. For a
track whose BIN file starts at disc position P and contains N sectors, the end LBA is
P + N (regardless of the INDEX 01 offset within the file).


## 9. Subchannel Data

When flags = 3 and sector_size = 2448, each sector contains 2352 bytes of main channel
data followed by 96 bytes of subchannel data.

Gap sectors (pregap, inter-session, lead-out) that would normally be 0xFFFF entries are
instead stored as compressed 2448-byte sectors containing zero main data and a fill
pattern in the subchannel region. The observed gap subchannel fill is 96 bytes of 0x80.

Data sectors that are all-zero in main content still carry a subchannel pattern of
repeating 12-byte units: 00 00 00 00 00 00 40 00 00 00 00 00 (repeated 8 times for
96 bytes).
