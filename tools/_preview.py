import asyncio
from qsick.config import RuntimeConfig
from qsick.runtime import Runtime

async def main():
    cfg = RuntimeConfig(data_dir="data/ui-chat")
    cfg.transport.mode = "mock"
    cfg.webui.port = 5199
    cfg.onebot.port = 3010
    rt = Runtime(cfg)
    await rt.start(serve=True)
    await asyncio.Event().wait()

asyncio.run(main())
