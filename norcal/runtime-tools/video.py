"""Render a cartoon-Erika explainer video (Professor Glitch-style layout, Erika's deep-blue brand).

usage: video.py <video_dir> <work_dir> <out.mp4>
<video_dir> holds voice.mp3, words.json, spoken.txt (from tts.py) and scenes/<key>.png.
Cues in words.json: "scene:key|headline 1|headline 2" and "pose:<pose name>", firing at the word's start.
"""
import sys, json, re, pathlib, subprocess
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageFilter
sys.path.insert(0, str(pathlib.Path(__file__).parent))
from puppet_lib import Puppet, mouth_cues, shape_at

VD, WORK, OUT = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]), pathlib.Path(sys.argv[3])
W, H, FPS = 1080, 1920, 30
CREAM, BLUE, NAVY = (245, 236, 220), (96, 165, 255), (12, 22, 52)
words = json.loads((VD / "words.json").read_text())
dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(VD / "voice.mp3")],
                           capture_output=True, text=True).stdout) + 0.6

FB = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
def font(n): return ImageFont.truetype(FB, n)
F_HEAD, F_CAP, F_TAG, F_CHIP, F_CHIPS = font(88), font(58), font(30), font(40), font(28)

# ---- timeline from cues
scenes, poses = [], []
for w in words:
    for c in w["cues"]:
        kind, _, val = c.partition(":")
        if kind == "scene":
            key, h1, h2 = (val.split("|") + ["", ""])[:3]; scenes.append((w["start"], key, h1, h2))
        elif kind == "pose":
            poses.append((w["start"], val))
scenes[0] = (0.0,) + scenes[0][1:]; poses[0] = (0.0, poses[0][1])
def active(lst, t):
    cur = lst[0]
    for x in lst:
        if t >= x[0]: cur = x
    return cur

# name chips: appear when the name is spoken, stay until the scene changes
CHIPS = {"Erika": ("runs the team", None), "Norman": ("infrastructure", (110, 140, 170)),
         "Olivia": ("business formations", (214, 168, 48)), "Linda": ("branding officer", (170, 90, 190)),
         "Nicole": ("government contracts", (60, 140, 220)), "Charles": ("logistics", (240, 140, 40)),
         "Lenny": ("knowledge base", (120, 118, 240)), "Atlas": ("writes the code", (176, 184, 196)),
         "Forge": ("machine, files, security", (232, 112, 48)), "Paris": ("brings in what's new", (240, 120, 100)),
         "Piper": ("files it all", (32, 170, 160))}
# per-video cast (2026-10-13): <video_dir>/cast.json {"Name": {"role": "...", "color": [r, g, b] or null, "face": "path"}}
# replaces the video-1 roster above; color null = round face chip from "face" (default cartoon Erika)
ERIKA_FACE = pathlib.Path(__file__).resolve().parents[3] / ".hermes/assets/erika/cartoon-drafts-20261005/erika-cartoon-draft1.png"
FACES = {}
if (VD / "cast.json").exists():
    cast = json.loads((VD / "cast.json").read_text())
    CHIPS = {n: (c["role"], tuple(c["color"]) if c.get("color") else None) for n, c in cast.items()}
    FACES = {n: pathlib.Path(c["face"]).expanduser() for n, c in cast.items() if c.get("face")}
chip_events = [(w["start"], re.sub(r"\W", "", w["word"])) for w in words if re.sub(r"\W", "", w["word"]) in CHIPS]
def face_crop(path):
    face = Image.open(path).convert("RGB")
    fw = face.width; return face.crop((int(fw * .22), int(face.height * .1), int(fw * .78), int(face.height * .1) + int(fw * .56)))

def chip_img(name):
    sub, col = CHIPS[name]
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    cw = int(max(probe.textlength(name, font=F_CHIP), probe.textlength(sub, font=F_CHIPS))) + 140   # fit the text
    im = Image.new("RGBA", (cw, 104), (0, 0, 0, 0)); d = ImageDraw.Draw(im)
    d.rounded_rectangle((0, 0, cw - 1, 103), 52, fill=(10, 18, 44, 235), outline=BLUE, width=3)
    if col is None:
        av = face_crop(FACES.get(name, ERIKA_FACE)).resize((84, 84)); m = Image.new("L", (84, 84), 0); ImageDraw.Draw(m).ellipse((0, 0, 83, 83), fill=255)
        im.paste(av, (10, 10), m)
    else:
        d.ellipse((10, 10, 94, 94), fill=col); d.text((52, 52), name[0], font=F_CHIP, fill=(255, 255, 255), anchor="mm")
    d.text((112, 30), name, font=F_CHIP, fill=CREAM, anchor="lm"); d.text((112, 74), sub, font=F_CHIPS, fill=BLUE, anchor="lm")
    return im
chip_cache = {n: chip_img(n) for n in CHIPS}

# officer cards (Christopher: "a little card that matches" each lead's job): <video_dir>/cards/<Name>.png
F_CARDN, F_CARDR = font(64), font(40)
def card_img(name, art):
    sub, col = CHIPS[name]; CW, CH = 640, 760
    im = Image.new("RGBA", (CW, CH), (0, 0, 0, 0)); d = ImageDraw.Draw(im)
    d.rounded_rectangle((0, 0, CW - 1, CH - 1), 40, fill=(10, 18, 44, 245), outline=col or BLUE, width=6)
    a = Image.open(art).convert("RGB").resize((560, 560), Image.LANCZOS)
    m = Image.new("L", (560, 560), 0); ImageDraw.Draw(m).rounded_rectangle((0, 0, 559, 559), 28, fill=255)
    im.paste(a, (40, 40), m)
    d.text((CW // 2, 652), name, font=F_CARDN, fill=CREAM, anchor="mm")
    d.text((CW // 2, 714), sub, font=F_CARDR, fill=col or BLUE, anchor="mm")
    return im
card_cache = {p.stem: card_img(p.stem, p) for p in sorted((VD / "cards").glob("*.png")) if p.stem in CHIPS}

# captions: 2-3 word chunks, break after punctuation
chunks, cur = [], []
for i, w in enumerate(words):
    cur.append(i)
    if len(cur) == 3 or re.search(r"[.,:?!]$", w["word"]) or (len(cur) == 2 and len(w["word"]) > 7):
        chunks.append(cur); cur = []
if cur: chunks.append(cur)

# ---- static layers
g = np.linspace(0, 1, H)[:, None]
bg = Image.fromarray(((np.array([8, 14, 36]) * (1 - g) + np.array([22, 52, 120]) * g)[:, None, :]
                      .repeat(W, 1)).astype(np.uint8).reshape(H, W, 3)).convert("RGBA")
tag = Image.new("RGBA", (260, 56), (0, 0, 0, 0)); td = ImageDraw.Draw(tag)
td.rounded_rectangle((0, 0, 259, 55), 28, fill=(255, 255, 255, 40), outline=(255, 255, 255, 120), width=2)
td.text((130, 28), "AI PRESENTER", font=F_TAG, fill=CREAM, anchor="mm")
SC_SIZE, SC_X, SC_Y = 880, 100, 430
scene_imgs = {}
def scene_img(key):
    if key not in scene_imgs:
        im = Image.open(VD / "scenes" / f"{key}.png").convert("RGB").resize((SC_SIZE, SC_SIZE), Image.LANCZOS)
        scene_imgs[key] = im
    return scene_imgs[key]
rmask = Image.new("L", (SC_SIZE, SC_SIZE), 0); ImageDraw.Draw(rmask).rounded_rectangle((0, 0, SC_SIZE - 1, SC_SIZE - 1), 44, fill=255)

def ease(x): x = max(0.0, min(1.0, x)); return 1 - (1 - x) ** 3

# headlines wider than the frame shrink to fit (2026-10-13: TripTracker 01 ran off both edges); ones that fit are unchanged
head_fonts = {}
def head_font(txt):
    if txt not in head_fonts:
        f, size = F_HEAD, 88; probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
        while probe.textlength(txt, font=f) > W - 80 and size > 48:
            size -= 2; f = font(size)
        head_fonts[txt] = f
    return head_fonts[txt]

puppet = Puppet(WORK / "rig.json", 820)
cues = mouth_cues(VD / "voice.mp3", WORK, VD / "spoken.txt")

ff = subprocess.Popen(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS),
                       "-i", "-", "-i", str(VD / "voice.mp3"), "-c:v", "libx264", "-preset", "medium", "-crf", "19",
                       "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-af", "apad", "-t", f"{dur:.2f}", str(OUT)],
                      stdin=subprocess.PIPE)   # pad audio to the video tail (was -shortest: cut the tail, broke the pipe)
n = int(dur * FPS)
for i in range(n):
    t = i / FPS
    fr = bg.copy(); d = ImageDraw.Draw(fr)
    s0, key, h1, h2 = active(scenes, t); since = t - s0
    nxt = [s for s in scenes if s[0] > s0]; s_end = nxt[0][0] if nxt else dur
    # scene image: fade/rise in, slow zoom
    a_in = ease(since / 0.35)
    z = 1.0 + 0.05 * min(1.0, since / max(0.1, s_end - s0))
    zs = int(SC_SIZE * z); im = scene_img(key).resize((zs, zs), Image.BILINEAR)
    off = (zs - SC_SIZE) // 2; im = im.crop((off, off, off + SC_SIZE, off + SC_SIZE)).convert("RGBA")
    m = rmask.point(lambda v: int(v * a_in)); fr.paste(im, (SC_X, SC_Y + int(30 * (1 - a_in))), m)
    # headline: line 1 cream, line 2 brand blue
    hl = Image.new("RGBA", (W, 300), (0, 0, 0, 0)); hd = ImageDraw.Draw(hl)
    hd.text((W // 2, 90), h1, font=head_font(h1), fill=CREAM, anchor="mm"); hd.text((W // 2, 190), h2, font=head_font(h2), fill=BLUE, anchor="mm")
    if a_in < 1: hl.putalpha(hl.split()[3].point(lambda v: int(v * a_in)))
    fr.alpha_composite(hl, (0, 140 + int(20 * (1 - a_in))))
    # officer cards: the most recently named lead gets a big card over the scene (pops in)
    cards_now = [(ts, nm) for (ts, nm) in chip_events if s0 <= ts <= t and nm in card_cache]
    if cards_now:
        ts, nm = cards_now[-1]; ca = ease((t - ts) / 0.3); cd = card_cache[nm]
        sc = 0.85 + 0.15 * ca; cw, chh = int(cd.width * sc), int(cd.height * sc)
        c2 = cd.resize((cw, chh), Image.BILINEAR)
        if ca < 1: c2.putalpha(c2.split()[3].point(lambda v: int(v * ca)))
        fr.alpha_composite(c2, (SC_X + (SC_SIZE - cw) // 2, SC_Y + (SC_SIZE - chh) // 2 - 20))
    # name chips spoken in this scene (people without a card)
    shown = [nm for (ts, nm) in chip_events if s0 <= ts <= t and nm not in card_cache]
    for k, nm in enumerate(dict.fromkeys(shown)):
        ts = next(ts for ts, x in chip_events if x == nm and ts >= s0)
        ca = ease((t - ts) / 0.25); ch = chip_cache[nm]
        cx = SC_X + SC_SIZE - ch.width - 20 - int(40 * (1 - ca)); cy = SC_Y + 30 + k * 118
        fr.alpha_composite(ch.copy() if ca >= 1 else Image.blend(Image.new("RGBA", ch.size, (0, 0, 0, 0)), ch, ca), (cx, cy))
    # Erika (bottom-left), pose by cue, mouth by Rhubarb
    spr = puppet.pose(active(poses, t)[1])[shape_at(cues, t)]
    fr.alpha_composite(spr, (-30, H - spr.height + 50))
    # caption pill (current word in brand blue)
    ci = next((c for c in chunks if words[c[0]]["start"] - 0.05 <= t <= words[c[-1]]["end"] + 0.25), None)
    if ci:
        parts = [(words[j]["word"], words[j]["start"] <= t) for j in ci]
        cur_j = max([j for j in ci if words[j]["start"] <= t] or [ci[0]])
        # pill sits right of Erika, shifted left from the first cut (Christopher: long phrases were cut off);
        # if a phrase is still wider than the space, shrink the font instead of running off-screen
        LEFT, RIGHT = 400, W - 36; fcap = F_CAP; size = 58
        txt = " ".join(p for p, _ in parts); tw = d.textlength(txt, font=fcap)
        while tw > RIGHT - LEFT - 56 and size > 36:
            size -= 2; fcap = font(size); tw = d.textlength(txt, font=fcap)
        x0 = 700 - tw / 2; x0 = max(LEFT + 28, min(RIGHT - 28 - tw, x0)); y = 1560
        d.rounded_rectangle((x0 - 28, y - 50, x0 + tw + 28, y + 50), 30, fill=(8, 14, 36, 225))
        x = x0
        for j in ci:
            wd = words[j]["word"]; colr = BLUE if j == cur_j else CREAM
            d.text((x, y), wd, font=fcap, fill=colr, anchor="lm"); x += d.textlength(wd + " ", font=fcap)
    fr.alpha_composite(tag, (40, 40))
    ff.stdin.write(fr.convert("RGB").tobytes())
ff.stdin.close(); ff.wait()
print(f"rendered {OUT} {dur:.1f}s, {len(scenes)} scenes, {len(poses)} pose cues, {len(chip_events)} name chips, {len(cues)} mouth cues")
