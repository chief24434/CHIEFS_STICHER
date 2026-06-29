import gradio as gr
import requests
import random
import os
import tempfile
import subprocess
import json

PEXELS_API_KEY = os.environ.get("PEXELS_API_KEY", "")
PIXABAY_API_KEY = os.environ.get("PIXABAY_API_KEY", "")

NATURE_QUERIES = {
    "Forest":"forest","Ocean":"ocean waves","Rain":"rain",
    "Mountains":"mountains","Sunset":"sunset","Flowers":"flowers",
    "Snow":"snow winter","Fields":"meadow grass","River":"river waterfall","Sky":"clouds sky",
}

def get_videos_pixabay(query, count=20):
    clips = []
    if not PIXABAY_API_KEY:
        return clips
    try:
        r = requests.get(
            f"https://pixabay.com/api/videos/?key={PIXABAY_API_KEY}&q={query}&per_page={count}&video_type=film&orientation=horizontal",
            timeout=15)
        if r.status_code == 200:
            for v in r.json().get("hits", []):
                c = v.get("videos", {})
                chosen = c.get("medium") or c.get("small") or c.get("large")
                if chosen:
                    clips.append({"url": chosen["url"], "duration": v.get("duration", 10)})
    except: pass
    return clips

def get_videos_pexels(query, count=20):
    clips = []
    if not PEXELS_API_KEY:
        return clips
    try:
        r = requests.get(
            f"https://api.pexels.com/videos/search?query={query}&per_page={count}&orientation=landscape",
            headers={"Authorization": PEXELS_API_KEY}, timeout=15)
        if r.status_code == 200:
            for v in r.json().get("videos", []):
                for vf in v.get("video_files", []):
                    if vf.get("quality") == "sd" and vf.get("width", 0) >= 640:
                        clips.append({"url": vf["link"], "duration": v.get("duration", 10)})
                        break
    except: pass
    return clips

def get_videos(query, count=20):
    # Pixabay first, Pexels second — combine both
    pixabay = get_videos_pixabay(query, count)
    pexels = get_videos_pexels(query, count)
    seen = set()
    result = []
    for c in pixabay + pexels:
        if c["url"] not in seen:
            seen.add(c["url"])
            result.append(c)
    return result

def get_audio_duration(path):
    try:
        result = subprocess.run(
            ["ffprobe","-v","quiet","-print_format","json","-show_format",path],
            capture_output=True, text=True, timeout=15)
        data = json.loads(result.stdout)
        return float(data["format"]["duration"])
    except:
        return 0

def parse_duration(s):
    s = s.strip()
    try:
        if ':' in s:
            p = s.split(':')
            return int(p[0])*60+float(p[1]) if len(p)==2 else int(p[0])*3600+int(p[1])*60+float(p[2])
        elif 'm' in s.lower():
            import re
            m = re.search(r'(\d+)m', s.lower())
            sec = re.search(r'(\d+)s', s.lower())
            return (int(m.group(1)) if m else 0)*60+(int(sec.group(1)) if sec else 0)
        else:
            return float(s)
    except:
        return 0

def download_clip(url, path):
    try:
        r = requests.get(url, stream=True, timeout=25)
        if r.status_code == 200:
            with open(path,'wb') as f:
                for chunk in r.iter_content(65536): f.write(chunk)
            return True
    except: pass
    return False

def build_cycling_overlay(valid_imgs, img_w, period, target_secs, stitched, audio_file, has_audio, eff, output):
    n = len(valid_imgs)
    cycle = 10
    inputs = ["-i", stitched]
    for img in valid_imgs:
        inputs += ["-i", img]
    fc_parts = []
    for i in range(n):
        fc_parts.append(f"[{i+1}:v]scale={img_w}:-1[img{i}]")
    mx = f"+12*sin(2*PI*t/{period:.2f})"
    my = f"+8*cos(2*PI*t/({period:.2f}*1.3))"
    prev = "0:v"
    for i in range(n):
        ox = f"(W-w)/2{mx}"
        oy = f"(H-h)/2{my}"
        enable = f"gte(mod(t,{n*cycle}),{i*cycle})*lt(mod(t,{n*cycle}),{(i+1)*cycle})"
        out_lbl = f"ov{i}" if i < n-1 else "comp"
        fc_parts.append(f"[{prev}][img{i}]overlay={ox}:{oy}:enable='{enable}'[{out_lbl}]")
        prev = out_lbl
    if eff:
        fc_parts.append(f"[comp]{eff}[out]")
        map_out = "[out]"
    else:
        map_out = "[comp]"
    fc = ";".join(fc_parts)
    audio_idx = n + 1
    if has_audio:
        inputs += ["-i", audio_file]
        cmd = (["ffmpeg","-y"] + inputs + [
            "-filter_complex", fc,
            "-map", map_out, "-map", f"{audio_idx}:a",
            "-c:v","libx264","-preset","fast","-crf","23",
            "-c:a","aac","-b:a","128k",
            "-t", str(target_secs), "-shortest", output])
    else:
        cmd = (["ffmpeg","-y"] + inputs + [
            "-filter_complex", fc,
            "-map", map_out,
            "-c:v","libx264","-preset","fast","-crf","23",
            "-t", str(target_secs), output])
    return cmd

def generate_video(audio_file, duration_str, video_name, images, img_size, motion_speed, nature_cats, effect, progress=gr.Progress()):
    target_secs = 0
    has_audio = audio_file is not None

    if has_audio:
        progress(0.03, desc="🎵 Reading audio duration...")
        target_secs = get_audio_duration(audio_file)
        if target_secs <= 0:
            return None, "⚠ Could not read audio. Use MP3 or WAV."
    elif duration_str and duration_str.strip():
        target_secs = parse_duration(duration_str)

    if target_secs <= 0:
        return None, "⚠ Please upload audio OR enter a duration."
    if target_secs > 1200:
        return None, "⚠ Max 20 minutes on free tier."
    if not nature_cats:
        return None, "⚠ Please select at least one nature category."
    if not PEXELS_API_KEY and not PIXABAY_API_KEY:
        return None, "⚠ No API key found in Space Secrets."

    mins = int(target_secs//60)
    secs_r = int(target_secs%60)
    per_cat = target_secs / len(nature_cats)

    progress(0.06, desc=f"🔍 Searching clips ({mins}m {secs_r}s)...")
    all_clips = []
    for cat in nature_cats:
        clips = get_videos(NATURE_QUERIES.get(cat,"nature"), 20)
        random.shuffle(clips)
        total = 0
        for clip in clips:
            if total >= per_cat + 60: break
            all_clips.append(clip)
            total += min(clip["duration"], 30)

    if not all_clips:
        return None, "⚠ No clips found. Check API keys in Space Secrets."
    random.shuffle(all_clips)

    tmpdir = tempfile.mkdtemp()

    # Download clips
    raw_clips = []
    for i, clip in enumerate(all_clips):
        progress(0.08+(i/len(all_clips))*0.35, desc=f"⬇ Downloading clip {i+1}/{len(all_clips)}...")
        path = os.path.join(tmpdir, f"raw_{i:03d}.mp4")
        if download_clip(clip["url"], path):
            raw_clips.append(path)
    if not raw_clips:
        return None, "⚠ Download failed."

    # Stitch with single-pass re-encode — fixes freeze without normalizing each clip
    progress(0.50, desc="🎬 Stitching + fixing format in one pass...")
    stitched = os.path.join(tmpdir, "stitched.mp4")
    lf = stitched + ".txt"

    # Loop clips to fill full duration
    with open(lf,'w') as f:
        total_written = 0
        loops = 0
        while total_written < target_secs + 10:
            for p in raw_clips:
                if total_written >= target_secs + 10: break
                f.write(f"file '{p}'\n")
                total_written += 20  # avg clip length estimate
            loops += 1
            if loops > 20: break

    subprocess.run([
        "ffmpeg","-y",
        "-f","concat","-safe","0","-i",lf,
        "-t",str(target_secs + 5),
        "-vf","scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=24",
        "-c:v","libx264","-preset","ultrafast","-crf","28",
        "-an","-threads","4","-movflags","+faststart",
        stitched
    ], capture_output=True, timeout=300)

    try: os.remove(lf)
    except: pass
    for p in raw_clips:
        try: os.remove(p)
        except: pass

    if not os.path.exists(stitched) or os.path.getsize(stitched) < 1000:
        return None, "⚠ Stitching failed."

    progress(0.72, desc="🎨 Applying effects and overlays...")

    # Output filename
    safe_name = "".join(c for c in (video_name or "chiefs_video") if c.isalnum() or c in "._- ").strip()
    if not safe_name: safe_name = "chiefs_video"
    if not safe_name.endswith(".mp4"): safe_name += ".mp4"
    output = os.path.join(tmpdir, safe_name)

    effect_filters = {
        "None":"",
        "Film Frame":"vignette=PI/4",
        "Grains":"noise=alls=15:allf=t+u",
        "Black & White":"hue=s=0,curves=preset=strong_contrast",
        "Film Frame 2":"vignette=PI/3,eq=contrast=1.05:brightness=-0.02:saturation=0.85",
        "Warm Golden":"curves=r='0/0 0.5/0.6 1/1':g='0/0 0.5/0.5 1/0.9':b='0/0 0.5/0.4 1/0.8'",
        "Cold Blue":"curves=r='0/0 0.5/0.4 1/0.8':g='0/0 0.5/0.5 1/0.9':b='0/0 0.5/0.6 1/1'",
        "Faded Matte":"eq=contrast=0.85:brightness=0.05:saturation=0.7,curves=all='0/0.08 1/0.92'",
        "Cinematic":"eq=contrast=1.2:saturation=0.8,curves=r='0/0 0.5/0.55 1/1':b='0/0 0.5/0.45 1/0.9'",
        "Moody Dark":"eq=contrast=1.3:brightness=-0.08:saturation=0.75,vignette=PI/3",
    }
    eff = effect_filters.get(effect,"")

    try:
        size_pct = float(img_size.replace('%',''))/100
    except:
        size_pct = 0.80
    img_w = int(1280*size_pct)

    try:
        spd = float(motion_speed)
    except:
        spd = 2.0
    period = max(1.0, 6.0-spd)

    valid_imgs = []
    if images:
        for img in images[:5]:
            img_path = img.name if hasattr(img, 'name') else img
            if img_path and os.path.exists(img_path):
                valid_imgs.append(img_path)

    if valid_imgs:
        cmd = build_cycling_overlay(valid_imgs, img_w, period, target_secs, stitched, audio_file, has_audio, eff, output)
    else:
        if has_audio:
            if eff:
                cmd = ["ffmpeg","-y","-i",stitched,"-i",audio_file,
                       "-filter_complex",f"[0:v]{eff}[out]","-map","[out]","-map","1:a",
                       "-c:v","libx264","-preset","fast","-crf","23",
                       "-c:a","aac","-b:a","128k","-t",str(target_secs),"-shortest",output]
            else:
                cmd = ["ffmpeg","-y","-i",stitched,"-i",audio_file,
                       "-map","0:v","-map","1:a","-c:v","copy",
                       "-c:a","aac","-b:a","128k","-t",str(target_secs),"-shortest",output]
        else:
            if eff:
                cmd = ["ffmpeg","-y","-i",stitched,"-vf",eff,
                       "-c:v","libx264","-preset","fast","-crf","23","-t",str(target_secs),output]
            else:
                cmd = ["ffmpeg","-y","-i",stitched,"-c:v","copy","-t",str(target_secs),output]

    subprocess.run(cmd, capture_output=True, timeout=400)

    if not os.path.exists(output) or os.path.getsize(output) < 1000:
        if has_audio:
            subprocess.run(["ffmpeg","-y","-i",stitched,"-i",audio_file,
                            "-map","0:v","-map","1:a","-c:v","copy",
                            "-c:a","aac","-b:a","128k","-t",str(target_secs),"-shortest",output],
                           capture_output=True, timeout=300)
        else:
            subprocess.run(["ffmpeg","-y","-i",stitched,"-c:v","copy","-t",str(target_secs),output],
                           capture_output=True, timeout=300)

    if not os.path.exists(output):
        return None, "⚠ Final merge failed."

    mb = os.path.getsize(output)/1024/1024
    progress(1.0)
    return output, f"✓ DONE! {mins}m {secs_r}s — {len(raw_clips)} clips — {mb:.1f} MB — {safe_name}"

LOGO_URL = "https://huggingface.co/spaces/chief24434/stitcher/resolve/main/logo.png"

css = """
@import url('https://fonts.googleapis.com/css2?family=Silkscreen:wght@400;700&family=Orbitron:wght@700;900&family=Share+Tech+Mono&family=Rajdhani:wght@400;600;700&display=swap');
body, .gradio-container { background: #020408 !important; color: #e0f4ff !important; font-family: 'Rajdhani', sans-serif !important; }
.gradio-container { max-width: 900px !important; margin: 0 auto !important; }
footer { display: none !important; }
.app-header, #app-header, .share-btn-container, .built-with,
header.svelte-1kyws56, .breadcrumbs, [data-testid="app-header"],
.top-bar, .hf-header, .header-bar, .app > header,
div[class*="header"], nav[class*="nav"] { display: none !important; }
h1,h2,h3,p,span,label,div { color: #e0f4ff !important; }
button.primary {
    background: transparent !important; border: 1px solid #00f5ff !important;
    color: #00f5ff !important; font-family: 'Orbitron', sans-serif !important;
    font-size: 11px !important; font-weight: 700 !important; letter-spacing: 2px !important;
    padding: 14px !important; width: 100% !important; cursor: pointer !important;
    text-shadow: 0 0 10px rgba(0,245,255,0.4) !important; transition: all 0.3s !important;
}
button.primary:hover { background: rgba(0,245,255,0.1) !important; box-shadow: 0 0 28px rgba(0,245,255,0.3) !important; }
input, textarea { background: rgba(0,245,255,0.05) !important; border: 1px solid rgba(0,245,255,0.2) !important; color: #e0f4ff !important; font-family: 'Share Tech Mono', monospace !important; }
.block, .form, fieldset, .wrap, .gap, section { background: transparent !important; border-color: rgba(0,245,255,0.1) !important; }
label span { color: #00f5ff !important; font-family: 'Share Tech Mono', monospace !important; font-size: 10px !important; letter-spacing: 2px !important; }
.wrap label { background: rgba(0,245,255,0.03) !important; border: 1px solid rgba(0,245,255,0.15) !important; color: #8ab4c8 !important; font-family: 'Share Tech Mono', monospace !important; font-size: 11px !important; border-radius: 3px !important; padding: 7px 12px !important; transition: all 0.2s !important; }
.wrap label:hover { border-color: #00f5ff !important; color: #00f5ff !important; }
.wrap label.selected { border-color: #00f5ff !important; color: #00f5ff !important; background: rgba(0,245,255,0.1) !important; }
video { border: 1px solid rgba(0,245,255,0.2) !important; border-radius: 4px !important; width: 100% !important; }
::-webkit-scrollbar { width: 4px; }
::-webkit-scrollbar-thumb { background: rgba(0,245,255,0.3); border-radius: 2px; }
"""

with gr.Blocks(
    css=css, title="CHIEF'S STITCHER",
    theme=gr.themes.Base(
        primary_hue=gr.themes.colors.cyan,
        neutral_hue=gr.themes.colors.slate,
        font=gr.themes.GoogleFont("Share Tech Mono"),
    ).set(
        body_background_fill="#020408",
        body_text_color="#e0f4ff",
        block_background_fill="rgba(0,245,255,0.02)",
        block_border_color="rgba(0,245,255,0.12)",
        block_label_text_color="#00f5ff",
        input_background_fill="rgba(0,245,255,0.05)",
        input_border_color="rgba(0,245,255,0.2)",
        button_primary_background_fill="transparent",
        button_primary_text_color="#00f5ff",
        button_primary_border_color="#00f5ff",
        checkbox_label_background_fill="rgba(0,245,255,0.03)",
        checkbox_label_text_color="#8ab4c8",
        checkbox_border_color="rgba(0,245,255,0.3)",
    )
) as demo:

    gr.HTML(f"""
    <link rel="manifest" href="/file=manifest.json"/>
    <link rel="icon" type="image/png" href="{LOGO_URL}"/>
    <meta name="apple-mobile-web-app-capable" content="yes"/>
    <meta name="apple-mobile-web-app-title" content="Chief's Stitcher"/>
    <meta name="theme-color" content="#00f5ff"/>
    <div style="text-align:center;padding:28px 16px 20px;border-bottom:1px solid rgba(0,245,255,0.1);margin-bottom:20px">
      <div style="display:flex;justify-content:center;margin-bottom:12px">
        <div style="position:relative;width:90px;height:90px;display:flex;align-items:center;justify-content:center">
          <svg style="position:absolute;inset:0;width:90px;height:90px" viewBox="0 0 90 90" fill="none">
            <g style="animation:rr1 6s linear infinite;transform-origin:45px 45px">
              <circle cx="45" cy="45" r="41" stroke="rgba(0,245,255,0.14)" stroke-width="1"/>
              <circle cx="45" cy="4" r="2" fill="#00f5ff"/><circle cx="86" cy="45" r="2" fill="#00f5ff"/>
              <circle cx="45" cy="86" r="2" fill="#00f5ff"/><circle cx="4" cy="45" r="2" fill="#00f5ff"/>
            </g>
            <g style="animation:rr2 9s linear infinite;transform-origin:45px 45px">
              <circle cx="45" cy="45" r="28" stroke="rgba(191,0,255,0.22)" stroke-width="1" stroke-dasharray="4 3"/>
              <circle cx="45" cy="17" r="2.5" fill="#bf00ff"/><circle cx="73" cy="45" r="2.5" fill="#bf00ff"/>
              <circle cx="45" cy="73" r="2.5" fill="#bf00ff"/><circle cx="17" cy="45" r="2.5" fill="#bf00ff"/>
            </g>
            <g style="animation:rr1 14s linear infinite;transform-origin:45px 45px">
              <circle cx="45" cy="45" r="16" stroke="rgba(0,255,136,0.2)" stroke-width="1"/>
              <circle cx="45" cy="29" r="1.5" fill="#00ff88"/><circle cx="61" cy="45" r="1.5" fill="#00ff88"/>
              <circle cx="45" cy="61" r="1.5" fill="#00ff88"/><circle cx="29" cy="45" r="1.5" fill="#00ff88"/>
            </g>
          </svg>
          <div style="position:relative;z-index:2;width:46px;height:46px;display:flex;align-items:center;justify-content:center">
            <img src="{LOGO_URL}" alt="logo"
                 style="width:46px;height:46px;object-fit:contain;border-radius:50%;animation:bob 4s ease-in-out infinite"
                 onerror="this.style.display='none';document.getElementById('fb-spider').style.display='block'"/>
            <svg id="fb-spider" width="32" height="40" viewBox="0 0 32 40" shape-rendering="crispEdges"
                 style="transform:rotate(180deg);display:none;animation:bob 4s ease-in-out infinite">
              <rect x="11" y="0" width="10" height="1" fill="#cc0000"/>
              <rect x="9" y="1" width="14" height="1" fill="#cc0000"/>
              <rect x="8" y="2" width="16" height="1" fill="#cc0000"/>
              <rect x="7" y="3" width="18" height="1" fill="#cc0000"/>
              <rect x="6" y="4" width="3" height="1" fill="#cc0000"/>
              <rect x="10" y="4" width="3" height="1" fill="#1a3aff"/>
              <rect x="14" y="4" width="4" height="1" fill="#cc0000"/>
              <rect x="19" y="4" width="3" height="1" fill="#1a3aff"/>
              <rect x="23" y="4" width="3" height="1" fill="#cc0000"/>
              <rect x="6" y="5" width="3" height="1" fill="#cc0000"/>
              <rect x="10" y="5" width="1" height="1" fill="#1a3aff"/>
              <rect x="11" y="5" width="1" height="1" fill="#fff"/>
              <rect x="12" y="5" width="1" height="1" fill="#1a3aff"/>
              <rect x="14" y="5" width="4" height="1" fill="#cc0000"/>
              <rect x="19" y="5" width="1" height="1" fill="#1a3aff"/>
              <rect x="20" y="5" width="1" height="1" fill="#fff"/>
              <rect x="21" y="5" width="1" height="1" fill="#1a3aff"/>
              <rect x="23" y="5" width="3" height="1" fill="#cc0000"/>
              <rect x="6" y="6" width="20" height="1" fill="#cc0000"/>
              <rect x="7" y="7" width="18" height="1" fill="#cc0000"/>
              <rect x="8" y="8" width="16" height="1" fill="#cc0000"/>
              <rect x="10" y="9" width="12" height="1" fill="#cc0000"/>
              <rect x="12" y="10" width="8" height="1" fill="#cc0000"/>
              <rect x="14" y="11" width="4" height="1" fill="#cc0000"/>
              <rect x="15" y="12" width="2" height="2" fill="rgba(0,245,255,0.9)"/>
            </svg>
          </div>
        </div>
      </div>
      <div style="font-family:'Silkscreen',cursive;font-size:clamp(16px,4vw,26px);color:#00f5ff;letter-spacing:3px;text-shadow:0 0 20px rgba(0,245,255,0.5),0 0 40px rgba(0,245,255,0.2);margin-bottom:6px">CHIEF'S STITCHER</div>
      <div style="font-family:'Share Tech Mono',monospace;font-size:9px;color:#4a6a7a;letter-spacing:4px">B-ROLL + IMAGE OVERLAY + EFFECTS // SPIDER AI CORE v1.0</div>
      <div style="margin-top:10px;display:inline-block;padding:3px 14px;border:1px solid rgba(191,0,255,0.4);border-radius:2px;font-family:'Share Tech Mono',monospace;font-size:9px;color:#bf00ff;letter-spacing:2px;background:rgba(191,0,255,0.06)">
        ⚡ FREE FOR ALL · NO WATERMARK · AUDIO INCLUDED · MAX 20 MIN
      </div>
    </div>
    <style>
      @keyframes rr1{{from{{transform:rotate(0deg)}}to{{transform:rotate(360deg)}}}}
      @keyframes rr2{{from{{transform:rotate(360deg)}}to{{transform:rotate(0deg)}}}}
      @keyframes bob{{0%,100%{{transform:translateY(0)}}50%{{transform:translateY(6px)}}}}
    </style>
    """)

    gr.HTML("""
    <div style="padding:10px 14px;border:1px solid rgba(255,170,0,0.3);border-radius:3px;background:rgba(255,170,0,0.04);margin:0 0 10px;font-family:'Share Tech Mono',monospace;font-size:10px;color:#ffaa00;letter-spacing:1px;line-height:1.8">
      ℹ Upload audio OR enter duration — not both needed.<br>
      If audio is uploaded it takes priority over duration.
    </div>
    """)

    audio_file   = gr.Audio(label="🎵 STEP 1A — Upload Audio (MP3/WAV · max 20 min · optional if duration entered)", type="filepath")
    duration_str = gr.Textbox(placeholder="e.g.  10:30  or  630  or  10m30s", label="⏱ STEP 1B — OR Enter Duration Manually (for video only without audio)", lines=1, max_lines=1)
    video_name   = gr.Textbox(placeholder="e.g.  my_video  or  episode_01", label="💾 Output File Name (optional · default: chiefs_video)", lines=1, max_lines=1)

    images = gr.File(
        label="🖼 STEP 2 — Upload Images (optional · up to 5 · cycles every 10s · always centered)",
        file_count="multiple", file_types=["image"])
    img_size = gr.Radio(["70%","80%","90%"], value="80%", label="📐 Image Size (applies to all · always centered)")

    gr.HTML('<div style="font-family:\'Share Tech Mono\',monospace;font-size:10px;color:#00f5ff;letter-spacing:2px;margin:14px 0 8px">🌀 STEP 3 — Overlay Motion Speed</div>')
    motion_speed = gr.Slider(minimum=1, maximum=5, value=2, step=0.5, label="Motion Speed (1=very slow · 5=fast) — always on")

    nature_cats = gr.CheckboxGroup(
        list(NATURE_QUERIES.keys()), value=["Forest"],
        label="🌿 STEP 4 — Nature Background (select multiple · splits equally)")

    effect = gr.Radio(
        ["None","Film Frame","Grains","Black & White","Film Frame 2",
         "Warm Golden","Cold Blue","Faded Matte","Cinematic","Moody Dark"],
        value="None", label="🎨 STEP 5 — Video Effect (applied to entire frame)")

    gr.HTML("""
    <div id="gen-warning" style="display:none;margin:10px 0;padding:11px 14px;border:1px solid #ff3366;border-radius:3px;font-family:'Share Tech Mono',monospace;font-size:11px;color:#ff3366;background:rgba(255,51,102,0.06);line-height:1.9">
      🚨 DO NOT close, minimize or lock your phone!<br>
      Keep this screen ON until the video is fully ready.
    </div>
    <script>
    document.addEventListener('DOMContentLoaded', function(){
      setTimeout(function(){
        document.querySelectorAll('button').forEach(function(b){
          if(b.innerText && b.innerText.includes('GENERATE')){
            b.addEventListener('click', function(){
              document.getElementById('gen-warning').style.display = 'block';
            });
          }
        });
      }, 2500);
    });
    </script>
    """)

    run_btn    = gr.Button("🕷 GENERATE VIDEO", variant="primary")
    status_out = gr.Textbox(label="● STATUS", interactive=False)
    video_out  = gr.Video(label="📹 OUTPUT VIDEO")

    gr.HTML("""
    <div style="margin-top:20px;padding:14px 16px;border:1px solid rgba(0,245,255,0.08);border-radius:3px;background:rgba(0,245,255,0.01)">
      <div style="font-family:'Share Tech Mono',monospace;font-size:10px;color:#00f5ff;letter-spacing:2px;margin-bottom:8px">ℹ INFO</div>
      <div style="font-family:'Share Tech Mono',monospace;font-size:10px;color:#4a6a7a;line-height:2">
        • Pixabay + Pexels combined — more variety<br>
        • Clips looped to always fill exact duration — no short videos<br>
        • Single pass re-encode — no freeze, fast processing<br>
        • Images cycle every 10s throughout video<br>
        • Effect on entire frame · Output: 720p · 16:9<br>
        • <span style="color:#ff3366">⚠ Keep screen ON while generating</span>
      </div>
    </div>
    <a href="https://wa.me/923106206972" target="_blank"
       style="display:flex;align-items:center;justify-content:center;gap:10px;margin-top:14px;padding:13px;
              border:1px solid rgba(0,255,136,0.3);border-radius:3px;background:rgba(0,255,136,0.05);text-decoration:none">
      <svg width="20" height="20" viewBox="0 0 24 24" fill="#00ff88">
        <path d="M17.472 14.382c-.297-.149-1.758-.867-2.03-.967-.273-.099-.471-.148-.67.15-.197.297-.767.966-.94 1.164-.173.199-.347.223-.644.075-.297-.15-1.255-.463-2.39-1.475-.883-.788-1.48-1.761-1.653-2.059-.173-.297-.018-.458.13-.606.134-.133.298-.347.446-.52.149-.174.198-.298.298-.497.099-.198.05-.371-.025-.52-.075-.149-.669-1.612-.916-2.207-.242-.579-.487-.5-.669-.51-.173-.008-.371-.01-.57-.01-.198 0-.52.074-.792.372-.272.297-1.04 1.016-1.04 2.479 0 1.462 1.065 2.875 1.213 3.074.149.198 2.096 3.2 5.077 4.487.709.306 1.262.489 1.694.625.712.227 1.36.195 1.871.118.571-.085 1.758-.719 2.006-1.413.248-.694.248-1.289.173-1.413-.074-.124-.272-.198-.57-.347m-5.421 7.403h-.004a9.87 9.87 0 01-5.031-1.378l-.361-.214-3.741.982.998-3.648-.235-.374a9.86 9.86 0 01-1.51-5.26c.001-5.45 4.436-9.884 9.888-9.884 2.64 0 5.122 1.03 6.988 2.898a9.825 9.825 0 012.893 6.994c-.003 5.45-4.437 9.884-9.885 9.884m8.413-18.297A11.815 11.815 0 0012.05 0C5.495 0 .16 5.335.157 11.892c0 2.096.547 4.142 1.588 5.945L.057 24l6.305-1.654a11.882 11.882 0 005.683 1.448h.005c6.554 0 11.89-5.335 11.893-11.893a11.821 11.821 0 00-3.48-8.413z"/>
      </svg>
      <span style="font-family:'Share Tech Mono',monospace;font-size:11px;color:#00ff88;letter-spacing:2px">WHATSAPP</span>
    </a>
    <div style="text-align:center;margin-top:20px;padding:16px;border-top:1px solid rgba(0,245,255,0.06);font-family:'Share Tech Mono',monospace;font-size:9px;color:#2a4a5a;letter-spacing:2px;line-height:2.2">
      CHIEF'S STITCHER © 2026 · Part of CHIEF'S FETCHER Suite<br>
      Nature clips from Pexels &amp; Pixabay · Royalty Free<br>
      <span style="color:#00f5ff">Built by CHIEF · 📱 +923106206972</span><br>
      <span style="color:rgba(0,255,136,0.6)">✓ Completely Free For All</span>
    </div>
    """)

    run_btn.click(
        fn=generate_video,
        inputs=[audio_file, duration_str, video_name, images, img_size, motion_speed, nature_cats, effect],
        outputs=[video_out, status_out]
    )

demo.launch(server_name="0.0.0.0", server_port=7860)
