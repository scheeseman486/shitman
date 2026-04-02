# SHITMAN

A slop coded Atari Jaguar CD disc image converter that converts bin/cue to BigPEmu's `.bigpimg` compressed format. BigPEmu doesn't support CHD so without a CHDMAN equivalent I needed a standalone converter, so here it is. Thanks SHITMAN!

## Requirements

Python 3.6+ (no external dependencies).

## Usage

```
shitman.py game.cue                          # Convert to game.bigpimg
shitman.py game.cue -o output.bigpimg        # Specify output path
shitman.py game.cue --subchannel             # Preserve subchannel data
shitman.py game.cue --prepass                # Enable deduplication dictionary
shitman.py game.cue --level 6               # Set compression level (1-9, default 9 and anything other than that isn't tested/supported)
shitman.py --batch /path/to/images/          # Convert all CUE files in a directory
```

### Verification

Compare your encoded output against the original source or a reference file:

```
shitman.py --verify game.bigpimg game.cue           # Verify against source BIN/CUE
shitman.py --compare ours.bigpimg reference.bigpimg  # Binary comparison of two files
```

Precompiled executables are provided for your convenience, so if you use those, take into account the name of the executable.

## Options

| Flag | Description |
|------|-------------|
| `-o, --output` | Output file path (default: input name with `.bigpimg` extension) |
| `--subchannel` | Include 96-byte subchannel data per sector (2448-byte sectors instead of 2352) |
| `--prepass` | Build a deduplication dictionary for sectors appearing 3+ times |
| `--level N` | DEFLATE compression level, 1–9 (default: 9) |
| `--batch` | Treat input as a directory and convert all `.cue` files found recursively |
| `-v, --verbose` | Print progress and statistics |
| `--verify` | Verify a `.bigpimg` decompresses correctly against its source CUE |
| `--compare` | Compare two `.bigpimg` files field-by-field |

## How It Works

SHITMAN parses the CUE sheet to determine disc geometry (sessions, tracks, pregaps, lead-outs), then compresses each sector individually using raw DEFLATE. The output file contains a header, session/track tables, a sector index, an optional deduplication dictionary, and the compressed sector payloads — all laid out per the BigPImage spec.

The `--prepass` option does a first pass over all sectors to identify content that appears three or more times, storing those as dictionary entries that are referenced by index rather than compressed repeatedly.

See `SPEC.md` for the full BigPImage format specification (reverse-engineered from BigPEmu). No guarantees on accuracy, but for what it's worth, the code in shitman.py results in 1:1 versions compared to the original executable (as far as I can tell through testing).

## License

GPLv2. See [LICENSE](LICENSE).
