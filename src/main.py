import asyncio

import core


async def main():
    await asyncio.gather(core.bot.start(), core.server.serve())


asyncio.run(main())
