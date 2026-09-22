"""屏幕摆放关系、边缘检测与坐标映射。

**这是纯逻辑模块，不 import 任何 Windows API**，因此可以直接单测。
所有"机器 A 的屏幕在主屏左边还是上边"的几何问题都在这里解决。

坐标约定
--------
每个操作系统的屏幕坐标原点都是自己的主屏左上角（0, 0）。
本机在 Windows 虚拟桌面坐标下工作（多显示器时原点可能为负），
对端则使用它自己上报的坐标。两者通过比例映射互转。

对于 `peer_position = "right"`（对端在本机右边）的情况::

          本机 (1920x1080)              对端 (2560x1440)
    +----------------------------+  +----------------------------+
    |                            |  |                            |
    |                            |  |                            |
    |                            |  |                            |
    +----------------------------+  +----------------------------+
                                  ^
                          共享边 = 本机的右边界 / 对端的左边界

穿越方向由上一帧的位置判定，这样可以避免"光标贴着边缘抖动时反复穿越"：

  * 本机 -> 对端：`x_prev < right_line <= x`
  * 对端 -> 本机：`x_prev > left_line >= x`

跨轴坐标按比例换算，两端分辨率不同时不会跳变。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

# --------------------------------------------------------------- 方位常量

RIGHT = "right"
LEFT = "left"
UP = "up"
DOWN = "down"
VALID_POSITIONS = (RIGHT, LEFT, UP, DOWN)

#: 每个方位对应的"shared edge"在本机上的名字，以及穿越的轴
#:   horizontal=True 表示沿 x 轴穿越（对端在左右两侧）
_AXIS_BY_POSITION = {
    RIGHT: ("x", True),
    LEFT: ("x", True),
    UP: ("y", False),
    DOWN: ("y", False),
}


def is_horizontal(position: str) -> bool:
    """对端在左右 -> 沿水平轴（x）穿越。"""
    return _AXIS_BY_POSITION[position][1]


@dataclass(frozen=True)
class Rect:
    """屏幕矩形，单位像素。"""

    x: int
    y: int
    w: int
    h: int

    @property
    def right(self) -> int:
        return self.x + self.w

    @property
    def bottom(self) -> int:
        return self.y + self.h

    @property
    def cx(self) -> int:
        return self.x + self.w // 2

    @property
    def cy(self) -> int:
        return self.y + self.h // 2

    def contains(self, px: int, py: int) -> bool:
        return self.x <= px < self.right and self.y <= py < self.bottom

    def clamp(self, px: int, py: int) -> Tuple[int, int]:
        """把坐标夹进矩形内（右/下边界取 -1）。"""
        cx = min(max(px, self.x), self.right - 1)
        cy = min(max(py, self.y), self.bottom - 1)
        return cx, cy

    def as_tuple(self) -> Tuple[int, int, int, int]:
        return (self.x, self.y, self.w, self.h)

    def __str__(self) -> str:  # pragma: no cover - 仅日志
        return "(%d,%d %dx%d)" % (self.x, self.y, self.w, self.h)


@dataclass(frozen=True)
class EdgeHit:
    """一次边缘命中。"""

    position: str  # 命中的是哪个方位的边（= 对端的方位）
    ratio: float  # 沿线方向的归一化位置 [0,1]，用于换算到对端


@dataclass(frozen=True)
class CrossEvent:
    """一次屏幕穿越。"""

    direction: str  # "to_peer" | "to_local"
    ratio: float  # 进入目标屏幕时的沿边比例 [0,1]


#: 窄屏相对于宽屏的对齐方式，决定"哪个区间会发生穿越"
ALIGN_CENTER = "center"
ALIGN_START = "start"
ALIGN_END = "end"
VALID_ALIGNMENTS = (ALIGN_CENTER, ALIGN_START, ALIGN_END)


class ScreenLayout:
    """管理本机/对端屏幕几何与穿越判定。

    参数
    ----
    local_rect: 本机屏幕（虚拟桌面坐标）。
    peer_rect:  对端上报的屏幕（其自身坐标）。
    peer_position: 对端相对本机的位置，right/left/up/down。
    edge_band_px:  边缘判定带宽（像素）。用于"贴着边停住"时的判定容差。
    warp_inset_px: 穿越后把本机光标从边缘拉回来的像素数，防抖必须。
    """

    def __init__(
        self,
        local_rect: Rect,
        peer_rect: Rect,
        peer_position: str = RIGHT,
        edge_band_px: int = 2,
        warp_inset_px: int = 8,
        alignment: str = ALIGN_CENTER,
    ) -> None:
        if peer_position not in VALID_POSITIONS:
            raise ValueError("非法 peer_position: %r（可选 %s）" % (peer_position, ", ".join(VALID_POSITIONS)))
        if alignment not in VALID_ALIGNMENTS:
            raise ValueError("非法 alignment: %r（可选 %s）" % (alignment, ", ".join(VALID_ALIGNMENTS)))
        if local_rect.w <= 0 or local_rect.h <= 0:
            raise ValueError("local_rect 尺寸非法: %s" % local_rect)
        if peer_rect.w <= 0 or peer_rect.h <= 0:
            raise ValueError("peer_rect 尺寸非法: %s" % peer_rect)

        self.local = local_rect
        self.peer = peer_rect
        self.peer_position = peer_position
        self.alignment = alignment
        self.edge_band_px = max(0, int(edge_band_px))
        self.warp_inset_px = max(0, int(warp_inset_px))
        # 穿越后的锁定期：刚穿越完的这段时间内不判反向穿越，避免在共享边上弹跳
        self.armed = True

    # ------------------------------------------------------------ 共享边位置

    @property
    def boundary(self) -> int:
        """共享边在本机坐标系下的位置。

        right -> 本机右边界；left -> 本机左边界；up -> 本机上边界；down -> 本机下边界。
        """
        if self.peer_position == RIGHT:
            return self.local.right
        if self.peer_position == LEFT:
            return self.local.x
        if self.peer_position == UP:
            return self.local.y
        return self.local.bottom

    # ------------------------------------------------------------ 边缘命中判定

    def hit_local_edge(self, px: int, py: int) -> Optional[EdgeHit]:
        """本机光标是否停在"朝向对端"的那条边上（状态机 LOCAL 时使用）。

        只认朝向对端的那一条边；其它三边完全不管，保证本机行为原生。
        """
        band = self.edge_band_px
        if self.peer_position == RIGHT:
            if px >= self.local.right - 1 - band:
                return EdgeHit(RIGHT, self._ratio_y(py))
        elif self.peer_position == LEFT:
            if px <= self.local.x + band:
                return EdgeHit(LEFT, self._ratio_y(py))
        elif self.peer_position == UP:
            if py <= self.local.y + band:
                return EdgeHit(UP, self._ratio_x(px))
        elif self.peer_position == DOWN:
            if py >= self.local.bottom - 1 - band:
                return EdgeHit(DOWN, self._ratio_x(px))
        return None

    def hit_peer_edge(self, px: int, py: int) -> Optional[EdgeHit]:
        """对端光标是否停在"朝向本机"的那条边上（状态机 REMOTE 时使用）。

        `px/py` 是对端坐标系。注意对端朝向本机的边与我们朝向对端的边是相反方向：
        对端在本机右边 => 对端朝本机的边是它的**左**边。
        """
        band = self.edge_band_px
        opposite = _opposite(self.peer_position)
        if opposite == LEFT:
            if px <= self.peer.x + band:
                return EdgeHit(LEFT, self._peer_ratio_y(py))
        elif opposite == RIGHT:
            if px >= self.peer.right - 1 - band:
                return EdgeHit(RIGHT, self._peer_ratio_y(py))
        elif opposite == UP:
            if py <= self.peer.y + band:
                return EdgeHit(UP, self._peer_ratio_x(px))
        elif opposite == DOWN:
            if py >= self.peer.bottom - 1 - band:
                return EdgeHit(DOWN, self._peer_ratio_x(px))
        return None

    # ------------------------------------------------------------ 穿越判定

    def detect_crossing(self, prev: Tuple[int, int], cur: Tuple[int, int]) -> Optional[CrossEvent]:
        """判定本机光标是否发生了"穿过共享边离开本机"的事件。

        用 `prev` -> `cur` 的跨越而不是单纯 `cur` 贴边，这样：
          * 光标贴着边停住不会立刻穿越（必须真的往外走）；
          * 从屏幕外/边缘回来时不会误触发。

        **分支感知**：当本机和对端沿共享边的尺寸不同时，只有共享的那一段区间
        会发生穿越。例如对端在本机右边，对端只有 1080 高而本机 1440 高、
        且 `alignment = "center"`，那么只有中间 1080 像素的高度能穿过去。
        """
        if not self.armed:
            return None

        if self.peer_position == RIGHT:
            line = self.local.right
            if prev[0] < line <= cur[0]:
                ratio = self._branch_ratio_y(cur[1])
                if ratio is not None:
                    return CrossEvent("to_peer", ratio)
        elif self.peer_position == LEFT:
            line = self.local.x
            if prev[0] >= line > cur[0]:
                ratio = self._branch_ratio_y(cur[1])
                if ratio is not None:
                    return CrossEvent("to_peer", ratio)
        elif self.peer_position == UP:
            line = self.local.y
            if prev[1] >= line > cur[1]:
                ratio = self._branch_ratio_x(cur[0])
                if ratio is not None:
                    return CrossEvent("to_peer", ratio)
        elif self.peer_position == DOWN:
            line = self.local.bottom
            if prev[1] < line <= cur[1]:
                ratio = self._branch_ratio_x(cur[0])
                if ratio is not None:
                    return CrossEvent("to_peer", ratio)
        return None

    # ------------------------------------------------------------ 统一坐标系

    def local_to_global(self, px: int, py: int) -> "tuple[int, int]":
        """把本机坐标映射到一个"把两台屏幕摆在一起"的统一坐标系。

        存在的意义：在 REMOTE 状态下**不能**再用本机坐标做判断 ——
        本机光标会被自己屏幕的边界钳住，而对端光标在某条轴上是自由的
        （只有一条边能和本机接触）。用本机坐标当基准，两边的位置认知会越漂越远。

        统一坐标系的定义：本机保持自己的坐标；
        对端沿穿越轴按 `alignment` 平移（左侧/上侧则整体移到本机的另一侧），
        另一条轴沿用本机坐标（因为共享边上那一条轴本来就是同一套值）。

              peer_position="right"，本机 1280x800，对端 1920x1080：
              对端被放在 x ∈ [1280, 3200)，y 沿用本机坐标。
        """
        if is_horizontal(self.peer_position):
            offset = 0 if self.peer_position == RIGHT else -self.peer.w
            # 沿 y 的对齐偏移：让对端的 y 区间跟本机对齐
            y_offset = _alignment_offset(self.local.h, self.peer.h, self.alignment)
            return int(px + offset), int(py - y_offset)
        offset = 0 if self.peer_position == DOWN else -self.peer.h
        x_offset = _alignment_offset(self.local.w, self.peer.w, self.alignment)
        return int(px - x_offset), int(py + offset)

    def global_to_local(self, gx: int, gy: int) -> "tuple[int, int]":
        """`local_to_global` 的逆运算。"""
        if is_horizontal(self.peer_position):
            offset = 0 if self.peer_position == RIGHT else -self.peer.w
            y_offset = _alignment_offset(self.local.h, self.peer.h, self.alignment)
            return int(gx - offset), int(gy + y_offset)
        offset = 0 if self.peer_position == DOWN else -self.peer.h
        x_offset = _alignment_offset(self.local.w, self.peer.w, self.alignment)
        return int(gx + x_offset), int(gy - offset)

    def local_rect_global(self) -> Rect:
        """本机屏幕在统一坐标系里的矩形。

        恒为 `(0, 0, local.w, local.h)` —— 统一坐标系就是**以本机屏幕左上角为原点**
        定义的，对端摆在它旁边。单独写成方法是为了让"本机也在统一坐标系里"
        这件事在代码里显式可见（早期版本就是这里算错，导致两块屏幕重叠）。
        """
        return Rect(0, 0, self.local.w, self.local.h)

    def global_contains_peer(self, gx: int, gy: int) -> bool:
        """统一坐标下的点是否落在对端屏幕内（含边界语义，右/下开区间）。"""
        rect = self.peer_rect_global()
        return rect.x <= gx < rect.right and rect.y <= gy < rect.bottom

    def global_contains_local(self, gx: int, gy: int) -> bool:
        rect = self.local_rect_global()
        return rect.x <= gx < rect.right and rect.y <= gy < rect.bottom

    def global_clamp_to_peer(self, gx: int, gy: int) -> "tuple[int, int]":
        return self.peer_rect_global().clamp(int(gx), int(gy))

    # ------------------------------------------------------------ 统一坐标系

    def peer_local_to_global(self, px: int, py: int) -> "Tuple[int, int]":
        """**对端坐标系** -> 统一坐标系。

        实现就是对 `peer_rect_global()` 的原点做**纯平移**。

        **两条轴都要加上偏移，不只是穿越轴。** 非穿越轴上的偏移来自 `alignment`
        （两边屏幕尺寸不同时的居中/贴边对齐）。真机上踩到的坑：上下叠放、
        主机 2560 宽、从机 1920 宽、`center` 对齐 -> 对端矩形整体偏移 320 像素，
        而这个函数早期只处理了穿越轴，于是发给从机的绝对落点整体偏 320 ——
        从机光标被推到屏幕外侧钳住，表现为"拉走又被慢慢拉回来"的阻尼弹簧。

        早期这里叫 `local_to_global`，但它的语义其实是"对端坐标 -> 统一坐标"，
        名字和实际含义不一致正是这个 bug 藏了这么久的原因，所以改名。
        """
        rect = self.peer_rect_global()
        return int(px) + rect.x, int(py) + rect.y

    def global_to_peer_local(self, gx: int, gy: int) -> "Tuple[int, int]":
        """统一坐标系 -> **对端坐标系**（`peer_local_to_global` 的逆运算）。"""
        rect = self.peer_rect_global()
        return int(gx) - rect.x, int(gy) - rect.y

    def peer_rect_global(self) -> Rect:
        """对端屏幕在统一坐标系里的矩形。

        沿穿越轴：紧贴本机矩形摆在另一侧。
        另一条轴：按 alignment 相对本机屏幕对齐（可能出现负坐标，这也正常）。
        """
        base = self.local_rect_global()
        if is_horizontal(self.peer_position):
            px = base.right if self.peer_position == RIGHT else base.x - self.peer.w
            py = base.y + _alignment_offset(self.local.h, self.peer.h, self.alignment)
            return Rect(px, py, self.peer.w, self.peer.h)
        py = base.bottom if self.peer_position == DOWN else base.y - self.peer.h
        px = base.x + _alignment_offset(self.local.w, self.peer.w, self.alignment)
        return Rect(px, py, self.peer.w, self.peer.h)

    def rect_containing(self, gx: int, gy: int) -> str:
        """统一坐标下的点落在哪块屏幕上: "local" / "peer" / "none"。"""
        if self.local_rect_global().contains(int(gx), int(gy)):
            return "local"
        if self.peer_rect_global().contains(int(gx), int(gy)):
            return "peer"
        return "none"

    # ------------------------------------------------------------ 分支区间

    def _shared_interval(self) -> Optional[Tuple[int, int, int, int]]:
        """算出共享边上的重叠区间。

        返回 `(local_lo, local_hi, peer_lo, peer_hi)`，单位分别是各自的坐标系。
        没有重叠（例如 4K 屏对上 800x600 且对齐方式导致完全不搭）则返回 None，
        此时表现为"这条边不触发穿越"，而不是乱穿。
        """
        if is_horizontal(self.peer_position):
            local_lo, local_hi = self.local.y, self.local.bottom
            peer_size, local_size = self.peer.h, self.local.h
        else:
            local_lo, local_hi = self.local.x, self.local.right
            peer_size, local_size = self.peer.w, self.local.w

        offset = _alignment_offset(local_size, peer_size, self.alignment)
        # 对端坐标系的起点换算到本机坐标系下是 `local_lo + offset`
        # （因为 本机坐标 = 对端坐标 + offset）
        peer_lo = local_lo + offset
        peer_hi = peer_lo + peer_size

        lo = max(local_lo, peer_lo)
        hi = min(local_hi, peer_hi)
        if hi <= lo:
            return None
        peer_start = lo - peer_lo
        return (lo, hi, peer_start, peer_start + (hi - lo))

    def _branch_ratio_y(self, y: int) -> Optional[float]:
        span = self._shared_interval()
        if span is None:
            return None
        local_lo, local_hi, _peer_lo, _peer_hi = span
        if not (local_lo <= y < local_hi):
            return None
        return _clamp01((y - local_lo) / float(max(1, local_hi - local_lo - 1)))

    def _branch_ratio_x(self, x: int) -> Optional[float]:
        span = self._shared_interval()
        if span is None:
            return None
        local_lo, local_hi, _peer_lo, _peer_hi = span
        if not (local_lo <= x < local_hi):
            return None
        return _clamp01((x - local_lo) / float(max(1, local_hi - local_lo - 1)))

    def _peer_branch_ratio_y(self, py: int) -> Optional[float]:
        span = self._shared_interval()
        if span is None:
            return None
        _local_lo, _local_hi, peer_lo, peer_hi = span
        if not (peer_lo <= py < peer_hi):
            return None
        return _clamp01((py - peer_lo) / float(max(1, peer_hi - peer_lo - 1)))

    def _peer_branch_ratio_x(self, px: int) -> Optional[float]:
        span = self._shared_interval()
        if span is None:
            return None
        _local_lo, _local_hi, peer_lo, peer_hi = span
        if not (peer_lo <= px < peer_hi):
            return None
        return _clamp01((px - peer_lo) / float(max(1, peer_hi - peer_lo - 1)))

    # ------------------------------------------------------------ 坐标换算

    def entry_point(self, ratio: float, from_side: str) -> Tuple[int, int]:
        """算出"从 from_side 边进入本机"时本机光标应该落在哪里。

        from_side 是**本机**的哪条边被进入（与对端方位一致）：
        对端在右边 -> 从本机右边进入。
        结果带 inset，避免落在边缘上立刻又被判出去。
        """
        inset = min(self.warp_inset_px, max(0, self.local.w // 2 - 1), max(0, self.local.h // 2 - 1))
        r = _clamp01(ratio)
        if from_side == RIGHT:
            return self.local.right - 1 - inset, _lerp(self.local.y, self.local.bottom - 1, r)
        if from_side == LEFT:
            return self.local.x + inset, _lerp(self.local.y, self.local.bottom - 1, r)
        if from_side == UP:
            return _lerp(self.local.x, self.local.right - 1, r), self.local.y + inset
        if from_side == DOWN:
            return _lerp(self.local.x, self.local.right - 1, r), self.local.bottom - 1 - inset
        raise ValueError("非法 from_side: %r" % from_side)

    def peer_entry_point(self, ratio: float, to_side: str) -> Tuple[int, int]:
        """算出"离开本机进入对端"时对端光标应该落在哪里（对端坐标系）。

        to_side 是**对端**的哪条边被进入，即与 peer_position 相反的边。
        """
        inset = min(self.warp_inset_px, max(0, self.peer.w // 2 - 1), max(0, self.peer.h // 2 - 1))
        r = _clamp01(ratio)
        if to_side == LEFT:
            return self.peer.x + inset, _lerp(self.peer.y, self.peer.bottom - 1, r)
        if to_side == RIGHT:
            return self.peer.right - 1 - inset, _lerp(self.peer.y, self.peer.bottom - 1, r)
        if to_side == UP:
            return _lerp(self.peer.x, self.peer.right - 1, r), self.peer.y + inset
        if to_side == DOWN:
            return _lerp(self.peer.x, self.peer.right - 1, r), self.peer.bottom - 1 - inset
        raise ValueError("非法 to_side: %r" % to_side)

    def local_to_peer(self, px: int, py: int) -> Tuple[int, int]:
        """本机坐标 -> 对端坐标（按比例）。"""
        rx = (px - self.local.x) / float(self.local.w)
        ry = (py - self.local.y) / float(self.local.h)
        return (
            int(self.peer.x + rx * self.peer.w),
            int(self.peer.y + ry * self.peer.h),
        )

    def peer_to_local(self, px: int, py: int) -> Tuple[int, int]:
        """对端坐标 -> 本机坐标（按比例）。"""
        rx = (px - self.peer.x) / float(self.peer.w)
        ry = (py - self.peer.y) / float(self.peer.h)
        return (
            int(self.local.x + rx * self.local.w),
            int(self.local.y + ry * self.local.h),
        )

    # ------------------------------------------------------------ 内部工具

    def _ratio_x(self, px: int) -> float:
        return _clamp01((px - self.local.x) / float(max(1, self.local.w - 1)))

    def _ratio_y(self, py: int) -> float:
        return _clamp01((py - self.local.y) / float(max(1, self.local.h - 1)))

    def _peer_ratio_x(self, px: int) -> float:
        return _clamp01((px - self.peer.x) / float(max(1, self.peer.w - 1)))

    def _peer_ratio_y(self, py: int) -> float:
        return _clamp01((py - self.peer.y) / float(max(1, self.peer.h - 1)))

    def describe(self) -> str:
        axis = "水平" if is_horizontal(self.peer_position) else "垂直"
        zh = {"right": "右", "left": "左", "up": "上", "down": "下"}[self.peer_position]
        return "对端在本机%s侧（%s轴穿越）| 本机=%s 对端=%s 边缘带宽=%dpx" % (
            zh,
            axis,
            self.local,
            self.peer,
            self.edge_band_px,
        )


def _opposite(position: str) -> str:
    return {RIGHT: LEFT, LEFT: RIGHT, UP: DOWN, DOWN: UP}[position]


def _alignment_offset(local_size: int, peer_size: int, alignment: str) -> int:
    """把对端的起点换算到本机坐标系时的偏移量。

    * `center`：对端与本机居中对齐（上下留均匀黑边）；
    * `start` ：对端起边与本机起边对齐（例如两块屏幕都贴左上角）；
    * `end`   ：对端末边与本机末边对齐。

    返回值是"本机坐标 = 对端坐标 + offset"里的 offset。
    """
    if alignment == ALIGN_START:
        return 0
    if alignment == ALIGN_END:
        return local_size - peer_size
    return (local_size - peer_size) // 2


def _clamp01(value: float) -> float:
    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return value


def _lerp(lo: int, hi: int, ratio: float) -> int:
    return int(round(lo + (hi - lo) * ratio))
