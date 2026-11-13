# Stash

Saves pictures and videos from Pinterest, Instagram and X/Twitter. Only public posts work, and nothing logs in. The design follows the Cairn design system.

- **Phone and web:** https://pin-video-saver-production.up.railway.app (code in `web/`, hosted on Railway)
- **Windows:** `dist/Stash.exe` (code in `Stash.pyw`)
- **Shared download engine:** `stash_core.py`, built on yt-dlp. X posts use X's public embed data.

## If downloads stop working

A site may have changed something. Getting the newest yt-dlp usually fixes it.

**Windows app:** double-click `build.bat`.

**Website:** run this from this folder:

```bash
& "$env:LOCALAPPDATA\PinterestDownloaderBuild\railway-cli\node_modules\.bin\railway.cmd" up --ci --service pin-video-saver
```
