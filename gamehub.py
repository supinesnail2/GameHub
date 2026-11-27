"""GameHub - a universal game launcher.

Runs a tiny local server (127.0.0.1 only) and shows the UI in an Edge app window.
Library data lives in ./data next to this file.
"""
import json, os, re, sys, time, uuid, shutil, socket, secrets, subprocess, threading, glob, winreg
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, unquote

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(APP_DIR, "data")
COVERS = os.path.join(DATA, "covers")
ICONS = os.path.join(DATA, "icons")
LIB = os.path.join(DATA, "library.json")
PORT = 8766
TOKEN = secrets.token_hex(16)
NOWIN = 0x08000000  # CREATE_NO_WINDOW

for d in (DATA, COVERS, ICONS):
    os.makedirs(d, exist_ok=True)

lock = threading.Lock()
running = {}          # game id -> start time
watching = set()      # game ids we're still waiting on to start
last_beat = [time.time()]


# ---------- library ----------
def load():
    try:
        with open(LIB, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"games": []}


def save(lib):
    tmp = LIB + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(lib, f, indent=2)
    os.replace(tmp, LIB)


def find(lib, gid):
    return next((g for g in lib["games"] if g["id"] == gid), None)


def is_uri(p):
    # xxx:// links (steam://, com.epicgames...) and shell: links (Store apps via shell:AppsFolder\<AppID>)
    return bool(re.match(r"^(?:[a-zA-Z][a-zA-Z0-9+.-]{1,}://|shell:)", p or ""))


# ---------- icons ----------
def icon_source(g):
    """Local file whose icon represents the game."""
    p = g.get("iconFrom") or g.get("path") or ""
    return p if p and not is_uri(p) and os.path.exists(p) else None


def icon_path(gid):
    return os.path.join(ICONS, gid + ".256.png")


def extract_icon(g):
    out = icon_path(g["id"])
    if os.path.exists(out):
        return out
    src = icon_source(g)
    if not src and (g.get("path") or "").lower().startswith("shell:appsfolder\\"):
        return store_app_icon(g["path"].split("\\", 1)[1], out)
    if not src:
        return None
    if src.lower().endswith(".lnk"):
        src = resolve_lnk(src) or src
    # the big (up to 256px) icon from the exe; falls back to the small associated icon
    ps = ("Add-Type -AssemblyName System.Drawing;"
          "Add-Type -Namespace W -Name I -MemberDefinition '[DllImport(\"user32.dll\",CharSet=CharSet.Unicode)]"
          "public static extern uint PrivateExtractIcons(string f,int i,int cx,int cy,IntPtr[] h,uint[] id,uint n,uint fl);';"
          "$h=New-Object IntPtr[] 1;$id=New-Object uint32[] 1;$i=$null;"
          "if([W.I]::PrivateExtractIcons($env:GH_SRC,0,256,256,$h,$id,1,0) -eq 1 -and $h[0] -ne [IntPtr]::Zero)"
          "{$i=[System.Drawing.Icon]::FromHandle($h[0])}"
          "if(-not $i){$i=[System.Drawing.Icon]::ExtractAssociatedIcon($env:GH_SRC)}"
          "if($i){$i.ToBitmap().Save($env:GH_OUT,[System.Drawing.Imaging.ImageFormat]::Png)}")
    env = dict(os.environ, GH_SRC=src, GH_OUT=out)
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", ps], env=env,
                       creationflags=NOWIN, timeout=15, capture_output=True)
    except Exception:
        pass
    return out if os.path.exists(out) else None


def store_app_icon(app_id, out):
    """Logo of a Microsoft Store app (shell:AppsFolder\\<PackageFamily>!<App>), biggest scale."""
    ps = r"""
$fam,$app=$env:GH_APP.Split('!');$p=Get-AppxPackage | ? PackageFamilyName -eq $fam | select -First 1
if(-not $p){exit};$m=[xml](Get-Content (Join-Path $p.InstallLocation 'AppxManifest.xml'))
$a=$m.Package.Applications.Application | ? Id -eq $app | select -First 1
$v=$a.ChildNodes | ? LocalName -eq 'VisualElements' | select -First 1
$logo=$v.Square150x150Logo;if(-not $logo){$logo=$v.Square44x44Logo};if(-not $logo){exit}
$base=Join-Path $p.InstallLocation $logo;$dir=Split-Path $base;$stem=[IO.Path]::GetFileNameWithoutExtension($base)
$f=Get-ChildItem $dir -Filter "$stem*" -ErrorAction SilentlyContinue | ? Name -notmatch 'contrast' | sort Length -Descending | select -First 1
if($f){Copy-Item $f.FullName $env:GH_OUT}"""
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", ps], env=dict(os.environ, GH_APP=app_id, GH_OUT=out),
                       creationflags=NOWIN, timeout=30, capture_output=True)
    except Exception:
        pass
    return out if os.path.exists(out) else None


# ---------- launching & play-time tracking ----------
WINDIR = os.environ.get("SystemRoot", r"C:\Windows").lower()


def steam_appid(g):
    m = (re.match(r"steam:(\d+)$", g.get("key") or "")
         or re.match(r"steam://(?:rungameid|run)/(\d+)", g.get("path") or "", re.I))
    return m.group(1) if m else None


def resolve_lnk(path):
    ps = "(New-Object -ComObject WScript.Shell).CreateShortcut($env:GH_SRC).TargetPath"
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], env=dict(os.environ, GH_SRC=path),
                           capture_output=True, text=True, creationflags=NOWIN, timeout=15)
        return r.stdout.strip()
    except Exception:
        return ""


def game_folder(g):
    """Folder whose processes count as 'the game is running'."""
    f = g.get("folder")
    if not f:
        p = g.get("path", "")
        if p.lower().endswith(".lnk"):
            p = resolve_lnk(p)
        if p and not is_uri(p) and os.path.isfile(p):
            f = os.path.dirname(p)
        elif g.get("iconFrom"):
            f = os.path.dirname(g["iconFrom"])
    if not f or not os.path.isdir(f):
        return None
    f = os.path.normcase(os.path.normpath(f))
    # never watch system or drive-root-level folders (too broad)
    if f.startswith(WINDIR) or "\\windowsapps" in f or len(f.rstrip("\\").split("\\")) < 3:
        return None
    return f


def procs_in(folder):
    ps = "Get-CimInstance Win32_Process -Property ExecutablePath | ForEach-Object { $_.ExecutablePath }"
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True,
                           creationflags=NOWIN, timeout=30)
    except Exception:
        return 0
    pre = folder.rstrip("\\") + "\\"
    return sum(1 for line in r.stdout.splitlines() if os.path.normcase(line.strip()).startswith(pre))


def add_playtime(gid, secs):
    if secs <= 0:
        return
    with lock:
        lib = load()
        g = find(lib, gid)
        if g:
            g["playtime"] = g.get("playtime", 0) + int(secs)
            save(lib)


def watch_folder(gid, folder, count, wait_start=180):
    """Track a game that runs as some process inside its folder (store links, launchers, shortcuts)."""
    watching.add(gid)
    try:
        return _watch(gid, folder, count, wait_start)
    finally:
        watching.discard(gid)


def _watch(gid, folder, count, wait_start):
    t0 = time.time()
    while time.time() - t0 < wait_start and not procs_in(folder):
        time.sleep(4)
    if not procs_in(folder):
        return 0
    running[gid] = time.time()
    start, misses = time.time(), 0
    while misses < 2:            # two empty checks in a row = closed (survives launcher hand-offs)
        time.sleep(10)
        misses = 0 if procs_in(folder) else misses + 1
    running.pop(gid, None)
    secs = time.time() - start - 10
    if count:
        add_playtime(gid, secs)
    return secs


# ---------- Lunar Client versions ----------
LUNAR_SETTINGS = os.path.join(os.path.expanduser("~"), ".lunarclient", "settings")


def lunar_running():
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Lunar Client.exe", "/NH"],
                             capture_output=True, text=True, creationflags=NOWIN).stdout
        return "Lunar Client.exe" in out
    except Exception:
        return False


LUNAR_DB = os.path.join(os.path.expanduser("~"), ".lunarclient", "db", "profiles.db")
LUNAR_API = "https://api.lunarclientprod.com/launcher/metadata/versions/"
LUNAR_CATALOG = os.path.join(DATA, "lunar-versions.json")
LUNAR_CATEGORIES = ("lunar", "vanilla", "badlion")
LUNAR_CATEGORY_NAMES = {"lunar": "Lunar", "vanilla": "Vanilla", "badlion": "Badlion"}
# loaders Lunar keeps in a profile row (it drops the rest, like "badlion")
LUNAR_LOADERS = ("ichor", "fabric", "forge", "optifine", "neoforge", "quilt")


def _ver_key(v):
    return tuple(int(x) if x.isdigit() else -1 for x in re.split(r"[.\-]", v))


def lunar_launcher_version():
    try:
        with open(os.path.join(LUNAR_SETTINGS, "cache.json"), encoding="utf-8") as f:
            v = json.load(f)["__internal__"]["migrations"]["version"]
        m = re.match(r"\d+\.\d+\.\d+", v)
        if m:
            return m.group()
    except Exception:
        pass
    return "3.7.40"


def lunar_catalog(refresh=False):
    """Every version Lunar offers, per category, newest first.

    Same lists the Lunar launcher shows (its public metadata API); cached in
    data/lunar-versions.json for 6 hours and used offline when the API is down.
    """
    cached = None
    try:
        with open(LUNAR_CATALOG, encoding="utf-8") as f:
            cached = json.load(f)
    except Exception:
        pass
    if cached and not refresh and time.time() - cached.get("fetched", 0) < 6 * 3600:
        return cached
    try:
        import urllib.request
        q = f"?os=win32&arch=x64&launcher_version={lunar_launcher_version()}&branch=master"
        cats = {}
        for cat in LUNAR_CATEGORIES:
            req = urllib.request.Request(LUNAR_API + cat + q, headers={"User-Agent": "GameHub"})
            with urllib.request.urlopen(req, timeout=15) as r:
                raw = json.load(r)["versions"]
            majors = []
            for m in raw:
                subs = []
                for s in m.get("subversions", []):
                    mods = [{"id": x["id"], "name": x.get("name") or x["id"], "default": bool(x.get("default")),
                             "loaders": x.get("loaders") or []}
                            for x in sorted(s.get("modules", []), key=lambda x: x.get("sort", 0))]
                    if mods:
                        subs.append({"id": s["id"], "default": bool(s.get("default")), "modules": mods})
                if subs:
                    subs.sort(key=lambda s: _ver_key(s["id"]), reverse=True)
                    majors.append({"id": m["id"], "name": m.get("name") or m["id"],
                                   "description": m.get("description") or "", "subversions": subs})
            majors.sort(key=lambda m: _ver_key(m["id"]), reverse=True)
            cats[cat] = majors
        data = {"fetched": time.time(), "categories": cats}
        tmp = LUNAR_CATALOG + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, LUNAR_CATALOG)
        return data
    except Exception:
        if cached:
            return cached
        raise


def lunar_find(category, version):
    for m in lunar_catalog()["categories"].get(category, []):
        for s in m["subversions"]:
            if s["id"] == version:
                return m, s
    return None, None


def _lunar_db():
    import sqlite3
    con = sqlite3.connect(LUNAR_DB, timeout=5)
    con.row_factory = sqlite3.Row
    return con


def lunar_select_profile(category, version, module):
    """Point the Lunar profile for this major version at an exact version and
    module (what picking them in Lunar's own version menu does), creating the
    profile if this major version was never played. Returns the profile id."""
    major, sub = lunar_find(category, version)
    if not sub:
        raise ValueError(f"Lunar doesn't offer {version} under {category}")
    mod = next((x for x in sub["modules"] if x["id"] == module), None)
    if not mod:
        raise ValueError(f"{version} has no '{module}' option")
    loaders = json.dumps([l for l in mod["loaders"] if l in LUNAR_LOADERS], separators=(",", ":"))
    con = _lunar_db()
    try:
        with con:
            row = con.execute(
                "SELECT id FROM profiles WHERE type=? AND major_game_version=? AND provider IS NULL "
                "AND temporary_of IS NULL ORDER BY created_at LIMIT 1", (category, major["id"])).fetchone()
            if row:
                pid = row["id"]
                con.execute("UPDATE profiles SET game_version=?, lunar_module=?, loaders=? WHERE id=?",
                            (version, module, loaders, pid))
            else:
                pid = str(uuid.uuid4())
                path = {"lunar": "", "vanilla": "vanilla-", "badlion": "badlion-"}[category] + major["id"]
                con.execute(
                    "INSERT INTO profiles (id, name, description, path, type, major_game_version, game_version, "
                    "loaders, use_lunar_features, is_badlion, lunar_module, created_at, config_version) "
                    "VALUES (?,?,?,?,?,?,?,?,1,?,?,?,3)",
                    (pid, f"Minecraft {major['id']}", major.get("description") or None, path, category,
                     major["id"], version, loaders, 1 if category == "badlion" else 0, module, int(time.time())))
    finally:
        con.close()
    return pid


def lunar_state():
    """What the picker shows: the catalog, the current selection and recent picks."""
    cat = lunar_catalog()
    current, recent = None, []
    try:
        with open(os.path.join(LUNAR_SETTINGS, "cache.json"), encoding="utf-8") as f:
            hist = json.load(f).get("profileSelectHistory", {})
        entries = sorted(((c, e) for c in LUNAR_CATEGORIES for e in hist.get(c, []) if e.get("version")),
                         key=lambda ce: ce[1].get("time", 0), reverse=True)
        seen = set()
        for c, e in entries:
            k = (c, e["version"], e.get("module"))
            if k in seen or not lunar_find(c, e["version"])[1]:
                continue
            seen.add(k)
            recent.append({"category": c, "version": e["version"], "module": e.get("module")})
        current = recent[0] if recent else None
    except Exception:
        pass
    # the profile row is the truth for what Lunar will launch
    if current:
        try:
            con = _lunar_db()
            major, _ = lunar_find(current["category"], current["version"])
            row = con.execute("SELECT game_version, lunar_module FROM profiles WHERE type=? AND "
                              "major_game_version=? AND provider IS NULL AND temporary_of IS NULL "
                              "ORDER BY created_at LIMIT 1", (current["category"], major["id"])).fetchone()
            con.close()
            if row:
                current = {"category": current["category"], "version": row[0], "module": row[1]}
        except Exception:
            pass
    return {"categories": cat["categories"], "names": LUNAR_CATEGORY_NAMES, "current": current,
            "recent": recent[:10], "running": lunar_running()}


def close_lunar():
    subprocess.run(["taskkill", "/IM", "Lunar Client.exe", "/F", "/T"], capture_output=True, creationflags=NOWIN)
    for _ in range(20):
        if not lunar_running():
            return True
        time.sleep(0.25)
    return False


def prepare_lunar(spec):
    """Make the Lunar launcher open with a given version selected.

    spec = {"category": "lunar"|"vanilla"|"badlion", "version": "1.8.9", "module": "lunar"|"fabric"|...}
    The exact version and module live in the Lunar profile for that major
    version (profiles.db); which profile is open comes from the newest entry of
    profileSelectHistory[category] and settings.gameProfile, so all three are
    set. Only possible while Lunar is closed: a running launcher keeps (and
    later rewrites) its own copy.
    """
    if lunar_running():
        return False
    category = spec.get("category", "lunar")
    if category in LUNAR_CATEGORIES:
        pid = lunar_select_profile(category, spec["version"], spec.get("module", "lunar"))
    else:
        pid = spec["profileId"]
    cache_p = os.path.join(LUNAR_SETTINGS, "cache.json")
    launcher_p = os.path.join(LUNAR_SETTINGS, "launcher.json")
    with open(cache_p, encoding="utf-8") as f:
        cache = json.load(f)
    hist = cache.setdefault("profileSelectHistory", {}).setdefault(category, [])
    hist.insert(0, {"time": int(time.time() * 1000), "version": spec["version"],
                    "module": spec.get("module", "lunar"), "profileId": pid})
    spec = dict(spec, profileId=pid)
    del hist[50:]
    with open(launcher_p, encoding="utf-8") as f:
        launcher = json.load(f)
    launcher.setdefault("settings", {})["gameProfile"] = spec["profileId"]
    for p, data, indent in ((cache_p, cache, None), (launcher_p, launcher, 2)):
        tmp = p + ".gamehub.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=indent)
        os.replace(tmp, p)
    return True


def is_lunar_launcher(g):
    return os.path.basename(g.get("path", "")).lower() == "lunar client.exe"


# ---------- launchers (Lunar, Epic, Steam, ...) ----------
LOCALAPP = os.environ.get("LOCALAPPDATA", "")
PF86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
PF = os.environ.get("ProgramFiles", r"C:\Program Files")
KNOWN_LAUNCHERS = [
    # (key, name, candidate paths)
    ("lunar", "Lunar Client", [os.path.join(LOCALAPP, "Programs", "launcher", "Lunar Client.exe")]),
    ("epic", "Epic Games Launcher", [os.path.join(PF86, r"Epic Games\Launcher\Portal\Binaries\Win64\EpicGamesLauncher.exe"),
                                     os.path.join(PF86, r"Epic Games\Launcher\Portal\Binaries\Win32\EpicGamesLauncher.exe")]),
    ("steam", "Steam", [os.path.join(PF86, r"Steam\steam.exe")]),
    ("badlion", "Badlion Client", [os.path.join(LOCALAPP, "Programs", "Badlion Client", "Badlion Client.exe")]),
    ("minecraft", "Minecraft Launcher", ["shell:AppsFolder\\Microsoft.4297127D64EC6_8wekyb3d8bbwe!Minecraft"]),
    ("battlenet", "Battle.net", [os.path.join(PF86, r"Battle.net\Battle.net Launcher.exe")]),
    ("plutonium", "Plutonium", [os.path.join(LOCALAPP, r"Plutonium\bin\plutonium-launcher-win32.exe")]),
    ("curseforge", "CurseForge", [os.path.join(LOCALAPP, r"Programs\CurseForge Windows\CurseForge.exe")]),
    ("xbox", "Xbox", ["shell:AppsFolder\\Microsoft.GamingApp_8wekyb3d8bbwe!Microsoft.Xbox.App"]),
    ("ea", "EA app", [os.path.join(PF, r"Electronic Arts\EA Desktop\EA Desktop\EALauncher.exe")]),
    ("ubisoft", "Ubisoft Connect", [os.path.join(PF86, r"Ubisoft\Ubisoft Game Launcher\UbisoftConnect.exe")]),
    ("riot", "Riot Client", [r"C:\Riot Games\Riot Client\RiotClientServices.exe"]),
    ("rockstar", "Rockstar Games Launcher", [os.path.join(PF, r"Rockstar Games\Launcher\Launcher.exe")]),
    ("gog", "GOG Galaxy", [os.path.join(PF86, r"GOG Galaxy\GalaxyClient.exe")]),
    ("modrinth", "Modrinth App", [os.path.join(LOCALAPP, r"Programs\modrinth-app\Modrinth App.exe"),
                                  os.path.join(PF, r"Modrinth App\Modrinth App.exe")]),
    ("prism", "Prism Launcher", [os.path.join(LOCALAPP, r"Programs\PrismLauncher\prismlauncher.exe"),
                                 os.path.join(LOCALAPP, r"Programs\Prism Launcher\prismlauncher.exe")]),
    ("hoyoplay", "HoYoPlay", [os.path.join(PF, r"HoYoPlay\launcher.exe"), os.path.join(LOCALAPP, r"HoYoPlay\launcher.exe")]),
    ("itch", "itch", [os.path.join(LOCALAPP, r"itch\itch.exe")]),
]
_store_apps = {}


def store_app_ids():
    """AppIDs of installed Store apps (so Xbox / Minecraft Launcher only show when installed)."""
    if "ids" not in _store_apps:
        try:
            r = subprocess.run(["powershell", "-NoProfile", "-Command", "Get-StartApps | % AppID"],
                               capture_output=True, text=True, creationflags=NOWIN, timeout=30)
            _store_apps["ids"] = {x.strip().lower() for x in r.stdout.splitlines() if x.strip()}
        except Exception:
            _store_apps["ids"] = set()
    return _store_apps["ids"]


def scan_launchers():
    found = []
    for key, name, paths in KNOWN_LAUNCHERS:
        for p in paths:
            ok = (p.split("\\", 1)[1].lower() in store_app_ids()) if p.startswith("shell:") else os.path.isfile(p)
            if ok:
                found.append({"name": name, "path": p, "source": "Launcher", "key": "launcher:" + key,
                              "launcher": True})
                break
    return found


def is_launcher_path(p):
    p = os.path.normcase(p or "")
    return any(os.path.normcase(c) == p for _, _, cs in KNOWN_LAUNCHERS for c in cs)


def sync_launchers():
    """Add installed launchers to the library, and move launcher entries already in it
    (e.g. a "Lunar Client" tile added by hand) into the Launchers section."""
    try:
        found = scan_launchers()
    except Exception:
        return
    with lock:
        lib = load()
        changed = False
        paths = {os.path.normcase(g.get("path", "")) for g in lib["games"]}
        for g in lib["games"]:
            # plain launcher tiles only: a Lunar tile pinned to a Minecraft version stays a game
            if "launcher" not in g and is_launcher_path(g.get("path")) and not g.get("args") \
                    and not (g.get("lunar") or {}).get("profileId") and "(" not in g.get("name", ""):
                g["launcher"] = True
                changed = True
        for x in found:
            if os.path.normcase(x["path"]) not in paths:
                x.update(id=uuid.uuid4().hex[:12], added=int(time.time()))
                lib["games"].append(x)
                changed = True
        if changed:
            save(lib)


def launch_launcher(g):
    """Open a launcher exactly like double-clicking it in Explorer: it starts on its own
    (not as a child of GameHub), and an already-open launcher comes to the front."""
    path = g.get("path", "")
    if not is_uri(path) and not os.path.exists(path):
        raise FileNotFoundError(path)
    if g.get("args"):
        os.startfile(path, "open", g["args"], g.get("cwd") or (None if is_uri(path) else os.path.dirname(path)))
    else:
        subprocess.Popen(["explorer.exe", path])
    mark_launched(g["id"])


def launch(g, prepared=False):
    if g.get("lunar") and not prepared:
        try:
            prepare_lunar(g["lunar"])
        except Exception as e:
            print("Lunar version preselect failed:", e)
    if g.get("launcher"):
        return launch_launcher(g)     # launchers sit in the tray for hours: no play-time tracking
    path = g.get("path", "")
    args = g.get("args", "")
    gid = g["id"]
    cwd = g.get("cwd") or (os.path.dirname(path) if path and not is_uri(path) else None)
    # Steam already counts hours for its own games; GameHub counts everything else
    count = steam_appid(g) is None
    if is_launcher_path(path) and not args:
        # a game tile that opens through a launcher (e.g. a Lunar version tile): start the launcher
        # detached like Explorer does, then time the game by watching its folder
        launch_launcher(g)
        folder = game_folder(g)
        if folder:
            threading.Thread(target=watch_folder, args=(gid, folder, count), daemon=True).start()
        return
    if is_uri(path) or path.lower().endswith((".lnk", ".url")) or (not path.lower().endswith((".exe", ".bat", ".cmd")) and os.path.isfile(path)):
        # shortcuts, store URIs (steam://, com.epicgames...), ROM files etc. -> let Windows open them
        if args:
            os.startfile(path, "open", args, cwd)
        else:
            os.startfile(path)
        mark_launched(gid)
        folder = game_folder(g)
        if folder:
            threading.Thread(target=watch_folder, args=(gid, folder, count), daemon=True).start()
        return
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    cmd = f'"{path}" {args}'.strip()
    if path.lower().endswith((".bat", ".cmd")):
        proc = subprocess.Popen(["cmd", "/c", cmd], cwd=cwd)
    else:
        proc = subprocess.Popen(cmd, cwd=cwd)
    mark_launched(gid)
    running[gid] = time.time()

    def wait():
        start = time.time()
        proc.wait()
        secs = time.time() - start
        folder = game_folder(g)
        if secs < 60 and folder:
            # exe was probably a launcher that started the real game and quit: keep watching its folder
            secs += watch_folder(gid, folder, False, wait_start=20)
        running.pop(gid, None)
        if count:
            add_playtime(gid, secs)
    threading.Thread(target=wait, daemon=True).start()


def mark_launched(gid):
    with lock:
        lib = load()
        g = find(lib, gid)
        if g:
            g["lastPlayed"] = int(time.time())
            g["launches"] = g.get("launches", 0) + 1
            save(lib)


# ---------- Steam hours ----------
def vdf_parse(text):
    toks = re.findall(r'"((?:[^"\\]|\\.)*)"|([{}])', text)
    root, stack, key = {}, [], None
    cur = root
    for s, br in toks:
        if br == "{":
            d = {}
            cur[key] = d
            stack.append(cur)
            cur, key = d, None
        elif br == "}":
            cur = stack.pop() if stack else root
            key = None
        elif key is None:
            key = s
        else:
            cur[key] = s
            key = None
    return root


def ci(d, *path):
    """Case-insensitive nested dict lookup."""
    for k in path:
        if not isinstance(d, dict):
            return {}
        d = next((v for kk, v in d.items() if kk.lower() == k.lower()), {})
    return d


def steam_path():
    try:
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam")
        return winreg.QueryValueEx(k, "SteamPath")[0]
    except OSError:
        return None


_steam_cache = {"sig": None, "data": {}}


def steam_hours():
    """appid -> (minutes, lastPlayed), added up over every Steam account on this PC."""
    sp = steam_path()
    files = glob.glob(os.path.join(sp, "userdata", "*", "config", "localconfig.vdf")) if sp else []
    # data/settings.json {"steamAccounts": ["<userdata folder id>", ...]} limits which accounts count
    try:
        allowed = json.load(open(os.path.join(DATA, "settings.json"), encoding="utf-8")).get("steamAccounts")
    except Exception:
        allowed = None
    if allowed:
        files = [f for f in files if os.path.basename(os.path.dirname(os.path.dirname(f))) in allowed]
    sig = tuple((f, os.path.getmtime(f)) for f in files)
    if sig == _steam_cache["sig"]:
        return _steam_cache["data"]
    data = {}
    for f in files:
        try:
            v = vdf_parse(open(f, encoding="utf-8", errors="ignore").read())
        except Exception:
            continue
        for aid, a in ci(v, "UserLocalConfigStore", "Software", "Valve", "Steam", "apps").items():
            if not isinstance(a, dict):
                continue
            mins = int(ci_val(a, "Playtime") or 0)
            last = int(ci_val(a, "LastPlayed") or 0)
            m0, l0 = data.get(aid, (0, 0))
            data[aid] = (m0 + mins, max(l0, last))
    _steam_cache.update(sig=sig, data=data)
    return data


def ci_val(d, k):
    v = next((v for kk, v in d.items() if kk.lower() == k.lower()), 0)
    return v if isinstance(v, str) and v.isdigit() else 0


def with_stats(g):
    """Copy of g with play time / last played taken from Steam where it applies."""
    g = dict(g)
    aid = steam_appid(g)
    g["trackedBy"] = "Steam" if aid else "GameHub"
    if aid:
        mins, last = steam_hours().get(aid, (0, 0))
        g["playtime"] = mins * 60
        g["lastPlayed"] = max(g.get("lastPlayed") or 0, last)
    g["running"] = g["id"] in running
    return g


def backfill_folders():
    """Give already-imported store games their install folder so GameHub can see them running."""
    try:
        found = {x["key"]: x for x in scan_steam() + scan_epic()}
    except Exception:
        return
    with lock:
        lib = load()
        changed = False
        for g in lib["games"]:
            x = found.get(g.get("key"))
            if x and x.get("folder") and not g.get("folder"):
                g["folder"] = x["folder"]
                changed = True
        if changed:
            save(lib)


# ---------- file dialog ----------
def browse(kind):
    py = sys.executable.replace("pythonw.exe", "python.exe")
    if kind == "image":
        ft = "[('Images','*.png *.jpg *.jpeg *.webp *.gif *.bmp'),('All files','*.*')]"
    elif kind == "folder":
        ft = None
    else:
        ft = "[('Games & shortcuts','*.exe *.lnk *.url *.bat *.cmd'),('All files','*.*')]"
    fn = "askdirectory()" if ft is None else f"askopenfilename(filetypes={ft})"
    code = ("import tkinter as t,tkinter.filedialog as f,sys;r=t.Tk();r.withdraw();"
            f"r.attributes('-topmost',1);p=f.{fn};sys.stdout.write(p or '')")
    r = subprocess.run([py, "-c", code], capture_output=True, text=True, creationflags=NOWIN)
    return os.path.normpath(r.stdout.strip()) if r.stdout.strip() else ""


# ---------- store scanning ----------
def scan_steam():
    found = []
    try:
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam")
        steam = winreg.QueryValueEx(k, "SteamPath")[0]
    except OSError:
        return found
    libs = {os.path.normpath(steam)}
    vdf = os.path.join(steam, "steamapps", "libraryfolders.vdf")
    if os.path.exists(vdf):
        txt = open(vdf, encoding="utf-8", errors="ignore").read()
        for m in re.finditer(r'"path"\s+"([^"]+)"', txt):
            libs.add(os.path.normpath(m.group(1).replace("\\\\", "\\")))
    skip = re.compile(r"redistributable|steamworks|proton|steam linux runtime|steamvr|soundtrack|dedicated server", re.I)
    for lib_dir in libs:
        for acf in glob.glob(os.path.join(lib_dir, "steamapps", "appmanifest_*.acf")):
            t = open(acf, encoding="utf-8", errors="ignore").read()
            aid = re.search(r'"appid"\s+"(\d+)"', t)
            name = re.search(r'"name"\s+"([^"]+)"', t)
            inst = re.search(r'"installdir"\s+"([^"]+)"', t)
            if not (aid and name) or skip.search(name.group(1)) or aid.group(1) == "228980":
                continue
            a = aid.group(1)
            folder = os.path.join(lib_dir, "steamapps", "common", inst.group(1)) if inst else ""
            found.append({
                "name": name.group(1), "path": f"steam://rungameid/{a}", "source": "Steam",
                "key": "steam:" + a,
                "cover": f"https://cdn.cloudflare.steamstatic.com/steam/apps/{a}/library_600x900.jpg",
                "hero": f"https://cdn.cloudflare.steamstatic.com/steam/apps/{a}/library_hero.jpg",
                "iconFrom": biggest_exe(folder), "folder": folder if os.path.isdir(folder) else "",
            })
    return found


def biggest_exe(folder):
    best, size = "", 0
    if not folder or not os.path.isdir(folder):
        return ""
    for root, dirs, files in os.walk(folder):
        if root.count(os.sep) - folder.count(os.sep) > 2:
            dirs[:] = []
            continue
        for f in files:
            if f.lower().endswith(".exe") and not re.search(r"unins|crash|setup|redist|vc_|dxsetup|launcherhelper", f, re.I):
                s = os.path.getsize(os.path.join(root, f))
                if s > size:
                    best, size = os.path.join(root, f), s
    return best


def scan_epic():
    found = []
    for item in glob.glob(r"C:\ProgramData\Epic\EpicGamesLauncher\Data\Manifests\*.item"):
        try:
            j = json.load(open(item, encoding="utf-8"))
        except Exception:
            continue
        if "games" not in [c.lower() for c in j.get("AppCategories", ["games"])]:
            continue
        ns, cid, app = j.get("CatalogNamespace"), j.get("CatalogItemId"), j.get("AppName")
        if not (ns and cid and app):
            continue
        exe = os.path.join(j.get("InstallLocation", ""), j.get("LaunchExecutable", ""))
        found.append({
            "name": j.get("DisplayName", app), "source": "Epic", "key": "epic:" + app,
            "path": f"com.epicgames.launcher://apps/{ns}%3A{cid}%3A{app}?action=launch&silent=true",
            "iconFrom": exe if os.path.isfile(exe) else "",
            "folder": j.get("InstallLocation", "") if os.path.isdir(j.get("InstallLocation", "")) else "",
        })
    return found


def import_scan():
    added = []
    with lock:
        lib = load()
        keys = {g.get("key") for g in lib["games"]}
        for g in scan_steam() + scan_epic() + scan_launchers():
            if g["key"] in keys or (g.get("launcher") and is_launcher_path(g["path"])
                                    and any(x.get("path") == g["path"] for x in lib["games"])):
                continue
            g.update(id=uuid.uuid4().hex[:12], added=int(time.time()))
            lib["games"].append(g)
            keys.add(g["key"])
            added.append(g["name"])
        save(lib)
    return added


# ---------- http ----------
def store_cover(gid, value, field):
    """Copy a local image into data/covers so the library survives moves."""
    if not value or is_uri(value) or value.startswith("/api/"):
        return value
    if not os.path.isfile(value):
        return ""
    ext = os.path.splitext(value)[1].lower() or ".png"
    dst = os.path.join(COVERS, f"{gid}-{field}{ext}")
    for old in glob.glob(os.path.join(COVERS, f"{gid}-{field}.*")):
        os.remove(old)
    shutil.copyfile(value, dst)
    return f"/api/img/{os.path.basename(dst)}"


# ---------- Switch mode quick apps ----------
LOCAL = os.environ.get("LOCALAPPDATA", "")
OPERA = os.path.join(LOCAL, "Programs", "Opera GX", "opera.exe")
QUICK = {
    # key: (label, how to start it)
    "steam": ("Steam Big Picture", lambda: os.startfile("steam://open/bigpicture")),
    "browser": ("Opera GX", lambda: subprocess.Popen([OPERA, "--start-fullscreen"]) if os.path.exists(OPERA)
                else os.startfile("https://www.google.com")),
    "youtube": ("YouTube", lambda: subprocess.Popen([OPERA, "--start-fullscreen", "https://www.youtube.com"])
                if os.path.exists(OPERA) else os.startfile("https://www.youtube.com")),
    "discord": ("Discord", lambda: subprocess.Popen([os.path.join(LOCAL, "Discord", "Update.exe"),
                                                     "--processStart", "Discord.exe"])),
}


def quick_apps():
    ok = {"browser": True, "youtube": True, "steam": bool(steam_path()),
          "discord": os.path.exists(os.path.join(LOCAL, "Discord", "Update.exe"))}
    return [{"key": k, "name": v[0]} for k, v in QUICK.items() if ok.get(k)]


EDITABLE = ("name", "path", "args", "cwd", "cover", "hero", "favorite", "hidden", "notes", "category", "launcher")
MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
        ".gif": "image/gif", ".bmp": "image/bmp"}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body=b"", ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def authed(self):
        if self.headers.get("X-Token") != TOKEN:
            self.send(403, {"error": "forbidden"})
            return False
        return True

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        p = urlparse(self.path).path
        if p in ("/", "/switch"):
            page = "switch.html" if p == "/switch" else "index.html"
            html = open(os.path.join(APP_DIR, page), encoding="utf-8").read()
            return self.send(200, html.replace("__TOKEN__", TOKEN), "text/html; charset=utf-8")
        if p in ("/icon.png", "/favicon.ico"):
            f = os.path.join(APP_DIR, "icon.png" if p == "/icon.png" else "gamehub.ico")
            return self.send(200, open(f, "rb").read(), "image/png" if p == "/icon.png" else "image/x-icon")
        if p.startswith("/api/img/"):
            f = os.path.join(COVERS, os.path.basename(unquote(p[9:])))
            if os.path.isfile(f):
                return self.send(200, open(f, "rb").read(), MIME.get(os.path.splitext(f)[1].lower(), "image/png"))
            return self.send(404)
        if p.startswith("/api/icon/"):
            lib = load()
            g = find(lib, p[10:])
            f = g and extract_icon(g)
            if f:
                return self.send(200, open(f, "rb").read(), "image/png")
            return self.send(404)
        if not self.authed():
            return
        if p == "/api/games":
            lib = load()
            lib["games"] = [with_stats(g) for g in lib["games"]]
            return self.send(200, lib)
        if p == "/api/quick":
            return self.send(200, quick_apps())
        if p == "/api/lunar":
            try:
                return self.send(200, lunar_state())
            except Exception as e:
                return self.send(502, {"error": f"Couldn't load Lunar's version list: {e}"})
        self.send(404)

    def do_POST(self):
        if not self.authed():
            return
        p = urlparse(self.path).path
        try:
            if p == "/api/heartbeat":
                last_beat[0] = time.time()
                return self.send(200, {"running": list(running)})
            if p == "/api/browse":
                return self.send(200, {"path": browse(self.body().get("kind", "exe"))})
            if p == "/api/scan":
                return self.send(200, {"added": import_scan()})
            if p == "/api/save":
                data = self.body()
                with lock:
                    lib = load()
                    g = find(lib, data.get("id", ""))
                    if not g:
                        g = {"id": uuid.uuid4().hex[:12], "added": int(time.time()), "source": "Custom"}
                        lib["games"].append(g)
                    old_path = g.get("path")
                    for k in EDITABLE:
                        if k in data:
                            g[k] = data[k]
                    if not g.get("name"):
                        base = os.path.basename(g.get("path", "")) or "Untitled"
                        g["name"] = os.path.splitext(base)[0]
                    g["cover"] = store_cover(g["id"], g.get("cover", ""), "cover")
                    g["hero"] = store_cover(g["id"], g.get("hero", ""), "hero")
                    if old_path != g.get("path"):
                        ic = icon_path(g["id"])
                        if os.path.exists(ic):
                            os.remove(ic)
                    save(lib)
                return self.send(200, g)
            if p.startswith("/api/delete/"):
                gid = p[12:]
                with lock:
                    lib = load()
                    lib["games"] = [g for g in lib["games"] if g["id"] != gid]
                    save(lib)
                for f in glob.glob(os.path.join(COVERS, gid + "-*")) + glob.glob(os.path.join(ICONS, gid + ".*png")):
                    os.remove(f)
                return self.send(200, {"ok": True})
            if p.startswith("/api/lunar/launch/"):
                # body: {category, version, module, close?}; picks the version, remembers it on the
                # tile, then starts Lunar with it selected
                data = self.body()
                spec = {k: data.get(k) for k in ("category", "version", "module")}
                if spec["category"] not in LUNAR_CATEGORIES or not spec["version"] or not spec["module"]:
                    return self.send(400, {"error": "Pick a version and a module"})
                with lock:
                    lib = load()
                    g = find(lib, p[18:])
                    if not g:
                        return self.send(404, {"error": "Game not found"})
                    if lunar_running():
                        if not data.get("close"):
                            return self.send(409, {"error": "Lunar Client is open", "lunarOpen": True})
                        if not close_lunar():
                            return self.send(409, {"error": "Couldn't close Lunar Client", "lunarOpen": True})
                    prepare_lunar(spec)
                    g["lunar"] = spec
                    save(lib)
                launch(g, prepared=True)
                return self.send(200, {"ok": True})
            if p.startswith("/api/quick/"):
                app = QUICK.get(p[11:])
                if not app:
                    return self.send(404, {"error": "Unknown app"})
                app[1]()
                return self.send(200, {"ok": True})
            if p.startswith("/api/launch/"):
                g = find(load(), p[12:])
                if not g:
                    return self.send(404, {"error": "Game not found"})
                launch(g)
                return self.send(200, {"ok": True})
            if p.startswith("/api/folder/"):
                g = find(load(), p[12:])
                src = g and (icon_source(g) or g.get("cwd"))
                if src:
                    subprocess.Popen(["explorer", "/select,", src] if os.path.isfile(src) else ["explorer", src])
                    return self.send(200, {"ok": True})
                return self.send(400, {"error": "No local folder for this game"})
        except FileNotFoundError as e:
            return self.send(400, {"error": f"File not found: {e}"})
        except Exception as e:
            return self.send(500, {"error": str(e)})
        self.send(404)


# ---------- window ----------
def find_edge():
    for base in (os.environ.get("ProgramFiles(x86)", ""), os.environ.get("ProgramFiles", ""),
                 os.environ.get("LOCALAPPDATA", "")):
        p = os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe")
        if os.path.exists(p):
            return p
    return None


def open_window(url, fullscreen=False):
    edge = find_edge()
    if not edge:
        import webbrowser
        webbrowser.open(url)
        return
    # Switch mode gets its own profile so it always opens as a new fullscreen window
    profile = "switch-profile" if fullscreen else "window-profile"
    size = ["--start-fullscreen"] if fullscreen else ["--window-size=1360,860"]
    subprocess.Popen([edge, f"--app={url}", *size,
                      f"--user-data-dir={os.path.join(DATA, profile)}",
                      "--no-first-run", "--disable-features=Translate",
                      "--autoplay-policy=no-user-gesture-required"])


def port_in_use():
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", PORT)) == 0


def main():
    # --switch: controller-friendly fullscreen "Switch mode" (used by the Moonlight/Sunshine tile)
    switch = "--switch" in sys.argv[1:]
    url = f"http://127.0.0.1:{PORT}/switch" if switch else f"http://127.0.0.1:{PORT}/"
    if port_in_use():
        # already running: just open another window onto it
        open_window(url, switch)
        return
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    threading.Thread(target=lambda: (sync_launchers(), backfill_folders()), daemon=True).start()
    open_window(url, switch)
    # exit once the window has been closed for a while and no tracked game is running
    while True:
        time.sleep(5)
        if time.time() - last_beat[0] > 45 and not running and not watching:
            break
    srv.shutdown()


if __name__ == "__main__":
    main()
