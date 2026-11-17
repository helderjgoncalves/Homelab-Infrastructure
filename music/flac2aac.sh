#!/bin/bash

# flac2aac.sh
# Usage: ./flac2aac.sh [threads] <folder> [folder...]
#        ./flac2aac.sh -j <threads> <folder> [folder...]
#        ./flac2aac.sh 8 *          # from the share root is OK
# Converts LOSSLESS audio to high-quality AAC (.m4a) in parallel, IN-PLACE.
#
# Handles FLAC, ALAC (.m4a), WAV/AIFF (PCM), APE, WavPack, TTA, TAK.
# Every candidate is probed with ffprobe and dispatched on its real codec, not
# its extension:
#   - lossless codec  -> encode to AAC
#   - already AAC     -> skip silently
#   - other lossy     -> skip (transcoding lossy->lossy only loses quality)
# Originals are replaced/deleted only after a successful encode AND a length
# check against the source; metadata and cover art are preserved.
# Default: 4 threads.
#
# Encoder: ffmpeg + aac_at (Apple AudioToolbox - best-in-class AAC, macOS only).
#          True VBR at quality 1: -q:a 1, Apple quality 118.
#          One step below the encoder maximum. Typical output sits between
#          the 256 and 320 kbps steps; dense tracks run higher.
#          No fallback: this script requires aac_at and will refuse to run
#          without it (use brew's ffmpeg on macOS).
#
# Sample rate follows the source family, so the output is 44.1 or 48 kHz:
#   44.1, 88.2, 176.4, ...  -> 44.1 kHz
#   48, 96, 192, ...        -> 48 kHz
# A file already at its target rate is not resampled. The Apple encoder
# accepts 44.1 and 48 and refuses 88.2 and above, so hi-res has to come down.
# A resampled file that would clip is lowered just enough to stay below
# full scale; files that are not resampled keep their level.
# TARGET_RATE=44100 or TARGET_RATE=48000 forces one rate for the whole run.

set -uo pipefail

# Suffix used for in-flight encodes. Leading dot keeps partials hidden from
# music-library scanners; the distinct suffix can't case-collide with the
# original on a case-insensitive SMB share.
TMP_SUFFIX=".aac-tmp.m4a"

# Check for ffmpeg / ffprobe
if ! command -v ffmpeg &> /dev/null; then
    echo "Error: ffmpeg is not installed. Install with: brew install ffmpeg" >&2
    exit 1
fi
if ! command -v ffprobe &> /dev/null; then
    echo "Error: ffprobe is not installed (it ships with ffmpeg)." >&2
    exit 1
fi

# Require aac_at (Apple AudioToolbox AAC) — no fallback.
if ! ffmpeg -hide_banner -encoders 2>/dev/null | grep -qw 'aac_at'; then
    echo "Error: this ffmpeg build does not include 'aac_at' (Apple AudioToolbox AAC)." >&2
    echo "       aac_at is macOS-only and is included in Homebrew's ffmpeg by default." >&2
    echo "       Reinstall with: brew reinstall ffmpeg" >&2
    exit 1
fi

usage() {
    cat >&2 <<EOF
Usage: $0 [threads] <folder> [folder...]
       $0 -j <threads> <folder> [folder...]

  threads   positive integer, default 4. As a bare first argument it is only
            treated as a thread count when it is not also an existing directory.

Examples:
  $0 8 "/path/Artist/[1977] Album"
  $0 8 "/path/Artist/[1977] Album A" "/path/Artist/[1979] Album B"
  $0 "/path/Artist/[1977] Album"          # default 4 threads
  $0 -j 10 /path/Artist/*/                # whole discography
  $0 8 *                                  # every folder here; skip-list is pruned

Work directories (skipped as arguments and pruned at any depth):
  incomplete  _ToDelete  flac-convert  @Recycle
Non-directory glob hits (AGENTS.md, etc.) are skipped, not fatal.
EOF
    exit 1
}

# Directory basenames the converter must never enter. The match is
# case-insensitive for arguments, and find prunes them at any depth.
SKIP_DIR_NAMES="incomplete _todelete flac-convert @recycle"

skip_dir() {
    local p="${1%/}"
    local part lower
    while [ -n "$p" ] && [ "$p" != "/" ] && [ "$p" != "." ]; do
        part=$(basename "$p")
        lower=$(printf '%s' "$part" | tr '[:upper:]' '[:lower:]')
        for skip in $SKIP_DIR_NAMES; do
            if [ "$lower" = "$skip" ]; then
                return 0
            fi
        done
        p=$(dirname "$p")
    done
    return 1
}

THREADS=4

# Optional -j/--jobs/--threads flag.
case "${1:-}" in
    -j|--jobs|--threads)
        [ $# -ge 2 ] || usage
        THREADS="$2"
        shift 2
        ;;
    -h|--help)
        usage
        ;;
    *)
        # A bare leading integer is a thread count — but only if it isn't also a
        # real directory, so a folder literally named "8" still works.
        if [ $# -ge 2 ] && [[ "${1:-}" =~ ^[0-9]+$ ]] && [ ! -d "${1:-}" ]; then
            THREADS="$1"
            shift
        fi
        ;;
esac

if ! [[ "$THREADS" =~ ^[0-9]+$ ]] || [ "$THREADS" -lt 1 ]; then
    echo "Error: threads must be a positive integer (got '$THREADS')" >&2
    exit 1
fi

[ $# -ge 1 ] || usage

# Validate folders up front — refuse to convert half a batch and then die.
# A share-root glob (`8 *`) matches skip-list dirs and files; those are skipped
# rather than aborting the whole run.
FOLDERS=()
bad=0
skipped=0
for arg in "$@"; do
    if [ ! -e "$arg" ]; then
        echo "Error: not a directory: $arg" >&2
        bad=1
    elif [ ! -d "$arg" ]; then
        echo "Skipping non-directory: $arg"
        skipped=$((skipped + 1))
    elif skip_dir "$arg"; then
        echo "Skipping off-limits folder: $arg"
        skipped=$((skipped + 1))
    else
        FOLDERS+=("$arg")
    fi
done
[ "$bad" -eq 0 ] || exit 1
if [ "${#FOLDERS[@]}" -eq 0 ]; then
    echo "Error: no folders to convert (everything is skipped or missing)." >&2
    exit 1
fi

# aac_at true-VBR quality. ffmpeg's scale is inverted: 0 is maximum (Apple
# quality 127) and 14 is minimum. Default 1 is Apple quality 118, one step
# below the top. There is no ceiling on dense material.
VBR_QUALITY="${VBR_QUALITY:-1}"
if ! [[ "$VBR_QUALITY" =~ ^[0-9]+$ ]] || [ "$VBR_QUALITY" -gt 14 ]; then
    echo "Error: VBR_QUALITY must be an integer from 0 (maximum) to 14 (got '$VBR_QUALITY')" >&2
    exit 1
fi

# Output sample rate. auto picks 44.1 or 48 from the source family.
# 44100 or 48000 forces that rate for every file.
TARGET_RATE="${TARGET_RATE:-auto}"
case "$TARGET_RATE" in
    auto|44100|48000) ;;
    *)
        echo "Error: TARGET_RATE must be auto, 44100, or 48000 (got '$TARGET_RATE')" >&2
        exit 1
        ;;
esac

# Max allowed difference, in seconds, between the source's advertised length and
# the encoded output's length. AAC padding accounts for ~4-25 ms in practice, so
# 0.10 leaves generous margin while still catching a truncated decode.
VERIFY_TOLERANCE="${VERIFY_TOLERANCE:-0.10}"

export VBR_QUALITY TARGET_RATE TMP_SUFFIX VERIFY_TOLERANCE

# Per-worker tallies land here as append-only marker files (small appends to
# O_APPEND fds are atomic, so parallel workers can't clobber each other).
STATS_DIR=$(mktemp -d "${TMPDIR:-/tmp}/to-aac.XXXXXX") || exit 1
export STATS_DIR

cleanup_stats() { [ -n "${STATS_DIR:-}" ] && rm -rf "$STATS_DIR"; }
trap 'echo ""; echo "[!] Interrupt - exiting (any in-flight *'"$TMP_SUFFIX"' partials stay on disk)"; cleanup_stats; exit 130' INT TERM

# Extensions worth probing. Anything not in this list is never opened.
EXTENSIONS="flac m4a alac wav wave aiff aif aifc ape wv tta tak"

if [ "${#FOLDERS[@]}" -eq 1 ]; then
    echo "Converting lossless audio in '${FOLDERS[0]}' to AAC using $THREADS parallel threads..."
else
    echo "Converting lossless audio to AAC using $THREADS parallel threads, in ${#FOLDERS[@]} folders."
    if [ "${#FOLDERS[@]}" -le 20 ]; then
        for f in "${FOLDERS[@]}"; do echo "  - $f"; done
    fi
fi
[ "$skipped" -gt 0 ] && echo "Skipping $skipped off-limits or non-directory argument(s)."
echo "Encoder: ffmpeg + 'aac_at' (Apple AudioToolbox AAC) — true VBR quality ${VBR_QUALITY} (0 = maximum)"
case "$TARGET_RATE" in
    auto) echo "Sample rate: 44.1 family → 44.1 kHz, 48 family → 48 kHz" ;;
    *)    echo "Sample rate: ${TARGET_RATE} Hz for every file" ;;
esac

tally() {
    printf '.' >> "$STATS_DIR/$1"
}

# Print captured ffmpeg stderr, indented, only when a file fails.
# ffmpeg's chatter is otherwise discarded: EAC rips carry a trailing ID3v1 tag,
# which makes the FLAC decoder log 'invalid sync code' / 'decode_frame() failed'
# at EOF for every single file. Harmless, but it buries real failures, so noise
# is only surfaced for files that don't verify.
dump_err() {
    [ -s "$1" ] && sed 's/^/      | /' "$1"
    return 0
}

# AAC rate for one source rate. Multiples of 44.1 land on 44.1, multiples of
# 48 land on 48, and anything else goes to whichever of the two is nearer.
# An unreadable rate gets 44.1.
choose_rate() {
    local rate="$1"
    case "$TARGET_RATE" in
        44100|48000)
            printf '%s\n' "$TARGET_RATE"
            return
            ;;
    esac
    if ! [[ "$rate" =~ ^[0-9]+$ ]] || [ "$rate" -lt 1 ]; then
        printf '44100\n'
        return
    fi
    if [ $((rate % 44100)) -eq 0 ]; then
        printf '44100\n'
    elif [ $((rate % 48000)) -eq 0 ]; then
        printf '48000\n'
    else
        local d44 d48
        d44=$(( rate > 44100 ? rate - 44100 : 44100 - rate ))
        d48=$(( rate > 48000 ? rate - 48000 : 48000 - rate ))
        if [ "$d44" -le "$d48" ]; then
            printf '44100\n'
        else
            printf '48000\n'
        fi
    fi
}

rate_label() {
    case "$1" in
        44100)  printf '44.1 kHz\n' ;;
        48000)  printf '48 kHz\n' ;;
        88200)  printf '88.2 kHz\n' ;;
        96000)  printf '96 kHz\n' ;;
        176400) printf '176.4 kHz\n' ;;
        192000) printf '192 kHz\n' ;;
        352800) printf '352.8 kHz\n' ;;
        384000) printf '384 kHz\n' ;;
        ""|0|*[!0-9]*) printf 'unknown rate\n' ;;
        *)      printf '%s Hz\n' "$1" ;;
    esac
}

# Function to convert a single file
convert_file() {
    FILE="$1"

    # Never re-process our own in-flight output.
    case "$FILE" in
        *"$TMP_SUFFIX") return 0 ;;
    esac

    # Never convert inside off-limits trees if find's prune misses one.
    case "$FILE" in
        */incomplete/*|*/_ToDelete/*|*/flac-convert/*|*/@Recycle/*)
            return 0
            ;;
    esac

    # Dispatch on the real codec of the first audio stream, not the extension.
    local codec
    codec=$(ffprobe -v error -select_streams a:0 -show_entries stream=codec_name \
        -of default=nw=1:nk=1 "$FILE" 2>/dev/null)

    case "$codec" in
        flac|alac|pcm_*|ape|wavpack|tta|tak|mlp|truehd|als|shorten)
            : # lossless — convert
            ;;
        aac|aac_latm)
            tally already_aac
            return 0
            ;;
        "")
            tally unreadable
            return 0
            ;;
        *)
            # Lossy source (mp3, vorbis, opus, ac3, ...). Re-encoding would only
            # throw away more quality, so leave it alone.
            tally lossy_skipped
            return 0
            ;;
    esac

    local dir base ext lower_ext tmp_output target
    dir=$(dirname "$FILE")
    base=$(basename "$FILE")
    ext="${base##*.}"
    lower_ext=$(printf '%s' "$ext" | tr '[:upper:]' '[:lower:]')
    base="${base%.*}"

    tmp_output="$dir/.${base}${TMP_SUFFIX}"
    # .m4a inputs are replaced in place; everything else lands on a new .m4a.
    target="$FILE"
    if [ "$lower_ext" != "m4a" ]; then
        target="$dir/$base.m4a"
        # Refuse to clobber an existing .m4a (e.g. a sibling ALAC/AAC of the
        # same track). The check runs before encoding so no CPU goes to waste.
        if [ -e "$target" ]; then
            echo "[!] Target already exists, skipping: $target"
            tally collision
            return 1
        fi
    fi

    # The length the source claims to be. A truncated file still advertises its
    # full length in the header, which is precisely what makes this a useful
    # reference: a short decode shows up as a mismatch against it.
    local src_dur
    src_dur=$(ffprobe -v error -show_entries format=duration \
        -of default=nw=1:nk=1 "$FILE" 2>/dev/null)

    local src_rate out_rate rate_note
    src_rate=$(ffprobe -v error -select_streams a:0 -show_entries stream=sample_rate \
        -of default=nw=1:nk=1 "$FILE" 2>/dev/null)
    out_rate=$(choose_rate "$src_rate")
    if [ "$src_rate" = "$out_rate" ]; then
        rate_note=$(rate_label "$out_rate")
    else
        rate_note="$(rate_label "$src_rate") → $(rate_label "$out_rate")"
    fi
    echo "[START] ${codec} → AAC: $base (VBR q${VBR_QUALITY}, ${rate_note})"

    [ -f "$tmp_output" ] && rm -f "$tmp_output"

    # aac_at only accepts s16/u8 sample formats, so 24-bit sources are reduced
    # to s16 with triangular dither (ffmpeg's default truncates). -ar is
    # always passed: it is a no-op at the source rate, and it also brings any
    # extra audio streams to a rate aac_at accepts.
    local rate_args=(-ar "$out_rate")

    # Downsampling a master that peaks at full scale can push samples above it,
    # and the s16 step would clip them. Measure the peak of the same resample
    # first, and lower the level only by what exceeds -0.1 dBFS.
    local filter_args=() resampled=0 r
    for r in $(ffprobe -v error -select_streams a -show_entries stream=sample_rate \
        -of default=nw=1:nk=1 "$FILE" 2>/dev/null); do
        [ "$r" != "$out_rate" ] && resampled=1
    done
    if [ "$resampled" -eq 1 ]; then
        local chain peak gain
        chain="aformat=sample_fmts=flt,aresample=${out_rate},aformat=sample_fmts=flt"
        # Only astats' own log line counts; tags print at the same log level.
        peak=$(ffmpeg -nostdin -hide_banner -v info -i "$FILE" -map 0:a \
            -af "${chain},astats=measure_perchannel=none:measure_overall=Peak_level" \
            -f null - 2>&1 \
            | LC_ALL=C awk '/\[Parsed_astats_[0-9]+ @ [^]]*\] Peak level dB:/ && $NF ~ /^-?[0-9.]+$/ {
                                if (p == "" || $NF+0 > p+0) p = $NF }
                            END { print p }')
        # Round down to the next 0.01 dB so the result never exceeds -0.1 dBFS.
        gain=$(LC_ALL=C awk -v p="$peak" -v c="-0.1" \
            'BEGIN { if (p != "" && p+0 > c+0) {
                         g = (c - p) * 100; f = int(g); if (f > g) f--
                         printf "%.2f", f / 100 } }')
        if [ -n "$gain" ]; then
            echo "[~] Lowering by ${gain#-} dB so the resample does not clip: $base"
            filter_args=(-af "${chain},volume=${gain}dB")
            tally headroom
        fi
    fi

    # stderr is captured rather than shown — see dump_err above.
    local errfile="$STATS_DIR/ffmpeg.err.$$"

    # Apple AudioToolbox AAC (aac_at) — true VBR. Default -q:a 1 is Apple quality 118.
    # Map audio + pass cover-art through as attached_pic.
    ffmpeg -hide_banner -loglevel error -i "$FILE" \
        -map 0:a -c:a aac_at -aac_at_mode vbr -q:a "${VBR_QUALITY}" \
        ${filter_args[@]+"${filter_args[@]}"} \
        "${rate_args[@]}" -sample_fmt s16 -dither_method triangular \
        -map 0:v? -c:v copy -disposition:v attached_pic \
        -map_metadata 0 -movflags +faststart \
        -y "$tmp_output" 2>"$errfile"
    local rc=$?

    if [ "$rc" -ne 0 ] || [ ! -s "$tmp_output" ]; then
        echo "[✗] Conversion error (rc=$rc, keeping original): $FILE"
        dump_err "$errfile"
        rm -f "$tmp_output" "$errfile"
        tally failed
        return 1
    fi

    # ffmpeg exits 0 on a partially-decodable input, so rc alone is not enough
    # to justify deleting the original. Require the encode to account for the
    # whole source before anything irreversible happens.
    local out_dur
    out_dur=$(ffprobe -v error -show_entries format=duration \
        -of default=nw=1:nk=1 "$tmp_output" 2>/dev/null)
    if ! LC_ALL=C awk -v a="$src_dur" -v b="$out_dur" -v t="$VERIFY_TOLERANCE" \
        'BEGIN { if (a+0 <= 0 || b+0 <= 0) exit 1
                 d = b - a; if (d < 0) d = -d
                 exit !(d <= t+0) }'; then
        echo "[✗] Length mismatch, keeping original: $FILE"
        echo "      source=${src_dur:-?}s encoded=${out_dur:-?}s tolerance=${VERIFY_TOLERANCE}s"
        dump_err "$errfile"
        rm -f "$tmp_output" "$errfile"
        tally unverified
        return 1
    fi

    rm -f "$errfile"

    if mv -f "$tmp_output" "$target"; then
        if [ "$target" = "$FILE" ]; then
            echo "[✓] Conversion successful, replacing in place: $FILE"
        else
            echo "[✓] Conversion successful, deleting: $FILE"
            rm -f "$FILE"
        fi
        tally ok
        # Average audio bitrate. VBR streams sometimes omit stream bit_rate;
        # the container rate is the fallback. Count encodes above 320 kbps.
        local br
        br=$(ffprobe -v error -select_streams a:0 -show_entries stream=bit_rate \
            -of default=nw=1:nk=1 "$target" 2>/dev/null)
        case "$br" in
            ""|N/A)
                br=$(ffprobe -v error -show_entries format=bit_rate \
                    -of default=nw=1:nk=1 "$target" 2>/dev/null)
                ;;
        esac
        if [[ "$br" =~ ^[0-9]+$ ]] && [ "$br" -gt 320000 ]; then
            tally over_320
        fi
        return 0
    fi

    echo "[✗] Rename error, keeping original: $FILE"
    rm -f "$tmp_output"
    tally failed
    return 1
}

# Repoint playlist entries at the new .m4a files. Runs as a post-pass
# (not per-file) so it sees the final state on disk, and only rewrites a line
# when the old target is genuinely gone and the .m4a is genuinely there.
fix_playlists() {
    local folder="$1"
    local fixed_files=0 fixed_lines=0

    while IFS= read -r -d '' playlist; do
        local pdir tmp line cr ref ext lower_ext newref abs_old abs_new changed=0
        pdir=$(dirname "$playlist")
        tmp="$playlist.playlist-tmp"
        : > "$tmp" 2>/dev/null || continue

        while IFS= read -r line || [ -n "$line" ]; do
            # EAC and friends write CRLF; preserve whatever each line has.
            cr=""
            case "$line" in
                *$'\r') cr=$'\r'; line="${line%$'\r'}" ;;
            esac

            # Blank lines and #EXTM3U/#EXTINF directives pass through untouched.
            case "$line" in
                ""|"#"*)
                    printf '%s%s\n' "$line" "$cr" >> "$tmp"
                    continue
                    ;;
            esac

            ref="$line"
            ext="${ref##*.}"
            lower_ext=$(printf '%s' "$ext" | tr '[:upper:]' '[:lower:]')

            # Only extensions this script converts are candidates. .m4a entries
            # are replaced in place and still resolve, so leave them alone.
            if [ "$lower_ext" = "m4a" ] || ! printf '%s' " $EXTENSIONS " | grep -q " $lower_ext "; then
                printf '%s%s\n' "$line" "$cr" >> "$tmp"
                continue
            fi

            newref="${ref%.*}.m4a"
            case "$ref" in
                /*) abs_old="$ref"      ; abs_new="$newref" ;;
                *)  abs_old="$pdir/$ref"; abs_new="$pdir/$newref" ;;
            esac

            if [ ! -e "$abs_old" ] && [ -e "$abs_new" ]; then
                printf '%s%s\n' "$newref" "$cr" >> "$tmp"
                changed=$((changed + 1))
            else
                printf '%s%s\n' "$line" "$cr" >> "$tmp"
            fi
        done < "$playlist"

        if [ "$changed" -gt 0 ] && mv -f "$tmp" "$playlist"; then
            echo "[playlist] $changed entries now point at .m4a: $playlist"
            fixed_files=$((fixed_files + 1))
            fixed_lines=$((fixed_lines + changed))
            local i=0
            while [ "$i" -lt "$changed" ]; do tally playlist_entries; i=$((i + 1)); done
        else
            rm -f "$tmp"
        fi
    done < <(find "$folder" \
        \( -iname incomplete -o -iname '_ToDelete' -o -iname flac-convert \
           -o -iname '@Recycle' \) -prune \
        -o -type f \( -iname "*.m3u" -o -iname "*.m3u8" \) -print0)
}

# Export functions and variables so they're available to subshells
export -f convert_file tally dump_err choose_rate rate_label

# Build the find expression from EXTENSIONS: -iname "*.flac" -o -iname "*.m4a" ...
find_args=()
for ext in $EXTENSIONS; do
    [ ${#find_args[@]} -gt 0 ] && find_args+=(-o)
    find_args+=(-iname "*.$ext")
done

# One find across every folder, deduped. `sort -zu` matters when the folder list
# overlaps (e.g. a parent and its child are both passed): without it the same
# file would be handed to two workers at once and they'd race on the same output.
collect() {
    find "${FOLDERS[@]}" \
        \( -iname incomplete -o -iname '_ToDelete' -o -iname flac-convert \
           -o -iname '@Recycle' \) -prune \
        -o -type f \( "${find_args[@]}" \) -print0 2>/dev/null | sort -zu
}

# Check if GNU parallel is available
if command -v parallel &> /dev/null; then
    echo "Using GNU Parallel for processing..."
    # Force bash for workers so `export -f` is honored; -0 for NUL-safe filenames.
    # SHELL= must sit on `parallel`, not `collect`: in a bash pipeline an
    # assignment only applies to that one command, and zsh workers glob-fail
    # ffmpeg's optional `-map 0:v?`.
    collect \
        | SHELL="$(command -v bash)" parallel -0 -j "$THREADS" --env convert_file --env tally --env dump_err \
            --env choose_rate --env rate_label \
            --env VBR_QUALITY --env TARGET_RATE --env TMP_SUFFIX --env STATS_DIR \
            --env VERIFY_TOLERANCE \
            convert_file {}
else
    echo "Using xargs for processing (install GNU parallel for better progress tracking: brew install parallel)"
    # xargs spawns fresh bash workers that don't inherit exported functions reliably
    # on macOS, so re-declare the functions inside each worker.
    FUNC_DEF="$(declare -f convert_file tally dump_err choose_rate rate_label)"
    collect \
        | xargs -0 -n 1 -P "$THREADS" bash -c "$FUNC_DEF"$'\n''convert_file "$1"' _
fi

# Report a tally line only when it's non-zero, unless the line is marked always.
report() {
    local n=0
    [ -f "$STATS_DIR/$1" ] && n=$(wc -c < "$STATS_DIR/$1" | tr -d ' ')
    if [ "$n" -gt 0 ] || [ "${3:-}" = "always" ]; then
        printf '  %-22s: %s\n' "$2" "$n"
    fi
}

for f in "${FOLDERS[@]}"; do
    fix_playlists "$f"
done

echo ""
echo "Summary:"
report ok               "conversions"         always
report over_320         "over 320 kbps"       always
report headroom         "headroom adjustments"
report playlist_entries "playlist entry fixes"
report already_aac   "skip: already AAC"
report lossy_skipped "skip: lossy source"
report collision     "skip: .m4a exists"
report unreadable    "skip: unreadable"
report unverified    "length check FAILURES"
report failed        "failures"

cleanup_stats
trap - INT TERM

echo ""
echo "All done!"
