#!/usr/bin/env python3
"""
Clipping Radar — paso 2: a partir de data/latest.json elige los mejores
momentos de cada canal, descarga SOLO esos tramos del directo, los pasa a
vertical 9:16 con el formato común (FORMATO.md), les pone subtítulos
palabra a palabra y los deja en clips/ con un manifiesto JSON.

Necesita: ffmpeg (con libass), yt-dlp, faster-whisper.
"""
import json
import math
import os
import re
import subprocess
import sys
import unicodedata
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "clips")
WORK = os.path.join(ROOT, "work")
FONTS = os.path.join(ROOT, "fonts")
FONT_NAME = os.environ.get("CLIP_FONT", "Montserrat Black")
HOOK_FONT = os.environ.get("HOOK_FONT", "Montserrat SemiBold")

W, H = 1080, 1920
FACE_H = 730          # alto del panel de facecam (38 %, como el ejemplo)
BAND_H = 608          # alto del juego en el formato sin facecam (16:9 a 1080 de ancho)

# Colores ASS (&HAABBGGRR)
WHITE = "&H00FFFFFF"
YELLOW = "&H0000E5FF"
BLACK = "&H00000000"


# ----------------------------------------------------------------- utilidades
def run(cmd, **kw):
    print("  $", " ".join(str(c) for c in cmd)[:200])
    return subprocess.run(cmd, check=True, **kw)


def slug(s, n=28):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-").lower()
    return s[:n].strip("-") or "clip"


def ass_time(t):
    t = max(0.0, t)
    h = int(t // 3600)
    m = int(t % 3600 // 60)
    s = t % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def ass_escape(s):
    return s.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ")


def clean_hook(title):
    t = re.sub(r"\s+", " ", title or "").strip()
    t = re.sub(r"(?i)\b(clip|clipped by \w+)\b", "", t).strip(" -|")
    if len(t) < 4:
        return ""
    return t[:70]


# ---------------------------------------------------------- selección de momentos
def candidates_for_vod(v, cfg):
    cands = []
    for p in v.get("chat_peaks", []):
        cands.append({
            "start": p["start"], "end": p["end"], "score": p["score"],
            "type": p["type"], "hook": "", "source": "chat",
        })
    for c in v.get("viewer_clips", []):
        if c["views"] < cfg["min_clip_views"]:
            continue
        a = max(0, c["vod_offset"] - 12)
        b = c["vod_offset"] + c["duration"] + 6
        bonus = math.log10(c["views"] + 1) * 1.5
        merged = False
        for k in cands:
            if k["source"] == "chat" and a < k["end"] and b > k["start"]:
                k["score"] += bonus
                k["start"] = min(k["start"], a)
                k["end"] = max(k["end"], b)
                k["hook"] = k["hook"] or clean_hook(c["title"])
                k["source"] = "chat+clips"
                merged = True
                break
        if not merged:
            cands.append({
                "start": a, "end": b, "score": 2 + bonus, "type": "hype",
                "hook": clean_hook(c["title"]), "source": "clips",
            })
    # duración entre min y max
    L = v["length_seconds"]
    for k in cands:
        d = k["end"] - k["start"]
        if d < cfg["min_seconds"]:
            pad = (cfg["min_seconds"] - d) / 2
            k["start"] -= pad
            k["end"] += pad
        if k["end"] - k["start"] > cfg["max_seconds"]:
            k["end"] = k["start"] + cfg["max_seconds"]
        k["start"] = max(0, k["start"])
        k["end"] = min(L, k["end"])
        k["vod"] = v
    return cands


def pick_for_channel(vods, cfg):
    cands = [c for v in vods for c in candidates_for_vod(v, cfg)]
    cands.sort(key=lambda c: c["score"], reverse=True)
    chosen = []
    for c in cands:
        if c["score"] < cfg["min_score"]:
            break
        if any(c["vod"]["vod_id"] == o["vod"]["vod_id"] and c["start"] < o["end"] and c["end"] > o["start"] for o in chosen):
            continue
        n = len(chosen)
        if n >= cfg["max_clips_per_channel"]:
            break
        if n >= cfg["clips_per_channel"] and c["score"] < cfg["strong_score"]:
            break
        chosen.append(c)
    return chosen


# --------------------------------------------------------------- subtítulos
def transcribe(path, lang, model):
    segs, _ = model.transcribe(path, language=lang, word_timestamps=True, vad_filter=True, beam_size=1)
    words = []
    for s in segs:
        for w in (s.words or []):
            txt = w.word.strip()
            if txt:
                words.append((w.start, w.end, txt))
    return words


def build_ass(words, offset, dur, layout, hook, name):
    """Subtítulos karaoke: grupos de hasta 3 palabras, la activa en amarillo."""
    sub_y = 1440 if layout == "blur" else 1480
    hook_y = 520 if layout == "blur" else FACE_H + 40
    name_y = 1300 if layout == "blur" else FACE_H - 50

    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Sub,{FONT_NAME},92,{WHITE},{WHITE},{BLACK},&H80000000,-1,0,0,0,100,100,0,0,1,7,3,5,60,60,0,1
Style: Hook,{HOOK_FONT},58,{WHITE},{WHITE},&H40000000,&H90000000,0,0,0,0,100,100,0,0,1,1.5,3,8,110,110,0,1
Style: Name,{FONT_NAME},40,{WHITE},{WHITE},{BLACK},&H80000000,-1,0,0,0,100,100,0,0,1,4,0,4,40,40,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    ev = []
    if hook:
        # como el ejemplo: frase gancho fija todo el vídeo, debajo de la facecam
        ev.append(f"Dialogue: 2,{ass_time(0)},{ass_time(dur)},Hook,,0,0,0,,{{\\an8\\q0\\pos(540,{hook_y})}}{ass_escape(hook)}")
    ev.append(f"Dialogue: 1,{ass_time(0)},{ass_time(dur)},Name,,0,0,0,,{{\\an4\\pos(40,{name_y})}}@{ass_escape(name)}")

    # pasar a tiempos del clip y quedarse con lo que cae dentro
    ws = [(s - offset, e - offset, t) for s, e, t in words if e - offset > 0 and s - offset < dur]
    groups, cur = [], []
    for w in ws:
        if cur and (len(cur) >= 3 or w[0] - cur[-1][1] > 0.6 or re.search(r"[.!?]$", cur[-1][2])):
            groups.append(cur)
            cur = []
        cur.append(w)
    if cur:
        groups.append(cur)

    for g in groups:
        for i, (s, e, _) in enumerate(g):
            end = g[i + 1][0] if i + 1 < len(g) else e
            parts = []
            for j, (_, _, t) in enumerate(g):
                t = ass_escape(t.upper())
                parts.append(f"{{\\c{YELLOW}}}{t}{{\\c{WHITE}}}" if j == i else t)
            pop = "\\fscx108\\fscy108\\t(0,90,\\fscx100\\fscy100)" if i == 0 else ""
            ev.append(
                f"Dialogue: 0,{ass_time(s)},{ass_time(max(end, s + 0.08))},Sub,,0,0,0,,"
                f"{{\\an5\\pos(540,{sub_y}){pop}}}" + " ".join(parts)
            )
    return head + "\n".join(ev) + "\n"


# ------------------------------------------------------------------- render
def filter_graph(layout, face, ass_path):
    ass = ass_path.replace("\\", "/").replace(":", "\\:")
    fonts = FONTS.replace(":", "\\:")
    if layout == "face":
        x, y, w, h = face
        # recorte del juego centrado, pero apartado de la facecam si se pisan
        cw = f"(ih*{W}/{H - FACE_H})"
        if x > 0.5:      # facecam a la derecha -> el recorte termina donde empieza la cámara
            gx = f"min((iw-{cw})/2\\,iw*{x}-{cw})"
        elif x + w < 0.5:  # facecam a la izquierda
            gx = f"max((iw-{cw})/2\\,iw*{x + w})"
        else:
            gx = f"(iw-{cw})/2"
        return (
            f"[0:v]split=2[f][g];"
            f"[f]crop=w=iw*{w}:h=ih*{h}:x=iw*{x}:y=ih*{y},"
            f"scale={W}:{FACE_H}:force_original_aspect_ratio=increase,crop={W}:{FACE_H}[top];"
            f"[g]crop=w=ih*{W}/{H - FACE_H}:h=ih:x={gx},scale={W}:{H - FACE_H}[bot];"
            f"[top][bot]vstack=2,setsar=1,fps=30,ass='{ass}':fontsdir='{fonts}'[v]"
        )
    return (
        f"[0:v]split=2[a][b];"
        f"[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},boxblur=25:6,eq=brightness=-0.12[bg];"
        f"[b]scale={W}:-2[fg];"
        f"[bg][fg]overlay=0:(H-h)/2,setsar=1,fps=30,ass='{ass}':fontsdir='{fonts}'[v]"
    )


def render(src, pad, dur, layout, face, ass_path, out):
    run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-ss", f"{pad:.2f}", "-i", src, "-t", f"{dur:.2f}",
        "-filter_complex", filter_graph(layout, face, ass_path),
        "-map", "[v]", "-map", "0:a?",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-pix_fmt", "yuv420p",
        "-af", "loudnorm=I=-14:TP=-1.5:LRA=11",
        "-c:a", "aac", "-b:a", "128k", "-ar", "48000",
        "-movflags", "+faststart", out,
    ])


def download_section(vod_url, a, b, out):
    """Descarga SOLO los segmentos HLS del VOD que cubren [a, b] (segundos).
    Devuelve el segundo del VOD en el que empieza el archivo descargado."""
    import urllib.request
    m3u8 = subprocess.check_output(
        ["yt-dlp", "-g", "--no-warnings", "-f", "best[height<=1080]/best", vod_url],
        timeout=180,
    ).decode().strip().splitlines()[0]
    playlist = urllib.request.urlopen(m3u8, timeout=60).read().decode()
    base = m3u8.rsplit("/", 1)[0]
    segs, t, dur = [], 0.0, None
    for line in playlist.splitlines():
        line = line.strip()
        if line.startswith("#EXTINF:"):
            dur = float(line[8:].split(",")[0])
        elif line and not line.startswith("#") and dur is not None:
            segs.append((t, dur, line if line.startswith("http") else f"{base}/{line}"))
            t += dur
            dur = None
    sel = [s for s in segs if s[0] + s[1] > a and s[0] < b]
    if not sel:
        raise RuntimeError("tramo fuera del VOD")
    print(f"   descargando {len(sel)} segmentos ({sel[0][0]:.0f}s-{sel[-1][0] + sel[-1][1]:.0f}s)")
    with open(out, "wb") as f:
        for _, _, url in sel:
            for intento in range(3):
                try:
                    f.write(urllib.request.urlopen(url, timeout=60).read())
                    break
                except Exception:  # noqa: BLE001
                    if intento == 2:
                        raise
    return sel[0][0]


def grab_frame(src, out):
    try:
        run(["ffmpeg", "-y", "-loglevel", "error", "-ss", "2", "-i", src, "-frames:v", "1", "-q:v", "3", out])
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------- main
def main():
    with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as f:
        cfg = json.load(f)
    c = cfg["clips"]
    if os.environ.get("TEST_MAX_CLIPS"):  # modo prueba
        c["clips_per_channel"] = c["max_clips_per_channel"] = int(os.environ["TEST_MAX_CLIPS"])
    with open(os.path.join(ROOT, "data", "latest.json"), encoding="utf-8") as f:
        radar = json.load(f)

    faces = {ch["login"].lower(): ch.get("facecam") for ch in cfg["channels"]}
    day = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=1))).strftime("%Y-%m-%d")
    repo = os.environ.get("GITHUB_REPOSITORY", "USUARIO/clipping-radar")
    tag = f"clips-{day}"
    os.makedirs(OUT, exist_ok=True)
    os.makedirs(WORK, exist_ok=True)
    os.makedirs(os.path.join(ROOT, "data", "frames"), exist_ok=True)

    by_channel = {}
    for v in radar["vods"]:
        by_channel.setdefault(v["channel"], []).append(v)

    from faster_whisper import WhisperModel  # import tardío: solo si hay trabajo
    model = WhisperModel(c.get("whisper_model", "small"), device="cpu", compute_type="int8") if by_channel else None

    manifest = {"date": day, "release_tag": tag, "clips": []}
    for channel, vods in by_channel.items():
        picks = pick_for_channel(vods, c)
        print(f"== {channel}: {len(picks)} clips")
        face = faces.get(channel)
        layout = "face" if face else "blur"
        for n, k in enumerate(picks, 1):
            v = k["vod"]
            dur = k["end"] - k["start"]
            raw = os.path.join(WORK, f"{channel}_{n}.ts")
            words = []
            try:
                seg_start = download_section(v["vod_url"], k["start"], k["end"] + 1, raw)
                pad = k["start"] - seg_start
                got = float(subprocess.check_output(
                    ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", raw]
                ).decode().strip() or 0)
                print(f"   clip {n}: {dur:.0f}s (descargado {got:.0f}s)")
                if got > dur + 120:  # protección: nunca transcribir horas de directo
                    raise RuntimeError(f"descarga demasiado larga ({got:.0f}s)")
                if n == 1:
                    grab_frame(raw, os.path.join(ROOT, "data", "frames", f"{channel}.jpg"))
                words = transcribe(raw, v["lang"], model)
                ass = os.path.join(WORK, f"{channel}_{n}.ass")
                with open(ass, "w", encoding="utf-8") as f:
                    f.write(build_ass(words, pad, dur, layout, k["hook"], v["display_name"]))
                name = f"{day}_{channel}_{n:02d}_{k['type']}.mp4"
                out = os.path.join(OUT, name)
                render(raw, pad, dur, layout, face, ass, out)
            except Exception as e:  # noqa: BLE001
                print(f"   ! clip {n} falló: {e}")
                continue
            finally:
                if os.path.exists(raw):
                    os.remove(raw)
            text = " ".join(t for s, e, t in words if pad <= s <= pad + dur)
            manifest["clips"].append({
                "channel": channel,
                "display_name": v["display_name"],
                "lang": v["lang"],
                "n": n,
                "type": k["type"],
                "score": round(k["score"], 2),
                "source": k["source"],
                "hook": k["hook"],
                "duration": round(dur, 1),
                "vod_url": v["vod_url"],
                "vod_title": v["title"],
                "vod_time": f"{int(k['start'] // 3600)}:{int(k['start'] % 3600 // 60):02d}:{int(k['start'] % 60):02d}",
                "transcript": text,
                "file": name,
                "url": f"https://github.com/{repo}/releases/download/{tag}/{name}",
                "size_mb": round(os.path.getsize(out) / 1e6, 1),
            })

    with open(os.path.join(ROOT, "data", f"clips-{day}.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    with open(os.path.join(ROOT, "data", "clips-latest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    print(f"\nOK: {len(manifest['clips'])} clips en clips/  (release {tag})")
    with open(os.environ.get("GITHUB_ENV", os.devnull), "a") as f:
        f.write(f"CLIPS_TAG={tag}\nCLIPS_COUNT={len(manifest['clips'])}\n")


if __name__ == "__main__":
    sys.exit(main())
