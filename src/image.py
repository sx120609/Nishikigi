import asyncio
import hashlib
import os
import random
from datetime import datetime
from typing import Final

from botx.models import User
from jinja2 import Environment, FileSystemLoader, select_autoescape
from PIL import Image, ImageDraw, ImageFilter
import playwright.async_api

_AVATAR_PLACEHOLDER: Final[str] = (
    "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR4nGMAAQAABQABDQottAAAAABJRU5ErkJggg=="
)

def _abs_data_path(*parts: str) -> str:
    return os.path.abspath(os.path.join(".", "data", *parts))

async def _download_avatar(user_id: int, post_id: int) -> str | None:
    """从 QQ 头像接口下载用户头像"""
    avatar_path = _abs_data_path(str(post_id), "avatar.png")
    url = f"https://q1.qlogo.cn/g?b=qq&nk={user_id}&s=640"
    process = await asyncio.create_subprocess_exec(
        "curl",
        "-fsSL",
        "--connect-timeout", "10",
        "--max-time", "20",
        url, "-o", avatar_path,
    )
    return_code = await process.wait()
    if return_code != 0 or not os.path.exists(avatar_path) or os.path.getsize(avatar_path) == 0:
        if os.path.exists(avatar_path):
            os.remove(avatar_path)
        return None
    return avatar_path

def _generate_anonymous_avatar(seed: int, post_id: int) -> str:
    """生成匿名头像（随机机器人样式）"""
    rng = random.Random(hashlib.sha256(str(seed).encode()).digest())
    size = 320

    bg_color = tuple(rng.randint(160, 210) for _ in range(3))
    accent = tuple(rng.randint(90, 150) for _ in range(3))
    light = tuple(min(255, c + rng.randint(35, 60)) for c in accent)
    dark = tuple(max(0, c - rng.randint(25, 45)) for c in accent)

    canvas = Image.new("RGBA", (size, size), bg_color + (255,))
    draw = ImageDraw.Draw(canvas, "RGBA")

    head_width = int(size * 0.6)
    head_height = int(size * 0.52)
    head_x0 = (size - head_width) // 2
    head_y0 = int(size * 0.2)
    head_x1 = head_x0 + head_width
    head_y1 = head_y0 + head_height
    draw.rounded_rectangle(
        (head_x0, head_y0, head_x1, head_y1),
        radius=int(size * 0.12),
        fill=accent + (245,),
        outline=dark + (220,),
        width=8,
    )

    eye_radius = int(size * 0.045)
    eye_y = int(size * 0.35)
    eye_spacing = int(size * 0.16)
    eye_center = (head_x0 + head_x1) // 2
    for offset in (-eye_spacing, eye_spacing):
        draw.ellipse(
            (
                eye_center + offset - eye_radius,
                eye_y - eye_radius,
                eye_center + offset + eye_radius,
                eye_y + eye_radius,
            ),
            fill=dark + (255,),
        )

    glow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    glow_draw = ImageDraw.Draw(glow, "RGBA")
    glow_draw.ellipse(
        (0, 0, size, size),
        fill=light + (80,),
    )
    glow = glow.filter(ImageFilter.GaussianBlur(radius=18))
    canvas = Image.alpha_composite(canvas, glow)

    avatar_path = _abs_data_path(str(post_id), "anon_avatar.png")
    canvas.save(avatar_path, format="PNG")
    return avatar_path

def _avatar_src_for_template(path: str, post_id: int) -> str:
    """将绝对路径转为模板可引用的相对路径"""
    data_dir = _abs_data_path(str(post_id))
    abs_path = os.path.abspath(path)
    try:
        relative = os.path.relpath(abs_path, data_dir)
    except ValueError:
        relative = os.path.basename(abs_path)
    if not relative.startswith("."):
        relative = f"./{relative}"
    return relative


async def generate_img(
    id: int, user: User | None, contents: list, admin: bool = False, avatar_seed: int | None = None
) -> str:
    os.makedirs(_abs_data_path(str(id)), exist_ok=True)

    env = Environment(
        loader=FileSystemLoader("templates"),
        trim_blocks=True,
        lstrip_blocks=True,
        autoescape=select_autoescape(["html"]),
    )


    _contents = []
    for items in contents:
        values = [
            "__no_border__" if (len(items) == 1 and items[0]["type"] == "image") else ""
        ]
        for d in items:
            match d["type"]:
                case "image":
                    values.append(
                        "file://"
                        + os.path.abspath(f"./data/{id}/{d['data']['file']}")
                    )
                case "text":
                    values.append(
                        d["data"]["text"]
                        .replace("\r\n", "\n")
                        .replace("\n", "__internal_br__")
                    )
                case "face":
                    values.append(
                        "face://" + os.path.abspath(f"./face/{d['data']['id']}.png")
                    )
        _contents.append(values)


    avatar_src = _AVATAR_PLACEHOLDER
    avatar_path: str | None = None
    if user is not None:
        try:
            avatar_path = await _download_avatar(user.user_id, id)
        except Exception:
            avatar_path = None
        if avatar_path:
            avatar_src = _avatar_src_for_template(avatar_path, id)
    elif avatar_seed is not None:
        try:
            avatar_path = _generate_anonymous_avatar(avatar_seed, id)
        except Exception:
            avatar_path = None
        if avatar_path:
            avatar_src = _avatar_src_for_template(avatar_path, id)

    output = env.get_template("normal.html" if user else "anonymous.html").render(
        contents=_contents,
        date=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        username=None if user is None else user.nickname,
        user_id=None if user is None else user.user_id,
        admin=admin,
        avatar_src=avatar_src,
    )

    with open(_abs_data_path(str(id), "page.html"), mode="w", encoding="utf-8") as f:
        f.write(output)

    await screenshoot(id=id, output_path=_abs_data_path(str(id), "image.png"))
    if avatar_path and os.path.exists(avatar_path):
        os.remove(avatar_path)
    return _abs_data_path(str(id), "image.png")

async def screenshoot(id: int, output_path: str):
    async with playwright.async_api.async_playwright() as p:
        browser = await p.chromium.launch(headless=True, chromium_sandbox=True)
        page = await browser.new_page(
            viewport={"width": 720, "height": 720}, device_scale_factor=4,
        )
        await page.goto(
            f"file://{_abs_data_path(str(id), 'page.html')}",
            wait_until="networkidle",
        )
        h = await page.evaluate(
            """
            () => {
                const doc = document.documentElement;
                const bod = document.body;
                const h = Math.max(
                    doc.scrollHeight, doc.offsetHeight, doc.clientHeight,
                    bod.scrollHeight, bod.offsetHeight, bod.clientHeight
                ) - 100;
                const el = document.querySelector('.blur-bg');
                if (el && h <= 1500) {
                    el.style.height = h + 'px';
                }
                return h;
            }
            """
        )
        print(f"[screenshoot] computed height: {h}")
        await page.screenshot(
            type="png",
            full_page=True,
            path=output_path,
            omit_background=True,
            animations="disabled",
        )
        await browser.close()
