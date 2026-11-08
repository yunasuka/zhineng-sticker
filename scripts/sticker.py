#!/usr/bin/env python3
"""表情包工厂：参考图 → 16 帧精灵图 → 240×240 GIF → 微信投稿素材包。

用法见 SKILL.md。依赖 Python 3.9+ 和 Pillow。
基于 ray-sticker-shop（MIT，Ray Wang）修改：加了选题库 new、Codex 内置出图、智能切图、白底抠图、批量处理、Windows 兼容。
"""
import argparse
import base64
import concurrent.futures as cf
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageOps, ImageStat

GRID = 4
SHEET = "960x960"
GIF_SIZE = 240
MAX_GIF = 500 * 1024
MAX_ICON = 100 * 1024
# 官方 token 单价（美元），用来记账；走中转站时以中转站账单为准
PRICE = {"text": 5e-6, "image": 8e-6, "out": 30e-6}
BANNED = ["微信", "wechat", "腾讯", "官方", "认证", "正版"]


# ---------- 生成 ----------

def _prompt_sheet(pack, s):
    style = pack.get("style", "").strip()
    caption = (s.get("caption") or "").strip()
    cap = (
        f'Add one big bold hand-lettered caption at the bottom of every cell, identical in every frame, reading exactly: "{caption}". '
        "Give the caption and the character a clean thick white sticker outline. No other text."
        if caption
        else "No text anywhere. Give the character a clean thick white sticker outline."
    )
    return (
        "Use the character in the input image exactly: same face, body shape, colors, clothing and line style. "
        + (f"Style notes: {style}. " if style else "")
        + "Draw a 4x4 animation sprite sheet of one short looping chat-sticker animation: 16 equal square cells, "
        "read left to right, top to bottom, each cell is the next frame. "
        "No grid lines, no borders, no cell numbers, fully transparent background. "
        "Same character size and same ground position in every cell, keep everything fully inside its cell with margin. "
        "Exaggerated, readable facial expression at small size. All shapes solid and crisp: no motion blur, no dust clouds, no semi-transparent effects. The last frame must flow smoothly back to the first. "
        f"Action: {s['action']}. {cap}"
    )


ASSET_PROMPTS = {
    "cover": (
        "1024x1024", "transparent",
        "Use the character in the input image exactly. Clean front-facing half-body portrait, centered, filling the frame, "
        "friendly expression, fully transparent background, no text, no white outline, no border.",
    ),
    "icon": (
        "1024x1024", "transparent",
        "Use the character in the input image exactly. Only the head, front-facing, centered, filling the frame, "
        "simple and bold so it reads at very small size, fully transparent background, no text, no white outline, no border.",
    ),
    "banner": (
        "1504x800", "opaque",
        "Use the character in the input image exactly. A rich, lively wide scene that tells a tiny story about this character, "
        "bright saturated colorful background (never white or near-white), absolutely no text, letters, logos or watermarks.",
    ),
    "reward_guide": (
        "1504x1120", "opaque",
        "Use the character in the input image exactly. The character cutely asks for a little tip, holding out both hands, "
        "bright saturated colorful background (never white or near-white), absolutely no text, letters, logos, QR codes or watermarks.",
    ),
    "reward_thanks": (
        "1024x1024", "opaque",
        "Use the character in the input image exactly. The character bows and blows hearts to say thank you, "
        "bright saturated colorful background (never white or near-white), absolutely no text, letters, logos, QR codes or watermarks.",
    ),
}


def _edit(image_path, prompt, size, background, model, quality, log_path):
    base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        sys.exit("缺 OPENAI_API_KEY。没有 key 时用 prompts 命令拿提示词，到 ChatGPT 或 Codex 里出图，再跑 build。")
    boundary = uuid.uuid4().hex
    fields = {"model": model, "prompt": prompt, "size": size, "quality": quality, "complexity": "medium", "background": background}
    body = b"".join(
        f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode() for k, v in fields.items()
    )
    body += (
        f'--{boundary}\r\nContent-Disposition: form-data; name="image[]"; filename="ref.png"\r\nContent-Type: image/png\r\n\r\n'.encode()
        + Path(image_path).read_bytes()
        + f"\r\n--{boundary}--\r\n".encode()
    )
    req = urllib.request.Request(
        f"{base}/images/edits",
        data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                data = json.load(r)
            break
        except urllib.error.HTTPError as e:
            msg = e.read().decode(errors="replace")[:400]
            if e.code in (429, 500, 502, 503, 504) and attempt < 3:
                time.sleep(5 * (attempt + 1))
                continue
            raise RuntimeError(f"HTTP {e.code}: {msg}") from None
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt == 3:
                raise RuntimeError(str(e)) from None
            time.sleep(5 * (attempt + 1))
    u = data.get("usage") or {}
    d = u.get("input_tokens_details") or {}
    cost = u.get("output_tokens", 0) * PRICE["out"] + d.get("image_tokens", 0) * PRICE["image"] + d.get("text_tokens", 0) * PRICE["text"]
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps({"size": size, "usage": u, "usd": round(cost, 4)}) + "\n")
    return base64.b64decode(data["data"][0]["b64_json"])


def load_pack(pack_dir):
    p = Path(pack_dir)
    if not (p / "pack.json").exists():
        sys.exit(f"{p} 里没有 pack.json，先跑 new 建一套")
    # Windows 默认编码是 GBK，不写 utf-8 中文会读坏
    pack = json.loads((p / "pack.json").read_text(encoding="utf-8"))
    return p, pack


def cmd_character(a):
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    prompt = (
        "Turn the subject of this photo into an original, heavily stylized cute cartoon sticker character: "
        f"{a.style}. Keep the identifying features (hairstyle, glasses, markings, signature clothing or fur pattern). "
        "It must clearly read as a drawing, never photorealistic. Front-facing full body, centered, "
        "fully transparent background, no text, no border."
    )
    log = out.parent / "costs.jsonl"
    out.write_bytes(_edit(a.photo, prompt, "1024x1024", "transparent", a.model, a.quality, log))
    print("角色图：", out)


def _gen_one(job):
    target, ref, prompt, size, bg, model, quality, log = job
    if target.exists():
        return f"跳过（已有）{target.name}"
    target.write_bytes(_edit(ref, prompt, size, bg, model, quality, log))
    return f"完成 {target.name}"


def _jobs(p, pack, model, quality, with_assets, redo=None):
    ref = p / pack["character"]
    if not ref.exists():
        sys.exit(f"找不到角色图 {ref}")
    log = p / "costs.jsonl"
    (p / "sheets").mkdir(exist_ok=True)
    (p / "assets_raw").mkdir(exist_ok=True)
    jobs = []
    for i, s in enumerate(pack["stickers"], 1):
        t = p / "sheets" / f"{i:02d}.png"
        if redo and i in redo and t.exists():
            t.unlink()
        jobs.append((t, ref, _prompt_sheet(pack, s), SHEET, "transparent", model, quality, log))
    if with_assets:
        for name, (size, bg, prompt) in ASSET_PROMPTS.items():
            jobs.append((p / "assets_raw" / f"{name}.png", ref, prompt, size, bg, model, quality, log))
    return jobs


def cmd_gen(a):
    p, pack = load_pack(a.pack)
    redo = {int(x) for x in a.redo.split(",")} if a.redo else None
    jobs = _jobs(p, pack, a.model, a.quality, not a.no_assets, redo)
    todo = [j for j in jobs if not j[0].exists()]
    print(f"要生成 {len(todo)} 张，按官方单价约 ${len(todo) * 0.052:.2f}（重抽另算）")
    if a.dry_run:
        return
    errors = []
    with cf.ThreadPoolExecutor(a.jobs) as ex:
        for j, f in zip(todo, [ex.submit(_gen_one, j) for j in todo]):
            try:
                print(f.result(), flush=True)
            except Exception as e:  # 单张失败不拖垮整套，重跑只补缺的
                errors.append(j[0].name)
                print(f"失败 {j[0].name}: {e}", flush=True)
    if errors:
        print("失败的可以直接重跑 gen，只补缺的：", ", ".join(errors))
    print(f"本目录累计花费约 ${total_cost(p):.2f}")


def total_cost(p):
    log = p / "costs.jsonl"
    if not log.exists():
        return 0.0
    return sum(json.loads(x)["usd"] for x in log.read_text(encoding="utf-8").splitlines() if x.strip())


def cmd_prompts(a):
    """没有 API key 时：把每张图的提示词写进 prompts/，在 Codex 里用内置出图或到 ChatGPT 网页出图，存回 sheets/、assets_raw/。"""
    for d in a.pack:
        p, pack = load_pack(d)
        out = p / "prompts"
        out.mkdir(exist_ok=True)
        todo = []
        for i, s in enumerate(pack["stickers"], 1):
            if not (p / "sheets" / f"{i:02d}.png").exists():
                (out / f"sheet_{i:02d}.txt").write_text(_prompt_sheet(pack, s), encoding="utf-8")
                todo.append(f"sheets/{i:02d}.png  ← prompts/sheet_{i:02d}.txt（正方形，透明底）")
        for name, (size, _, prompt) in ASSET_PROMPTS.items():
            if not (p / "assets_raw" / f"{name}.png").exists():
                (out / f"{name}.txt").write_text(prompt, encoding="utf-8")
                todo.append(f"assets_raw/{name}.png  ← prompts/{name}.txt（{size}）")
        print(f"== {p}：还缺 {len(todo)} 张，每张都要附上角色图 {pack['character']}")
        for t in todo:
            print("  " + t)


# 选题库：每套都是聊天里天天发的话，动作各不相同（审核要求差异足够）。new 命令从这里挑
THEMES = {
    "打工人": [
        ("收到", "收到", "snaps upright, nods twice energetically and gives a big thumbs up, then relaxes back"),
        ("好的", "好的", "makes a big OK sign next to its face and winks, bouncing slightly"),
        ("在吗", "在吗", "peeks in from the side of the frame, waves one hand curiously, then slides back"),
        ("马上", "马上来", "sprints in place with legs spinning in a blur-free cartoon cycle, sweat drops flying"),
        ("加班", "加班中", "types furiously on a tiny laptop with dark eye bags, coffee cup shaking"),
        ("摸鱼", "摸鱼中", "sits relaxed sipping a small cup of tea with eyes closed, steam curls rising"),
        ("下班", "下班啦", "throws both arms up and jumps for joy, then lands and spins once"),
        ("不想上班", "不想上班", "bursts into huge cartoon waterfall tears, shaking with sobs, then sniffles"),
        ("谢谢老板", "谢谢老板", "bows deeply with both hands pressed together, sparkles around, rises with a smile"),
        ("辛苦了", "辛苦了", "pats an invisible shoulder gently twice and smiles warmly"),
        ("无语", "无语", "stares blankly, a big sweat drop slides down, three dots appear one by one above the head"),
        ("震惊", "震惊", "jumps back in shock, eyes huge, hair standing on end, then freezes"),
        ("干饭", "干饭", "holds a big rice bowl and shovels food in happily with chopsticks, cheeks puffing"),
        ("躺平", "躺平", "flops flat on the ground face-up, arms spread, a tiny zzz rises"),
        ("加油", "加油", "pumps one fist in the air twice with a fiery determined face"),
        ("晚安", "晚安", "yawns widely, rubs one eye, then curls up with a little moon above the head"),
    ],
    "萌宠": [
        ("早安", "早安", "stretches long like a waking pet, yawns, then sits up bright-eyed"),
        ("晚安", "晚安", "curls into a ball, tail wrapping around, eyes slowly closing, zzz rising"),
        ("抱抱", "抱抱", "opens both front paws wide and leans in for a hug, blushing, hearts popping"),
        ("贴贴", "贴贴", "rubs its cheek side to side against the frame edge affectionately"),
        ("饿了", "饿了", "holds out an empty bowl with big shiny begging eyes, tummy rumbling lines"),
        ("开心", "开心", "bounces up and down happily with tail wagging fast"),
        ("委屈", "委屈", "lowers ears, lip trembling, big glossy tears welling up without falling"),
        ("生气", "哼", "turns head away with puffed cheeks, a small anger mark pulsing"),
        ("在吗", "在吗", "pokes its head up from below the frame, ears twitching, then ducks down"),
        ("好耶", "好耶", "jumps up with both paws raised, little confetti bursting"),
        ("想你", "想你了", "hugs a small heart pillow and sways side to side dreamily"),
        ("谢谢", "谢谢", "bows its head with paws together, a small heart floats up"),
        ("睡觉", "困了", "head nods down slowly, jerks awake, nods down again"),
        ("溜了", "溜了", "turns and scampers off sideways, then peeks back once"),
        ("疑问", "啊？", "tilts head sharply to one side, a big question mark pops above"),
        ("爱你", "爱你", "forms a heart shape with both paws above the head, winking"),
    ],
    "情侣": [
        ("想你", "想你了", "hugs itself tightly and sways, tiny hearts floating up"),
        ("爱你", "爱你", "makes a big heart with both arms over the head and blows a kiss"),
        ("抱抱", "抱抱", "runs forward with arms open wide and squeezes into a hug pose"),
        ("亲亲", "亲亲", "puckers lips and leans in, a kiss mark floats out"),
        ("哄我", "哄我", "pouts with arms crossed, peeks sideways waiting to be comforted"),
        ("不理你", "不理你了", "turns its back with a huff, then glances back once"),
        ("晚安", "晚安", "tucks into a blanket, blows a goodnight kiss, then closes eyes"),
        ("早安", "早安", "pops up from bed with a big smile and waves both hands"),
        ("在干嘛", "在干嘛", "leans in close to the camera with curious sparkling eyes"),
        ("吃醋", "吃醋", "cheeks puffed, holds a little vinegar bottle, steam puffs from head"),
        ("乖", "乖", "pats an invisible head gently with a soft smile"),
        ("好的", "好哒", "nods quickly twice with a cute salute"),
        ("开心", "开心", "spins around once with arms out, flowers popping"),
        ("委屈", "委屈", "lip trembling, eyes glossy, tugs at its own sleeve"),
        ("宝贝", "宝贝", "holds a big glowing heart in both hands and offers it forward"),
        ("等你", "等你", "sits hugging knees, looks left, looks right, sighs"),
    ],
    "长辈祝福": [
        ("早上好", "早上好", "raises a teacup in greeting with a big warm smile, sun rising behind"),
        ("发财", "恭喜发财", "bows with fists cupped in greeting, gold coins raining down"),
        ("财运", "财运亨通", "holds up a big gold ingot that sparkles, bouncing with joy"),
        ("来财", "八方来财", "spreads arms wide as red envelopes fly in from all sides"),
        ("平安", "平安喜乐", "holds a red lucky knot and sways gently, soft glow"),
        ("身体好", "身体健康", "does a slow tai chi move then gives a thumbs up"),
        ("顺利", "万事顺利", "walks forward confidently, little flowers blooming behind each step"),
        ("谢谢", "谢谢", "bows twice with hands together, a red flower pops out"),
        ("点赞", "点赞", "gives two big thumbs up, sparkles bursting"),
        ("好运", "好运连连", "catches a falling four-leaf clover and holds it up proudly"),
        ("吉祥", "吉祥如意", "holds a ruyi scepter and nods with a gentle smile"),
        ("开心", "天天开心", "claps hands happily with red lanterns swinging"),
        ("收到", "收到", "nods solemnly, then gives a firm OK gesture"),
        ("晚安", "晚安好梦", "waves goodbye under a crescent moon and twinkling stars"),
        ("辛苦", "辛苦了", "offers a cup of hot tea with both hands, steam rising"),
        ("福", "福气满满", "holds up a big red fu character card, it flips around and glows"),
    ],
}


def cmd_new(a):
    """从选题库建一套：写 pack.json，再按需改"""
    if a.theme not in THEMES:
        sys.exit(f"没有这个主题。可选：{' / '.join(THEMES)}")
    p = Path(a.dir)
    if (p / "pack.json").exists():
        sys.exit(f"{p / 'pack.json'} 已存在，不覆盖")
    p.mkdir(parents=True, exist_ok=True)
    items = THEMES[a.theme][: a.count]
    pack = {
        "name": a.name or f"{a.theme}表情",
        "intro": "一句话讲讲这个角色的性格或者小故事（≤80 字）",
        "copyright": "你的艺术家名",
        "reward_line": "赞赏引导语，5到15个字",
        "character": "character.png",
        "style": "thick clean dark outlines, flat colors",
        "frame_ms": 120,
        "background": "transparent",
        "stickers": [{"word": w, "caption": c, "action": act} for w, c, act in items],
    }
    (p / "pack.json").write_text(json.dumps(pack, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"建好了：{p / 'pack.json'}（{a.theme} {len(items)} 张）。把角色图存成 {p / 'character.png'}，名字、介绍、版权改成自己的")


# ---------- 合成 ----------

def _key_white(s):
    """ChatGPT 网页有时给白底不给透明：从四边把连着的白色背景抠掉"""
    if s.getchannel("A").getextrema()[0] < 8:
        return s, False
    MAGIC = (255, 0, 254)
    rgb = s.convert("RGB")
    w, h = rgb.size
    step = max(1, min(w, h) // 64)
    seeds = [(x, y) for x in range(0, w, step) for y in (0, h - 1)] + [(x, y) for y in range(0, h, step) for x in (0, w - 1)]
    for xy in seeds:
        if min(rgb.getpixel(xy)) > 225:
            ImageDraw.floodfill(rgb, xy, MAGIC, thresh=40)
    mask = Image.eval(ImageChops.difference(rgb, Image.new("RGB", rgb.size, MAGIC)).convert("L"), lambda v: 255 if v > 6 else 0)
    out = s.copy()
    out.putalpha(mask)
    return out, True


def _cuts(alpha, axis):
    """沿一个方向找 GRID-1 条切线：在等分线附近找最空的那一列 / 行。模型画的格子常常不齐，硬切会切到角色"""
    length = alpha.width if axis == "x" else alpha.height
    solid = alpha.point(lambda v: 255 if v > 128 else 0)
    prof = solid.resize((length, 1) if axis == "x" else (1, length), Image.BOX)
    occ = list(prof.tobytes())  # L 模式一个像素一个字节
    cell = length / GRID
    cuts = [0]
    for k in range(1, GRID):
        mid = round(k * cell)
        win = range(max(1, mid - round(cell * 0.2)), min(length - 1, mid + round(cell * 0.2)))
        best = min(win, key=lambda i: (occ[i], abs(i - mid)))
        cuts.append(best if occ[best] < 8 else mid)  # 找不到空隙就退回等分
    cuts.append(length)
    return cuts


def _slice(sheet_path):
    s = Image.open(sheet_path).convert("RGBA")
    s, keyed = _key_white(s)
    xs, ys = _cuts(s.getchannel("A"), "x"), _cuts(s.getchannel("A"), "y")
    # 每格按自己的中心贴到统一大小的画布上，保证每帧缩放比例一致
    cw, ch = s.width // GRID, s.height // GRID
    frames, border = [], 0
    for y in range(GRID):
        for x in range(GRID):
            box = (xs[x], ys[y], xs[x + 1], ys[y + 1])
            c = s.crop(box)
            canvas = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
            canvas.paste(c, ((cw - c.width) // 2, (ch - c.height) // 2))
            frames.append(canvas.resize((GIF_SIZE, GIF_SIZE), Image.LANCZOS))
            # 内容压到格子边上说明这一格可能被切断
            px = c.getchannel("A").load()
            edge = sum(px[i, 0] > 128 or px[i, c.height - 1] > 128 for i in range(c.width)) + sum(
                px[0, j] > 128 or px[c.width - 1, j] > 128 for j in range(c.height)
            )
            border = max(border, edge)
    return frames, border, keyed


def _encode(frames, path, ms, colors, bg):
    """全局调色板编码。全局调色板让相邻帧的像素能复用，GIF 体积明显更小。"""
    flat = []
    for f in frames:
        base = Image.new("RGBA", f.size, "white")
        base.alpha_composite(f)
        flat.append(base.convert("RGB"))
    strip = Image.new("RGB", (GIF_SIZE * len(flat), GIF_SIZE))
    for i, f in enumerate(flat):
        strip.paste(f, (i * GIF_SIZE, 0))
    pal = strip.quantize(colors=colors - 1 if bg == "transparent" else colors, method=Image.MEDIANCUT, dither=Image.Dither.NONE)
    out = []
    for f, rgb in zip(frames, flat):
        q = rgb.quantize(palette=pal, dither=Image.Dither.NONE)
        if bg == "transparent":
            mask = f.getchannel("A").point(lambda a: 255 if a < 128 else 0)
            q.paste(colors - 1, (0, 0), mask)
        out.append(q)
    kw = {"save_all": True, "append_images": out[1:], "duration": ms, "loop": 0, "optimize": False}
    if bg == "transparent":
        kw.update(transparency=colors - 1, disposal=2)
    out[0].save(path, **kw)


def build_gif(sheet, path, ms, bg, keep=None):
    frames, border, keyed = _slice(sheet)
    if keep:  # 循环接不上时只留一段帧，写进 pack.json 才不会被下次 build 覆盖
        frames = frames[keep[0] - 1 : keep[1]]
    for frs, dur in ((frames, ms), (frames[::2], ms * 2)):  # 压不下去再抽一半帧
        for colors in (128, 96, 64, 48):
            _encode(frs, path, dur, colors, bg)
            if path.stat().st_size <= MAX_GIF:
                return path.stat().st_size, colors, len(frs), border, keyed
    return path.stat().st_size, colors, len(frs), border, keyed


def _fit_png(src, size, dst, limit, keep_alpha):
    im = Image.open(src).convert("RGBA")
    if keep_alpha:
        bbox = im.getchannel("A").point(lambda a: 255 if a > 8 else 0).getbbox()
        if bbox:
            im = im.crop(bbox)
        side = max(im.size)
        square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
        square.paste(im, ((side - im.width) // 2, (side - im.height) // 2))
        im = square.resize(size, Image.LANCZOS)
        im.save(dst, optimize=True)
    else:
        im = ImageOps.fit(im.convert("RGB"), size, Image.LANCZOS)
        im.save(dst, optimize=True)
        if dst.stat().st_size > limit:
            im.quantize(colors=200).save(dst, optimize=True)


def build_one(d):
    p, pack = load_pack(d)
    sub = p / "submit"
    gifs = sub / "表情图"
    gifs.mkdir(parents=True, exist_ok=True)
    ms = pack.get("frame_ms", 120)
    bg = pack.get("background", "transparent")
    rows = []
    for i, s in enumerate(pack["stickers"], 1):
        sheet = p / "sheets" / f"{i:02d}.png"
        if not sheet.exists():
            print(f"缺 {sheet.name}，先跑 gen 或把图放进 sheets/")
            continue
        size, colors, n, border, keyed = build_gif(sheet, gifs / f"{i:02d}.gif", ms, bg, s.get("keep_frames"))
        flag = "  ⚠ 内容压到格子边，重点看这张" if border > 8 else ""
        flag += "  （白底已自动抠成透明，看看白描边有没有被吃掉）" if keyed else ""
        print(f"{i:02d} {s['word']}: {size // 1024}KB {n}帧 {colors}色{flag}")
        rows.append(gifs / f"{i:02d}.gif")
    raw = p / "assets_raw"
    specs = [
        ("cover", (240, 240), "封面图.png", 500, True),
        ("icon", (50, 50), "聊天面板图标.png", 100, True),
        ("banner", (750, 400), "详情页横幅.png", 500, False),
        ("reward_guide", (750, 560), "赞赏引导图.png", 500, False),
        ("reward_thanks", (750, 750), "赞赏致谢图.png", 500, False),
    ]
    for name, size, fn, kb, alpha in specs:
        if (raw / f"{name}.png").exists():
            _fit_png(raw / f"{name}.png", size, sub / fn, kb * 1024, alpha)
    _contact_sheet(rows, pack, p / "预览.png")
    _preview_html(pack, p)
    _listing(pack, p)
    with zipfile.ZipFile(p / "交付给客户.zip", "w", zipfile.ZIP_STORED) as z:
        for g in rows:
            z.write(g, f"{pack['stickers'][int(g.stem) - 1]['word']}.gif")
    print("输出：", sub, "；预览.png / 预览.html / 上架文案.md / 交付给客户.zip")


def _contact_sheet(gifs, pack, dst):
    if not gifs:
        return
    cols = min(4, len(gifs))
    rows = (len(gifs) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * GIF_SIZE, rows * GIF_SIZE), "#e9ecef")
    for i, g in enumerate(gifs):
        im = Image.open(g)
        im.seek(im.n_frames // 2)
        fr = im.convert("RGBA")
        sheet.paste(fr, ((i % cols) * GIF_SIZE, (i // cols) * GIF_SIZE), fr)
    sheet.save(dst)


def _preview_html(pack, p):
    cells = "".join(
        f'<figure><img src="submit/表情图/{i:02d}.gif"><figcaption>{s["word"]}</figcaption></figure>'
        for i, s in enumerate(pack["stickers"], 1)
    )
    (p / "预览.html").write_text(
        f"""<!doctype html><meta charset="utf-8"><title>{pack.get('name','表情包')}</title>
<style>body{{font:14px -apple-system,sans-serif;margin:24px;background:#ededed}}main{{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:16px}}
figure{{margin:0;text-align:center}}img{{width:120px;height:120px;background:#fff;border-radius:8px}}
.dark img{{background:#191919}}button{{margin-bottom:16px}}</style>
<h2>{pack.get('name','')}</h2><button onclick="document.body.classList.toggle('dark')">切换深色聊天背景</button>
<main>{cells}</main>""",
        encoding="utf-8",
    )


def _listing(pack, p):
    lines = [
        f"# {pack.get('name','')} 上架文案\n",
        f"- 表情名称：{pack.get('name','')}",
        f"- 表情介绍：{pack.get('intro','')}",
        f"- 版权信息：{pack.get('copyright','')}",
        f"- 赞赏引导语：{pack.get('reward_line','')}\n",
        "| 序号 | 含义词 |",
        "|---|---|",
    ]
    lines += [f"| {i:02d} | {s['word']} |" for i, s in enumerate(pack["stickers"], 1)]
    (p / "上架文案.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------- 检查 ----------

def _dhash(im):
    g = im.convert("L").resize((9, 8), Image.LANCZOS)
    px = g.load()
    return sum(1 << (x + y * 8) for y in range(8) for x in range(8) if px[x, y] > px[x + 1, y])


def _flat_frame(im, n):
    im.seek(n)
    fr = im.convert("RGBA")
    base = Image.new("RGBA", fr.size, "white")
    base.alpha_composite(fr)
    return base.convert("RGB")


def _chars(s):
    return len(s)


def check_one(d):
    p, pack = load_pack(d)
    print(f"== 检查 {p}")
    sub = p / "submit"
    fails, warns = [], []
    F = fails.append
    W = warns.append

    name, intro, cr, line = pack.get("name", ""), pack.get("intro", ""), pack.get("copyright", ""), pack.get("reward_line", "")
    if not 1 <= _chars(name) <= 8 or re.search(r"[\s，。！？、,.!?]", name):
        F(f"表情名称要 1–8 字、无标点无空格：{name!r}")
    if _chars(intro) > 80:
        F(f"表情介绍超过 80 字：{_chars(intro)}")
    if _chars(cr) > 10:
        F(f"版权信息超过 10 字：{cr!r}")
    if line and not 5 <= _chars(line) <= 15:
        F(f"赞赏引导语要 5–15 字：{line!r}")
    for field, text in (("名称", name), ("介绍", intro), ("引导语", line)):
        for b in BANNED:
            if b.lower() in text.lower():
                F(f"{field}里有「{b}」，容易被驳回")

    stickers = pack["stickers"]
    if not 8 <= len(stickers) <= 24:
        F(f"一套要 8–24 张，现在 {len(stickers)} 张")
    words = [s["word"] for s in stickers]
    for w in words:
        if _chars(w) > 4 or re.search(r"[^\w]", w):
            F(f"含义词要 ≤4 字、不带符号：{w!r}")
    for w in {x for x in words if words.count(x) > 1}:
        F(f"含义词重复：{w}")
    for s in stickers:
        for b in BANNED:
            if b.lower() in (s.get("caption") or "").lower():
                F(f"配字里有「{b}」：{s['caption']}")

    hashes = []
    for i, s in enumerate(stickers, 1):
        g = sub / "表情图" / f"{i:02d}.gif"
        tag = f"{i:02d} {s['word']}"
        if not g.exists():
            F(f"{tag} 缺 GIF")
            continue
        im = Image.open(g)
        if im.size != (240, 240):
            F(f"{tag} 尺寸 {im.size}")
        kb = g.stat().st_size / 1024
        if kb > 500:
            F(f"{tag} 体积 {kb:.0f}KB 超 500KB")
        if getattr(im, "n_frames", 1) < 2:
            F(f"{tag} 不是动图")
            continue
        if im.info.get("loop") != 0:
            F(f"{tag} 没设无限循环")
        n = im.n_frames
        first, mid = _flat_frame(im, 0), _flat_frame(im, n // 2)
        diff = ImageStat.Stat(ImageChops.difference(first, mid).convert("L")).mean[0]
        if diff < 1.0:
            W(f"{tag} 动作太小（帧差 {diff:.1f}），可能看着不动")
        last = _flat_frame(im, n - 1)
        if ImageStat.Stat(ImageChops.difference(first, last).convert("L")).mean[0] > 18:
            W(f"{tag} 首尾帧差别大，循环可能跳一下")
        hashes.append((tag, _dhash(mid)))
    for x in range(len(hashes)):
        for y in range(x + 1, len(hashes)):
            d = bin(hashes[x][1] ^ hashes[y][1]).count("1")
            if d < 6:
                W(f"{hashes[x][0]} 和 {hashes[y][0]} 太像（距离 {d}），官方要求差异足够")

    for fn, size, kb, alpha in (
        ("封面图.png", (240, 240), 500, True),
        ("聊天面板图标.png", (50, 50), 100, True),
        ("详情页横幅.png", (750, 400), 500, False),
    ):
        f = sub / fn
        if not f.exists():
            F(f"缺 {fn}")
            continue
        _check_img(f, fn, size, kb, alpha, F, W)
    for fn, size in (("赞赏引导图.png", (750, 560)), ("赞赏致谢图.png", (750, 750))):
        f = sub / fn
        if f.exists():
            _check_img(f, fn, size, 500, False, F, W)
        elif line:
            F(f"写了赞赏引导语但缺 {fn}")

    for x in warns:
        print("WARN", x)
    for x in fails:
        print("FAIL", x)
    print(f"结果：{len(fails)} 个必须改，{len(warns)} 个要人看。")
    print("机器看不了的，交给人：配字有没有错别字、角色有没有变形、横幅有没有文字、权利是否清楚。")
    return len(fails)


def cmd_build(a):
    for d in a.pack:
        build_one(d)


def cmd_check(a):
    sys.exit(min(sum(check_one(d) for d in a.pack), 100))


def cmd_all(a):
    """批量：合成再检查，一次处理多套"""
    for d in a.pack:
        build_one(d)
    cmd_check(a)


def _check_img(f, fn, size, kb, alpha, F, W):
    im = Image.open(f)
    if im.size != size:
        F(f"{fn} 尺寸 {im.size}，要 {size}")
    if f.stat().st_size > kb * 1024:
        F(f"{fn} 超过 {kb}KB")
    rgba = im.convert("RGBA")
    lo = rgba.getchannel("A").getextrema()[0]
    if alpha and lo > 8:
        F(f"{fn} 要透明背景")
    if not alpha:
        if lo < 250:
            F(f"{fn} 不能用透明背景")
        if min(ImageStat.Stat(rgba.convert("RGB")).mean) > 235:
            W(f"{fn} 背景太接近白色")


# ---------- 入口 ----------

def main():
    if hasattr(sys.stdout, "reconfigure"):  # Windows 终端默认 GBK，中文和 ⚠ 会报错
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__)
    sp = ap.add_subparsers(dest="cmd", required=True)

    c = sp.add_parser("character", help="照片变卡通角色图")
    c.add_argument("--photo", required=True)
    c.add_argument("--out", required=True)
    c.add_argument("--style", default="cute chibi, two-head-tall, thick clean dark outlines, flat soft colors")
    c.add_argument("--model", default="gpt-image-2.6-sunburst")
    c.add_argument("--quality", default="medium")
    c.set_defaults(fn=cmd_character)

    g = sp.add_parser("gen", help="生成精灵图和配套素材，已有的自动跳过")
    g.add_argument("pack")
    g.add_argument("--model", default="gpt-image-2.6-flare")
    g.add_argument("--quality", default="medium")
    g.add_argument("--jobs", type=int, default=4)
    g.add_argument("--redo", help="重抽这几张，如 3,7")
    g.add_argument("--no-assets", action="store_true")
    g.add_argument("--dry-run", action="store_true", help="只报数量和预估花费")
    g.set_defaults(fn=cmd_gen)

    n = sp.add_parser("new", help="从选题库建一套 pack.json")
    n.add_argument("dir")
    n.add_argument("--theme", default="打工人", help=" / ".join(THEMES))
    n.add_argument("--count", type=int, default=8, choices=[8, 16], help="张数")
    n.add_argument("--name", help="表情名称，≤8 字")
    n.set_defaults(fn=cmd_new)

    for name, fn, h in (
        ("prompts", cmd_prompts, "把缺的图的提示词写进 prompts/，给用 Codex 内置出图或 ChatGPT 网页的人"),
        ("build", cmd_build, "切帧、合 GIF、出投稿包"),
        ("check", cmd_check, "按微信规范检查"),
        ("all", cmd_all, "build + check，可以一次给多套目录"),
    ):
        s = sp.add_parser(name, help=h)
        s.add_argument("pack", nargs="+", help="一套或多套的目录")
        s.set_defaults(fn=fn)

    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
