# GameHub

A universal game launcher for Windows. Put every game in one Steam-style library, whatever it was bought from or however it starts, and launch it from there.

## Features

- **Add anything:** `.exe`, `.bat`, shortcuts (`.lnk`/`.url`), store links like `steam://rungameid/…` or `com.epicgames.launcher://…`, or any file Windows can open (e.g. a ROM, with an emulator set up in launch arguments).
- **Import installed games:** scans your Steam libraries (on every drive) and Epic Games, with cover and banner art pulled in for Steam games.
- **Hours played:** Steam games show Steam's own play time. Every other game is timed by GameHub while it runs, including games whose launcher hands off to another program.
- **Steam-style library:** cover grid, "Recently played" shelf, game pages with a big PLAY button, favourites, categories, notes, search, and sorting by name, most played or recently played.
- **Launchers section:** Lunar Client, Epic Games, Steam, Badlion, Battle.net, CurseForge, Plutonium, the Minecraft Launcher, Xbox, EA app, Ubisoft Connect, Riot, GOG, Rockstar, Modrinth, Prism and HoYoPlay are found automatically and kept apart from your games. One click opens a launcher the way Explorer does (an open one comes to the front), and launchers aren't counted towards hours played. Right-click any tile, then **Move to Launchers**, to add your own.
- **Minecraft versions through Lunar Client:** pick any version Lunar offers (Lunar, Vanilla or Badlion, with Fabric, Forge, OptiFine and so on) from the game page, and Lunar opens with it selected.
- **Switch mode:** `pythonw gamehub.py --switch` opens a fullscreen, controller-friendly view, handy when streaming the PC to a TV or handheld.
- **Sharp icons:** 256px icons pulled from each exe (and Store app logos), and a proper app icon on the GameHub window.
- **Custom art:** set your own cover (portrait) and banner (wide) images per game.
- **No installs:** pure Python standard library; the UI runs in a Microsoft Edge app window.

## Requirements

- Windows 10/11
- [Python 3.10+](https://www.python.org/downloads/) (tkinter, included with the standard installer, is used for file pickers)
- Microsoft Edge (preinstalled on Windows; falls back to your default browser)

## Run it

```powershell
pythonw gamehub.py
```

Or make a shortcut to `pythonw.exe` with `gamehub.py` as the argument and `gamehub.ico` as the icon.

GameHub runs a small server on `127.0.0.1:8766` (local only, with a per-session token) and opens its window. The server shuts itself down shortly after you close the window, unless it's still timing a game.

## Data

Everything is stored in `data/` next to `gamehub.py`:

| File | What |
|---|---|
| `library.json` | your games, play time and settings per game |
| `covers/`, `icons/` | copied cover art and extracted exe icons |
| `settings.json` | optional; see below |

### Choosing which Steam accounts count

By default, hours from every Steam account on the PC are added together. To count only some accounts, create `data/settings.json`:

```json
{ "steamAccounts": ["123456789"] }
```

The IDs are the folder names in `C:\Program Files (x86)\Steam\userdata\`.

## License

MIT
