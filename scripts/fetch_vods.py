#!/usr/bin/env python3
"""
Clipping Radar — busca los directos (VODs) del día anterior de una lista de
canales de Twitch, se queda con los de GTA 6, descarga el chat y detecta los
momentos en los que el chat "explota" (risas, hype). Guarda el resultado en
data/latest.json y data/AAAA-MM-DD.json.

Solo usa la librería estándar de Python. Variables de entorno necesarias:
  TWITCH_CLIENT_ID, TWITCH_CLIENT_SECRET
Opcional:
  TDCLI = ruta a TwitchDownloaderCLI (para descargar el chat)
"""
import json
import os
import re
import statistics
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HELIX = "https://api.twitch.tv/helix"
GQL = "https://gql.twitch.tv/gql"
GQL_PUBLIC_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"  # cliente web público de Twitch

# Palabras que indican risa o hype en el chat (es + en + emotes típicos)
LAUGH_RE = re.compile(
    r"(j[aj]{3,}|xd+|xdd|k{4,}|lol+|lmao+|lmfao|kekw|omegalul|lul|icant|"
    r"pepelaugh|lulw|😂|🤣|💀|dead+|crying|me meo|me muero)",
    re.IGNORECASE,
)
HYPE_RE = re.compile(
    r"(pog+|pogchamp|poggers|\bw+\b|clip+\s*(it|eso|that)?|clipea|"
    r"que+\s*(cojones|locura|bestia)|no way|holy|omg|wtf|\bbro+\b|hype|"
    r"monkas|aware|o7|🔥|😱|😳)",
    re.IGNORECASE,
)

BUCKET = 10  # segundos por "cubo" de chat


# ----------------------------------------------------------------- HTTP utils
def http(method, url, headers=None, data=None, timeout=60):
    body = None
    if data is not None:
        body = json.dumps(data).encode() if isinstance(data, (dict, list)) else data
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def get_token(cid, secret):
    q = urllib.parse.urlencode(
        {"client_id": cid, "client_secret": secret, "grant_type": "client_credentials"}
    )
    return http("POST", f"https://id.twitch.tv/oauth2/token?{q}", data=b"")["access_token"]


def helix(path, params, cid, token):
    q = urllib.parse.urlencode(params, doseq=True)
    return http(
        "GET",
        f"{HELIX}/{path}?{q}",
        headers={"Client-Id": cid, "Authorization": f"Bearer {token}"},
    )


def gql(query, variables=None):
    return http(
        "POST",
        GQL,
        headers={"Client-Id": GQL_PUBLIC_CLIENT_ID, "Content-Type": "application/json"},
        data={"query": query, "variables": variables or {}},
    )


# ------------------------------------------------------------------- helpers
def parse_duration(s):
    """'3h8m33s' -> segundos"""
    total = 0
    for n, u in re.findall(r"(\d+)([hms])", s or ""):
        total += int(n) * {"h": 3600, "m": 60, "s": 1}[u]
    return total


def hms(sec):
    sec = int(max(0, sec))
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def vod_game_info(vod_id):
    """Juego principal y capítulos (tramos por juego) del VOD vía GQL. Best effort."""
    q = """
    query($id: ID!) {
      video(id: $id) {
        game { name displayName }
        moments(momentRequestType: VIDEO_CHAPTER_MARKERS, first: 50) {
          edges { node {
            positionMilliseconds durationMilliseconds
            details { ... on GameChangeMomentDetails { game { name displayName } } }
          } }
        }
      }
    }"""
    try:
        d = gql(q, {"id": vod_id}).get("data", {}).get("video") or {}
    except Exception as e:  # noqa: BLE001
        print(f"   (GQL no disponible para {vod_id}: {e})")
        return None, []
    game = (d.get("game") or {}).get("name")
    chapters = []
    for edge in ((d.get("moments") or {}).get("edges") or []):
        n = edge.get("node") or {}
        g = (((n.get("details") or {}).get("game")) or {}).get("name")
        start = (n.get("positionMilliseconds") or 0) / 1000
        dur = (n.get("durationMilliseconds") or 0) / 1000
        chapters.append({"game": g, "start": start, "end": start + dur})
    return game, chapters


def is_target(title, game, chapters, cfg):
    names = {g.lower() for g in cfg["game_names"]}
    if "*" in names:  # modo prueba: cualquier juego
        return True
    if game and game.lower() in names:
        return True
    if any((c.get("game") or "").lower() in names for c in chapters):
        return True
    t = (title or "").lower()
    return any(k in t for k in cfg["title_keywords"])


def target_ranges(chapters, cfg, length):
    """Tramos del VOD en los que se juega a GTA 6 (si hay capítulos)."""
    names = {g.lower() for g in cfg["game_names"]}
    if "*" in names:
        return [(0, length)]
    r = [(c["start"], c["end"] or length) for c in chapters if (c.get("game") or "").lower() in names]
    return r or [(0, length)]


# ------------------------------------------------------------- chat analysis
def download_chat(vod_id, tdcli):
    out = os.path.join(tempfile.gettempdir(), f"chat_{vod_id}.json")
    cmd = [tdcli, "chatdownload", "--id", str(vod_id), "-o", out, "--collision", "Overwrite"]
    print("   descargando chat:", " ".join(cmd[1:4]))
    try:
        subprocess.run(cmd, check=True, timeout=60 * 60, capture_output=True)
    except Exception as e:  # noqa: BLE001
        print(f"   (no se pudo descargar el chat: {e})")
        return None
    with open(out, encoding="utf-8") as f:
        data = json.load(f)
    os.remove(out)
    msgs = []
    for c in data.get("comments", []):
        body = ((c.get("message") or {}).get("body")) or ""
        msgs.append((float(c.get("content_offset_seconds", 0)), body))
    return msgs


def find_peaks(msgs, length, ranges, max_peaks):
    """Detecta picos de chat. Devuelve lista de momentos con inicio/fin estimados."""
    if not msgs:
        return []
    n = int(length // BUCKET) + 1
    total = [0] * n
    laugh = [0] * n
    hype = [0] * n
    samples = [[] for _ in range(n)]
    for t, body in msgs:
        i = min(int(t // BUCKET), n - 1)
        total[i] += 1
        if LAUGH_RE.search(body):
            laugh[i] += 1
        if HYPE_RE.search(body):
            hype[i] += 1
        if len(samples[i]) < 40:
            samples[i].append(body)

    # señal: risas pesan más, luego hype, luego volumen
    sig = [laugh[i] * 2 + hype[i] * 1.5 + total[i] * 0.3 for i in range(n)]

    # línea base local: mediana de ±5 min
    w = int(300 // BUCKET)
    base = []
    for i in range(n):
        win = sig[max(0, i - w): i + w + 1]
        base.append(statistics.median(win) if win else 0)

    score = [sig[i] / (base[i] + 3) for i in range(n)]

    def in_ranges(sec):
        return any(a <= sec <= b for a, b in ranges)

    cand = sorted(
        (i for i in range(n) if score[i] >= 2.2 and in_ranges(i * BUCKET)),
        key=lambda i: score[i],
        reverse=True,
    )
    chosen = []
    min_gap = int(120 // BUCKET)
    for i in cand:
        if all(abs(i - j) > min_gap for j in chosen):
            chosen.append(i)
        if len(chosen) >= max_peaks:
            break

    peaks = []
    for i in sorted(chosen):
        # retroceder mientras el chat siga por encima de la base (máx 2 min)
        a = i
        while a > 0 and i - a < 12 and sig[a - 1] > base[a - 1] * 1.3:
            a -= 1
        b = i
        while b < n - 1 and b - i < 18 and sig[b + 1] > base[b + 1] * 1.3:
            b += 1
        # el chat reacciona ~10-20 s tarde: adelantamos el inicio para coger el contexto
        start = max(0, a * BUCKET - 30)
        end = min(length, (b + 1) * BUCKET + 5)
        kind = "risa" if laugh[i] * 2 >= hype[i] * 1.5 else "hype"
        peaks.append({
            "start": start,
            "end": end,
            "start_hms": hms(start),
            "end_hms": hms(end),
            "score": round(score[i], 2),
            "type": kind,
            "msgs_per_10s": total[i],
            "chat_sample": samples[i][:12],
        })
    return peaks


# ---------------------------------------------------------------------- main
def main():
    cid = os.environ.get("TWITCH_CLIENT_ID")
    secret = os.environ.get("TWITCH_CLIENT_SECRET")
    if not cid or not secret:
        sys.exit("Faltan TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET")
    tdcli = os.environ.get("TDCLI")

    with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as f:
        cfg = json.load(f)

    # Modo prueba (lanzado a mano): otro juego / otros canales, 1 directo por canal
    if os.environ.get("TEST_GAME"):
        cfg["game_names"] = [os.environ["TEST_GAME"]]
        cfg["title_keywords"] = []
        cfg["max_vods_per_channel"] = 1
        print(f"*** MODO PRUEBA: juego = {cfg['game_names'][0]}")
    if os.environ.get("TEST_CHANNELS"):
        keep = {x.strip().lower() for x in os.environ["TEST_CHANNELS"].split(",") if x.strip()}
        cfg["channels"] = [c for c in cfg["channels"] if c["login"].lower() in keep] or [{"login": x, "lang": "es"} for x in keep]
        print(f"*** MODO PRUEBA: canales = {sorted(keep)}")

    token = get_token(cid, secret)
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=cfg["lookback_hours"])

    logins = [c["login"].lower() for c in cfg["channels"]]
    lang = {c["login"].lower(): c.get("lang", "es") for c in cfg["channels"]}
    users = helix("users", {"login": logins}, cid, token)["data"]

    result = {"generated_at": now.isoformat(), "since": since.isoformat(), "vods": []}

    for u in users:
        login = u["login"]
        print(f"== {login}")
        try:
            vids = helix(
                "videos",
                {"user_id": u["id"], "type": "archive", "first": 10},
                cid, token,
            )["data"]
        except Exception as e:  # noqa: BLE001
            print(f"   error listando vídeos: {e}")
            continue

        recent = [v for v in vids if datetime.fromisoformat(v["created_at"].replace("Z", "+00:00")) >= since]
        for v in recent[: cfg["max_vods_per_channel"]]:
            length = parse_duration(v["duration"])
            game, chapters = vod_game_info(v["id"])
            if not is_target(v["title"], game, chapters, cfg):
                print(f"   - {v['id']} descartado (juego: {game}) {v['title'][:60]}")
                continue
            print(f"   + {v['id']} {hms(length)} {v['title'][:60]}")
            ranges = target_ranges(chapters, cfg, length)

            # clips creados por los espectadores durante ese directo
            viewer_clips = []
            try:
                end_at = datetime.fromisoformat(v["created_at"].replace("Z", "+00:00")) + timedelta(seconds=length)
                cl = helix("clips", {
                    "broadcaster_id": u["id"],
                    "started_at": v["created_at"],
                    "ended_at": end_at.isoformat().replace("+00:00", "Z"),
                    "first": 50,
                }, cid, token)["data"]
                for c in cl:
                    if c.get("video_id") == v["id"] and c.get("vod_offset") is not None:
                        viewer_clips.append({
                            "title": c["title"],
                            "views": c["view_count"],
                            "vod_offset": c["vod_offset"],
                            "vod_offset_hms": hms(c["vod_offset"]),
                            "duration": c["duration"],
                            "url": c["url"],
                        })
                viewer_clips.sort(key=lambda c: c["views"], reverse=True)
                viewer_clips = viewer_clips[:15]
            except Exception as e:  # noqa: BLE001
                print(f"   (clips no disponibles: {e})")

            peaks = []
            if cfg.get("download_chat") and tdcli:
                msgs = download_chat(v["id"], tdcli)
                peaks = find_peaks(msgs or [], length, ranges, cfg["max_peaks_per_vod"])
                print(f"   picos de chat: {len(peaks)}")

            result["vods"].append({
                "channel": login,
                "display_name": u["display_name"],
                "lang": lang.get(login, "es"),
                "vod_id": v["id"],
                "vod_url": f"https://www.twitch.tv/videos/{v['id']}",
                "title": v["title"],
                "game": game,
                "created_at": v["created_at"],
                "length_seconds": length,
                "length_hms": hms(length),
                "gta_ranges": [{"start_hms": hms(a), "end_hms": hms(b)} for a, b in ranges],
                "chat_peaks": peaks,
                "viewer_clips": viewer_clips,
            })

    os.makedirs(os.path.join(ROOT, "data"), exist_ok=True)
    day = now.astimezone(timezone(timedelta(hours=1))).strftime("%Y-%m-%d")
    for name in ("latest.json", f"{day}.json"):
        with open(os.path.join(ROOT, "data", name), "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"\nOK: {len(result['vods'])} directos de GTA 6 guardados en data/{day}.json")


if __name__ == "__main__":
    main()
