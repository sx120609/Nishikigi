import asyncio
from datetime import datetime
import hashlib
import os
import random
from typing import Final

from botx.models import User
from jinja2 import Environment, FileSystemLoader, select_autoescape
from PIL import Image, ImageDraw, ImageFilter
import playwright.async_api
import qrcode


async def generate_img(
    id: int,
    user: User | None,
    contents: list,
    admin: bool = False,
    avatar_seed: int | None = None,
) -> str:
    env = Environment(
        loader=FileSystemLoader("templates"),
        trim_blocks=True,
        lstrip_blocks=True,
        autoescape=select_autoescape(
            [
                "html",
            ]
        ),
    )
    _contents = []
    for items in contents:
        values = [
            "__no_border__" if (len(items) == 1 and items[0]["type"] == "image") else ""
        ]
        for d in items:
            match (d["type"]):
                case "image":
                    if d["data"]["sub_type"] == 1:
                        # 表情包
                        values.append(
                            "_file://"
                            + os.path.abspath(f"./data/{id}/{d["data"]["file"]}")
                        )
                    else:
                        values.append(
                            "file://"
                            + os.path.abspath(f"./data/{id}/{d["data"]["file"]}")
                        )
                case "text":
                    values.append(
                        d["data"]["text"]
                        .replace("\r\n", "\n")
                        .replace("\n", "__internal_br__")
                    )
                case "face":
                    values.append(
                        "face://" + os.path.abspath(f"./face/{d["data"]["id"]}.png")
                    )
        _contents.append(values)
    # if user != None:
    #     url = f"https://3lu.cn/qq.php?qq={user.user_id}"
    #     qr = qrcode.QRCode(border=0)
    #     qr.add_data(url)
    #     img = qr.make_image(back_color="#f0f0f0")
    #     img.save(f"./data/{id}/qrcode.png")  # type: ignore

    avatar_path: str | None = None
    avatar_src = _AVATAR_PLACEHOLDER
    if user is not None:
        try:
            avatar_path = await _download_avatar(user.user_id, id)
        except Exception:
            avatar_path = None
        if avatar_path:
            avatar_src = f"file://{avatar_path}"
    elif avatar_seed is not None:
        try:
            avatar_path = _generate_anonymous_avatar(avatar_seed, id)
        except Exception:
            avatar_path = None
        if avatar_path:
            avatar_src = f"file://{avatar_path}"

    output = env.get_template("normal.html" if user else "anonymous.html").render(
        contents=_contents,
        date=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        username=None if user == None else user.nickname,
        # qrcode=os.path.abspath(f"./data/{id}/qrcode.png") if user else None,
        admin=admin,
        avatar_src=avatar_src,
    )
    with open(f"./data/{id}/page.html", mode="w") as f:
        f.write(output)
    try:
        await screenshoot(id=id, output_path=f"./data/{id}/image.png")
    finally:
        if avatar_path and os.path.exists(avatar_path):
            os.remove(avatar_path)
    return os.path.abspath(f"./data/{id}/image.png")


_HEIGHT_SCRIPT: Final[str] = """
() => {
    const doc = document.documentElement;
    const bod = document.body;
    const card = document.querySelector('.card');
    if (card) {
        return Math.max(card.scrollHeight, card.offsetHeight, card.clientHeight);
    }
    return Math.max(
        doc.scrollHeight, doc.offsetHeight, doc.clientHeight,
        bod.scrollHeight, bod.offsetHeight, bod.clientHeight
    );
}
"""


async def _prepare_page(browser: playwright.async_api.Browser, id: int, scale: int):
    page = await browser.new_page(
        viewport={"width": 720, "height": 720},
        device_scale_factor=scale,
    )
    await page.goto(
        f"file://{os.path.abspath(f"./data/{id}/page.html")}",
        wait_until="domcontentloaded",
        timeout=60_000,
    )
    height = await page.evaluate(_HEIGHT_SCRIPT)
    return page, float(height)


async def screenshoot(id: int, output_path: str):
    async with playwright.async_api.async_playwright() as p:
        browser = await p.chromium.launch(headless=True, chromium_sandbox=True)

        # 先以较低缩放加载页面获取高度, 避免超大页面渲染超时
        temp_page, height = await _prepare_page(browser, id, scale=1)
        await temp_page.close()

        page, height = await _prepare_page(browser, id, scale=3)

        viewport_height = max(720, min(int(height) + 120, 4096))
        await page.set_viewport_size({"width": 720, "height": viewport_height})

        card = page.locator(".card")
        await card.screenshot(
            type="png",
            path=output_path,
            omit_background=True,
            animations="disabled",
        )
        await page.close()
        await browser.close()


_AVATAR_PLACEHOLDER: Final[str] = (
    "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR4nGMAAQAABQABDQottAAAAABJRU5ErkJggg=="
)


def _generate_anonymous_avatar(seed: int, post_id: int) -> str:
    rng = random.Random(hashlib.sha256(str(seed).encode()).digest())
    size = 320

    bg_color = tuple(rng.randint(160, 210) for _ in range(3))
    accent = tuple(rng.randint(90, 150) for _ in range(3))
    light = tuple(min(255, c + rng.randint(35, 60)) for c in accent)
    dark = tuple(max(0, c - rng.randint(25, 45)) for c in accent)

    canvas = Image.new("RGBA", (size, size), bg_color + (255,))
    draw = ImageDraw.Draw(canvas, "RGBA")

    frame_margin = int(size * 0.08)
    draw.rounded_rectangle(
        (frame_margin, frame_margin, size - frame_margin, size - frame_margin),
        radius=int(size * 0.12),
        outline=light + (180,),
        width=6,
        fill=(255, 255, 255, 90),
    )

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

    visor_margin = int(size * 0.08)
    visor_height = int(size * 0.22)
    visor_y0 = head_y0 + visor_margin
    visor_y1 = visor_y0 + visor_height
    draw.rounded_rectangle(
        (head_x0 + visor_margin, visor_y0, head_x1 - visor_margin, visor_y1),
        radius=int(size * 0.09),
        fill=(255, 255, 255, 210),
    )

    eye_radius = int(size * 0.045)
    eye_y = (visor_y0 + visor_y1) // 2
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

    mouth_width = int(size * 0.28)
    mouth_height = int(size * 0.08)
    mouth_x0 = eye_center - mouth_width // 2
    mouth_y0 = visor_y1 + int(size * 0.06)
    mouth_x1 = mouth_x0 + mouth_width
    mouth_y1 = mouth_y0 + mouth_height
    draw.rounded_rectangle(
        (mouth_x0, mouth_y0, mouth_x1, mouth_y1),
        radius=mouth_height // 2,
        fill=dark + (220,),
    )

    body_width = int(size * 0.52)
    body_height = int(size * 0.26)
    body_x0 = (size - body_width) // 2
    body_y0 = head_y1 - int(size * 0.04)
    body_x1 = body_x0 + body_width
    body_y1 = body_y0 + body_height
    draw.rounded_rectangle(
        (body_x0, body_y0, body_x1, body_y1),
        radius=int(size * 0.1),
        fill=accent + (235,),
        outline=dark + (200,),
        width=6,
    )

    panel_margin = int(size * 0.06)
    panel_height = int(size * 0.1)
    draw.rounded_rectangle(
        (
            body_x0 + panel_margin,
            body_y0 + panel_margin,
            body_x1 - panel_margin,
            body_y0 + panel_margin + panel_height,
        ),
        radius=int(panel_height * 0.4),
        fill=(255, 255, 255, 180),
    )

    antenna_width = int(size * 0.06)
    antenna_height = int(size * 0.14)
    antenna_x = (size - antenna_width) // 2
    draw.rounded_rectangle(
        (
            antenna_x,
            head_y0 - antenna_height,
            antenna_x + antenna_width,
            head_y0,
        ),
        radius=antenna_width // 2,
        fill=accent + (235,),
    )
    antenna_tip_radius = int(size * 0.05)
    draw.ellipse(
        (
            (size // 2) - antenna_tip_radius,
            head_y0 - antenna_height - antenna_tip_radius,
            (size // 2) + antenna_tip_radius,
            head_y0 - antenna_height + antenna_tip_radius,
        ),
        fill=light + (240,),
    )

    glow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    glow_draw = ImageDraw.Draw(glow, "RGBA")
    glow_draw.ellipse(
        (
            body_x0 - int(size * 0.08),
            head_y0 - int(size * 0.12),
            body_x1 + int(size * 0.08),
            body_y1 + int(size * 0.18),
        ),
        fill=light + (90,),
    )
    glow = glow.filter(ImageFilter.GaussianBlur(radius=18))
    canvas = Image.alpha_composite(canvas, glow)

    mask = Image.new("L", (size, size), 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.ellipse((6, 6, size - 6, size - 6), fill=255)

    avatar = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    avatar.paste(canvas, (0, 0), mask)

    avatar_path = os.path.abspath(f"./data/{post_id}/anon_avatar.png")
    avatar.save(avatar_path, format="PNG")
    return avatar_path


async def _download_avatar(user_id: int, post_id: int) -> str | None:
    avatar_path = os.path.abspath(f"./data/{post_id}/avatar.png")
    url = f"https://q1.qlogo.cn/g?b=qq&nk={user_id}&s=640"
    process = await asyncio.create_subprocess_exec(
        "curl",
        "-fsSL",
        "--connect-timeout",
        "10",
        "--max-time",
        "20",
        url,
        "-o",
        avatar_path,
    )
    return_code = await process.wait()
    if return_code != 0 or not os.path.exists(avatar_path) or os.path.getsize(avatar_path) == 0:
        if os.path.exists(avatar_path):
            os.remove(avatar_path)
        return None
    return avatar_path
