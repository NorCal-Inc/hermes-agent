"""Shared cartoon-Erika puppet logic: cut-out, mouth chart sprites, per-pose mouth rigs, Rhubarb cues."""
import json, pathlib, subprocess, shutil
import numpy as np
from PIL import Image, ImageDraw, ImageFilter

SRC = pathlib.Path(__file__).resolve().parents[3] / ".hermes/assets/erika/cartoon-drafts-20261005"
SHAPES = "ABCDEFGHX"                 # Rhubarb shapes -> chart cells 1..9 (row-major)


def cutout_alpha(im):
    """Alpha mask that removes the flat cream background (flood fill from the borders)."""
    rgb = np.asarray(im.convert("RGB")).astype(int); h, w = rgb.shape[:2]
    bg = np.median(np.concatenate([rgb[0], rgb[-1], rgb[:, 0], rgb[:, -1]]), axis=0)
    near = np.abs(rgb - bg).sum(axis=2) < 60
    seen = np.zeros_like(near)
    stack = [(0, x) for x in range(w)] + [(h - 1, x) for x in range(w)] + [(y, 0) for y in range(h)] + [(y, w - 1) for y in range(h)]
    while stack:
        y, x = stack.pop()
        if seen[y, x] or not near[y, x]: continue
        seen[y, x] = True
        if y > 0: stack.append((y - 1, x))
        if y < h - 1: stack.append((y + 1, x))
        if x > 0: stack.append((y, x - 1))
        if x < w - 1: stack.append((y, x + 1))
    return Image.fromarray(np.where(seen, 0, 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(1.2))


class Puppet:
    def __init__(self, rig_path, height):
        self.rig = json.loads(pathlib.Path(rig_path).read_text())
        self.height = height
        self.chart = Image.open(SRC / "erika-mouth-chart.png").convert("RGB")
        self.mouths = self.rig["mouths"]
        self.rest = self.mouths[8]
        self.rest_w = self.rest["mouth_bbox"][2] - self.rest["mouth_bbox"][0]
        self.sprites = [self._mouth_sprite(m["cell"], m["mouth_bbox"]) for m in self.mouths]
        self._cache = {}

    def _mouth_sprite(self, cell, bb):
        x0, y0, x1, y1 = bb; pad = 18
        crop = self.chart.crop((x0 - pad, y0 - pad, x1 + pad, y1 + pad))
        a = np.asarray(crop).astype(int)
        skin = np.asarray(self.chart).astype(int)[cell[1] + int((cell[3] - cell[1]) * .2), (cell[0] + cell[2]) // 2]
        m = (np.abs(a - skin).sum(axis=2) > 70).astype(np.uint8) * 255
        ov = Image.new("L", crop.size, 0)           # oval: the crop corners hold chart-cell hair/background
        ImageDraw.Draw(ov).ellipse((pad - 4, pad - 2, crop.width - pad + 4, crop.height - pad + 2), fill=255)
        m = np.minimum(m, np.asarray(ov))
        mask = Image.fromarray(m).filter(ImageFilter.MaxFilter(5)).filter(ImageFilter.GaussianBlur(1.5))
        spr = crop.convert("RGBA"); spr.putalpha(mask)
        return spr, (x0 - pad, y0 - pad)

    def pose(self, name):
        """{shape: RGBA image at self.height} for one pose (cached)."""
        if name in self._cache: return self._cache[name]
        im = Image.open(SRC / f"{name}.png").convert("RGB"); a = np.asarray(im).astype(int)
        bx0, by0, bx1, by1 = self.rig["poses"][name]["mouth_bbox"]
        cx = (bx0 + bx1) / 2; top = by0; mw = bx1 - bx0
        # centre on the face: midpoint of the jaw outlines on a skin-only row just above the lips
        bright = a[top - 30].sum(axis=1); L = R = int(cx)
        while L > 0 and bright[L] > 240: L -= 1
        while R < len(bright) - 1 and bright[R] > 240: R += 1
        cx = (L + R) / 2
        skin = tuple(int(v) for v in np.median(a[top - 45:top - 20, int(cx) - 60:int(cx) + 60].reshape(-1, 3), axis=0))
        base = im.copy(); er = Image.new("L", im.size, 0)
        bb0 = self.rig["poses"]["erika-base-wide"]["mouth_bbox"]; ew = min(mw, (bb0[2] - bb0[0]) * 1.05)
        ImageDraw.Draw(er).ellipse((cx - ew * .52, top - 22, cx + ew * .52, min(by1 + 5, top + 150)), fill=255)
        base.paste(Image.new("RGB", im.size, skin), (0, 0), er.filter(ImageFilter.GaussianBlur(6)))
        alpha = cutout_alpha(im)
        s = mw / self.rest_w * 1.05; rc = self.rest["cell"]; rb = self.rest["mouth_bbox"]
        out = {}
        for k, (spr, (ox, oy)) in enumerate(self.sprites):
            m = self.mouths[k]; cell = m["cell"]; mb = m["mouth_bbox"]
            dx = ((mb[0] + mb[2]) / 2 - cell[0]) - ((rb[0] + rb[2]) / 2 - rc[0])
            dy = (mb[1] - cell[1]) - (rb[1] - rc[1])
            sp = spr.resize((int(spr.width * s), int(spr.height * s)), Image.LANCZOS)
            px = int(cx + dx * s - ((mb[0] + mb[2]) / 2 - ox) * s); py = int(top + dy * s - (mb[1] - oy) * s)
            fr = base.convert("RGBA"); fr.alpha_composite(sp, (px, py)); fr.putalpha(alpha)
            sc = self.height / fr.height
            out[SHAPES[k]] = fr.resize((int(fr.width * sc), self.height), Image.LANCZOS)
        self._cache[name] = out
        return out


def mouth_cues(audio, work, dialog=None):
    """Rhubarb mouth cues for an audio file (optionally guided by the spoken text)."""
    work = pathlib.Path(work); wav = work / "voice.wav"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(audio), "-ac", "1", "-ar", "16000", str(wav)], check=True)
    rh = shutil.which("rhubarb") or str(pathlib.Path(__file__).resolve().parents[3] / ".local/bin/rhubarb")
    cmd = [rh, "-q", "-f", "json", "--extendedShapes", "GHX"]
    if dialog: cmd += ["-d", str(dialog)]
    cues = json.loads(subprocess.run(cmd + [str(wav)], check=True, capture_output=True, text=True).stdout)["mouthCues"]
    (work / "cues.json").write_text(json.dumps(cues))
    return cues


def shape_at(cues, t):
    for c in cues:
        if c["start"] <= t < c["end"]: return c["value"]
    return "X"
