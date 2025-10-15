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

    base_color = tuple(rng.randint(60, 150) for _ in range(3))
    accent_color = tuple(rng.randint(120, 220) for _ in range(3))
    highlight_color = tuple(min(255, accent_color[i] + rng.randint(15, 40)) for i in range(3))

    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    background = Image.new("RGBA", (size, size), base_color + (235,))
    canvas.paste(background, (0, 0))

    draw = ImageDraw.Draw(canvas, "RGBA")
    layer_scales = (0.85, 0.65, 0.45)
    for index, scale in enumerate(layer_scales):
        radius = int(size * scale)
        cx = rng.randint(int(size * 0.25), int(size * 0.75))
        cy = rng.randint(int(size * 0.25), int(size * 0.75))
        mix = (index + 1) / (len(layer_scales) + 1)
        color = tuple(
            int(base_color[i] * (1 - mix) + highlight_color[i] * mix)
            for i in range(3)
        ) + (160,)
        bbox = (cx - radius, cy - radius, cx + radius, cy + radius)
        draw.ellipse(bbox, fill=color)

    accent = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    accent_draw = ImageDraw.Draw(accent, "RGBA")
    for _ in range(3):
        radius = rng.randint(int(size * 0.15), int(size * 0.35))
        cx = rng.randint(int(size * 0.2), int(size * 0.8))
        cy = rng.randint(int(size * 0.2), int(size * 0.8))
        color = accent_color + (rng.randint(120, 200),)
        bbox = (cx - radius, cy - radius, cx + radius, cy + radius)
        accent_draw.ellipse(bbox, fill=color)
    accent = accent.filter(ImageFilter.GaussianBlur(radius=18))
    canvas = Image.alpha_composite(canvas, accent)

    sheen = Image.new("RGBA", (size, size), (255, 255, 255, 0))
    sheen_draw = ImageDraw.Draw(sheen, "RGBA")
    sheen_draw.ellipse((size * 0.05, size * 0.05, size * 0.95, size * 0.65), fill=(255, 255, 255, 90))
    sheen = sheen.rotate(-15, resample=Image.BICUBIC, expand=False)
    sheen = sheen.filter(ImageFilter.GaussianBlur(radius=14))
    canvas = Image.alpha_composite(canvas, sheen)

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
