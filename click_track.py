#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
click_track.py — creates a click track (metronome WAV) aligned on each backing track.

  python click_track.py <files or folders> [options]   (run without arguments for the help)

For every audio file (WAV, MP3, FLAC, M4A, OGG, AIFF...):
  1. detects the tempo and fits a beat grid on the attacks, to a fraction of a BPM,
     with no drift over the whole song; songs whose tempo varies are followed beat by beat;
  2. puts a beat on the start of the music, even on syncopated or shuffle songs, and
     accents the first beat of every bar (the very first click is the "one");
  3. writes "<name>_click.wav", EXACTLY as long as the backing track, with silence before
     the first beat: drop it at the very start of a track in your DAW and it is in sync.

Existing click files are never overwritten (use --overwrite to recreate them).
Per-song settings are kept in "click_settings.txt", next to the songs: it is created on the
first run (one line per song, with the detected tempo) and can be edited by hand.

Requirements: Python 3.9+, numpy, librosa, soundfile (see requirements.txt).

Copyright (C) 2026 Philippe Thomas
This program is free software: you can redistribute it and/or modify it under the terms of
the GNU General Public License as published by the Free Software Foundation, either
version 3 of the License, or (at your option) any later version. It is distributed WITHOUT
ANY WARRANTY; see the GNU General Public License (LICENSE file) for more details.
"""

__version__ = "1.0.0"

import argparse
import locale
import os
import sys
from pathlib import Path


# =========================================================================== LANGUAGE
def _detect_lang():
    """Message language: --fr / --en, else CLICK_LANG, else the Windows / system language."""
    if "--fr" in sys.argv[1:]:
        return "fr"
    if "--en" in sys.argv[1:]:
        return "en"
    env = os.environ.get("CLICK_LANG", "").lower()
    if env[:2] in ("fr", "en"):
        return env[:2]
    if os.name == "nt":
        try:
            import ctypes
            lang_id = ctypes.windll.kernel32.GetUserDefaultUILanguage()
            return "fr" if lang_id & 0x3FF == 0x0C else "en"
        except Exception:
            pass
    for var in ("LC_ALL", "LC_MESSAGES", "LANG"):
        if os.environ.get(var):
            return "fr" if os.environ[var].lower().startswith("fr") else "en"
    try:
        return "fr" if (locale.getlocale()[0] or "").lower().startswith("fr") else "en"
    except Exception:
        return "en"


LANG = _detect_lang()


def T(fr, en):
    """Text in the display language."""
    return fr if LANG == "fr" else en


try:
    import numpy as np
    import librosa
    import soundfile as sf
except ImportError:
    sys.exit(T("Il manque des bibliothèques : pip install -r requirements.txt",
               "Missing libraries: pip install -r requirements.txt"))
except KeyboardInterrupt:          # Ctrl-C while librosa is loading
    print(T("\nInterrompu (Ctrl-C).", "\nInterrupted (Ctrl-C)."))
    sys.exit(130)


# =========================================================================== FILES
AUDIO_EXT = {".wav", ".mp3", ".flac", ".m4a", ".aac", ".ogg", ".aif", ".aiff"}
SETTINGS_NAME = "click_settings.txt"     # per-song settings, next to the songs
OUT_NAME = "clicks"                      # output folder, next to the songs
LEGACY_SETTINGS = "reglages.txt"         # names used by earlier versions, still read
LEGACY_OUT = "clics"


def settings_path(folder, args):
    """Settings file of a folder (an existing file with the legacy name is kept)."""
    if args.settings:
        return Path(args.settings)
    new, old = folder / SETTINGS_NAME, folder / LEGACY_SETTINGS
    return old if old.is_file() and not new.is_file() else new


def output_dir(folder, args):
    """Output folder (an existing folder with the legacy name is kept)."""
    if args.output_dir:
        return Path(args.output_dir)
    new, old = folder / OUT_NAME, folder / LEGACY_OUT
    return old if old.is_dir() and not new.is_dir() else new


# ----------------------------------------------------------- Safe writing ----
def write_wav(path, data, sr, subtype):
    """
    Writes to a temporary file first, then renames it: if the script is
    interrupted (Ctrl-C), no half-written WAV is left on disk.
    """
    path = Path(path)
    tmp = path.with_name(path.stem + ".partial.wav")
    try:
        sf.write(str(tmp), data, sr, subtype=subtype, format="WAV")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


# ----------------------------------------------------------------- Clicks ----
def make_click(sr, freq, duration=0.035, gain=1.0):
    """Short sine 'beep' with a sharp attack and a fast decay."""
    n = int(sr * duration)
    t = np.arange(n) / sr
    env = np.exp(-t * 90.0)                         # exponential decay
    attack = min(int(sr * 0.001), n)                # 1 ms fade-in, no pop
    env[:attack] *= np.linspace(0, 1, attack)
    return (gain * np.sin(2 * np.pi * freq * t) * env).astype(np.float32)


# =============================================================== ANALYSIS
# Backing tracks are usually played or generated at a steady tempo: rather than
# following beats one by one (which can "jump" from one pulse to another), we look
# for THE regular grid that best fits the attacks, then refine it to a fraction of
# a BPM with a regression over the whole song -> no drift.

RATIOS = {1: "", 2/3: "×2/3", 3/4: "×3/4", 4/3: "×4/3", 3/2: "×3/2"}


def onset_env(y, sr, hop):
    """Onset strength curve, computed on the percussive part of the signal."""
    yp = librosa.effects.percussive(y, margin=2.0)
    return librosa.onset.onset_strength(y=yp, sr=sr, hop_length=hop)


def fold_score(env, fps, bpm, nb=48):
    """Folds the onset curve over one period: a sharp peak = a good grid."""
    period = fps * 60.0 / bpm
    b = ((np.arange(len(env)) % period) / period * nb).astype(int) % nb
    h = np.bincount(b, weights=env, minlength=nb) / np.maximum(
        np.bincount(b, minlength=nb), 1)
    return h.max() / (h.mean() + 1e-9)


def best_near(env, fps, bpm, spread=0.04, step=0.02):
    grid = np.arange(bpm * (1 - spread), bpm * (1 + spread), step)
    scores = [fold_score(env, fps, b) for b in grid]
    i = int(np.argmax(scores))
    return float(grid[i]), float(scores[i])


def choose_tempo(y, sr, env, fps, bpm_forced=None, bpm_hint=None):
    """Returns (chosen bpm, list of candidates [(bpm, score, label)])."""
    if bpm_forced:
        b, s = best_near(env, fps, bpm_forced, spread=0.01, step=0.005)
        return b, [(b, s, "forced")]
    kwargs = {"start_bpm": bpm_hint} if bpm_hint else {}
    t0, _ = librosa.beat.beat_track(y=y, sr=sr, **kwargs)
    t0 = float(np.atleast_1d(t0)[0])
    cands = []
    for r, lab in RATIOS.items():
        b, s = best_near(env, fps, t0 * r)
        if 50 <= b <= 220:
            cands.append((b, s, lab))
    ref = cands[0]
    # The detected pulse is only abandoned if a "triplet" or "syncopated"
    # pulse (3:2, 4:3 ratio...) fits clearly better.
    best = max(cands, key=lambda c: c[1])
    chosen = best if best[1] > ref[1] * 1.05 else ref
    return chosen[0], sorted(cands, key=lambda c: -c[1])


def fit_grid(env, fps, bpm, n_frames):
    """
    Fits the grid: initial phase, then a linear regression on the attacks
    actually present (to about 1.5 ms). Returns (period s, t0 s).
    """
    period = 60.0 / bpm
    P = period * fps
    nb = 96
    b = ((np.arange(len(env)) % P) / P * nb).astype(int) % nb
    h = np.bincount(b, weights=env, minlength=nb) / np.maximum(
        np.bincount(b, minlength=nb), 1)
    t0 = (np.argmax(h) + 0.5) / nb * period

    for _ in range(3):  # a few refinement passes
        idx, times, w = [], [], []
        k_max = int((n_frames / fps - t0) / period)
        win = max(1, int(P / 8))
        for k in range(k_max + 1):
            c = int(round((t0 + k * period) * fps))
            lo, hi = max(0, c - win), min(len(env), c + win + 1)
            if hi <= lo:
                continue
            j = lo + int(np.argmax(env[lo:hi]))
            idx.append(k); times.append(j / fps); w.append(env[j])
        idx, times, w = map(np.array, (idx, times, w))
        keep = w > np.percentile(w, 50)          # clear attacks only
        if keep.sum() < 8:
            break
        A = np.vstack([idx[keep], np.ones(keep.sum())]).T
        (period, t0), *_ = np.linalg.lstsq(A, times[keep], rcond=None)
    while t0 - period >= 0:   # bring t0 back to the first possible beat of the file
        t0 -= period
    return float(period), float(t0)


def track_adaptive(env, fps, period, t0, t_end):
    """
    Flexible tracking, for songs whose tempo varies (speeding up, slowing
    down): moves forward beat by beat, snapping each beat to the nearest
    attack within a narrow window so it never jumps to another pulse, then
    smooths the result.
    """
    win = 0.12 * period
    thr = np.percentile(env, 75)
    ks, obs = [], []
    k, t_pred = 0, t0
    hist_k, hist_t = [], []
    while t_pred <= t_end:
        lo = max(0, int((t_pred - win) * fps))
        hi = min(len(env), int((t_pred + win) * fps) + 1)
        if hi > lo:
            j = lo + int(np.argmax(env[lo:hi]))
            if env[j] > thr:
                ks.append(k); obs.append(j / fps)
                hist_k.append(k); hist_t.append(j / fps)
        k += 1
        # prediction of the next beat: line fitted on the last 16 beats
        if len(hist_k) >= 4:
            a, b = np.polyfit(hist_k[-16:], hist_t[-16:], 1)
            t_pred = a * k + b
        else:
            t_pred = t0 + k * period
    n = k
    ks, obs = np.array(ks), np.array(obs)
    if len(ks) < 8:
        return None
    # smoothing: local regression (±8 beats) for each beat
    out = np.empty(n)
    for i in range(n):
        m = np.abs(ks - i) <= 8
        if m.sum() < 4:
            m = np.argsort(np.abs(ks - i))[:8]
        a, b = np.polyfit(ks[m], obs[m], 1)
        out[i] = a * i + b
    return out


def start_shift(ya, sr, env, fps, t0, period):
    """
    Backing tracks start on a beat, as soon as the music becomes audible. On
    syncopated or shuffle songs, the grid can lock onto the off-beats (1/2) or
    onto a shuffle note (1/3, 2/3). Among the possible positions that match
    regular attacks in the song, keeps the one that falls on the start of the
    music. Returns (fraction of a beat to add, start of the music).
    """
    hop = 64
    db = librosa.amplitude_to_db(
        librosa.feature.rms(y=ya, hop_length=hop)[0], ref=np.max)
    loud = np.where(db > -45)[0]
    if len(loud) == 0:
        return 0.0, None
    t_start = loud[0] * hop / sr

    # onset profile folded over one beat (24 bins), relative to t0
    nb = 24
    ph = ((np.arange(len(env)) / fps - t0) / period) % 1.0
    b = (ph * nb).astype(int) % nb
    prof = np.bincount(b, weights=env, minlength=nb) / np.maximum(
        np.bincount(b, minlength=nb), 1)
    prof = (prof - prof.min()) / (prof.max() - prof.min() + 1e-9)

    def strength(f):           # presence of attacks around position f
        c = int(round(f * nb)) % nb
        return max(prof[(c - 1) % nb], prof[c], prof[(c + 1) % nb])

    def gap(f):                # distance (in beats) between the start and the grid
        g = ((t_start - t0) / period - f + 0.5) % 1.0 - 0.5
        return abs(g + 0.07)   # the audible start comes ~0.07 beat before the beat

    cands = [f for f in (0, 1 / 4, 1 / 3, 1 / 2, 2 / 3, 3 / 4)
             if f == 0 or strength(f) >= 0.5]
    best = min(cands, key=gap)
    if best and gap(best) < 0.15 and gap(best) < gap(0) - 0.05:
        return best, t_start
    return 0.0, t_start


def parse_time(text):
    """'29', '29.35', '00:29', '0:29,350', '1:05.3' -> seconds."""
    bad = T(f"« {text} » n'est pas un instant (ex. 29.35 ou 00:29.350)",
            f'"{text}" is not a time (e.g. 29.35 or 00:29.350)')
    t = text.strip().replace(",", ".")
    try:
        parts = [float(p) for p in t.split(":")]
    except ValueError:
        raise argparse.ArgumentTypeError(bad)
    if len(parts) > 3 or any(p < 0 for p in parts):
        raise argparse.ArgumentTypeError(bad)
    sec = 0.0
    for p in parts:            # h:m:s, m:s or s
        sec = sec * 60 + p
    return sec


def parse_fraction(text):
    """'1/2', '-1/3', '0.5' -> float (fraction of a beat)."""
    try:
        if "/" in text:
            a, b = text.split("/", 1)
            v = float(a) / float(b)
        else:
            v = float(text)
    except (ValueError, ZeroDivisionError):
        raise argparse.ArgumentTypeError(
            T(f"« {text} » n'est pas une fraction (ex. 1/2, -1/3, 0)",
              f'"{text}" is not a fraction (e.g. 1/2, -1/3, 0)'))
    if not -1 < v < 1:
        raise argparse.ArgumentTypeError(
            T(f"« {text} » : le décalage doit être compris entre -1 et 1 temps",
              f'"{text}": the shift must be between -1 and 1 beat'))
    return v


def music_bounds(y, sr, thresh_db=-45):
    """Start and end of the audible part (no clicks in the silence)."""
    rms = librosa.feature.rms(y=y, hop_length=512)[0]
    db = librosa.amplitude_to_db(rms, ref=np.max)
    on = np.where(db > thresh_db)[0]
    if len(on) == 0:
        return 0.0, len(y) / sr
    return on[0] * 512 / sr, (on[-1] + 1) * 512 / sr


def guess_downbeat(y, sr, beat_samples, beats_per_bar, verbose=False):
    """
    Finds the "one" of the bar (--accent-auto). Main clue: chord changes,
    which almost always fall on the first beat. Secondary clue: the kick
    drum. Not foolproof -> --accent-offset.
    """
    n = beats_per_bar
    if len(beat_samples) < n * 4:
        return 0
    hop = 512
    yh = librosa.effects.harmonic(y, margin=2.0)
    chroma = librosa.feature.chroma_cqt(y=yh, sr=sr, hop_length=hop)
    frames = np.clip(beat_samples // hop, 0, chroma.shape[1] - 1)
    sync = librosa.util.sync(chroma, frames, aggregate=np.median)
    sync = sync / (np.linalg.norm(sync, axis=0, keepdims=True) + 1e-9)
    # harmonic novelty when beat i starts (compared with the previous beat);
    # sync[:, i] covers the interval [beat i-1, beat i)
    nov = 1 - np.sum(sync[:, 1:] * sync[:, :-1], axis=0)
    nov = nov[: len(beat_samples)]   # nov[i] = change at the start of beat i
    harm = np.array([nov[p::n].mean() for p in range(n)])

    onset = librosa.onset.onset_strength(y=y, sr=sr, fmax=250, n_mels=12)
    f2 = np.clip(librosa.samples_to_frames(beat_samples), 0, len(onset) - 1)
    kick = np.array([onset[f2][p::n].mean() for p in range(n)])

    def rel(v):  # deviation from the mean
        return (v - v.mean()) / (v.std() + 1e-9)
    score = rel(harm) * 2 + rel(kick)
    if verbose:
        print("     harmony", np.round(harm, 3), "bass", np.round(kick, 2))
    return int(np.argmax(score))


# =============================================================== PROCESSING
def process(path, out_dir, args):
    """Creates the click track of one song. Returns (bpm, alternatives) or None."""
    out = out_dir / f"{path.stem}_click.wav"
    if out.exists() and not args.overwrite:
        print(T(f"  {path.name}\n     déjà traité, fichier conservé "
                f"({out.name}) — --overwrite pour le recréer",
                f"  {path.name}\n     already done, file kept "
                f"({out.name}) — --overwrite to recreate it"))
        return None
    # sr=None: keep the original sample rate -> strictly identical length
    y, sr = librosa.load(str(path), sr=None, mono=True)
    n_samples = len(y)

    # Analysis on a 22 kHz copy (faster, just as accurate for rhythm)
    asr = 22050
    ya = librosa.resample(y, orig_sr=sr, target_sr=asr) if sr != asr else y
    hop = 64
    env = onset_env(ya, asr, hop)
    fps = asr / hop
    env_coarse = env[: len(env) // 4 * 4].reshape(-1, 4).max(axis=1)

    bpm, cands = choose_tempo(ya, asr, env_coarse, fps / 4,
                              args.bpm, args.bpm_hint)
    # The grid is always computed at the detected (or forced) tempo;
    # --half / --double are applied AFTERWARDS, from the "one", so they never
    # move the alignment or the accent.
    period, t0 = fit_grid(env, fps, bpm, len(env))

    # Possible realignment on the start of the music (off-beats, shuffle)
    recal = ""
    if args.beat_at is not None:          # reference point given by ear
        frac = ((args.beat_at - t0) / period) % 1.0
        recal = T(f"     grille calée sur le repère --beat-at {args.beat_at:.3f} s\n",
                  f"     grid aligned on the --beat-at mark {args.beat_at:.3f} s\n")
    elif args.shift is None:
        frac, t_on = start_shift(ya, asr, env, fps, t0, period)
        if frac:
            names = {1 / 4: "1/4", 1 / 3: "1/3", 1 / 2: "1/2",
                     2 / 3: "2/3", 3 / 4: "3/4"}
            recal = T(f"     grille recalée de {names[frac]} temps sur le début "
                      f"de la musique ({t_on:.3f} s)  (--shift 0 pour annuler)\n",
                      f"     grid moved by {names[frac]} beat onto the start "
                      f"of the music ({t_on:.3f} s)  (--shift 0 to cancel)\n")
    else:
        frac = args.shift
    t0 += frac * period
    while t0 - period >= 0:
        t0 -= period
    while t0 < 0:
        t0 += period

    times = t0 + period * np.arange(int((n_samples / sr - t0) / period) + 1)

    # Is the tempo really steady? Otherwise, adaptive tracking.
    adaptive_used = False
    if not args.fixed:
        adapt = track_adaptive(env, fps, period, t0, n_samples / sr)
        if adapt is not None:
            m = min(len(adapt), len(times))
            dev = np.abs(adapt[:m] - times[:m])
            if np.percentile(dev, 95) > 0.025 or args.adaptive:
                times, adaptive_used = adapt, True

    if args.beat_at is not None:
        # tracking keeps the tempo variations, the mark sets the position
        j = int(np.argmin(np.abs(times - args.beat_at)))
        times = times + (args.beat_at - times[j])

    start, end = music_bounds(y, sr)
    first = int(np.argmax(times >= start - period * 0.25))   # the opening "one"
    lead = args.count_in * args.beats_per_bar * period        # count-in
    bar = args.beats_per_bar     # accent every N clicks, also with --half/--double
    t_first = times[first]

    if args.half:      # one click out of two, keeping the "one"
        times = times[(np.arange(len(times)) - first) % 2 == 0]
        period *= 2
    elif args.double:  # an extra click in the middle of each beat
        mid = (times[:-1] + times[1:]) / 2
        times = np.sort(np.r_[times, mid])
        period /= 2
    bpm = 60.0 / period

    times = times[(times >= t_first - lead - period * 0.25) & (times <= end)]

    variable = ""
    if adaptive_used and len(times) > 2:
        local = 60.0 / np.diff(times)
        lo_bpm, hi_bpm = np.percentile(local, 5), np.percentile(local, 95)
        variable = T(f"     tempo variable ({lo_bpm:.1f} à {hi_bpm:.1f} BPM) : "
                     f"clic adaptatif\n",
                     f"     variable tempo ({lo_bpm:.1f} to {hi_bpm:.1f} BPM): "
                     f"adaptive click\n")
    beats = np.round((times + args.offset_ms / 1000.0) * sr).astype(int)
    # a beat falling just before the start of the file (up to 1/4 beat) is
    # moved to 0: it is the opening "one" and must not disappear
    early = (beats < 0) & (beats > -0.25 * period * sr)
    beats[early] = 0
    beats = beats[(beats >= 0) & (beats < n_samples)]
    if len(beats) == 0:
        print(T(f"  !! Aucun temps détecté dans {path.name}, fichier ignoré.",
                f"  !! No beat detected in {path.name}, file skipped."))
        return None

    # By default, the first click (start of the music, or of the count-in) is
    # the "one": backing tracks start on a first beat.
    # --accent-auto: infers the "one" from chord changes + kick drum.
    if args.accent_auto:
        downbeat = guess_downbeat(y, sr, beats, bar, args.debug)
    elif args.beat_at is not None:        # the accent falls on the mark
        downbeat = int(np.argmin(np.abs(beats / sr - args.beat_at))) % bar
    else:
        downbeat = 0
    downbeat = (downbeat + args.accent_offset) % bar

    accent = make_click(sr, args.accent_freq, gain=args.volume)
    normal = make_click(sr, args.click_freq, gain=args.volume * 0.6)

    click = np.zeros(n_samples, dtype=np.float32)
    for i, pos in enumerate(beats):
        c = accent if (i - downbeat) % bar == 0 else normal
        e = min(pos + len(c), n_samples)
        click[pos:e] += c[: e - pos]

    out_dir.mkdir(parents=True, exist_ok=True)
    write_wav(out, click, sr, "PCM_24")

    preview = out_dir / f"{path.stem}_preview.wav"
    if args.preview and (args.overwrite or not preview.exists()):
        mix = 0.7 * y / (np.max(np.abs(y)) or 1.0) + 0.5 * click
        mix /= max(1.0, np.max(np.abs(mix)))
        write_wav(preview, mix, sr, "PCM_16")

    top = max(c[1] for c in cands)
    others = ", ".join(f"{b:.1f}" for b, s, l in cands
                       if abs(b - bpm) > 1 and s >= 0.85 * top)
    print(f"  {path.name}\n"
          + T(f"     tempo = {bpm:.2f} BPM | {len(beats)} temps | "
              f"1er clic à {beats[0] / sr:.3f} s | durée {n_samples / sr:.2f} s | {sr} Hz\n",
              f"     tempo = {bpm:.2f} BPM | {len(beats)} beats | "
              f"1st click at {beats[0] / sr:.3f} s | length {n_samples / sr:.2f} s | {sr} Hz\n")
          + recal + variable
          + (T(f"     autres pulsations possibles : {others}  (→ --bpm pour forcer)\n",
               f"     other possible pulses: {others}  (→ --bpm to force one)\n")
             if others else "")
          + f"     -> {out}")
    return bpm, others


# =============================================================== SETTINGS FILE
def norm(text):
    """Loose name comparison: case, _ and apostrophes ignored."""
    for a, b in (("_", " "), ("’", "'"), ("  ", " ")):
        text = text.replace(a, b)
    return text.lower().strip()


def load_settings(path, keep_bad=False):
    """
    Per-song settings file (optional), one line per song:
        My Song = --bpm 82.4 --accent-offset 1
        Insight = --bpm 85
    The left part is the file name, or part of it (case ignored).
    Returns [(name, options, line number)]; with keep_bad, lines without "="
    are returned as (None, line, line number).
    """
    rules = []
    if path and Path(path).is_file():
        text = Path(path).read_text(encoding="utf-8")
        for num, line in enumerate(text.splitlines(), start=1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                if keep_bad:
                    rules.append((None, line, num))
                continue
            k, v = line.split("=", 1)
            rules.append((k.strip(), v.split(), num))
    return rules


def find_rule(rules, stem):
    """Exact line first; otherwise every line contained in the file name."""
    exact = [o for k, o, _ in rules if norm(k) == norm(stem)]
    if exact:
        return exact[-1]
    part = [o for k, o, _ in rules if norm(k) in norm(stem)]
    return sum(part, [])


HEADER_MARKS = ("# click_track.py",
                "# Réglages par morceau — créé automatiquement")   # earlier versions


def settings_header():
    return T("""\
# click_track.py — réglages par morceau (fichier créé automatiquement)
# (ces lignes de commentaire sont remises à jour par le script ;
#  les lignes de réglage ci-dessous ne sont jamais modifiées)
#
# Une ligne par morceau :  nom du morceau = options
# Le nom peut être complet ou partiel ("Insight" suffit).
# Chaque ligne fige le réglage de ce morceau à chaque relance du script.
# Après modification, relancez le script avec --overwrite
# (ou supprimez le fichier _click.wav du morceau concerné).
# Supprimez une ligne pour que le morceau soit de nouveau détecté
# automatiquement (la ligne sera alors recréée avec le nouveau résultat).
#
# Options par morceau :
#   --bpm 85             tempo imposé
#   --half               tempo divisé par 2   (clic deux fois plus lent)
#   --double             tempo multiplié par 2 (clic deux fois plus rapide)
#   --accent-offset 1    décale l'accent (le "un") de 1, 2 ou 3 temps
#                        (par défaut, le tout premier clic est accentué)
#   --accent-auto        cherche le "un" par analyse des accords
#   --beats-per-bar 3    morceau en 3/4
#   --count-in 1         une mesure de décompte avant la musique
#   --beat-at 00:29.350  instant d'un "un" repéré à l'oreille (ou 29.35) :
#                        la grille et l'accent sont calés dessus
#   --shift 1/2          décale la grille d'1/2 temps (ou 1/3, 2/3, -1/3...)
#   --shift 0            annule le recalage automatique sur le début
#   --offset-ms -10      avance tous les clics de 10 ms
#   --fixed              grille parfaitement régulière
#   --adaptive           suivi d'un tempo variable
#   (sans --fixed ni --adaptive, le script choisit seul)
#
# Option globale (sur la ligne de commande, pas dans ce fichier) :
#   --overwrite          recrée tous les fichiers clic déjà existants
#                        (ce fichier de réglages n'est jamais écrasé)
""", """\
# click_track.py — per-song settings (file created automatically)
# (these comment lines are refreshed by the script;
#  the setting lines below are never modified)
#
# One line per song:  song name = options
# The name can be complete or partial ("Insight" is enough).
# Each line fixes the settings of that song on every run of the script.
# After a change, run the script again with --overwrite
# (or delete the _click.wav file of that song).
# Delete a line to have the song detected automatically again
# (the line is then written back with the new result).
#
# Per-song options:
#   --bpm 85             forced tempo
#   --half               tempo divided by 2    (click twice as slow)
#   --double             tempo multiplied by 2 (click twice as fast)
#   --accent-offset 1    moves the accent (the "one") by 1, 2 or 3 beats
#                        (by default, the very first click is accented)
#   --accent-auto        finds the "one" by analysing the chords
#   --beats-per-bar 3    song in 3/4
#   --count-in 1         one bar of count-in before the music
#   --beat-at 00:29.350  time of a "one" found by ear (or 29.35):
#                        the grid and the accent are aligned on it
#   --shift 1/2          moves the grid by 1/2 beat (or 1/3, 2/3, -1/3...)
#   --shift 0            cancels the automatic realignment on the start
#   --offset-ms -10      moves all clicks 10 ms earlier
#   --fixed              perfectly regular grid
#   --adaptive           follows a variable tempo
#   (without --fixed or --adaptive, the script decides)
#
# Global option (on the command line, not in this file):
#   --overwrite          recreates all existing click files
#                        (this settings file is never overwritten)
""")


def split_header(text):
    """Separates the generated comment header from the rest of the file."""
    if not text.startswith(HEADER_MARKS):
        return None, text            # hand-written file: left untouched
    lines = text.splitlines(keepends=True)
    i = 0
    while i < len(lines) and lines[i].startswith("#"):
        i += 1
    return "".join(lines[:i]), "".join(lines[i:])


def update_settings(path, results):
    """
    Updates the settings file WITHOUT ever changing existing lines: only
    refreshes the comment header and appends the songs that have no line yet
    (complete or partial name).
    """
    path = Path(path)
    rules = load_settings(path)
    new = [(stem, bpm, alt) for stem, bpm, alt in results
           if not find_rule(rules, stem)
           and not any(norm(k) == norm(stem) for k, _, _ in rules)]

    if path.is_file():
        old = path.read_text(encoding="utf-8")
        header, body = split_header(old)
        text = (settings_header() + body) if header is not None else old
    else:
        old, text = None, settings_header()
    for stem, bpm, alt in new:
        text += "\n"
        if alt:
            text += T(f"# autres pulsations possibles : {alt}\n",
                      f"# other possible pulses: {alt}\n")
        text += f"{stem} = --bpm {bpm:.2f}\n"
    if text == old:
        return
    tmp = path.with_name(path.name + ".partial")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)          # atomic replacement
    finally:
        if tmp.exists():
            tmp.unlink()
    if new:
        print(T(f"\nRéglages enregistrés dans : {path}  "
                f"({len(new)} morceau(x) ajouté(s))",
                f"\nSettings saved in: {path}  "
                f"({len(new)} song(s) added)"))


def collect_inputs(items):
    files = []
    for item in items:
        p = Path(item).expanduser()
        if p.is_dir():
            files += sorted(f for f in p.iterdir()
                            if f.suffix.lower() in AUDIO_EXT
                            and not f.stem.endswith(("_click", "_preview")))
        elif p.is_file():
            files.append(p)
        else:
            print(T(f"  !! Introuvable : {item}", f"  !! Not found: {item}"))
    return files


# =============================================================== OPTIONS
class SettingsError(Exception):
    pass


class QuietParser(argparse.ArgumentParser):
    """Parser for settings file lines: raises an error instead of printing
    the whole help and exiting."""
    def error(self, message):
        raise SettingsError(message)


def add_track_options(p):
    """Options valid both on the command line and in the settings file."""
    p.add_argument("--bpm", type=float, metavar="BPM", default=None,
                   help=T("impose le tempo (affiné automatiquement à ±1 %%)",
                          "forces the tempo (automatically refined within ±1 %%)"))
    tempo = p.add_mutually_exclusive_group()
    tempo.add_argument("--half", action="store_true",
                       help=T("divise le tempo par 2 (clic deux fois plus lent)",
                              "divides the tempo by 2 (click twice as slow)"))
    tempo.add_argument("--double", action="store_true",
                       help=T("multiplie le tempo par 2 (clic deux fois plus rapide)",
                              "multiplies the tempo by 2 (click twice as fast)"))
    p.add_argument("--beats-per-bar", type=int, metavar="N", default=4,
                   help=T("temps par mesure (défaut : 4 = 4/4)",
                          "beats per bar (default: 4 = 4/4)"))
    p.add_argument("--accent-offset", type=int, metavar="N", default=0,
                   help=T("décale l'accent de N temps si le « un » tombe à côté "
                          "(par défaut, le premier clic est accentué)",
                          'moves the accent by N beats if the "one" is wrong '
                          "(by default, the first click is accented)"))
    p.add_argument("--accent-auto", action="store_true",
                   help=T("cherche le « un » par analyse (accords, grosse caisse) "
                          "au lieu de l'accentuer sur le premier clic",
                          'finds the "one" by analysis (chords, kick drum) '
                          "instead of accenting the first click"))
    p.add_argument("--count-in", type=int, metavar="N", default=0,
                   help=T("nombre de mesures de décompte avant la musique",
                          "number of count-in bars before the music"))
    p.add_argument("--beat-at", type=parse_time, metavar=T("MM:SS", "MM:SS"), default=None,
                   help=T("instant d'un « un » repéré à l'oreille (29.35 ou "
                          "00:29.350) : la grille et l'accent sont calés dessus",
                          'time of a "one" found by ear (29.35 or 00:29.350): '
                          "the grid and the accent are aligned on it"))
    p.add_argument("--shift", type=parse_fraction, metavar="1/2", default=None,
                   help=T("décale la grille d'une fraction de temps (1/2, 1/3, "
                          "-1/3...) ; 0 = aucun recalage automatique",
                          "moves the grid by a fraction of a beat (1/2, 1/3, "
                          "-1/3...); 0 = no automatic realignment"))
    p.add_argument("--offset-ms", type=float, metavar="MS", default=0.0,
                   help=T("avance (<0) ou retarde (>0) tous les clics, en ms",
                          "moves all clicks earlier (<0) or later (>0), in ms"))
    grid = p.add_mutually_exclusive_group()
    grid.add_argument("--fixed", action="store_true",
                      help=T("force une grille parfaitement régulière",
                             "forces a perfectly regular grid"))
    grid.add_argument("--adaptive", action="store_true",
                      help=T("force le suivi adaptatif (tempo variable)",
                             "forces adaptive tracking (variable tempo)"))
    p.add_argument("--bpm-hint", type=float, metavar="BPM", default=None,
                   help=T("tempo approximatif pour guider la détection",
                          "approximate tempo to guide the detection"))
    p.add_argument("--accent-freq", type=float, metavar="HZ", default=1600.0,
                   help=T("fréquence du clic accentué en Hz (défaut : 1600)",
                          "pitch of the accented click in Hz (default: 1600)"))
    p.add_argument("--click-freq", type=float, metavar="HZ", default=1000.0,
                   help=T("fréquence des autres clics en Hz (défaut : 1000)",
                          "pitch of the other clicks in Hz (default: 1000)"))
    p.add_argument("--volume", type=float, metavar="0-1", default=0.9,
                   help=T("volume du clic accentué, de 0 à 1 (défaut : 0.9)",
                          "volume of the accented click, 0 to 1 (default: 0.9)"))


TRACK_PARSER = QuietParser(add_help=False, allow_abbrev=False)
add_track_options(TRACK_PARSER)
TRACK_OPTIONS = sorted(o for a in TRACK_PARSER._actions for o in a.option_strings)
GLOBAL_ONLY = {"--overwrite", "--force", "--preview", "--no-settings", "--settings",
               "-o", "--output-dir", "--fr", "--en", "--debug", "--version",
               "-h", "--help"}


def check_values(ns):
    """Value checks (command line or settings file)."""
    if ns.bpm is not None and not 20 <= ns.bpm <= 400:
        raise SettingsError(T(f"--bpm {ns.bpm:g} : le tempo doit être entre 20 et 400",
                              f"--bpm {ns.bpm:g}: the tempo must be between 20 and 400"))
    if not 1 <= ns.beats_per_bar <= 16:
        raise SettingsError(T(f"--beats-per-bar {ns.beats_per_bar} : "
                              "doit être entre 1 et 16",
                              f"--beats-per-bar {ns.beats_per_bar}: "
                              "must be between 1 and 16"))
    if ns.beat_at is not None and ns.beat_at < 0:
        raise SettingsError(T("--beat-at doit être un instant positif",
                              "--beat-at must be a positive time"))
    if ns.count_in < 0:
        raise SettingsError(T("--count-in ne peut pas être négatif",
                              "--count-in cannot be negative"))
    if not 0 < ns.volume <= 1:
        raise SettingsError(T(f"--volume {ns.volume:g} : doit être entre 0 et 1",
                              f"--volume {ns.volume:g}: must be between 0 and 1"))


def explain(message, cli=False):
    """Turns argparse messages into clear messages in the display language
    (cli=True: error on the command line, otherwise in the settings file)."""
    import difflib
    import re
    if "the following arguments are required" in message:
        return T("indiquez au moins un fichier ou un dossier",
                 "give at least one file or folder")
    m = re.match(r"unrecognized arguments: (.*)", message)
    if m:
        parts = []
        known = TRACK_OPTIONS + (sorted(GLOBAL_ONLY) if cli else [])
        for tok in m.group(1).split():
            if tok in GLOBAL_ONLY and not cli:
                parts.append(T(f"{tok} est une option globale : à mettre sur la "
                               "ligne de commande, pas dans le fichier de réglages",
                               f"{tok} is a global option: put it on the "
                               "command line, not in the settings file"))
            elif tok.startswith("-"):
                close = difflib.get_close_matches(tok, known, n=1)
                hint = (T(f" (vouliez-vous dire {close[0]} ?)",
                          f" (did you mean {close[0]}?)") if close else "")
                parts.append(T(f"option inconnue {tok}{hint}",
                               f"unknown option {tok}{hint}"))
            else:
                parts.append(T(f"valeur en trop « {tok} »", f'extra value "{tok}"'))
        return T(" ; ", "; ").join(parts)
    m = re.match(r"argument (\S+): invalid (int|float) value: '(.*)'", message)
    if m:
        kind = (T("un nombre entier", "a whole number") if m.group(2) == "int"
                else T("un nombre", "a number"))
        return T(f"{m.group(1)} attend {kind}, pas « {m.group(3)} »",
                 f'{m.group(1)} expects {kind}, not "{m.group(3)}"')
    m = re.match(r"argument (\S+): expected one argument", message)
    if m:
        return T(f"{m.group(1)} doit être suivi d'une valeur",
                 f"{m.group(1)} must be followed by a value")
    m = re.match(r"argument (\S+): not allowed with argument (\S+)", message)
    if m:
        return T(f"{m.group(1)} et {m.group(2)} ne peuvent pas être utilisés ensemble",
                 f"{m.group(1)} and {m.group(2)} cannot be used together")
    m = re.match(r"argument (\S+): ignored explicit argument '(.*)'", message)
    if m:
        return T(f"{m.group(1)} ne prend pas de valeur (« {m.group(2)} » en trop)",
                 f'{m.group(1)} takes no value ("{m.group(2)}" is extra)')
    m = re.match(r"argument (\S+): (.*)$", message)   # our own messages (times, fractions)
    if m:
        return f"{m.group(1)} : {m.group(2)}" if LANG == "fr" else f"{m.group(1)}: {m.group(2)}"
    return message


def track_args(args, opts):
    """Applies the options of a settings line on top of the command line.
    Raises SettingsError with a clear message."""
    ns = argparse.Namespace(**vars(args))
    if opts:
        try:
            TRACK_PARSER.parse_args(opts, namespace=ns)
        except SettingsError as e:
            raise SettingsError(explain(str(e))) from None
    check_values(ns)
    return ns


def check_settings_files(paths, args):
    """Checks every settings file BEFORE starting the computations.
    Returns False (and prints the errors) if a line is wrong."""
    ok, bad_file = True, None
    for path in paths:
        for key, opts, num in load_settings(path, keep_bad=True):
            if key is None:
                msg = T(f"ligne sans « = » : « {opts} »", f'line without "=": "{opts}"')
            else:
                try:
                    track_args(args, opts)
                    continue
                except SettingsError as e:
                    msg = T(f"« {key} » : {e}", f'"{key}": {e}')
            if bad_file != path:
                print(T(f"Erreur(s) dans le fichier de réglages {path} :",
                        f"Error(s) in the settings file {path}:"))
                bad_file, ok = path, False
            print(T(f"  ligne {num} — {msg}", f"  line {num} — {msg}"))
    if not ok:
        print(T("\nCorrigez ces lignes puis relancez le script "
                "(aucun fichier n'a été modifié).",
                "\nFix these lines and run the script again "
                "(no file was changed)."))
    return ok


EPILOG_FR = f"""\
exemples :
  python click_track.py "D:\\Mes instrus"               tout un dossier
  python click_track.py "My Song.mp3" --preview        un morceau, avec un mix de contrôle
  python click_track.py "D:\\Mes instrus" --overwrite   recrée tous les clics

fichiers créés, à côté des morceaux :
  {OUT_NAME + "/My Song_click.wav":28} la piste clic, de la même durée que le morceau
  {OUT_NAME + "/My Song_preview.wav":28} morceau + clic, pour vérifier à l'oreille (--preview)
  {SETTINGS_NAME:28} réglages par morceau : une ligne par morceau, modifiable

Les fichiers clic existants ne sont jamais écrasés (--overwrite pour les recréer).
Les options par morceau peuvent être données sur la ligne de commande ou, pour
un seul morceau, sur sa ligne de {SETTINGS_NAME}, par exemple :
  My Song = --bpm 82.38 --beat-at 00:29.2
"""

EPILOG_EN = f"""\
examples:
  python click_track.py "D:\\Backing tracks"               a whole folder
  python click_track.py "My Song.mp3" --preview          one song, with a check mix
  python click_track.py "D:\\Backing tracks" --overwrite   recreate all clicks

files created next to the songs:
  {OUT_NAME + "/My Song_click.wav":28} the click track, as long as the song
  {OUT_NAME + "/My Song_preview.wav":28} song + click, to check by ear (--preview)
  {SETTINGS_NAME:28} per-song settings: one line per song, editable

Existing click files are never overwritten (--overwrite to recreate them).
Per-song options can be given on the command line or, for a single song, on its
line of {SETTINGS_NAME}, for example:
  My Song = --bpm 82.38 --beat-at 00:29.2
"""


class MainParser(argparse.ArgumentParser):
    """Command-line parser: a short, clear error message instead of argparse's."""
    def error(self, message):
        print(T(f"Erreur : {explain(message, cli=True)}",
                f"Error: {explain(message, cli=True)}"), file=sys.stderr)
        print(T("Aide : python click_track.py --help",
                "Help: python click_track.py --help"), file=sys.stderr)
        sys.exit(2)


class HelpFormatter(argparse.RawDescriptionHelpFormatter):
    """Help layout with the "usage" prefix in the display language."""
    def add_usage(self, usage, actions, groups, prefix=None):
        super().add_usage(usage, actions, groups, T("utilisation : ", "usage: "))


def build_parser():
    ap = MainParser(
        add_help=False,
        prog="click_track.py",
        usage=T("python click_track.py <fichiers ou dossiers> [options]",
                "python click_track.py <files or folders> [options]"),
        formatter_class=HelpFormatter,
        description=T(f"click_track.py {__version__} — crée une piste clic (WAV) "
                      "calée sur chaque morceau.",
                      f"click_track.py {__version__} — creates a click track (WAV) "
                      "aligned on each song."),
        epilog=T(EPILOG_FR, EPILOG_EN))
    ap._positionals.title = T("arguments", "arguments")
    ap.add_argument("inputs", nargs="+", metavar=T("fichier_ou_dossier", "file_or_folder"),
                    help=T("fichiers audio et/ou dossiers", "audio files and/or folders"))
    g = ap.add_argument_group(T("options globales", "global options"))
    g.add_argument("-h", "--help", action="help",
                   help=T("affiche cette aide", "shows this help"))
    g.add_argument("--overwrite", "--force", dest="overwrite", action="store_true",
                   help=T("recrée les fichiers clic déjà existants "
                          "(par défaut, ils ne sont jamais écrasés)",
                          "recreates existing click files "
                          "(by default, they are never overwritten)"))
    g.add_argument("--preview", action="store_true",
                   help=T("écrit aussi un mix morceau + clic pour vérifier",
                          "also writes a song + click mix to check by ear"))
    g.add_argument("-o", "--output-dir", metavar=T("DOSSIER", "FOLDER"),
                   help=T(f"dossier de sortie (défaut : sous-dossier « {OUT_NAME} » "
                          "à côté des morceaux)",
                          f'output folder (default: "{OUT_NAME}" subfolder '
                          "next to the songs)"))
    g.add_argument("--settings", default=None, metavar=T("FICHIER", "FILE"),
                   help=T(f"fichier de réglages par morceau (défaut : {SETTINGS_NAME} "
                          "à côté des morceaux)",
                          f"per-song settings file (default: {SETTINGS_NAME} "
                          "next to the songs)"))
    g.add_argument("--no-settings", action="store_true",
                   help=T(f"ne crée ni ne complète {SETTINGS_NAME}",
                          f"does not create or update {SETTINGS_NAME}"))
    g.add_argument("--fr", "--en", dest="lang_flag", action="store_true",
                   help=T("force la langue des messages (sinon : langue du système)",
                          "forces the message language (otherwise: system language)"))
    g.add_argument("--version", action="version", version=f"click_track.py {__version__}",
                   help=T("affiche le numéro de version", "shows the version number"))
    g.add_argument("--debug", action="store_true", help=argparse.SUPPRESS)
    add_track_options(ap.add_argument_group(
        T("options par morceau (aussi dans le fichier de réglages)",
          "per-song options (also in the settings file)")))
    return ap


# =============================================================== MAIN
def main():
    ap = build_parser()
    argv = sys.argv[1:]
    if not [a for a in argv if a not in ("--fr", "--en")]:
        ap.print_help()                  # no argument: built-in help
        return
    args = ap.parse_args(argv)
    try:
        check_values(args)
    except SettingsError as e:
        sys.exit(T(f"Erreur : {e}", f"Error: {e}"))

    files = collect_inputs(args.inputs)
    if not files:
        sys.exit(T("Aucun fichier audio à traiter.", "No audio file to process."))

    paths = sorted({str(settings_path(f.parent, args)) for f in files})
    if not check_settings_files([p for p in paths if Path(p).is_file()], args):
        sys.exit(2)

    done, stats = {}, {"made": 0}
    print(T(f"{len(files)} fichier(s) à traiter  (Ctrl-C pour arrêter) :",
            f"{len(files)} file(s) to process  (Ctrl-C to stop):"))
    try:
        process_all(files, args, done, stats)
    except KeyboardInterrupt:
        print(T(f"\n\nInterrompu (Ctrl-C) : {stats['made']} piste(s) clic créée(s) "
                f"sur {len(files)} morceau(x). Le morceau en cours n'a pas été écrit.",
                f"\n\nInterrupted (Ctrl-C): {stats['made']} click track(s) created "
                f"out of {len(files)} song(s). The current song was not written."))
        save_all_settings(done)
        sys.exit(130)
    save_all_settings(done)


def save_all_settings(done):
    # Songs already finished are saved even after a Ctrl-C.
    try:
        for settings, results in done.items():
            update_settings(settings, results)
    except KeyboardInterrupt:
        print(T("Enregistrement des réglages interrompu, fichier inchangé.",
                "Saving of the settings interrupted, file unchanged."))


def process_all(files, args, done, stats):
    for f in files:
        out_dir = output_dir(f.parent, args)
        settings = settings_path(f.parent, args)
        fargs = track_args(args, find_rule(load_settings(settings), f.stem))
        try:
            res = process(f, out_dir, fargs)
        except Exception as e:  # one failing file does not stop the others
            print(T(f"  !! Erreur sur {f.name} : {e}", f"  !! Error on {f.name}: {e}"))
            continue
        if res:
            stats["made"] += 1
        if not args.no_settings:
            lst = done.setdefault(str(settings), [])
            if res:
                lst.append((f.stem, *res))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:      # safety net: never a Python traceback
        print(T("\nInterrompu (Ctrl-C).", "\nInterrupted (Ctrl-C)."))
        sys.exit(130)
