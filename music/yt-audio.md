# yt-audio.sh

Saves the best audio-only stream from a YouTube link, or any other URL yt-dlp supports. Opus is copied to `.opus`. AAC stays `.m4a`. The audio is not re-encoded.

Files land in the current directory. `-o` chooses another folder. Tags come from the video: artist, title, album, date, and the cover. If YouTube names no album, the album is the song title. The filename is the video title.

## Requirements

- `yt-dlp` and `ffmpeg` on `PATH`
- Python 3. The first run creates `.venv` next to the script and installs `mutagen`.

## Usage

```bash
./yt-audio.sh "https://www.youtube.com/watch?v=..."
./yt-audio.sh -o ~/Music "https://youtu.be/..." "https://www.youtube.com/watch?v=..."
```
