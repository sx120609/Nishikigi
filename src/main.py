import asyncio

import core


async def main():
    core.scheduler.start()
    await asyncio.gather(core.bot.start(), core.server.serve())


asyncio.run(main())
