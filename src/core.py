import asyncio
from datetime import datetime
import os
import shutil
import time

import config
from models import Article, Session
import image
import random
import traceback
import utils

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from botx import Bot
from botx.models import PrivateMessage, GroupMessage, User, PrivateRecall, FriendAdd
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import FileResponse
import httpx
from uvicorn import Config, Server

app = FastAPI()
bot = Bot(ws_uri=config.WS_URL, token=config.ACCESS_TOKEN, log_level="DEBUG", msg_cd=0.5)

# workers 必须为 1. 因为没有多进程数据同步.
server = Server(Config(app=app, host="localhost", port=config.PORT, workers=1))

sessions: dict[User, Session] = {}

token = hex(random.randint(0, 2 << 128))[2:]
start_time = time.time()

# 管理的一些操作要上锁
lock = asyncio.Lock()

scheduler = AsyncIOScheduler()

def get_file_url(path: str):
    return f"http://{config.HOST}:{config.PORT}/image?p={path}&t={token}"


@app.get("/image")
def get_image(p: str, t: str, req: Request):
    if req.client.host != "127.0.0.1" or t != token:
        raise HTTPException(status_code=401, detail="Nothing.")
    return FileResponse(path=p)


@app.get("/article")
async def article():
    pass


@bot.on_error()
async def error(context: dict, data: dict):
    if "user_id" in data:
        await bot.send_private(
            data["user_id"],
            f"出了一点小问题😵‍💫:\n\n{context["exception"]}",
        )
        await bot.send_group(
            config.GROUP,
            f"和用户 {data["user_id"]} 对话时出错:\n{"\n\n".join(traceback.format_exception(context["exception"]))}",
        )
    else:
        await bot.send_group(
            config.GROUP,
            f"出错了:\n{"\n\n".join(traceback.format_exception(context["exception"]))}",
        )


@bot.on_cmd(
    "投稿",
    help_msg=f"我想来投个稿😉\n发送 #投稿 单发 可以要求单发, #投稿 匿名 就可以匿名了, #投稿 单发 匿名 就可以匿名单发\n如图所示:[CQ:image,url={get_file_url("help/article.jpg")}]",
)
async def article(msg: PrivateMessage):
    parts = msg.raw_message.split(" ")
    if msg.sender in sessions:
        await msg.reply("你还有投稿未结束呢🤔\n请先使用 #结束 来结束当前投稿")
        return
    
    id = Article.create(
        sender_id=msg.sender.user_id,
        sender_name=None if "匿名" in parts else msg.sender.nickname,
        time=datetime.now(),
        single="单发" in parts,
    ).id
    sessions[msg.sender] = Session(id=id, anonymous="匿名" in parts)
    os.makedirs(f"./data/{id}", exist_ok=True)
    await msg.reply(
        f"开始投稿😉\n接下来你说的内容除了指令外都将被计入投稿当中\n发送 #结束 来结束投稿, 发送 #取消 取消本次投稿\n匿名: {"匿名" in parts}\n单发: {"单发" in parts}"
    )
    
    await bot.send_group(config.GROUP, f"{msg.sender} 开始投稿")


@bot.on_cmd("结束", help_msg="我已经说完啦😏")
async def end(msg: PrivateMessage):
    if msg.sender not in sessions:
        await msg.reply("你还没有投稿哦~")
        return
    
    bot.getLogger().debug(sessions[msg.sender].contents)
    if not sessions[msg.sender].contents:
        await msg.reply(
            "你好像啥都没有说呢😵‍💫\n如果不想投稿了就发个 #取消 \n或者说点什么再发 #结束"
        )
        return
    await msg.reply("正在生成预览图🚀\n请稍等片刻")
    ses = sessions[msg.sender]
    
    for m in ses.contents:
        if m["type"] == "image":
            filepath = f"./data/{ses.id}/{m["data"]["file"]}"
            if not os.path.isfile(filepath):
                with httpx.stream("GET", m["data"]["url"].replace("https://", "http://"), timeout=60) as resp:
                    with open(filepath, mode="bw") as file:
                        for chunk in resp.iter_bytes():
                            file.write(chunk)
                bot.getLogger().info(f"下载图片: {filepath}")
    
    path = await image.generate_img(
        ses.id, user=None if ses.anonymous else msg.sender, contents=ses.contents
    )
    await msg.reply(
        f"[CQ:image,file={get_file_url(path)}]这样投稿可以吗😘\n可以的话请发送 #确认, 要是算了的话就发个 #取消"
    )


@bot.on_cmd("确认", help_msg="确认要发送当前投稿🤔")
async def done(msg: PrivateMessage):
    if not msg.sender in sessions:
        await msg.reply("你都还没投稿确认啥🤨")
        return
    
    session = sessions[msg.sender]
    if not os.path.isfile(f"./data/{session.id}/image.png"):
        await msg.reply("请先发送 #结束 查看效果图🤔")
        return
    sessions.pop(msg.sender)
    Article.update({"tid": "wait"}).where(Article.id == session.id).execute()
    article = Article.get_by_id(session.id)
    await bot.send_group(
        config.GROUP,
        f"#{session.id} 用户 {msg.sender} {"匿名" if article.sender_name == None else ""}投稿{", 要求单发" if article.single else ""}\n[CQ:image,file={get_file_url(f"./data/{session.id}/image.png")}]",
    )
    await msg.reply("已成功投稿, 请耐心等待管理员审核😘")
    
    await bot.call_api("set_diy_online_status", {"face_id": random.choice(config.STATUS_ID), "wording": f"已接 {len(Article.select())} 单"})
    
    await update_name()
    
@bot.on_cmd("取消", help_msg="取消当前投稿🫢")
async def cancel(msg: PrivateMessage):
    if not msg.sender in sessions:
        await msg.reply("你都还没投稿取消啥🤨")
        return
    
    id = sessions[msg.sender].id
    Article.delete_by_id(id)
    sessions.pop(msg.sender)
    shutil.rmtree(f"./data/{id}")
    await msg.reply("已取消本次投稿🫢")
    
    await bot.send_group(config.GROUP, f"{msg.sender} 取消了投稿")


@bot.on_cmd(
    "反馈",
    help_msg=f"向管理员反馈你的问题😘\n[CQ:image,file={get_file_url("help/feedback.png")}]",
)
async def feedback(msg: PrivateMessage):
    await bot.send_group(
        config.GROUP,
        f"用户 {msg.sender} 反馈:\n{msg.raw_message}",
    )
    await msg.reply("感谢你的反馈😘")


@bot.on_msg()
async def content(msg: PrivateMessage):
    if msg.sender not in sessions:
        await msg.reply(
            f"✨欢迎使用 {config.NAME}\n本墙使用 Bot 实现自动化投稿😎\n请发送 #帮助 查看使用教程"
        )
        await bot.send_group(
                config.GROUP,
                f"用户 {msg.sender} 触发了自动回复",
            ) 
        return
    session = sessions[msg.sender]
    for m in msg.message:
        m["id"] = msg.message_id
        if m["type"] not in ["image", "text", "face"]:
            await msg.reply("当前版本仅支持发送文字、图片、表情哦～\n如果你觉得你一定要发送该类消息, 请使用 #反馈 来告诉我们哦")
            await bot.send_group(
                config.GROUP,
                f"用户 {msg.sender} 发送了不支持的消息: {m["type"]}",
            )
            continue
        session.contents.append(m)
    session.contents.append({"type": "br", "id": msg.message_id})


@bot.on_notice()
async def recall(r: PrivateRecall):
    ses = sessions.get(User(nickname=None, user_id=r.user_id))
    if not ses:
        return
    ses.contents = [c for c in ses.contents if c["id"] != r.message_id]


@bot.on_notice()
async def friend(r: FriendAdd):
    await bot.send_group(config.GROUP, f"{r.user_id} 添加了好友")

@bot.on_cmd("通过", help_msg="通过投稿. 可以一次通过多条, 以空格分割. 如 #通过 1 2", targets=[config.GROUP])
async def accept(msg: GroupMessage):
    async with lock:
        parts = msg.raw_message.split(" ")
        if len(parts) < 2:
            await msg.reply("请带上要通过的投稿编号")
            return
        
        ids = parts[1:]
        flag = False  # 只有有投稿加入队列时才判断是否推送
        for id in ids:
            article = Article.get_or_none((Article.id == id) & (Article.tid == "wait"))
            if not article:
                await msg.reply(f"投稿 #{id} 不存在或已通过审核")
                continue
            if article.single:
                await msg.reply(f"开始推送 #{id}")
                await publish([id])
                await msg.reply(f"投稿 #{id} 已经单发")
                continue
            else:
                await bot.send_private(article.sender_id, f"您的投稿 {article} 已通过审核, 正在队列中等待发送")
            flag = True
            Article.update({Article.tid: "queue"}).where(Article.id == id).execute()

        if flag:
            articles = (
                Article.select().where(Article.tid == "queue").order_by(Article.id.asc()).limit(9)
            )
            if len(articles) < 4:
                await msg.reply(f"当前队列中有{len(articles)}个稿件, 暂不推送")
            else:
                await msg.reply(f"队列已积压{len(articles)}个稿件, 将推送前4个稿件...")
                tid = await publish(list(map(lambda a: a.id, articles)))
                await msg.reply(f"已推送{list(map(lambda a: a.id, articles))}\ntid: {tid}")
            
        await update_name()


@bot.on_cmd(name="驳回", help_msg="驳回一条投稿, 需附带理由. 如 #驳回 1 不能引战", targets=[config.GROUP])
async def refuse(msg: GroupMessage):
    async with lock:
        parts = msg.raw_message.split(" ")
        if len(parts) < 3:
            await msg.reply("请带上要通过的投稿和理由")
            return

        id = parts[1]
        reason = parts[2:]
        article = Article.get_or_none((Article.id == id) & (Article.tid == "wait"))
        if article == None:
            await msg.reply(f"投稿 #{id} 不存在或已通过审核")
            return

        # 保留证据
        # Article.delete_by_id(id)
        # shutil.rmtree(f"./data/{id}")
        Article.update({"tid": "refused"}).where(Article.id == id).execute()
        await bot.send_private(
            article.sender_id,
            f"抱歉, 你的投稿 #{id} 已被管理员驳回😵‍💫 理由: {" ".join(reason)}",
        )
        await msg.reply(f"已驳回投稿 #{id}")
        
        await update_name()


@bot.on_cmd("推送", help_msg="推送指定的投稿, 可以推送多个. 如 #推送 1 2", targets=[config.GROUP])
async def push(msg: GroupMessage):
    async with lock:
        parts = msg.raw_message.split(" ")
        if len(parts) < 2:
            await msg.reply("请带上要通过的投稿id")
            return
        
        ids = parts[1:]
        for id in ids:
            article = Article.get_or_none((Article.id == id) & (Article.tid == "queue"))
            if not article:
                await msg.reply(f"投稿 #{id} 不存在或已被推送或未通过审核")
                return
        await msg.reply(f"开始推送 {ids}")
        tid = await publish(ids)
        await msg.reply(f"已推送 {ids}\ntid: {tid}")


@bot.on_cmd("查看", help_msg="查看投稿, 可以查看多个, 如 #查看 1 2 3", targets=[config.GROUP])
async def view(msg: GroupMessage):
    parts = msg.raw_message.split(" ")
    if len(parts) < 2:
        await msg.reply("请带上要通过的投稿id")
        return
    
    ids = parts[1:]
    for id in ids:
        article = Article.get_or_none(Article.id == id)
        if not article or not os.path.exists(f"./data/{id}/image.png"):
            await msg.reply(f"投稿 #{id} 不存在")
            return
        
        status = article.tid
        if article.tid == "wait":
            status = "待审核"
        elif article.tid == "queue":
            status = "待发送"
        elif article.tid == "refused":
            status = "已驳回"
            
        await msg.reply(
            f"#{id} 用户 {article.sender_name}({article.sender_id}) {"匿名" if article.sender_name == None else ""}投稿{", 要求单发" if article.single else ""}\n" + 
            f"[CQ:image,file={get_file_url(f"./data/{id}/image.png")}]" + 
            f"状态: {status}",
        )

@bot.on_cmd("状态", help_msg="查看队列状态", targets=[config.GROUP])
async def status(msg: GroupMessage):
    waiting = Article.select().where(Article.tid == "wait")
    queue = Article.select().where(Article.tid == "queue")
    
    await msg.reply(f"Nishikigi 已运行 {int(time.time() - start_time)}s\n待审核: {utils.to_list(waiting)}\n待推送: {utils.to_list(queue)}")
    
@bot.on_cmd("链接", help_msg="获取登录 QZone 的链接", targets=[config.GROUP])
async def link(msg: GroupMessage):
    clientkey = (await bot.call_api("get_clientkey"))["data"]["clientkey"]
    await msg.reply(f"http://ssl.ptlogin2.qq.com/jump?ptlang=1033&clientuin={bot.me.user_id}&clientkey={clientkey}" +
                    f"&u1=https%3A%2F%2Fuser.qzone.qq.com%2F{bot.me.user_id}%2Finfocenter&keyindex=19")

@bot.on_cmd("回复", help_msg="回复用户. 如 #回复 10001 你是麻花疼吗? 你家的QQ真好用", targets=[config.GROUP])
async def reply(msg: GroupMessage):
    parts = msg.raw_message.split(" ")
    if len(parts) < 3:
        await msg.reply("请带上你想回复的人和内容")
        return
    try:
        int(parts[1])
    except:
        await msg.reply(f"\"{parts[1]}\" 不是一个有效的 QQ 号")
        return
        
    resp = await bot.send_private(int(parts[1]), f"😘管理员回复:\n{" ".join(parts[2:])}")
    if resp is None:
        await msg.reply(f"无法回复用户 {parts[1]}\n请检查 QQ 号是否正确")
    else:
        await msg.reply(f"已回复用户 {parts[1]}")

async def publish(ids: list[int | str]) -> str:
    qzone = await bot.get_qzone()
    images = []
    for id in ids:
        images.append(
            await qzone.upload_image(utils.read_image(f"./data/{id}/image.png"))
        )
    
    tid = await qzone.publish("", images=images)
    
    for id in ids:
        Article.update({"tid": tid}).where(Article.id == id).execute()
        await bot.send_private(
            Article.get_by_id(id).sender_id, f"您的投稿 #{id} 已被推送😋"
        )
    return tid

async def update_name():
    bot.getLogger().debug("更新群备注")
    waiting = Article.select().where(Article.tid == "wait")
    queue = Article.select().where(Article.tid == "queue")
    await bot.call_api("set_group_card", {"group_id": config.GROUP, "user_id": bot.me.user_id, 
                                          "card": f"待审核: {utils.to_list(waiting)}\n待推送: {utils.to_list(queue)}"})
    
@scheduler.scheduled_job(IntervalTrigger(hours=1))
async def clear():
    async with lock:
        for sess in sessions:
            a = Article.get_by_id(sessions[sess].id)
            time = (datetime.now() - a.time).total_seconds()

            if time > 60 * 60 * 2:
                sessions.pop(sess)
                Article.delete_by_id(a.id)
                shutil.rmtree(f"./data/{a.id}")
                
                await bot.send_private(sess.user_id, f"您的投稿 {a} 因为超时而被自动取消.")
                await bot.send_group(config.GROUP, f"用户 {sess.user_id} 的投稿 {a} 因超时而被自动取消.")
                bot.getLogger().warning(f"取消用户 {sess.user_id} 的投稿 {a}")