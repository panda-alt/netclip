"""临时诊断脚本：单独跑 NetworkManager 的启动/停止，定位 shutdown 卡住的位置。

用一次就删。
"""

import asyncio
import logging
import sys
import time

sys.path.insert(0, ".")

logging.basicConfig(level=logging.DEBUG, format="%(relativeCreated)7.0fms %(name)-22s %(message)s")

from netclip.config import from_dict
from netclip.net.manager import NetManager


async def main() -> None:
    cfg = from_dict(
        {
            "device": {"name": "diag"},
            "network": {"peer_ip": "", "listen_port": 24800, "heartbeat_sec": 2, "timeout_sec": 6},
            "security": {"psk": "diag"},
        }
    )
    mgr = NetManager(cfg)
    print("[%s] starting manager" % time.strftime("%H:%M:%S"))
    await mgr.start()
    print("[%s] manager started; sleeping 1s" % time.strftime("%H:%M:%S"))
    await asyncio.sleep(1.0)
    print("[%s] calling stop" % time.strftime("%H:%M:%S"))
    t0 = time.monotonic()
    try:
        await asyncio.wait_for(mgr.stop(), timeout=6.0)
    except (asyncio.TimeoutError, TimeoutError):
        print("!!! stop 超时 %.1fs" % (time.monotonic() - t0))
    else:
        print("[%s] stop 正常返回，耗时 %.2fs" % (time.strftime("%H:%M:%S"), time.monotonic() - t0))

    print("剩余任务:")
    for task in asyncio.all_tasks():
        if task is not asyncio.current_task():
            print("   ", task.get_name(), task)
    await asyncio.sleep(0.3)
    print("done")


asyncio.run(main())
