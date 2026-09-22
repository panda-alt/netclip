"""把一张 PNG 转成 Windows 图标（`.ico`）。

为什么要专门写一个：Windows 的 `.ico` 是个**容器**，里面可以塞多种尺寸，
系统按使用场景挑 —— 16/24/32 给托盘和小列表、48/64 给资源管理器、
128/256 给大图标和任务栏。只放一张 256 的话，小尺寸由系统临时缩放，
糊得没法看。

另外源图**不一定是正方形**，而图标必须方 —— 所以默认从"内容重心"裁一个正方形。

用法::

    python tools/make_icon.py 源图.png 输出.ico
    python tools/make_icon.py 源图.png 输出.ico --center 0.50,0.45 --zoom 0.85

`--center` 是裁剪中心的相对位置（0~1），`--zoom` 控制裁剪框占短边的比例
（越小 = 放得越大）。默认值是按人物头像调的。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

#: Windows 图标里该有的尺寸。顺序无所谓，Pillow 会自己排。
SIZES = (16, 24, 32, 48, 64, 128, 256)


def build(source: Path, target: Path, center: "tuple[float, float]", zoom: float) -> None:
    try:
        from PIL import Image
    except ImportError:
        raise SystemExit("需要 Pillow：python -m pip install pillow")

    image = Image.open(source)
    if image.mode != "RGBA":
        image = image.convert("RGBA")

    width, height = image.size
    short = min(width, height)
    side = max(16, int(round(short * zoom)))

    #: 圆心按相对位置换算成像素，再夹回图内，保证裁剪框完整落在图里
    cx, cy = int(width * center[0]), int(height * center[1])
    left = min(max(0, cx - side // 2), width - side)
    top = min(max(0, cy - side // 2), height - side)
    square = image.crop((left, top, left + side, top + side))

    print("源图 %dx%d -> 正方形 %dx%d（中心 %s，缩放 %.2f）" % (width, height, side, side, center, zoom))
    square.save(target, format="ICO", sizes=[(s, s) for s in SIZES])
    print("已写出 %s（%d 种尺寸：%s）" % (target, len(SIZES), ", ".join(str(s) for s in SIZES)))


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description="PNG -> 多尺寸 ICO")
    parser.add_argument("source")
    parser.add_argument("target")
    parser.add_argument("--center", default="0.50,0.45", help="裁剪中心（相对位置，默认 0.50,0.45）")
    parser.add_argument("--zoom", type=float, default=0.85, help="裁剪框占短边的比例，默认 0.85")
    args = parser.parse_args(argv)

    cx, _, cy = args.center.partition(",")
    build(Path(args.source), Path(args.target), (float(cx), float(cy or cx)), args.zoom)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
