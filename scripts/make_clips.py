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
FACE_H = 844          # alto del panel de facecam (44 %, como el ejemplo; par)
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


def clean_hook(title, views=0):
    """Frase gancho solo si es clara: título de un clip con visitas y con palabras de verdad."""
    t = re.sub(r"\s+", " ", title or "").strip()
    t = re.sub(r"(?i)\b(clip|clipped by \w+)\b", "", t).strip(" -|")
    words = re.findall(r"[A-Za-zÁÉÍÓÚÑáéíóúñü']{2,}", t)
    if views < 50 or len(words) < 2 or len(t) < 10 or "@" in t or "http" in t:
        return ""
    return t[:70]


# ---------------------------------------------------------- selección de momentos
def candidates_for_vod(v, cfg):
    """Momentos candidatos: picos de risa/hype del chat y clips hechos por los espectadores.
    Puntuación: chat normalizado (0-6) + visitas de los clips (lo que más pesa)."""
    cands = []
    for p in v.get("chat_peaks", []):
        peak = p.get("peak", (p["start"] + p["end"]) / 2)
        chat = min(p["score"], 15) / 15 * 6 + p.get("laugh_ratio", 0) * 3
        cands.append({
            "start": max(0, peak - 25), "end": peak + 18, "peak": peak, "score": chat,
            "type": p["type"], "hook": "", "source": "chat", "views": 0,
        })
    for c in v.get("viewer_clips", []):
        if c["views"] < cfg["min_clip_views"]:
            continue
        a = max(0, c["vod_offset"] - 8)
        b = c["vod_offset"] + c["duration"] + 3
        vscore = 4 * math.log10(c["views"] + 1)
        merged = False
        for k in cands:
            if a < k["end"] and b > k["start"]:
                k["score"] += vscore
                k["views"] += c["views"]
                if c["views"] > k.get("own_views", 0):
                    k["own_views"] = c["views"]
                    k["peak"] = c["vod_offset"] + c["duration"] / 2
                k["start"], k["end"] = min(k["start"], a), max(k["end"], b)
                k["hook"] = k["hook"] or clean_hook(c["title"], c["views"])
                k["source"] = "chat+clips" if k["source"] != "clips" else "clips"
                merged = True
                break
        if not merged:
            cands.append({
                "start": a, "end": b, "peak": c["vod_offset"] + c["duration"] / 2,
                "score": vscore, "type": "hype", "hook": clean_hook(c["title"], c["views"]),
                "source": "clips", "views": c["views"], "own_views": c["views"],
            })
    # juntar candidatos que se solapan o están pegados (mismo momento contado dos veces)
    cands.sort(key=lambda k: k["start"])
    merged_list = []
    for k in cands:
        last = merged_list[-1] if merged_list else None
        if last and k["start"] <= last["end"] + 5:
            # centrar en lo que más gente clipeó; si nadie lo clipeó, en el pico del chat
            best = max((last, k), key=lambda x: (x.get("own_views", 0), x["score"]))
            last["peak"] = best["peak"]
            last["own_views"] = best.get("own_views", 0)
            last["end"] = max(last["end"], k["end"])
            last["score"] += k["score"]
            last["views"] += k["views"]
            last["hook"] = last["hook"] or k["hook"]
            if k["source"] != last["source"]:
                last["source"] = "chat+clips"
        else:
            merged_list.append(dict(k))
    L = v["length_seconds"]
    for k in merged_list:
        fit_window(k, cfg, L)
        k["vod"] = v
    return merged_list


def fit_window(k, cfg, L):
    """Duración entre min y max SIN perder el pico (el momento que hizo reír)."""
    if k["end"] - k["start"] > cfg["max_seconds"]:
        k["start"] = max(k["start"], k["peak"] - cfg["max_seconds"] * 0.6)
        k["end"] = k["start"] + cfg["max_seconds"]
    d = k["end"] - k["start"]
    if d < cfg["min_seconds"]:
        pad = (cfg["min_seconds"] - d) / 2
        k["start"] -= pad
        k["end"] += pad
    k["start"] = max(0, k["start"])
    k["end"] = min(L, k["end"])


def audio_reaction(vod_url, k):
    """Reacción del streamer: descarga solo el AUDIO del tramo y mide subidas bruscas de
    volumen (gritos, carcajadas). Devuelve (dB por encima de lo normal, segundo del VOD)."""
    import numpy as np
    tmp = os.path.join(WORK, "audio_probe.m4a")
    try:
        a, b = max(0, k["start"] - 10), k["end"] + 5
        seg0 = download_section(vod_url, a, b, tmp, fmt="audio_only/bestaudio/worst")
        pcm = subprocess.check_output(
            ["ffmpeg", "-loglevel", "error", "-i", tmp, "-ac", "1", "-ar", "8000", "-f", "s16le", "-"],
            timeout=120)
        x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
        win = 4000  # 0,5 s
        n = len(x) // win
        if n < 6:
            return 0.0, None
        rms = np.sqrt((x[: n * win].reshape(n, win) ** 2).mean(axis=1) + 1e-9)
        db = np.maximum(20 * np.log10(rms + 1e-9), 20.0)  # suelo: el silencio no cuenta como "base"
        base = float(np.percentile(db, 30))
        if base <= 21:  # tramo casi mudo (silenciado por copyright): no hay reacción medible
            return 0.0, None
        # pico sostenido (1,5 s) para no reaccionar a un golpe suelto
        sm = np.convolve(db, np.ones(3) / 3, mode="same")
        j = int(np.argmax(sm))
        return float(min(sm[j] - base, 25.0)), seg0 + j * 0.5
    except Exception as e:  # noqa: BLE001
        print(f"   (audio no disponible: {e})")
        return 0.0, None
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def pick_for_channel(vods, cfg):
    cands = [c for v in vods for c in candidates_for_vod(v, cfg)]
    cands.sort(key=lambda c: c["score"], reverse=True)
    # quitar solapes, quedarse con los mejores candidatos
    pool = []
    for c in cands:
        if any(c["vod"]["vod_id"] == o["vod"]["vod_id"] and c["start"] < o["end"] and c["end"] > o["start"] for o in pool):
            continue
        pool.append(c)
        if len(pool) >= cfg["max_clips_per_channel"] * 3:
            break
    # reacción del streamer: escuchar el audio de cada candidato
    for c in pool:
        boost, t = audio_reaction(c["vod"]["vod_url"], c)
        c["reaction_db"] = round(boost, 1)
        c["score"] += max(0.0, boost - 6) * 0.6   # +0,6 por cada dB por encima de 6 dB
        if t is not None and boost >= 8 and c["source"] == "chat":
            # centrar el clip en el grito/carcajada: ~20 s antes y ~10 s después
            c["peak"] = t
            c["start"], c["end"] = max(0, t - 20), t + 12
            fit_window(c, cfg, c["vod"]["length_seconds"])
        print(f"   candidato {c['start']:.0f}s score={c['score']:.1f} reacción={boost:.1f}dB views={c['views']} [{c['source']}]")
    pool.sort(key=lambda c: c["score"], reverse=True)
    chosen = []
    for c in pool:
        if c["score"] < cfg["min_score"]:
            break
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
    sub_y = 1440 if layout == "blur" else 1500
    hook_y = 520 if layout == "blur" else FACE_H + 30

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
def filter_graph(layout, face, ass_path, game_x=None):
    ass = ass_path.replace("\\", "/").replace(":", "\\:")
    fonts = FONTS.replace(":", "\\:")
    if layout == "face":
        x, y, w, h = face
        # recorte del juego centrado, pero apartado de la facecam si se pisan
        cw = f"(ih*{W}/{H - FACE_H})"
        if game_x is not None:  # el canal tiene cámara+chat a un lado: el juego empieza aquí
            gx = f"max((iw-{cw})/2\\,iw*{game_x})" if game_x < 0.5 else f"min((iw-{cw})/2\\,iw*{game_x}-{cw})"
        elif x > 0.5:      # facecam a la derecha -> el recorte termina donde empieza la cámara
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


def render(src, pad, dur, layout, face, ass_path, out, game_x=None):
    run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-ss", f"{pad:.2f}", "-i", src, "-t", f"{dur:.2f}",
        "-filter_complex", filter_graph(layout, face, ass_path, game_x),
        "-map", "[v]", "-map", "0:a?",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-pix_fmt", "yuv420p",
        "-af", "loudnorm=I=-14:TP=-1.5:LRA=11",
        "-c:a", "aac", "-b:a", "128k", "-ar", "48000",
        "-movflags", "+faststart", out,
    ])


def detect_facecam(src, start, dur):
    """Busca la cara del streamer en varios fotogramas, localiza los bordes rectos del
    recuadro de su cámara y devuelve (x, y, w, h) en fracciones con el aspecto del panel
    de arriba. None si no hay una cámara clara."""
    try:
        import cv2
        import numpy as np
    except ImportError:
        return None
    cas = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    cap = cv2.VideoCapture(src)
    found, grads, size = [], [], None
    for i in range(8):
        cap.set(cv2.CAP_PROP_POS_MSEC, (start + 1 + i * max(dur - 2, 1) / 7) * 1000)
        ok, frame = cap.read()
        if not ok:
            continue
        hh, ww = frame.shape[:2]
        size = (ww, hh)
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        grads.append((np.abs(cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)),
                      np.abs(cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3))))
        faces = cas.detectMultiScale(gray, 1.1, 6, minSize=(int(ww * 0.035), int(ww * 0.035)))
        if len(faces):
            x, y, w, h = max(faces, key=lambda f: f[2] * f[3])
            found.append((x + w / 2, y + h / 2, w))
    cap.release()
    if len(found) < 3 or not size:
        return None
    ww, hh = size
    cx = sorted(f[0] for f in found)[len(found) // 2]
    cy = sorted(f[1] for f in found)[len(found) // 2]
    fw = sorted(f[2] for f in found)[len(found) // 2]
    near = [f for f in found if abs(f[0] - cx) < 0.06 * ww and abs(f[1] - cy) < 0.08 * hh]
    if len(near) < 3:  # caras sueltas del juego, no una cámara fija
        return None

    # bordes del recuadro: la primera línea recta y LARGA (que cruza casi todo el recuadro)
    # que aparece al alejarse de la cara. Los bordes del fondo de la cámara son más cortos.
    gx = np.median(np.stack([g[0] for g in grads]), axis=0)
    gy = np.median(np.stack([g[1] for g in grads]), axis=0)
    thr = 40.0

    def first_line(profile_fn, start_i, stop_i, step, default):
        i = int(start_i)
        stop_i = int(stop_i)
        while (i >= stop_i if step < 0 else i <= stop_i):
            if profile_fn(i) >= 0.8:
                return i
            i += step
        return default

    xa, xb = int(max(cx - 1.3 * fw, 0)), int(min(cx + 1.3 * fw, ww))
    ya, yb = int(max(cy - 0.9 * fw, 0)), int(min(cy + 0.9 * fw, hh))

    def row_frac(r):  # fracción de la franja horizontal con borde fuerte
        r = min(max(r, 0), hh - 1)
        return float((gy[max(r - 1, 0):r + 2, xa:xb].max(axis=0) > thr).mean())

    def col_frac(c):
        c = min(max(c, 0), ww - 1)
        return float((gx[ya:yb, max(c - 1, 0):c + 2].max(axis=1) > thr).mean())

    top = first_line(row_frac, cy - 0.6 * fw, max(cy - 3 * fw, 0), -1, 0)
    bottom = first_line(row_frac, cy + 0.6 * fw, min(cy + 3.5 * fw, hh - 1), 1, hh)
    left = first_line(col_frac, cx - 0.6 * fw, max(cx - 3.5 * fw, 0), -1, 0)
    right = first_line(col_frac, cx + 0.6 * fw, min(cx + 3.5 * fw, ww - 1), 1, ww)
    # pequeño margen hacia dentro para no coger el marco
    m = 3
    left, right, top, bottom = left + m, right - m, top + m, bottom - m
    cw, ch = right - left, bottom - top
    if cw < fw * 1.2 or ch < fw * 1.2:
        return None

    aspect = W / FACE_H  # ancho/alto del panel en píxeles
    if cw / ch > aspect:  # recuadro más ancho que el panel: recortar a lo ancho, centrado en la cara
        bw, bh = ch * aspect, ch
        x0 = min(max(cx - bw / 2, left), right - bw)
        y0 = top
    else:                 # más alto: recortar a lo alto, con la cara arriba
        bw, bh = cw, cw / aspect
        x0 = left
        y0 = min(max(cy - bh * 0.42, top), bottom - bh)
    return (round(x0 / ww, 4), round(y0 / hh, 4), round(bw / ww, 4), round(bh / hh, 4))


def download_section(vod_url, a, b, out, fmt="best[height<=1080]/best"):
    """Descarga SOLO los segmentos HLS del VOD que cubren [a, b] (segundos).
    Devuelve el segundo del VOD en el que empieza el archivo descargado."""
    import urllib.request
    m3u8 = subprocess.check_output(
        ["yt-dlp", "-g", "--no-warnings", "-f", fmt, vod_url],
        timeout=180,
    ).decode().strip().splitlines()[0]
    playlist = urllib.request.urlopen(m3u8, timeout=60).read().decode()
    base = m3u8.rsplit("/", 1)[0]
    segs, t, dur, init = [], 0.0, None, None
    for line in playlist.splitlines():
        line = line.strip()
        if line.startswith("#EXT-X-MAP:"):  # fMP4: cabecera de inicio obligatoria
            uri = line.split('URI="', 1)[1].split('"', 1)[0]
            init = uri if uri.startswith("http") else f"{base}/{uri}"
        elif line.startswith("#EXTINF:"):
            dur = float(line[8:].split(",")[0])
        elif line and not line.startswith("#") and dur is not None:
            segs.append((t, dur, line if line.startswith("http") else f"{base}/{line}"))
            t += dur
            dur = None
    sel = [s for s in segs if s[0] + s[1] > a and s[0] < b]
    if not sel:
        raise RuntimeError("tramo fuera del VOD")
    print(f"   descargando {len(sel)} segmentos ({sel[0][0]:.0f}s-{sel[-1][0] + sel[-1][1]:.0f}s)")
    import urllib.error

    def fetch(url):
        for intento in range(3):
            try:
                return urllib.request.urlopen(url, timeout=60).read()
            except urllib.error.HTTPError as e:
                # trozos silenciados por copyright: Twitch los sirve como "-muted"
                if e.code in (403, 404) and "-unmuted" in url:
                    url = url.replace("-unmuted", "-muted")
                    continue
                if intento == 2:
                    raise
            except Exception:  # noqa: BLE001
                if intento == 2:
                    raise
        raise RuntimeError(f"no se pudo descargar {url.split('?')[0][-60:]}")

    part = out + ".part"
    with open(part, "wb") as f:
        if init:
            f.write(fetch(init))
        for _, _, url in sel:
            f.write(fetch(url))
    # remux: timestamps desde 0 (Twitch guarda la hora original del directo)
    r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", part, "-map", "0", "-c", "copy",
                        "-avoid_negative_ts", "make_zero", out], capture_output=True, text=True, timeout=300)
    ok = r.returncode == 0 and subprocess.run(
        ["ffprobe", "-v", "error", out], capture_output=True).returncode == 0
    if not ok:  # diagnóstico para poder arreglarlo
        with open(part, "rb") as f:
            head = f.read(48)
        sample = [ln.split("?")[0][:90] for ln in playlist.splitlines()[:12]]
        print(f"   DIAG playlist: {sample}")
        print(f"   DIAG part={os.path.getsize(part)}B head={head[:48]!r} init={'sí' if init else 'no'}")
        print(f"   DIAG ffmpeg: {r.stderr.strip()[:300]}")
        os.remove(part)
        raise RuntimeError("el tramo descargado no es un vídeo válido (ver DIAG)")
    os.remove(part)
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
    game_xs = {ch["login"].lower(): ch.get("game_x") for ch in cfg["channels"]}
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
    need_subs = any(v["lang"] == "en" for v in radar["vods"])
    model = WhisperModel(c.get("whisper_model", "small"), device="cpu", compute_type="int8") if need_subs else None

    manifest = {"date": day, "release_tag": tag, "clips": []}
    for channel, vods in by_channel.items():
        picks = pick_for_channel(vods, c)
        print(f"== {channel}: {len(picks)} clips")
        for n, k in enumerate(picks, 1):
            v = k["vod"]
            dur = k["end"] - k["start"]
            raw = os.path.join(WORK, f"{channel}_{n}.mp4")
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
                face = faces.get(channel) or detect_facecam(raw, pad, dur)
                layout = "face" if face else "blur"
                print(f"   facecam: {face or 'no detectada -> fondo desenfocado'}")
                # subtítulos solo en los canales en inglés
                words = transcribe(raw, v["lang"], model) if v["lang"] == "en" else []
                ass = os.path.join(WORK, f"{channel}_{n}.ass")
                with open(ass, "w", encoding="utf-8") as f:
                    f.write(build_ass(words, pad, dur, layout, k["hook"], v["display_name"]))
                name = f"{day}_{channel}_{n:02d}_{k['type']}.mp4"
                out = os.path.join(OUT, name)
                render(raw, pad, dur, layout, face, ass, out, game_xs.get(channel) if faces.get(channel) else None)
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
                "layout": layout,
                "views": k.get("views", 0),
                "reaction_db": k.get("reaction_db", 0),
                "facecam": face,
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
