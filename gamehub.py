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
    return bool(re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]{1,}://", p or ""))


# ---------- icons ----------
def icon_source(g):
    """Local file whose icon represents the game."""
    p = g.get("iconFrom") or g.get("path") or ""
    return p if p and not is_uri(p) and os.path.exists(p) else None


def extract_icon(g):
    out = os.path.join(ICONS, g["id"] + ".png")
    if os.path.exists(out):
        return out
    src = icon_source(g)
    if not src:
        return None
    ps = ("Add-Type -AssemblyName System.Drawing;"
          "$i=[System.Drawing.Icon]::ExtractAssociatedIcon($env:GH_SRC);"
          "if($i){$i.ToBitmap().Save($env:GH_OUT,[System.Drawing.Imaging.ImageFormat]::Png)}")
    env = dict(os.environ, GH_SRC=src, GH_OUT=out)
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", ps], env=env,
                       creationflags=NOWIN, timeout=15, capture_output=True)
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


def launch(g):
    path = g.get("path", "")
    args = g.get("args", "")
    gid = g["id"]
    cwd = g.get("cwd") or (os.path.dirname(path) if path and not is_uri(path) else None)
    # Steam already counts hours for its own games; GameHub counts everything else
    count = steam_appid(g) is None
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
        for g in scan_steam() + scan_epic():
            if g["key"] in keys:
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


EDITABLE = ("name", "path", "args", "cwd", "cover", "hero", "favorite", "hidden", "notes", "category")
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
        if p == "/":
            html = open(os.path.join(APP_DIR, "index.html"), encoding="utf-8").read()
            return self.send(200, html.replace("__TOKEN__", TOKEN), "text/html; charset=utf-8")
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
                        ic = os.path.join(ICONS, g["id"] + ".png")
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
                for f in glob.glob(os.path.join(COVERS, gid + "-*")) + glob.glob(os.path.join(ICONS, gid + ".png")):
                    os.remove(f)
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


def open_window(url):
    edge = find_edge()
    if not edge:
        import webbrowser
        webbrowser.open(url)
        return
    subprocess.Popen([edge, f"--app={url}", "--window-size=1360,860",
                      f"--user-data-dir={os.path.join(DATA, 'window-profile')}",
                      "--no-first-run", "--disable-features=Translate"])


def port_in_use():
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", PORT)) == 0


def main():
    if port_in_use():
        # already running: just open another window onto it
        open_window(f"http://127.0.0.1:{PORT}/")
        return
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    threading.Thread(target=backfill_folders, daemon=True).start()
    open_window(f"http://127.0.0.1:{PORT}/")
    # exit once the window has been closed for a while and no tracked game is running
    while True:
        time.sleep(5)
        if time.time() - last_beat[0] > 45 and not running and not watching:
            break
    srv.shutdown()


if __name__ == "__main__":
    main()
