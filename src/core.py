import asyncio
from datetime import datetime
import os
import shutil
import time
from typing import Sequence

import config
from models import Article, Session
import image
import random
import traceback
import utils
import config

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from botx import Bot
from botx.models import PrivateMessage, GroupMessage, User, PrivateRecall, FriendAdd
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import FileResponse
import httpx
from uvicorn import Config, Server

import json
import hashlib

app = FastAPI()
bot = Bot(
    ws_uri=config.WS_URL, token=config.ACCESS_TOKEN, log_level="DEBUG", msg_cd=0.5
)

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
    if t != token:
        raise HTTPException(status_code=401, detail="Nothing.")
    return FileResponse(path=p)


@bot.on_error()
async def error(context: dict, data: dict):
    exc = context.get("exception")
    tb = "".join(traceback.format_exception(exc)) if exc is not None else "no traceback"
    if "user_id" in data:
        await bot.send_private(
            data["user_id"],
            f"出了一点小问题😵‍💫:\n\n{str(exc)}",
        )
        await bot.send_group(
            config.GROUP,
            f"和用户 {data['user_id']} 对话时出错:\n{tb}",
        )
    else:
        await bot.send_group(
            config.GROUP,
            f"出错了:\n{tb}",
        )

# ----------------- AI 辅助相关 -----------------
AGENT_ROUTER_BASE = config.AGENT_ROUTER_BASE
AGENT_ROUTER_KEY = config.AGENT_ROUTER_KEY

def _can_call_ai(user_id: int) -> bool:
    return True

def _mark_ai_called(user_id: int):
    pass

# 缓存以减少重复 prompt 调用
_ai_cache: dict[str, dict] = {}  # key -> {"resp":..., "_ts":...}

def is_known_command(raw: str) -> bool:
    if not raw:
        return False
    s = raw.strip()
    if not s.startswith("#"):
        return False
    normalized = "#" + s[1:].split(" ")[0]
    known_cmds = {
        "#投稿","#结束","#确认","#取消","#帮助","#反馈","#通过","#驳回","#推送",
        "#查看","#删除","#回复","#状态","#链接"
    }
    return normalized in known_cmds and s.strip() == normalized

def _conf_label(conf: str) -> str:
    """把置信度映射为可读标签，更直观"""
    if not conf:
        return "❓不确定此答复是否有效"
    c = str(conf).lower()
    if "高" in c or "high" in c:
        return "✅很确定此答复有效"
    if "中" in c or "medium" in c or "mid" in c:
        return "⚠️此答复可能有效"
    return "❓不确定此答复是否有效"

async def ai_suggest_intent(raw: str, context_summary: str = "") -> dict:
    """
    调用 agentrouter 的 ChatCompletions 风格接口，返回结构体:
    {"intent_candidates":[{"label":"...","suggestion":"#投稿 匿名","confidence":"高","reason":"..."}]}
    出错或无法解析时返回 {"intent_candidates": []}
    """
    prompt = (
        "你是“苏州实验中学校墙”的智能助手，任务是把用户短文本映射为墙的命令或友好回复。"
        "最终请返回 JSON：{\"intent_candidates\":[{\"label\":\"\",\"suggestion\":\"\",\"confidence\":\"\",\"reason\":\"\"}]}\n\n"
        f"墙的指令和说明：\n"
        f"#帮助：查看使用说明。\n"
        f"#投稿：开启投稿模式。\n"
        f"投稿方式：\n"
        f"  📝 #投稿 ：普通投稿（显示昵称，由墙统一发布）\n"
        f"  📮 #投稿 单发 ：单独发一条空间动态\n"
        f"  🕶️ #投稿 匿名 ：匿名投稿（不显示昵称/头像）\n"
        f"  💌 #投稿 单发 匿名 ：匿名并单发\n"
        f"#结束：结束当前投稿\n"
        f"#确认：确认发送当前投稿\n"
        f"#取消：取消投稿\n"
        f"#反馈：向管理员反馈（示例：#反馈 机器人发不出去）\n\n"
        f"上下文: {context_summary}\n"
        f"原始消息: {raw}\n"
        "注意：如果能直接给出建议命令（如 #投稿 匿名）请放在 suggestion 字段；"
        "如果只能给自然语言建议，放在 reason 字段。请不要输出非 JSON 的内容。"
        "建议每次都补充一下，如果想要完整帮助，请输入 #帮助 来查看"
        "投稿方法是先发送命令，再发送想要投稿的内容，然后按照提示操作"
        "反馈就直接指令空格跟着反馈的内容就行"
        "以上两条具体的方式，不需要每次都说，只要在涉及到有所了解即可"
        "当用户发送 请求添加你为好友 或者类似的语句，或者没有什么意义的话，直接返回帮助"
        "如果用户发送了不正确的#开头的命令，请告知用户如何修改为正确的指令，必须要精确匹配才行"
    )

    key = hashlib.sha1((prompt).encode()).hexdigest()
    cache_item = _ai_cache.get(key)
    ttl = getattr(config, "AI_CACHE_TTL", 300)
    if cache_item and time.time() - cache_item.get("_ts", 0) < ttl:
        return cache_item["resp"]

    headers = {
        "Authorization": f"Bearer {AGENT_ROUTER_KEY}",
        "Content-Type": "application/json",
    }

    body = {
        "model": getattr(config, "OPENAI_MODEL", "gpt-4o-mini"),
        "messages": [
            {"role": "system", "content": "你是把用户短文本转换成墙命令或友好建议的助手。输出 JSON。"},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": 300,
        "temperature": 0.0,
    }

    resp_obj = {"intent_candidates": []}
    try:
        url = AGENT_ROUTER_BASE.rstrip("/") + "/v1/chat/completions"
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(url, headers=headers, json=body)
            r.raise_for_status()
            j = r.json()
            text = ""
            if "choices" in j and len(j["choices"]) > 0:
                cand = j["choices"][0]
                if isinstance(cand, dict) and "message" in cand and isinstance(cand["message"], dict):
                    text = cand["message"].get("content", "") or ""
                else:
                    text = cand.get("text", "") or ""
            if not text and "text" in j:
                text = j.get("text", "")

            # 尝试解析 JSON
            try:
                parsed = json.loads(text)
                resp_obj = parsed
            except Exception:
                # 尝试提取文本中的 JSON 块
                start = text.find("{")
                end = text.rfind("}")
                if start != -1 and end != -1 and end > start:
                    snippet = text[start:end+1]
                    try:
                        parsed = json.loads(snippet)
                        resp_obj = parsed
                    except Exception:
                        resp_obj = {"intent_candidates": [{"label": "无法结构化解析", "suggestion": "", "confidence": "低", "reason": text[:400]}]}
                else:
                    resp_obj = {"intent_candidates": [{"label": "无法结构化解析", "suggestion": "", "confidence": "低", "reason": text[:400]}]}
    except Exception as e:
        bot.getLogger().warning(f"AI call failed: {e}")
        resp_obj = {"intent_candidates": []}

    _ai_cache[key] = {"resp": resp_obj, "_ts": time.time()}
    return resp_obj

def _shorten(s: str, n: int = 200) -> str:
    if not s:
        return ""
    s = str(s).strip()
    return s if len(s) <= n else s[: n - 1] + "…"

async def _reply_ai_suggestions(msg: PrivateMessage, ai_result: dict, raw: str):
    """
    把 ai_result 转成人能看懂且可操作的回复并发送。
    期待 ai_result = {"intent_candidates":[{"label":"","suggestion":"","confidence":"","reason":""}, ...]}
    """
    candidates = ai_result.get("intent_candidates", []) if isinstance(ai_result, dict) else []
    if not candidates:
        await msg.reply(
            "抱歉我没能猜出你具体想做什么😵‍💫\n"
            "试试：\n"
            "1) 简短说明你想做的事（例如：我要匿名投稿）\n"
            "2) 发送 #帮助 查看使用说明\n"
            "我可以把你的描述改写成合适的命令，或者直接给出步骤。"
        )
        return

    have_sugg = [c for c in candidates if c.get("suggestion")]
    no_sugg = [c for c in candidates if not c.get("suggestion")]

    lines = []
    lines.append("我把你的意思整理成了这些建议（直接复制建议命令并发送即可）：")

    idx = 1
    for c in have_sugg[:3]:
        label = _shorten(c.get("label", "意图"), 40)
        suggestion = c.get("suggestion", "").strip()
        conf = _conf_label(c.get("confidence", ""))
        reason = _shorten(c.get("reason", ""), 120)

        lines.append(f"{idx}. {label}（{conf}）")
        lines.append(f"   → 建议发送命令：{suggestion}")
        if reason:
            lines.append(f"   说明：{reason}")
        lines.append(f"   操作：复制上面的建议命令内容并发送。")
        idx += 1

    for c in no_sugg[:2]:
        label = _shorten(c.get("label", "可能意图"), 60)
        conf = _conf_label(c.get("confidence", ""))
        reason = _shorten(c.get("reason", ""), 200)
        lines.append(f"{idx}. {label}（{conf}）")
        if reason:
            lines.append(f"   说明：{reason}")
        lines.append(f"   操作：如合适，请直接回复对应的命令或简短说明你的需求。")
        idx += 1

    lines.append("")
    lines.append("不合适？直接回复一句你的目标（例如：我要匿名投稿），我会把它改写成命令。")
    await msg.reply("\n".join(lines))

# ----------------- End AI 辅助相关 -----------------

@bot.on_cmd(
    "投稿",
    help_msg=(
        f"我想来投个稿 😉\n"
        "—— 投稿方式 ——\n"
        "📝 #投稿 ：普通投稿（显示昵称，由墙统一发布）\n"
        "📮 #投稿 单发 ：让墙单独发一条空间动态\n"
        "🕶️ #投稿 匿名 ：隐藏投稿者身份\n"
        "💌 #投稿 单发 匿名 ：匿名并单独发一条动态\n"
        "\n⚠️ 提示：请正确输入命令，不要多或少空格，比如：#投稿 匿名\n"
        f"\n示例见图：[CQ:image,url={get_file_url('help/article.jpg')}]"
    ),
)
async def article(msg: PrivateMessage):
    parts = msg.raw_message.split(" ")
    if msg.sender in sessions:
        await msg.reply("你还有投稿未结束🤔\n请先输入 #结束 来结束当前投稿")
        return

    id = Article.create(
        sender_id=msg.sender.user_id,
        sender_name=None if "匿名" in parts else msg.sender.nickname,
        time=datetime.now(),
        single="单发" in parts,
    ).id
    sessions[msg.sender] = Session(id=id, anonymous="匿名" in parts)
    os.makedirs(f"./data/{id}", exist_ok=True)

    def status_words(value: bool) -> str:
        return "是" if value else "否"

    await msg.reply(
        f"✨ 开始投稿 😉\n"
        f"你发送的内容（除命令外）会计入投稿。\n\n"
        f"—— 投稿操作指南 ——\n"
        f"1️⃣ 完成投稿：发送 #结束 来结束投稿并生成预览图\n"
        f"2️⃣ 取消投稿：发送 #取消 来放弃本次投稿\n"
        f"匿名模式启用状态: {status_words('匿名' in parts)}\n"
        f"单发模式启用状态: {status_words('单发' in parts)}\n"
        f"⚠️ 匿名和单发在设定后无法更改，如需更改请先取消本次投稿"
    )
    if "单发" in parts:
        await msg.reply(
            "单发大概率被驳回! \n都单发的话, 大家的空间就会被挤满了😵‍💫\n节约你我时间，无需单发, 发送 #取消 后再重新投稿"
        )
    if "匿名" in parts:
        await msg.reply(
            "匿名投稿不显示你的昵称和头像\n若无需匿名， 发送 #取消 后再重新投稿\nPS: 之前有人匿名发失物招领"
        )
    await bot.send_group(config.GROUP, f"{msg.sender} 开始投稿")


@bot.on_cmd("结束", help_msg="用于结束当前投稿")
async def end(msg: PrivateMessage):
    if msg.sender not in sessions:
        await msg.reply("你还没有投稿哦~")
        return

    bot.getLogger().debug(sessions[msg.sender].contents)
    if not sessions[msg.sender].contents:
        await msg.reply(
            "你好像啥都没有说呢😵‍💫\n不想投稿了请输入 #取消 \n或者说点什么再输入 #结束"
        )
        return
    await msg.reply("正在生成预览图🚀\n请稍等片刻")
    ses = sessions[msg.sender]

    for content in ses.contents:
        for m in content:
            if m["type"] == "image":
                filepath = f"./data/{ses.id}/{m['data']['file']}"
                if not os.path.isfile(filepath):
                    with httpx.stream(
                        "GET",
                        m["data"]["url"].replace("https://", "http://"),
                        timeout=60,
                    ) as resp:
                        with open(filepath, mode="bw") as file:
                            for chunk in resp.iter_bytes():
                                file.write(chunk)
                    bot.getLogger().info(f"下载图片: {filepath}")

    path = await image.generate_img(
        ses.id, user=None if ses.anonymous else msg.sender, contents=ses.contents
    )
    await msg.reply(
        f"[CQ:image,file={get_file_url(path)}]这样投稿可以吗😘\n可以的话请发送 #确认, 不可以就发送 #取消"
    )


@bot.on_cmd("确认", help_msg="用于确认发送当前投稿")
async def done(msg: PrivateMessage):
    if not msg.sender in sessions:
        await msg.reply("你都还没投稿确认啥🤨")
        return

    session = sessions[msg.sender]
    if not os.path.isfile(f"./data/{session.id}/image.png"):
        await msg.reply("请先发送 #结束 来查看效果图🤔")
        return
    sessions.pop(msg.sender)
    Article.update({"tid": "wait"}).where(Article.id == session.id).execute()
    article = Article.get_by_id(session.id)
    anon_text = "匿名" if article.sender_name is None else ""
    single_text = ", 要求单发" if article.single else ""
    image_url = get_file_url(f"./data/{session.id}/image.png")
    await bot.send_group(
        config.GROUP,
        f"#{session.id} 用户 {msg.sender} {anon_text}投稿{single_text}\n[CQ:image,file={image_url}]",
    )
    await msg.reply("已成功投稿, 请耐心等待管理员审核😘")

    await bot.call_api(
        "set_diy_online_status",
        {
            "face_id": random.choice(config.STATUS_ID),
            "wording": f"已接 {len(Article.select())} 单",
        },
    )

    await update_name()


@bot.on_cmd("取消", help_msg="用于取消当前投稿")
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
    help_msg=f"用于向管理员反馈你的问题😘\n使用方法：输入 #反馈 后直接加上你要反馈的内容\n本账号无人值守，不使用反馈发送的消息无法被看到\n使用案例：[CQ:image,file={get_file_url('help/feedback.png')}]",
)
async def feedback(msg: PrivateMessage):
    await bot.send_group(
        config.GROUP,
        f"用户 {msg.sender} 反馈:\n{msg.raw_message}",
    )
    await msg.reply("感谢你的反馈😘")

@bot.on_msg()
async def content(msg: PrivateMessage):
    raw = msg.raw_message or ""

    # 先处理投稿会话
    if msg.sender in sessions:
        # 如果是已知命令，直接忽略，不加入投稿内容
        if raw.startswith("#") and is_known_command(raw):
            return  # 已知命令由 @bot.on_cmd 处理，不加入投稿
        session = sessions[msg.sender]
        items = []
        for m in msg.message:
            m["id"] = msg.message_id
            if m["type"] not in ["image", "text", "face"]:
                await msg.reply(
                    "当前版本仅支持文字、图片、表情～\n如需发送其他类型，请用 #反馈 告诉我们\n请不要使用QQ的回复/引用功能，该功能无法被机器人理解"
                )
                await bot.send_group(
                    config.GROUP,
                    f"用户 {msg.sender} 发送了不支持的消息: {m.get('type')}",
                )
                continue
            items.append(m)
        if items:
            session.contents.append(items)
        return

    # ----------------------
    # 只对未知命令调用 AI
    # ----------------------
    if raw.startswith("#"):
        if not is_known_command(raw):
            await msg.reply("收到，你的消息我交给智能助手分析，请稍等...")
            ctx_summary = "用户当前不在投稿会话"
            ai_result = await ai_suggest_intent(raw, ctx_summary)
            await _reply_ai_suggestions(msg, ai_result, raw)
        else:
            # 已知命令，直接忽略，让对应 @bot.on_cmd 处理
            return
        return

    # 普通消息（非 # 开头）也可以交给 AI
    await msg.reply("收到，你的消息我交给智能助手分析，请稍等...")
    ctx_summary = "用户当前不在投稿会话"
    ai_result = await ai_suggest_intent(raw, ctx_summary)
    await _reply_ai_suggestions(msg, ai_result, raw)


    # 审计：把该交互记录到管理员群（可删除或替换为日志）
    #try:
    #    await bot.send_group(config.GROUP, f"AI 帮助记录 用户 {msg.sender} 原文: {raw}\nAI 建议: {json.dumps(ai_result.get('intent_candidates', []), ensure_ascii=False)}")
    #except Exception:
    #    bot.getLogger().warning("Failed to send AI log to group")


@bot.on_notice()
async def recall(r: PrivateRecall):
    ses = sessions.get(User(nickname=None, user_id=r.user_id))
    if not ses:
        return
    ses.contents = [c for c in ses.contents if c[0]["id"] != r.message_id]


# @bot.on_notice()
# async def friend(r: FriendAdd):
#     await bot.send_group(config.GROUP, f"{r.user_id} 添加了好友")


@bot.on_cmd(
    "通过",
    help_msg="通过投稿. 可以一次通过多条, 以空格分割. 如 #通过 1 2",
    targets=[config.GROUP],
)
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
                await bot.send_private(
                    article.sender_id,
                    f"您的投稿 {article} 已通过审核, 正在队列中等待发送",
                )
            flag = True
            Article.update({Article.tid: "queue"}).where(Article.id == id).execute()

        if flag:
            articles = (
                Article.select()
                .where(Article.tid == "queue")
                .order_by(Article.id.asc())
                .limit(config.QUEUE)
            )
            if len(articles) < config.QUEUE:
                await msg.reply(f"当前队列中有{len(articles)}个稿件, 暂不推送")
            else:
                await msg.reply(
                    f"队列已积压{len(articles)}个稿件, 将推送前{config.QUEUE}个稿件..."
                )
                tid = await publish(list(map(lambda a: a.id, articles)))
                await msg.reply(
                    f"已推送{list(map(lambda a: a.id, articles))}\ntid: {tid}"
                )

        await update_name()


@bot.on_cmd(
    name="驳回",
    help_msg="驳回一条投稿, 需附带理由. 如 #驳回 1 不能引战",
    targets=[config.GROUP],
)
async def refuse(msg: GroupMessage):
    async with lock:
        parts = msg.raw_message.split(" ")
        if len(parts) < 3:
            await msg.reply("请带上要驳回的投稿和理由")
            return

        id = parts[1]
        reason = parts[2:]
        article = Article.get_or_none((Article.id == id) & (Article.tid == "wait"))
        if article == None:
            await msg.reply(f"投稿 #{id} 不存在或已通过审核")
            return

        Article.update({"tid": "refused"}).where(Article.id == id).execute()
        await bot.send_private(
            article.sender_id,
            f"抱歉, 你的投稿 #{id} 已被管理员驳回😵‍💫 理由: {' '.join(reason)}",
        )
        await msg.reply(f"已驳回投稿 #{id}")

        await update_name()


@bot.on_cmd(
    "推送",
    help_msg="推送指定的投稿, 可以推送多个. 如 #推送 1 2",
    targets=[config.GROUP],
)
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


@bot.on_cmd(
    "查看", help_msg="查看投稿, 可以查看多个, 如 #查看 1 2 3", targets=[config.GROUP]
)
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

        anon_text = "匿名" if article.sender_name is None else ""
        single_text = ", 要求单发" if article.single else ""
        image_url = get_file_url(f"./data/{id}/image.png")

        await msg.reply(
            f"#{id} 用户 {article.sender_name}({article.sender_id}) {anon_text}投稿{single_text}\n"
            + f"[CQ:image,file={image_url}]\n"
            + f"状态: {status}",
        )


@bot.on_cmd("状态", help_msg="查看队列状态", targets=[config.GROUP])
async def status(msg: GroupMessage):
    waiting = Article.select().where(Article.tid == "wait")
    queue = Article.select().where(Article.tid == "queue")

    await msg.reply(
        f"Nishikigi 已运行 {int(time.time() - start_time)}s\n待审核: {utils.to_list(waiting)}\n待推送: {utils.to_list(queue)}"
    )


@bot.on_cmd("链接", help_msg="获取登录 QZone 的链接", targets=[config.GROUP])
async def link(msg: GroupMessage):
    clientkey = (await bot.call_api("get_clientkey"))["data"]["clientkey"]
    await msg.reply(
        f"http://ssl.ptlogin2.qq.com/jump?ptlang=1033&clientuin={bot.me.user_id}&clientkey={clientkey}"
        + f"&u1=https%3A%2F%2Fuser.qzone.qq.com%2F{bot.me.user_id}%2Finfocenter&keyindex=19"
    )


@bot.on_cmd(
    "回复",
    help_msg="回复用户. 如 #回复 10001 你是麻花疼吗? 你家的QQ真好用",
    targets=[config.GROUP],
)
async def reply(msg: GroupMessage):
    parts = msg.raw_message.split(" ")
    if len(parts) < 3:
        await msg.reply("请带上你想回复的人和内容")
        return
    try:
        int(parts[1])
    except:
        await msg.reply(f'"{parts[1]}" 不是一个有效的 QQ 号')
        return

    resp = await bot.send_private(
        int(parts[1]), f"😘管理员回复:\n{' '.join(parts[2:])}"
    )
    if resp is None:
        await msg.reply(f"无法回复用户 {parts[1]}\n请检查 QQ 号是否正确")
    else:
        await msg.reply(f"已回复用户 {parts[1]}")


async def publish(ids: Sequence[int | str]) -> str:
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
    await bot.call_api(
        "set_group_card",
        {
            "group_id": config.GROUP,
            "user_id": bot.me.user_id,
            "card": f"待审核: {utils.to_list(waiting)}\n待推送: {utils.to_list(queue)}",
        },
    )


@scheduler.scheduled_job(IntervalTrigger(hours=1))
async def clear():
    async with lock:
        # 注意：遍历 dict 时不可直接修改，先收集要移除的 key
        to_remove = []
        for sess in list(sessions.keys()):
            try:
                a = Article.get_by_id(sessions[sess].id)
            except Exception:
                continue
            time_passed = (datetime.now() - a.time).total_seconds()

            if time_passed > 60 * 60 * 2:
                to_remove.append(sess)
                Article.delete_by_id(a.id)
                if os.path.exists(f"./data/{a.id}"):
                    shutil.rmtree(f"./data/{a.id}")

                await bot.send_private(
                    sess.user_id, f"您的投稿 {a} 因为超时而被自动取消."
                )
                await bot.send_group(
                    config.GROUP, f"用户 {sess.user_id} 的投稿 {a} 因超时而被自动取消."
                )
                bot.getLogger().warning(f"取消用户 {sess.user_id} 的投稿 {a}")

        for sess in to_remove:
            sessions.pop(sess, None)


@bot.on_cmd(
    "删除", help_msg="删除一条投稿, 可以删除多条, 如 #删除 1 2", targets=[config.GROUP]
)
async def delete(msg: GroupMessage):
    async with lock:
        parts = msg.raw_message.split(" ")
        if len(parts) < 2:
            await msg.reply("请带上要删除的投稿id")
            return

        ids = parts[1:]
        for id in ids:
            article = Article.get_or_none((Article.id == id) & (Article.tid == "queue"))
            if not article:
                await msg.reply(f"投稿 #{id} 不在队列中")
                return
            Article.delete_by_id(id)
            if os.path.exists(f"./data/{id}"):
                shutil.rmtree(f"./data/{id}")
            await bot.send_private(
                article.sender_id, f"你的投稿 #{id} 已被管理员删除😵‍💫"
            )

    await msg.reply(f"已删除 {ids}")
    await update_name()
