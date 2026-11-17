# flac2aac.sh

Converts lossless audio to Apple AAC (`.m4a`) in place, several files at a time.
The goal is the best possible quality at AAC VBR q1. The source file is deleted
only after the new file passes a length check.

## Requirements

- macOS
- Homebrew `ffmpeg` and `ffprobe` with the `aac_at` encoder (Apple AudioToolbox):
  `brew install ffmpeg`. The script refuses to run without `aac_at`.
- Optional: GNU `parallel` (`brew install parallel`). Without it the script
  uses `xargs`.

## Usage

```bash
./flac2aac.sh [threads] <folder> [folder...]
./flac2aac.sh -j <threads> <folder> [folder...]
```

- `threads` defaults to 4. A bare number counts as the thread count only when
  no folder with that name exists.
- Several folders in one run are fine, and so is `8 *` from the share root.
  Non-folder arguments are skipped, not fatal.

Examples:

```bash
./flac2aac.sh 8 "/path/Artist/[1977] Album"
./flac2aac.sh -j 10 /path/Artist/*/
TARGET_RATE=44100 ./flac2aac.sh 8 "/path/Artist/[1981] Album"
```

## Settings

| Variable | Default | Values |
|---|---|---|
| `VBR_QUALITY` | `1` | `0` (maximum) to `14`. `1` is Apple quality 118. |
| `TARGET_RATE` | `auto` | `auto`, `44100` or `48000` |
| `VERIFY_TOLERANCE` | `0.10` | Allowed length difference in seconds |

## What it converts

The script scans these extensions: `flac m4a alac wav wave aiff aif aifc ape wv
tta tak`. It decides what to do from the real codec of the first audio stream,
not from the extension:

- Lossless (FLAC, ALAC, PCM, APE, WavPack, TTA, TAK, MLP, TrueHD, ALS,
  Shorten): converts to AAC.
- AAC: skips silently.
- Other lossy (MP3, Vorbis, Opus, AC-3 and so on): skips, because re-encoding
  lossy audio only loses more quality.

These folders are never entered, at any depth and in any letter case:
`incomplete`, `_ToDelete`, `flac-convert`, `@Recycle`.

## How each file is processed

1. **Sample rate.** aac_at accepts 44.1 and 48 kHz only. With `TARGET_RATE=auto`:
   - 44.1, 88.2, 176.4 and 352.8 kHz become 44.1 kHz.
   - 48, 96, 192 and 384 kHz become 48 kHz.
   - Any other rate goes to whichever of the two is nearer. An unreadable
     rate gets 44.1 kHz.

   A file already at its target rate is not resampled. `TARGET_RATE=44100` or
   `48000` sets one rate for every file.
2. **Headroom.** Downsampling a master that peaks at full scale can push
   samples above it, and those samples would clip. For every resampled file,
   the script first measures the peak after the same resample. If the peak is
   above −0.1 dBFS, it lowers the level by exactly that excess. Files that are
   not resampled keep their original level.
3. **Bit depth.** aac_at accepts 16-bit input only, so 24-bit sources go to
   16-bit with triangular dither. 16-bit sources at their native rate pass
   through unchanged.
4. **Encode.** `aac_at`, true VBR, `-q:a 1` by default. Tags and a JPEG, PNG
   or BMP cover come along into the `.m4a`. The output goes to a hidden
   temporary file, `.<name>.aac-tmp.m4a`.
5. **Length check.** The new file must be within `VERIFY_TOLERANCE` seconds of
   the length the source declares. A damaged or truncated source fails here
   and stays on disk.
6. **Replace.** A `.m4a` source is replaced in place. Any other source gets a
   new `.m4a` next to it, and the source is deleted. If a `.m4a` with the same
   name already exists, the file is skipped before encoding.
7. **Playlists.** After all files finish, `.m3u` and `.m3u8` entries that point
   at a deleted source now point at the new `.m4a`. CRLF line endings are
   kept.

## Output

Each file prints one of these lines:

- `[START] flac → AAC: <name> (VBR q1, 96 kHz → 48 kHz)`
- `[~] Lowering by 0.59 dB so the resample does not clip: <name>`
- `[✓] Conversion successful, deleting: <file>` or `... replacing in place: <file>`
- `[✗] Conversion error (rc=…, keeping original): <file>`, followed by ffmpeg's errors
- `[✗] Length mismatch, keeping original: <file>`, with both lengths
- `[!] Target already exists, skipping: <file>`

At the end comes a summary. `conversions` and `over 320 kbps` always appear;
the other lines appear only when the count is non-zero: `headroom adjustments`,
`playlist entry fixes`, the `skip:` lines, `length check FAILURES` and
`failures`.

## Limits

- A cover that MP4 can't hold (GIF, WebP) makes the encode fail, and the source
  stays on disk.
- When a file is lowered for headroom, any ReplayGain tags copy over unchanged,
  so they are off by that amount (about a dB at most in practice).
- Each resampled file takes one extra decode pass for the peak measurement.
- An album with mixed source rates can end up with mixed 44.1 and 48 kHz files.
- Ctrl-C stops the run, and any in-flight `.aac-tmp.m4a` files stay on disk.
