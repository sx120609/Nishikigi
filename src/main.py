import asyncio
import os
import random

from fastapi import FastAPI, HTTPException
from botx import Bot
from fastapi.responses import FileResponse
from uvicorn import Config, Server

import config

if os.geteuid() == 0:
    print("请不要使用 root 用户运行此程序.")
    exit(-1)

token = hex(random.randint(0, 2 << 128))[2:]

app = FastAPI()
bot = Bot(ws_uri=config.WS_URL, token=config.ACCESS_TOKEN, log_level="INFO", msg_cd=0.5)

# workers 必须为 1. 因为没有多进程数据同步.
server = Server(Config(app=app, host="localhost", port=config.PORT, workers=1))


def get_file_url(path: str):
    return f"http://{config.HOST}:{config.PORT}/image?p={path}&t={token}"


@app.get("/image")
def get_image(p: str, t: str):
    if t != token:
        raise HTTPException(status_code=401, detail="Nothing.")
    return FileResponse(path=p)


async def main():
    import core

    core.scheduler.start()
    await asyncio.gather(bot.start(), server.serve())


asyncio.run(main())
