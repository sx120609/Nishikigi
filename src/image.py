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

# -----------------------
# 路径工具
# -----------------------
def _abs_data_path(*parts: str) -> str:
    return os.path.abspath(os.path.join(".", "data", *parts))

def _abs_face_path(name: str) -> str:
    return os.path.abspath(os.path.join(".", "face", name))

# -----------------------
# 核心：生成图片（含窗口化渲染）
# -----------------------
async def generate_img(
    id: int,
    user: User | None,
    contents: list,
    admin: bool = False,
    avatar_seed: int | None = None,
) -> str:
    # 确保目录存在
    os.makedirs(_abs_data_path(str(id)), exist_ok=True)

    env = Environment(
        loader=FileSystemLoader("templates"),
        trim_blocks=True,
        lstrip_blocks=True,
        autoescape=select_autoescape(["html"]),
    )
    _contents = []
    for items in contents:
        values = ["__no_border__" if (len(items) == 1 and items[0]["type"] == "image") else ""]
        for d in items:
            t = d["type"]
            if t == "image":
                abs_img = _abs_data_path(str(id), d["data"]["file"])
                if d["data"]["sub_type"] == 1:
                    # 表情包
                    values.append("_file://" + abs_img)
                else:
                    values.append("file://" + abs_img)
            elif t == "text":
                values.append(
                    d["data"]["text"]
                    .replace("\r\n", "\n")
                    .replace("\n", "__internal_br__")
                )
            elif t == "face":
                values.append("face://" + _abs_face_path(f'{d["data"]["id"]}.png'))
        _contents.append(values)

    # if user is not None:
    #     url = f"https://3lu.cn/qq.php?qq={user.user_id}"
    #     qr = qrcode.QRCode(border=0)
    #     qr.add_data(url)
    #     img = qr.make_image(back_color="#f0f0f0")
    #     img.save(_abs_data_path(str(id), "qrcode.png"))  # type: ignore

    avatar_path: str | None = None
    avatar_src = _AVATAR_PLACEHOLDER
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
        # qrcode=_abs_data_path(str(id), "qrcode.png") if user else None,
        admin=admin,
        avatar_src=avatar_src,
    )
    with open(_abs_data_path(str(id), "page.html"), mode="w", encoding="utf-8") as f:
        f.write(output)

    out_path = _abs_data_path(str(id), "image.png")
    try:
        await screenshoot(id=id, output_path=out_path)
    finally:
        if avatar_path and os.path.exists(avatar_path):
            os.remove(avatar_path)
    return out_path


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

# -----------------------
# Playwright 页面准备
# -----------------------
async def _prepare_page(browser: playwright.async_api.Browser, id: int, scale: int):
    page = await browser.new_page(
        viewport={"width": 720, "height": 720},
        device_scale_factor=scale,
    )
    await page.goto(
        f'file://{_abs_data_path(str(id), "page.html")}',
        wait_until="domcontentloaded",
        timeout=60_000,
    )
    height = await page.evaluate(_HEIGHT_SCRIPT)
    return page, float(height)

# -----------------------
# 分块截图 + 拼接
# -----------------------
async def _screenshot_windowed_card(page, output_path: str, dpr: int = 3, chunk_h: int = 2000):
    """对 .card 进行窗口化分块截图并拼接，突破单次截图高度限制。"""
    card = page.locator(".card")
    if await card.count() == 0:
        # 兜底：全页窗口化
        await _screenshot_windowed_fullpage(page, output_path, dpr=dpr, chunk_h=chunk_h)
        return

    # 获取 .card 的位置与尺寸（CSS px）
    box = await card.bounding_box()
    if not box:
        # 无法拿到 bbox 时兜底
        await page.screenshot(type="png", path=output_path, omit_background=True, animations="disabled", full_page=True)
        return

    x, y, w, h = box["x"], box["y"], box["width"], box["height"]

    # 逐块截取
    slices = []
    current_top = 0
    while current_top < h:
        this_h = min(chunk_h, h - current_top)
        clip = {
            "x": x,
            "y": y + current_top,
            "width": w,
            "height": this_h,
        }
        buf = await page.screenshot(type="png", clip=clip, omit_background=True, animations="disabled")
        slices.append(buf)
        current_top += this_h

    # 拼接（按 DPR 放大后的像素）
    imgs = [Image.open(io := __import__("io").BytesIO(b)).convert("RGBA") for b in slices]
    # Playwright 返回的是以 CSS 像素为单位的渲染像素（已乘以 DPR）。无需再倍增。
    total_h = sum(im.height for im in imgs)
    total_w = max(im.width for im in imgs)
    canvas = Image.new("RGBA", (total_w, total_h), (0, 0, 0, 0))
    offset = 0
    for im in imgs:
        canvas.paste(im, (0, offset))
        offset += im.height
    canvas.save(output_path, format="PNG")

async def _screenshot_windowed_fullpage(page, output_path: str, dpr: int = 3, chunk_h: int = 2000):
    """全页窗口化分块截图并拼接（当 .card 不存在时使用）。"""
    total_h = await page.evaluate(_HEIGHT_SCRIPT)
    # 逐块按页面坐标截图
    slices = []
    taken = 0
    while taken < total_h:
        this_h = int(min(chunk_h, total_h - taken))
        clip = {"x": 0, "y": taken, "width": 720, "height": this_h}
        buf = await page.screenshot(type="png", clip=clip, omit_background=True, animations="disabled")
        slices.append(buf)
        taken += this_h

    imgs = [Image.open(io := __import__("io").BytesIO(b)).convert("RGBA") for b in slices]
    total_h_px = sum(im.height for im in imgs)
    total_w_px = max(im.width for im in imgs)
    canvas = Image.new("RGBA", (total_w_px, total_h_px), (0, 0, 0, 0))
    y = 0
    for im in imgs:
        canvas.paste(im, (0, y))
        y += im.height
    canvas.save(output_path, format="PNG")

# -----------------------
# 截图入口：先试普通法，超长则走窗口化
# -----------------------
async def screenshoot(id: int, output_path: str):
    async with playwright.async_api.async_playwright() as p:
        # 更稳妥的启动参数（容器内不启用 sandbox）
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox"]
        )

        # 先低缩放测高，再高缩放渲染
        temp_page, height = await _prepare_page(browser, id, scale=1)
        await temp_page.close()

        page, height = await _prepare_page(browser, id, scale=3)

        # 普通截图路径（尝试一次）
        viewport_height = max(720, min(int(height) + 120, 4096))
        await page.set_viewport_size({"width": 720, "height": viewport_height})

        card = page.locator(".card")
        try:
            if await card.count() == 0:
                # 没有 .card：全页
                if viewport_height >= 4096:
                    # 太高，直接走窗口化
                    await _screenshot_windowed_fullpage(page, output_path, dpr=3, chunk_h=2000)
                else:
                    await page.screenshot(
                        type="png",
                        path=output_path,
                        omit_background=True,
                        animations="disabled",
                        full_page=True,
                    )
            else:
                # 有 .card：先尝试一次性截
                if viewport_height >= 4096:
                    # 超限走窗口化
                    await _screenshot_windowed_card(page, output_path, dpr=3, chunk_h=2000)
                else:
                    await card.screenshot(
                        type="png",
                        path=output_path,
                        omit_background=True,
                        animations="disabled",
                    )
        finally:
            await page.close()
            await browser.close()

_AVATAR_PLACEHOLDER: Final[str] = (
    "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR4nGMAAQAABQABDQottAAAAABJRU5ErkJggg=="
)

# -----------------------
# 匿名头像生成
# -----------------------
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

    avatar_path = _abs_data_path(str(post_id), "anon_avatar.png")
    avatar.save(avatar_path, format="PNG")
    return avatar_path

# -----------------------
# 头像下载
# -----------------------
async def _download_avatar(user_id: int, post_id: int) -> str | None:
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

# -----------------------
# 模板友好的相对路径
# -----------------------
def _avatar_src_for_template(path: str, post_id: int) -> str:
    data_dir = _abs_data_path(str(post_id))
    abs_path = os.path.abspath(path)
    try:
        relative = os.path.relpath(abs_path, data_dir)
    except ValueError:
        relative = os.path.basename(abs_path)
    if not relative.startswith("."):
        relative = f"./{relative}"
    return relative
