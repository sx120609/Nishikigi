from datetime import datetime
import os
from typing import Final

from botx.models import User
from jinja2 import Environment, FileSystemLoader, select_autoescape
import playwright.async_api
import qrcode


async def generate_img(
    id: int, user: User | None, contents: list, admin: bool = False
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

    output = env.get_template("normal.html" if user else "anonymous.html").render(
        contents=_contents,
        date=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        username=None if user == None else user.nickname,
        user_id=None if user == None else user.user_id,
        # qrcode=os.path.abspath(f"./data/{id}/qrcode.png") if user else None,
        admin=admin,
    )
    with open(f"./data/{id}/page.html", mode="w") as f:
        f.write(output)
    await screenshoot(id=id, output_path=f"./data/{id}/image.png")
    return os.path.abspath(f"./data/{id}/image.png")


_HEIGHT_SCRIPT: Final[str] = """
() => {
    const doc = document.documentElement;
    const bod = document.body;
    const card = document.querySelector('.card');
    const blur = document.querySelector('.blur-bg');
    const height = card
        ? Math.max(card.scrollHeight, card.offsetHeight, card.clientHeight)
        : Math.max(
            doc.scrollHeight, doc.offsetHeight, doc.clientHeight,
            bod.scrollHeight, bod.offsetHeight, bod.clientHeight
        );
    if (blur) {
        const limited = Math.min(height + 120, 1500);
        blur.style.height = `${limited}px`;
    }
    return height;
}
"""


def _select_scale(height: float) -> int:
    if height <= 2400:
        return 3
    if height <= 4800:
        return 2
    return 1


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

        device_scale = _select_scale(height)
        page, height = await _prepare_page(browser, id, scale=device_scale)

        viewport_height = max(720, min(int(height) + 120, 4096))
        await page.set_viewport_size({"width": 720, "height": viewport_height})

        card = page.locator(".card")
        await card.screenshot(
            type="png",
            path=output_path,
            omit_background=True,
            animations="disabled",
        )
        await browser.close()
