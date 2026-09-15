# -*- coding: utf-8 -*-
# super_mario.py 资源加载部分（PC 验证与 K230 板端共用）
# 板端使用 image.Image + to_rgb565；PC 验证版用 pygame.Surface
# 本文件由 游戏.py 在 PC 上直接运行，由 super_mario.py 在板端以同一份逻辑运行。
# 为避免两份代码漂移，游戏逻辑全部放在 super_mario.py，PC 验证层通过 monkey patch
# 替换 draw_surface / load_image 即可。

# ============================================================
# 以下 SUPER_MARIO_BODY 标记之间是唯一的游戏逻辑真源。
# 游戏.py 提取它生成 PC 验证版；板端直接运行本文件。
# ============================================================
# ==== SUPER_MARIO_BODY_BEGIN ====
import gc
import os
import struct
import sys
import time

try:
    import ujson as json_mod
except ImportError:
    import json as json_mod

from machine import TOUCH, I2C
from media.display import *
from media.media import *

BASE = '/sdcard'
RESOURCE_DIR = '/data/mario'   # 资源放在 data 分区
MARKER_DIR = '/sdcard'  # 标记文件与 main.py 同区

SCREEN_W = 800
SCREEN_H = 480
HUD_H = 42
CHUNK_W = 400
SIM_MS = 16
FRAME_MS = 20
MAX_CATCHUP = 5
TOUCH_POINTS = 5
TOUCH_POLL_FRAMES = 1
BG_STREAM_BYTES = 16384
# Synchronous level preparation can use larger reads because it does not share
# the frame budget.  This reduces DeflateIO/read call overhead at level entry.
BG_LOAD_BYTES = 32768
# Keep several full background chunks ready before the camera can reach them.
# A 400px chunk needs more than a few frames to inflate, so two chunks is not
# enough when the player is running at full speed.
BG_PREFETCH_AHEAD = 3
BG_STARTUP_PRELOAD = 3
BG_CACHE_BEHIND = 1
# Keep recently used levels so death/restart and level selection do not need
# to read the same background files again.  Five levels leave more room for
# sprites and gameplay objects on the MicroPython image heap.
BG_KEEP_LEVELS = 5
# Chunk limit is a safety valve for the current MicroPython image heap.  It is
# intentionally independent from the requested 10-level history.
BG_CACHE_MAX_CHUNKS = 50
# A 400x480 RGB565 chunk is about 384KB.  Levels up to 30 chunks (including
# 2-1's 26 chunks) are prepared before gameplay so the camera never triggers
# a synchronous decode during normal movement.  Larger levels stay streamed
# to avoid exhausting the display and sprite buffers.
BG_FULL_LOAD_MAX_CHUNKS = 30
# Runtime file writes can block the frame loop on the data partition.  Keep
# this disabled for normal play; enable temporarily only for diagnostics.
PERF_LOG_ENABLED = False

SCALE_UNIT = 0.8  # 原逻辑坐标 -> K230 坐标

TITLE = 0
PLAYING = 1
PAUSED = 2
DEAD = 3
LEVEL_CLEAR = 4
GAME_OVER = 5
COMPLETE = 6
MAIN_LEVEL_COUNT = 32

PAUSE_MENU_ITEMS = ('继续游戏', '重新开始', '选择关卡')

SOLID_BUCKET_W = 256

GRAVITY = 0.8
JUMP_GRAVITY = 0.24
MAX_FALL = 8.8
WALK_SPEED = 4.8
RUN_SPEED = 9.6
WALK_ACCEL = 0.36
RUN_ACCEL = 0.60
TURN_BRAKE = 0.82
JUMP_VY = -8.4
ENEMY_SPEED = 0.8
STOMP_BOUNCE = -6.0

TOUCH_BUTTONS = (
    ('left', 80, 400, 65),
    ('right', 220, 400, 65),
    ('down', 150, 444, 34),
    ('b', 610, 400, 60),
    ('a', 735, 380, 68),
)
PAUSE_AREA = (700, 20, 90, 50)


def path_join(a, b, c=None):
    if c is not None:
        return path_join(path_join(a, b), c)
    if a.endswith('/'):
        return a + b
    return a + '/' + b


def path_exists(path):
    try:
        os.stat(path)
        return True
    except BaseException:
        return False


def write_marker(name, text):
    try:
        with open(path_join(MARKER_DIR, name), 'w') as f:
            f.write(text)
    except BaseException:
        pass


def intersects(a, b):
    return (a[0] < b[0] + b[2] and a[0] + a[2] > b[0]
            and a[1] < b[1] + b[3] and a[1] + a[3] > b[1])


# 32 点整数正弦表 (飞鱼游动用)
try:
    import math as _math
    _sin_tbl = tuple(round(100 * _math.sin(i * 3.14159265 / 16)) / 100
                     for i in range(32))
except ImportError:  # 兜底折线表
    _sin_tbl = tuple((i if i < 16 else 31 - i) / 8.0 - 1.0 for i in range(32))


class FastFT5316Touch:
    """Read all FT5316 points in one I2C transaction instead of 31 driver calls."""
    EVENT_NONE = 0
    EVENT_UP = 1
    EVENT_DOWN = 2
    EVENT_MOVE = 3
    reliable_multitouch = True

    def __init__(self, bus_id=3):
        self.i2c = None
        self.buf = bytearray(31)
        try:
            self.i2c = I2C(bus_id, freq=400000)
            # Fail during setup so main() can select the native fallback.
            self.i2c.readfrom_mem_into(0x38, 2, self.buf, addrsize=8)
            if (self.buf[0] & 0x0f) > 5:
                raise OSError('invalid FT5316 response')
        except BaseException:
            self.deinit()
            raise

    def read(self, point_count=5):
        self.i2c.readfrom_mem_into(0x38, 2, self.buf, addrsize=8)
        count = min(self.buf[0] & 0x0f, point_count, 5)
        points = []
        offset = 1
        for _ in range(count):
            raw_event = self.buf[offset] >> 6
            if raw_event == 0:
                event = self.EVENT_DOWN
            elif raw_event == 1:
                event = self.EVENT_UP
            elif raw_event == 2:
                event = self.EVENT_MOVE
            else:
                event = self.EVENT_NONE
            raw_x = ((self.buf[offset] & 0x0f) << 8) | self.buf[offset + 1]
            raw_y = ((self.buf[offset + 2] & 0x0f) << 8) | self.buf[offset + 3]
            # CanMV v1.6 on this LCKFB board reports screen (raw_y, raw_x).
            points.append((event, raw_y, raw_x))
            offset += 6
        return points

    def deinit(self):
        if self.i2c is not None:
            self.i2c.deinit()
            self.i2c = None


class SpriteMask:
    """Precomputed opaque rectangles for K230, whose draw_image mask is inert."""

    def __init__(self, bitmap):
        self.rects = []
        active = {}
        for y in range(bitmap.height() + 1):
            runs = []
            if y < bitmap.height():
                x = 0
                width = bitmap.width()
                while x < width:
                    while x < width and bitmap.get_pixel(x, y) != 0:
                        x += 1
                    if x >= width:
                        break
                    start = x
                    while x < width and bitmap.get_pixel(x, y) == 0:
                        x += 1
                    runs.append((start, x - start))
            current = set(runs)
            for key in list(active):
                if key not in current:
                    x0, width, y0 = active.pop(key)
                    self.rects.append((x0, width, y0, y - y0))
            for key in current:
                if key not in active:
                    active[key] = (key[0], key[1], y)
        # A full-frame mask does not need the expensive ROI loop.  This is
        # common for brick tiles and keeps dense 2-1 brick walls cheap.
        self.opaque = (len(self.rects) == 1 and
                       self.rects[0][0] == 0 and
                       self.rects[0][1] == bitmap.width() and
                       self.rects[0][2] == 0 and
                       self.rects[0][3] == bitmap.height())


class USBGamepadInput:
    """USB input reader for CanMV v1.8 HID and legacy evdev backends."""

    EVENT_SIZE = 24  # K230 is 64-bit: timeval(16) + type/code/value(8).
    EV_KEY = 1
    EV_ABS = 3
    LEFT_KEYS = (30, 105, 546)       # keyboard A/Left, gamepad D-pad left
    RIGHT_KEYS = (32, 106, 547)      # keyboard D/Right, gamepad D-pad right
    DOWN_KEYS = (31, 108, 545)       # keyboard S/Down, gamepad D-pad down
    JUMP_KEYS = (37, 44, 57, 289, 304)  # keyboard K/Z/Space, gamepad A
    ACTION_KEYS = (36, 42, 45, 54, 288, 290, 305, 307, 308)
    START_KEYS = (1, 25, 28, 297, 299, 314, 315)

    def __init__(self):
        self.device = None
        self.device_kind = ''
        self.fds = []
        self.paths = []
        self.connected = False
        self.state = {'left': False, 'right': False, 'down': False,
                      'a': False, 'b': False, 'pause': False}
        self.axis_x = 0
        self.axis_y = 0
        self.hat_x = 0
        self.hat_y = 0
        self.start_pressed = False
        self.last_scan = 0
        self.scan()

    def _event_paths(self):
        selected = []
        try:
            text = open('/proc/bus/input/devices', 'r').read()
            for block in text.split('\n\n'):
                low = block.lower()
                is_pad = ('js' in low or 'gamepad' in low or 'joystick' in low
                          or 'controller' in low or 'playstation' in low
                          or 'ps2' in low)
                if not is_pad:
                    continue
                for line in block.split('\n'):
                    if 'Handlers=' not in line:
                        continue
                    for word in line.split():
                        if word.startswith('event'):
                            selected.append('/dev/input/' + word)
        except BaseException:
            pass
        if selected:
            return selected
        return []

    def scan(self):
        self.deinit()
        # CanMV v1.8 exposes HID devices through the usb module.  Prefer a
        # Gamepad class when using the patched firmware, then accept keyboard
        # mode adapters as an immediately usable fallback.
        try:
            import usb
            for class_name in ('Gamepad', 'Keyboard'):
                cls = getattr(usb, class_name, None)
                if cls is None:
                    continue
                try:
                    dev = cls(timeout_ms=1, auto_reconnect=False)
                    dev.open()
                    info = dev.info()
                    self.device = dev
                    self.device_kind = class_name.lower()
                    self.paths = [info.get('path', self.device_kind)]
                    self.connected = True
                    self.last_scan = time.ticks_ms()
                    return
                except BaseException:
                    try:
                        dev.close()
                    except BaseException:
                        pass
        except BaseException:
            pass

        flags = getattr(os, 'O_RDONLY', 0) | getattr(os, 'O_NONBLOCK', 0x800)
        for path in self._event_paths():
            try:
                self.fds.append(os.open(path, flags))
                self.paths.append(path)
            except BaseException:
                pass
        self.connected = bool(self.fds)
        self.last_scan = time.ticks_ms()

    @staticmethod
    def _axis_direction(value):
        if -1 <= value <= 1:
            return value
        if 0 <= value <= 255:
            return -1 if value < 88 else (1 if value > 168 else 0)
        return -1 if value < -9000 else (1 if value > 9000 else 0)

    def _apply_event(self, event_type, code, value):
        down = value != 0
        before = dict(self.state)
        if event_type == self.EV_KEY:
            if code in self.LEFT_KEYS:
                self.state['left'] = down
            elif code in self.RIGHT_KEYS:
                self.state['right'] = down
            elif code in self.DOWN_KEYS:
                self.state['down'] = down
            elif code in self.JUMP_KEYS:
                self.state['a'] = down
            elif code in self.ACTION_KEYS:
                self.state['b'] = down
            elif code in self.START_KEYS:
                self.state['pause'] = down
        elif event_type == self.EV_ABS:
            if code == 0:
                self.axis_x = self._axis_direction(value)
            elif code == 1:
                self.axis_y = self._axis_direction(value)
            elif code == 16:
                self.hat_x = self._axis_direction(value)
            elif code == 17:
                self.hat_y = self._axis_direction(value)
            direction_x = self.hat_x if self.hat_x else self.axis_x
            direction_y = self.hat_y if self.hat_y else self.axis_y
            self.state['left'] = direction_x < 0
            self.state['right'] = direction_x > 0
            self.state['down'] = direction_y > 0
        if any(self.state[name] and not before[name]
               for name in ('left', 'right', 'down', 'a', 'b', 'pause')):
            self.start_pressed = True

    def poll(self):
        self.start_pressed = False
        if self.device is not None:
            try:
                frame = self.device.read(1)
                if frame:
                    for event in frame.get('events', ()):
                        if len(event) >= 3:
                            self._apply_event(event[0], event[1], event[2])
                        elif len(event) == 2:
                            self._apply_event(self.EV_KEY, event[0], event[1])
                return self.state
            except BaseException:
                self.deinit()
                self.last_scan = time.ticks_ms()

        if not self.fds:
            if time.ticks_diff(time.ticks_ms(), self.last_scan) > 2000:
                self.scan()
            return self.state
        alive = []
        for fd in self.fds:
            try:
                data = os.read(fd, self.EVENT_SIZE * 16)
            except OSError:
                data = b''
            except BaseException:
                try:
                    os.close(fd)
                except BaseException:
                    pass
                continue
            alive.append(fd)
            usable = len(data) - (len(data) % self.EVENT_SIZE)
            for offset in range(0, usable, self.EVENT_SIZE):
                event_type, code, value = struct.unpack(
                    '<HHi', data[offset + 16:offset + 24])
                self._apply_event(event_type, code, value)
        self.fds = alive
        self.connected = bool(alive)
        return self.state

    def deinit(self):
        device = getattr(self, 'device', None)
        if device is not None:
            try:
                device.close()
            except BaseException:
                pass
        self.device = None
        self.device_kind = ''
        for fd in getattr(self, 'fds', ()):
            try:
                os.close(fd)
            except BaseException:
                pass
        self.fds = []
        self.paths = []
        self.connected = False


class TouchInput:
    """多点触摸状态机。每显示帧只读一次，物理子步复用当前状态。"""

    def __init__(self, tp, gamepad=None):
        self.tp = tp
        self.gamepad = gamepad
        self.points = []
        self.pressed = {'left': False, 'right': False, 'down': False,
                        'a': False, 'b': False}
        self.prev = {'left': False, 'right': False, 'down': False,
                     'a': False, 'b': False}
        self.jump_pressed = False
        self.action_pressed = False
        self.left_pressed = False
        self.right_pressed = False
        self.down_pressed = False
        self.pause_pressed = False
        self.touch_pressed = False
        self.stale_frames = 0
        self.missing = {'left': 0, 'right': 0, 'down': 0, 'a': 0, 'b': 0}
        self._pause_prev = False
        self._touch_prev = False

    def poll(self):
        self.prev = dict(self.pressed)
        seen = {'left': False, 'right': False, 'down': False,
                'a': False, 'b': False}
        up_seen = {'left': False, 'right': False, 'down': False,
                   'a': False, 'b': False}
        pause_now = False
        try:
            self.points = self.tp.read(TOUCH_POINTS)
        except BaseException:
            self.points = []
        touch_type = type(self.tp)
        event_up = getattr(self.tp, 'EVENT_UP', getattr(touch_type, 'EVENT_UP', 1))
        event_down = getattr(self.tp, 'EVENT_DOWN', getattr(touch_type, 'EVENT_DOWN', 2))
        event_move = getattr(self.tp, 'EVENT_MOVE', getattr(touch_type, 'EVENT_MOVE', 3))
        any_contact = False
        for p in self.points:
            if isinstance(p, tuple):
                ev, x, y = p
            else:
                ev, x, y = p.event, p.x, p.y
            if ev == event_up:
                for name, cx, cy, r in TOUCH_BUTTONS:
                    dx = x - cx
                    dy = y - cy
                    if dx * dx + dy * dy <= r * r:
                        up_seen[name] = True
                continue
            if ev == 0 or ev == event_down or ev == event_move:
                any_contact = True
                for name, cx, cy, r in TOUCH_BUTTONS:
                    dx = x - cx
                    dy = y - cy
                    if dx * dx + dy * dy <= r * r:
                        seen[name] = True
                if PAUSE_AREA[0] <= x <= PAUSE_AREA[0] + PAUSE_AREA[2] \
                        and PAUSE_AREA[1] <= y <= PAUSE_AREA[1] + PAUSE_AREA[3]:
                    pause_now = True
        if getattr(self.tp, 'reliable_multitouch', False):
            self.stale_frames = 0
            for name in self.pressed:
                self.pressed[name] = seen[name] if any_contact else False
                self.missing[name] = 0
            if seen['down']:
                self.pressed['left'] = False
                self.pressed['right'] = False
            elif seen['left']:
                self.pressed['right'] = False
            elif seen['right']:
                self.pressed['left'] = False
        elif any_contact:
            self.stale_frames = 0
            direction_seen = seen['left'] or seen['right'] or seen['down']
            action_seen = seen['a'] or seen['b']
            for name in self.pressed:
                if seen[name]:
                    self.pressed[name] = True
                    self.missing[name] = 0
                else:
                    self.missing[name] += 1
                    # 部分触摸固件会轮流上报触点。动作键出现时保持已有方向；
                    # A/B 允许短暂漏报，避免方向+A/B 的组合被拆成单键。
                    if name in ('left', 'right', 'down') and action_seen:
                        continue
                    grace = 3 if name in ('a', 'b') and direction_seen else 0
                    if up_seen[name] or self.missing[name] > grace:
                        self.pressed[name] = False
            if seen['down']:
                self.pressed['left'] = False
                self.pressed['right'] = False
            elif seen['left']:
                self.pressed['right'] = False
            elif seen['right']:
                self.pressed['left'] = False
        else:
            self.stale_frames += 1
            if self.stale_frames > 2:
                # 连续多帧无触点，认为全部释放
                self.pressed = {'left': False, 'right': False, 'down': False,
                                'a': False, 'b': False}
                self.missing = {'left': 0, 'right': 0, 'down': 0,
                                'a': 0, 'b': 0}
        gamepad_start = False
        if self.gamepad is not None:
            pad = self.gamepad.poll()
            for name in ('left', 'right', 'down', 'a', 'b'):
                self.pressed[name] = self.pressed[name] or pad[name]
            pause_now = pause_now or pad['pause']
            gamepad_start = self.gamepad.start_pressed
        self.left_pressed = self.left_pressed or (
            self.pressed['left'] and not self.prev['left'])
        self.right_pressed = self.right_pressed or (
            self.pressed['right'] and not self.prev['right'])
        self.down_pressed = self.down_pressed or (
            self.pressed['down'] and not self.prev['down'])
        self.jump_pressed = self.jump_pressed or (self.pressed['a'] and not self.prev['a'])
        self.action_pressed = self.action_pressed or (self.pressed['b'] and not self.prev['b'])
        self.touch_pressed = (self.touch_pressed or gamepad_start
                              or (any_contact and not self._touch_prev))
        self.pause_pressed = self.pause_pressed or (pause_now and not self._pause_prev)
        self._pause_prev = pause_now
        self._touch_prev = any_contact

    def is_down(self, name):
        return self.pressed[name]

    def consume_actions(self):
        self.jump_pressed = False
        self.action_pressed = False
        self.left_pressed = False
        self.right_pressed = False
        self.down_pressed = False
        self.touch_pressed = False


class Player:
    SMALL_W = 32
    SMALL_H = 38
    BIG_W = 36
    BIG_H = 74

    def __init__(self):
        self.reset(110, 400)

    def reset(self, x, y_bottom):
        self.x = float(x)
        self.y = float(y_bottom - self.SMALL_H)
        self.vx = 0.0
        self.vy = 0.0
        self.mode = 0  # 0 small 1 big 2 fire
        self.face_right = True
        self.on_ground = True
        self.invulnerable = 0
        self.anim = 0
        self.anim_timer = 0
        self.dead = False
        self.death_timer = 0
        self.fire_cooldown = 0
        self.jump_held = False
        self.walk_auto = False
        self.flag_slide = False

    @property
    def big(self):
        return self.mode >= 1

    @property
    def fire(self):
        return self.mode == 2

    @property
    def w(self):
        return self.BIG_W if self.mode >= 1 else self.SMALL_W

    @property
    def h(self):
        return self.BIG_H if self.mode >= 1 else self.SMALL_H

    @property
    def bottom(self):
        return self.y + self.h

    def set_bottom(self, b):
        self.y = b - self.h

    def grow(self, mode):
        foot = self.bottom
        cx = self.x + self.w / 2
        self.mode = max(self.mode, mode)
        self.x = cx - self.w / 2
        self.set_bottom(foot)

    def shrink(self):
        foot = self.bottom
        cx = self.x + self.w / 2
        self.mode = 0
        self.x = cx - self.w / 2
        self.set_bottom(foot)
        self.invulnerable = 2000

    def rect(self):
        return (self.x, self.y, self.w, self.h)


class Solid:
    __slots__ = ('x', 'y', 'w', 'h', 'kind', 'obj')

    def __init__(self, x, y, w, h, kind, obj=None):
        self.x = x
        self.y = y
        self.w = w
        self.h = h
        self.kind = kind  # 0 static 1 slider 2 brick 3 box
        self.obj = obj

    def rect(self):
        return (self.x, self.y, self.w, self.h)


class Slider:
    def __init__(self, data):
        self.num = data.get('num', 1)
        self.direction = data.get('direction', 0)
        self.x = data['x'] * SCALE_UNIT
        self.y = data['y'] * SCALE_UNIT
        self.range_start = data.get('range_start', 0)
        self.range_end = data.get('range_end', 0)
        self.velocity = data.get('velocity', 1) * SCALE_UNIT
        self.tile_w = 34
        self.tile_h = 34
        if self.direction == 0:
            self.w = self.num * self.tile_w
            self.h = self.tile_h
            self.pos = self.x
            self.min_pos = self.range_start * SCALE_UNIT
            self.max_pos = self.range_end * SCALE_UNIT - self.w
        else:
            self.w = self.tile_w
            self.h = self.num * self.tile_h
            self.pos = self.y
            self.min_pos = self.range_start * SCALE_UNIT
            self.max_pos = self.range_end * SCALE_UNIT - self.h
        self.vx = self.velocity if self.direction == 0 else 0
        self.vy = self.velocity if self.direction == 1 else 0

    def update(self):
        if self.direction == 0:
            self.x += self.vx
            if self.x < self.min_pos:
                self.x = self.min_pos
                self.vx = abs(self.vx)
            elif self.x > self.max_pos:
                self.x = self.max_pos
                self.vx = -abs(self.vx)
        else:
            self.y += self.vy
            if self.y < self.min_pos:
                self.y = self.min_pos
                self.vy = abs(self.vy)
            elif self.y > self.max_pos:
                self.y = self.max_pos
                self.vy = -abs(self.vy)

    def rect(self):
        return (self.x, self.y, self.w, self.h)


class Brick:
    def __init__(self, data):
        self.x = data['x'] * SCALE_UNIT
        self.y = data['y'] * SCALE_UNIT
        self.type = data.get('type', 0)
        self.item = data.get('item', 0)
        self.brick_num = data.get('brick_num', 1)
        self.direction = data.get('direction', 0)
        self.used = False
        self.bump_t = 0
        self.w = 34
        self.h = 34

    def rect(self):
        return (self.x, self.y, self.w, self.h)


class Box:
    def __init__(self, data):
        self.x = data['x'] * SCALE_UNIT
        self.y = data['y'] * SCALE_UNIT
        self.type = data.get('type', 1)
        self.used = False
        self.bump_t = 0
        self.w = 34
        self.h = 34
        self.anim_t = 0

    def rect(self):
        return (self.x, self.y, self.w, self.h)


class Coin:
    def __init__(self, data):
        self.x = data['x'] * SCALE_UNIT
        self.y = data['y'] * SCALE_UNIT
        self.w = 19
        self.h = 28
        self.taken = False
        self.anim_t = 0

    def rect(self):
        return (self.x, self.y, self.w, self.h)


class PopCoin:
    def __init__(self, x, y):
        self.x = x
        self.y = y
        self.vy = -8.0
        self.w = 17
        self.h = 25
        self.dead = False
        self.age = 0

    def update(self, dt_ms):
        self.age += dt_ms
        self.y += self.vy
        self.vy += 0.5
        if self.age >= 700:
            self.dead = True

    def rect(self):
        return (self.x, self.y, self.w, self.h)


class PowerUp:
    MUSHROOM = 3
    FLOWER = 4
    ONEUP = 5
    STAR = 6

    def __init__(self, kind, x, y):
        self.kind = kind
        self.x = x
        self.y = y
        self.w = 34
        self.h = 34
        self.vx = 1.6
        self.vy = 0.0
        self.rise_from = y + 34
        self.rising = True
        self.dead = False
        self.anim_t = 0

    def update(self, game, dt_ms):
        self.anim_t += dt_ms
        if self.rising:
            self.y -= 0.8
            if self.y <= self.rise_from - self.h:
                self.rising = False
            return
        if self.kind == self.FLOWER:
            return  # 火焰花原地不动
        # 蘑菇/1UP 水平走; 星星弹跳
        self.x += self.vx
        if self.kind == self.STAR:
            self.vy = min(self.vy + GRAVITY * 0.7, MAX_FALL)
            self.y += self.vy
            r = self.rect()
            for s in game.solids_near(r):
                sr = s.rect()
                if intersects(r, sr):
                    if self.vy > 0 and r[1] + r[3] - self.vy <= sr[1] + 4:
                        self.y = sr[1] - self.h
                        self.vy = -6.5
                    elif self.vy < 0:
                        self.y = sr[1] + sr[3]
                        self.vy = 1.0
                    r = self.rect()
        else:
            self.vy = min(self.vy + GRAVITY, MAX_FALL)
            self.y += self.vy
            r = self.rect()
            solids = game.solids_near(r)
            for s in solids:
                if intersects(r, s.rect()):
                    sr = s.rect()
                    if self.vy > 0 and r[1] + r[3] - self.vy <= sr[1] + 4:
                        self.y = sr[1] - self.h
                        self.vy = 0
                    else:
                        if self.vx > 0:
                            self.x = sr[0] - self.w
                        else:
                            self.x = sr[0] + sr[2]
                        self.vx = -self.vx
                    r = self.rect()
        if self.y > game.world_h + 200:
            self.dead = True

    def rect(self):
        return (self.x, self.y, self.w, self.h)


class Fireball:
    def __init__(self, x, y, direction):
        self.x = x
        self.y = y
        self.w = 17
        self.h = 17
        self.vx = 6.4 if direction else -6.4
        self.vy = 2.0
        self.dead = False
        self.boom_t = 0
        self.anim_t = 0

    def update(self, game, dt_ms):
        self.anim_t += dt_ms
        if self.boom_t > 0:
            self.boom_t += dt_ms
            if self.boom_t > 150:
                self.dead = True
            return
        self.x += self.vx
        self.vy = min(self.vy + 0.4, 7.0)
        self.y += self.vy
        r = self.rect()
        solids = game.solids_near(r)
        for s in solids:
            sr = s.rect()
            if intersects(r, sr):
                if self.vy > 0 and r[1] + r[3] - self.vy <= sr[1] + 6:
                    self.y = sr[1] - self.h
                    self.vy = -5.0
                else:
                    self.boom_t = 1
                    return
                r = self.rect()
        if self.x < game.camera_x - 60 or self.x > game.camera_x + SCREEN_W + 60 \
                or self.y > game.world_h + 100:
            self.dead = True

    def rect(self):
        return (self.x, self.y, self.w, self.h)


class Enemy:
    GOOMBA = 0
    KOOPA = 1
    PIRANHA = 3
    BEETLE = 4
    SPINY = 5
    CHEEP = 6
    BLOOPER = 7
    BULLET = 9
    PODOBOO = 10
    HAMMERBRO = 11
    HAMMER = 12
    BOWSER = 13
    BFIRE = 14

    # 新类型尺寸 (screen px)
    SIZES = {
        4: (32, 44), 5: (32, 32), 6: (32, 32), 7: (32, 48), 9: (32, 32),
        10: (32, 32), 11: (32, 44), 12: (24, 24), 13: (64, 64), 14: (28, 28),
    }
    # 不可踩踏类型
    NOT_STOMPABLE = (3, 5, 7, 10, 12, 13, 14)

    def __init__(self, data):
        self.type = data.get('type', 0)
        self.x = data['x'] * SCALE_UNIT
        # 食人花等由 range_start/range_end 定位, 无 y 字段
        y_bottom = data.get('y', data.get('range_end', 538)) * SCALE_UNIT
        self.direction = data.get('direction', 0)
        self.vx = -ENEMY_SPEED if self.direction == 0 else ENEMY_SPEED
        self.vy = 0.0
        self.dead = False
        self.death_timer = 0
        self.death_mode = ''
        self.anim_t = 0
        self.shell = False
        self.sliding = False
        self.shell_timer = 0
        self.base_y = y_bottom
        self.home_x = self.x
        if self.type == self.KOOPA or self.type == self.BEETLE:
            self.w = 32
            self.h = 44
        elif self.type == self.PIRANHA:
            self.w = 32
            self.h = 48
            self.range_start = data.get('range_start', y_bottom - 80) * SCALE_UNIT
            self.range_end = data.get('range_end', y_bottom) * SCALE_UNIT
            self.y = self.range_end - self.h
            self.state = 'hidden'
            self.wait_t = 0
            return
        elif self.type in self.SIZES:
            self.w, self.h = self.SIZES[self.type]
            if self.type == self.PODOBOO:
                # 从熔岩/水下周期跃出
                self.y = y_bottom
                self.vy = -11.0
                self.wait_t = 0
                return
            if self.type in (self.BULLET, self.BFIRE):
                # 直线飞行, 无重力; direction=1 向左射向玩家
                self.vx = -4.6 if self.direction == 1 else 4.6
                self.y = y_bottom - self.h
                return
            if self.type in (self.CHEEP, self.BLOOPER):
                self.y = y_bottom - self.h
                return
            if self.type == self.BOWSER:
                self.vx = 0.0
                self.y = y_bottom - self.h
                return
        else:
            self.w = 32
            self.h = 32
        self.y = y_bottom - self.h

    def rect(self):
        return (self.x, self.y, self.w, self.h)

    def hurt_player(self):
        return not (self.dead or self.death_mode)

    def update(self, game, dt_ms):
        self.anim_t += dt_ms
        if self.death_mode == 'flat':
            self.death_timer += dt_ms
            if self.death_timer > 450:
                self.dead = True
            return
        if self.death_mode == 'flip':
            self.x += self.vx
            self.vy += 0.6
            self.y += self.vy
            if self.y > game.world_h + 100:
                self.dead = True
            return
        if self.type == self.PIRANHA:
            self.update_piranha(game, dt_ms)
            return
        if self.type == self.PODOBOO:
            # 周期跃出: 上升到顶点后落回, 等待再跳
            self.vy = min(self.vy + 0.35, 9.0)
            self.y += self.vy
            if self.vy > 0 and self.y >= self.base_y:
                self.y = self.base_y
                self.wait_t += dt_ms
                if self.wait_t > 1400:
                    self.vy = -11.0
                    self.wait_t = 0
            return
        if self.type in (self.BULLET, self.BFIRE):
            self.x += self.vx
            if self.x < game.camera_x - 80 or self.x > game.camera_x + SCREEN_W + 80:
                self.dead = True
            return
        if self.type == self.CHEEP:
            self.x += self.vx
            self.y = self.base_y - self.h + _sin_tbl[int(self.anim_t // 90) % 32] * 18
            if self.x < game.camera_x - 80 or self.x > game.camera_x + SCREEN_W + 80:
                self.dead = True
            return
        if self.type == self.BLOOPER:
            # 漂浮乌贼: 缓慢下落 + 周期横漂
            self.vy = min(self.vy + 0.12, 1.4)
            if int(self.anim_t // 900) % 2 == 0:
                self.x += 0.5
            else:
                self.x -= 0.5
            self.y += self.vy
            if self.y > game.world_h + 100:
                self.dead = True
            return
        if self.type == self.HAMMERBRO:
            # 走动 + 周期扔锤子
            self.x += self.vx
            r = self.rect()
            for s in game.solids_near(r):
                sr = s.rect()
                if intersects(r, sr):
                    self.vx = -self.vx
                    self.x += self.vx * 2
                    break
            self.vy = min(self.vy + GRAVITY, MAX_FALL)
            self.y += self.vy
            r = self.rect()
            for s in game.solids_near(r):
                sr = s.rect()
                if intersects(r, sr) and self.vy > 0:
                    self.y = sr[1] - self.h
                    self.vy = 0
            self.wait_t = getattr(self, 'wait_t', 0) + dt_ms
            if self.wait_t > 1900:
                self.wait_t = 0
                game.enemies.append(Enemy({
                    'type': self.HAMMER, 'x': (self.x + 4) / SCALE_UNIT,
                    'y': (self.y + 4) / SCALE_UNIT, 'direction': 1}))
                game.enemies[-1].vy = -6.5
                game.enemies[-1].vx = -2.6 if self.x > game.player.x else 2.6
            return
        if self.type == self.HAMMER:
            # 弧线锤子
            self.x += self.vx
            self.vy = min(self.vy + 0.3, 8.0)
            self.y += self.vy
            if self.y > game.world_h + 80:
                self.dead = True
            return
        # 重力
        self.vy = min(self.vy + GRAVITY, MAX_FALL)
        # 水平
        speed = 5.0 if self.sliding else ENEMY_SPEED
        self.vx = -speed if self.vx < 0 else speed
        self.x += self.vx
        r = self.rect()
        for s in game.solids_near(r):
            sr = s.rect()
            if intersects(r, sr):
                if self.vx > 0:
                    self.x = sr[0] - self.w
                else:
                    self.x = sr[0] + sr[2]
                self.vx = -self.vx
                if self.sliding:
                    self.bump_other_enemies(game)
                r = self.rect()
        # 垂直
        self.y += self.vy
        r = self.rect()
        landed = False
        for s in game.solids_near(r):
            sr = s.rect()
            if intersects(r, sr):
                if self.vy > 0:
                    self.y = sr[1] - self.h
                    self.vy = 0
                    landed = True
                else:
                    self.y = sr[1] + sr[3]
                    self.vy = 0
                r = self.rect()
        if not landed and self.vy != 0:
            pass
        if self.y > game.world_h + 100:
            self.dead = True
        if self.type == self.KOOPA and self.shell and not self.sliding:
            self.shell_timer += dt_ms
            if self.shell_timer > 5000:
                self.shell = False
                self.shell_timer = 0

    def bump_other_enemies(self, game):
        if not getattr(self, 'sliding', False):
            return
        r = self.rect()
        for e in game.enemies:
            if e is self or e.dead or e.death_mode:
                continue
            if intersects(r, e.rect()):
                e.death_mode = 'flip'
                e.vy = -6
                e.vx = self.vx
                game.score += 100

    def update_piranha(self, game, dt_ms):
        p = game.player
        near = abs((p.x + p.w / 2) - (self.x + self.w / 2)) < 90
        on_top = abs((p.x + p.w / 2) - (self.x + self.w / 2)) < 50 and p.bottom <= self.y + 20
        if self.state == 'hidden':
            self.wait_t += dt_ms
            if self.wait_t > 2500 and not near and not on_top:
                self.state = 'rising'
        elif self.state == 'rising':
            self.y -= 1.0
            if self.y <= self.range_start - self.h:
                self.y = self.range_start - self.h
                self.state = 'shown'
                self.wait_t = 0
        elif self.state == 'shown':
            self.wait_t += dt_ms
            if self.wait_t > 2000:
                self.state = 'hiding'
        elif self.state == 'hiding':
            self.y += 1.0
            if self.y >= self.range_end - self.h:
                self.y = self.range_end - self.h
                self.state = 'hidden'
                self.wait_t = 0


class Particle:
    def __init__(self, x, y, vx, vy):
        self.x = x
        self.y = y
        self.vx = vx
        self.vy = vy
        self.dead = False

    def update(self, dt_ms):
        self.x += self.vx
        self.vy += 0.5
        self.y += self.vy
        if self.y > 2000:
            self.dead = True


class MarioGame:
    def __init__(self, draw_api, load_api, touch_dev=None):
        """
        draw_api: 平台绘图对象（板端为 Display 包装，PC 为 pygame 包装）
        load_api: 平台资源加载器，提供 load_sprite(name) -> (img, mask)
                  load_bg(level, idx) -> img
        """
        self.draw_api = draw_api
        self.load_api = load_api
        self.tp = touch_dev
        self.state = TITLE
        self.level_num = 1
        self.score = 0
        self.coins = 0
        self.lives = 3
        self.time_left = 400
        self.time_acc = 0
        self.manifest = None
        self.sprites = {}
        self.masks = {}
        self.bg_chunks = {}
        self.bg_cache = {}
        self.bg_level_lru = []
        self.bg_complete_levels = set()
        self.bg_prefetch_queue = []
        self.bg_prefetch_level = None
        self.bg_all_loaded = False
        self.bg_loading = False
        self.bg_loading_done = 0
        self.bg_loading_total = 0
        self.bg_pending = None
        self.bg_prefetch_failed = set()
        self.bg_prefetch_max_ms = 0
        # Diagnostic counters: written to mario_perf.txt every three seconds.
        # They do not change the loading or rendering path.
        self.bg_sync_loads = 0
        self.bg_sync_max_ms = 0
        self.bg_prefetch_started = 0
        self.bg_prefetch_finished = 0
        self.level = None
        self.player = Player()
        self.camera_x = 0.0
        self.state_t = 0
        self.frame_count = 0
        self.fps_start = time.ticks_ms()
        self.fps = 0.0
        self.max_frame_ms = 0
        self.paused = False
        self.menu_blink = 0
        self.pause_menu_mode = 'main'
        self.pause_menu_index = 0
        self.selected_level = 1
        self.underwater = False
        self.transport_lock = False

    # ------------------------------------------------ 资源
    def load_manifest(self):
        with open(path_join(RESOURCE_DIR, 'manifest.json'), 'r') as f:
            self.manifest = json_mod.load(f)
        # 兜底初始化运行时容器（load_level 单独调用时也可用）
        self.enemies = getattr(self, 'enemies', [])
        self.particles = getattr(self, 'particles', [])
        self.popcoins = getattr(self, 'popcoins', [])
        self.powerups = getattr(self, 'powerups', [])
        self.fireballs = getattr(self, 'fireballs', [])
        self.active_groups = getattr(self, 'active_groups', set())

    def preload(self):
        self.load_manifest()
        names = list(self.manifest.get('sprites', {}).keys())
        if not names:
            names = [
            'small_stand', 'small_walk1', 'small_walk2', 'small_walk3',
            'small_jump', 'small_turn', 'small_die',
            'big_stand', 'big_walk1', 'big_walk2', 'big_walk3',
            'big_jump', 'big_turn',
            'fire_stand', 'fire_walk1', 'fire_walk2', 'fire_walk3',
            'fire_jump', 'fire_turn',
            'goomba1', 'goomba2', 'goomba_flat',
            'koopa1', 'koopa2', 'shell',
            'piranha1', 'piranha2',
            'brick', 'brick_used', 'debris',
            'box1', 'box2', 'box3', 'box_used',
            'mushroom', 'flower1', 'flower2', 'flower3', 'flower4',
            'coin1', 'coin2', 'coin3', 'coin4',
            'popcoin1', 'popcoin2', 'popcoin3', 'popcoin4',
            'fireball1', 'fireball2', 'fireball3', 'fireball4',
            'fireboom1', 'fireboom2', 'fireboom3',
            'flag', 'menu_logo',
        ]
        for n in names:
            img, mask = self.load_api.load_sprite(n)
            self.sprites[n] = img
            self.masks[n] = mask
            _has_left = False
            for _pfx in ('small', 'big', 'fire_stand', 'fire_walk', 'fire_jump', 'fire_turn',
                         'goomba', 'koopa', 'piranha', 'shell'):
                if n.startswith(_pfx):
                    _has_left = True
                    break
            if _has_left:
                img_l, mask_l = self.load_api.load_sprite(n + '_l')
                self.sprites[n + '_l'] = img_l
                self.masks[n + '_l'] = mask_l

    def load_level(self, num):
        self.level_num = num
        with open(path_join(RESOURCE_DIR, 'maps', 'level_%d.json' % num), 'r') as f:
            data = json_mod.load(f)
        # 兜底：运行时容器已初始化（start_level 会重置它们）
        lvl = {
            'image_name': data.get('image_name', 'level_%d' % num),
            'sliders': [],
            'bricks': [],
            'boxes': [],
            'coins': [],
            'enemies': [],
            'checkpoints': [],
            'flagpoles': [],
            'grounds': [],
            'pipes': [],
            'steps': [],
            'transports': [{
                'x': t['x'] * SCALE_UNIT, 'y': t['y'] * SCALE_UNIT,
                'w': t['w'] * SCALE_UNIT, 'h': t['h'] * SCALE_UNIT,
                'direction': t.get('direction', 'auto'),
                'to_level': t.get('to_level', 0),
                'to_x': t.get('to_x', 0) * SCALE_UNIT,
                'to_y': t.get('to_y', 0) * SCALE_UNIT,
            } for t in data.get('transports', [])],
        }
        self.underwater = bool(data.get('underwater'))
        self.theme = data.get('theme', 'overworld')
        # 出生点
        maps = data.get('maps')
        if not maps:
            # level_3 没有 maps 键
            maps = [{'start_x': 0, 'end_x': 7000, 'player_x': 110, 'player_y': 538}]
        elif isinstance(maps, dict):
            maps = [maps]
        m0 = maps[0]
        lvl['start_x'] = m0['start_x'] * SCALE_UNIT
        lvl['end_x'] = m0['end_x'] * SCALE_UNIT
        lvl['spawn_x'] = m0['player_x'] * SCALE_UNIT
        lvl['spawn_y'] = m0['player_y'] * SCALE_UNIT

        # Levels 1-4 encode pipe travel with legacy checkpoints instead of
        # the newer transports list. Convert those records at load time.
        legacy_checkpoints = data.get('checkpoint', ())
        raw_pipes = data.get('pipe', ())

        def legacy_destination(map_index):
            if map_index is None or map_index < 0 or map_index >= len(maps):
                return None
            target = maps[map_index]
            return ((target.get('start_x', 0) + target.get('player_x', 0)) * SCALE_UNIT,
                    target.get('player_y', 538) * SCALE_UNIT)

        for c in legacy_checkpoints:
            if c.get('type') != 6:
                continue
            destination = legacy_destination(c.get('map_index'))
            if destination is None:
                continue
            center = c['x'] + c.get('width', 0) * 0.5
            pipe = None
            for candidate in raw_pipes:
                if candidate['x'] <= center <= candidate['x'] + candidate['width']:
                    pipe = candidate
                    break
            if pipe is None:
                continue
            inset = min(6, pipe['width'] * 0.1)
            lvl['transports'].append({
                'x': (pipe['x'] + inset) * SCALE_UNIT,
                'y': pipe['y'] * SCALE_UNIT,
                'w': (pipe['width'] - inset * 2) * SCALE_UNIT,
                'h': 10 * SCALE_UNIT,
                'direction': 'down',
                'to_level': self.level_num,
                'to_x': destination[0], 'to_y': destination[1],
            })

        horizontal_starts = [c for c in legacy_checkpoints if c.get('type') == 4]
        for end in (c for c in legacy_checkpoints if c.get('type') == 5):
            destination = legacy_destination(end.get('map_index'))
            if destination is None:
                continue
            candidates = [c for c in horizontal_starts if c['x'] <= end['x']]
            if not candidates:
                continue
            start = max(candidates, key=lambda c: c['x'])
            mouth_x = start['x']
            for pipe in raw_pipes:
                if pipe['x'] <= start['x'] <= pipe['x'] + pipe['width']:
                    mouth_x = pipe['x']
                    break
            lvl['transports'].append({
                'x': mouth_x * SCALE_UNIT,
                'y': min(start['y'], end['y']) * SCALE_UNIT,
                'w': (end['x'] + end.get('width', 0) - mouth_x) * SCALE_UNIT,
                'h': max(start.get('height', 0), end.get('height', 0)) * SCALE_UNIT,
                'direction': 'right',
                'to_level': self.level_num,
                'to_x': destination[0], 'to_y': destination[1],
            })
        info = self.manifest['levels'].get(str(num))
        if info:
            lvl['world_w'] = info['width']
        else:
            lvl['world_w'] = lvl['end_x'] + 300
        lvl['world_h'] = SCREEN_H

        for g in data.get('ground', []):
            lvl['grounds'].append(Solid(g['x'] * SCALE_UNIT, g['y'] * SCALE_UNIT,
                                        g['width'] * SCALE_UNIT, g['height'] * SCALE_UNIT, 0))
        for p in data.get('pipe', []):
            lvl['grounds'].append(Solid(p['x'] * SCALE_UNIT, p['y'] * SCALE_UNIT,
                                        p['width'] * SCALE_UNIT, p['height'] * SCALE_UNIT, 0))
        for s in data.get('step', []):
            kind = 4 if s.get('spring') else 0
            lvl['grounds'].append(Solid(s['x'] * SCALE_UNIT, s['y'] * SCALE_UNIT,
                                        s['width'] * SCALE_UNIT, s['height'] * SCALE_UNIT,
                                        kind))
        for s in data.get('slider', []):
            lvl['sliders'].append(Slider(s))
        for b in data.get('brick', []):
            num = b.get('brick_num', 1)
            direction = b.get('direction', 0)
            for i in range(num):
                d = dict(b)
                if num > 1:
                    if direction == 0:
                        d['x'] = b['x'] + i * 16
                    else:
                        d['y'] = b['y'] + i * 16
                lvl['bricks'].append(Brick(d))
        # Consecutive ordinary bricks are rendered as one strip while they
        # remain untouched.  Dynamic bricks automatically fall back to the
        # individual path when bumped or used.
        lvl['brick_groups'] = self._build_brick_groups(lvl['bricks'])
        for b in data.get('box', []):
            lvl['boxes'].append(Box(b))
        for c in data.get('coin', []):
            lvl['coins'].append(Coin(c))
        for group in data.get('enemy', []):
            for gid, elist in group.items():
                enemy_list = []
                for e in elist:
                    enemy = Enemy(e)
                    lvl['enemies'].append(enemy)
                    enemy_list.append(enemy)
                lvl.setdefault('enemy_group_list', []).append((str(gid), enemy_list))
        for c in data.get('checkpoint', []):
            lvl['checkpoints'].append({
                'x': c['x'] * SCALE_UNIT,
                'y': c.get('y', 0) * SCALE_UNIT,
                'w': c.get('width', 10) * SCALE_UNIT,
                'h': c.get('height', 600) * SCALE_UNIT,
                'type': c.get('type', 0),
                'gid': str(c.get('enemy_groupid', '')),
                'used': False,
            })
        fp = data.get('flagpole')
        if fp:
            for f in fp:
                lvl['flagpoles'].append({
                    'x': f['x'] * SCALE_UNIT,
                    'y': f['y'] * SCALE_UNIT,
                    'type': f.get('type', 1),
                })
            lvl['goal_x'] = max(f['x'] for f in fp) * SCALE_UNIT
            lvl['flag_x'] = next((f['x'] * SCALE_UNIT for f in fp if f.get('type') == 2), None)
            lvl['flag_y'] = next((f['y'] * SCALE_UNIT for f in fp if f.get('type') == 2), None)
            lvl['flag_bottom'] = 485 * SCALE_UNIT
        elif data.get('goal_x') is not None:
            # 城堡关: 走到斧头/终点 x 直接过关
            lvl['goal_x'] = data['goal_x'] * SCALE_UNIT
        else:
            # level_4 无 flagpole：走到指定 x 直接过关
            lvl['goal_x'] = round(6809 * SCALE_UNIT)

        # 碰撞分桶
        buckets = {}
        for s in lvl['grounds']:
            self._bucket_add(buckets, s)
        for b in lvl['bricks']:
            self._bucket_add(buckets, Solid(b.x, b.y, b.w, b.h, 2, b))
        for b in lvl['boxes']:
            self._bucket_add(buckets, Solid(b.x, b.y, b.w, b.h, 3, b))
        lvl['buckets'] = buckets
        self.level = lvl
        self._select_bg_level(num)
        self.camera_x = 0.0
        self.time_left = 400
        self.time_acc = 0

    def _build_brick_groups(self, bricks):
        make_strip = getattr(self.load_api, 'make_brick_strip', None)
        sprite = self.sprites.get('brick')
        if make_strip is None or sprite is None:
            return []
        groups = []
        ordered = sorted((b for b in bricks if b.type == 0),
                         key=lambda b: (b.y, b.x))
        current = []
        last_y = None
        last_x = None
        for b in ordered:
            # Some converted levels use a 16px logical step (12.8 screen
            # pixels) while the rendered brick is 34px wide.  Accept both
            # that overlapping layout and the normal one-tile spacing.
            step_x = b.x - last_x if current else 0
            contiguous = (current and abs(b.y - last_y) < 1.0 and
                          10.0 <= step_x <= b.w + 1.0)
            if not contiguous:
                if len(current) >= 4:
                    groups.append(current)
                current = []
            current.append(b)
            last_y = b.y
            last_x = b.x
        if len(current) >= 4:
            groups.append(current)
        result = []
        for members in groups:
            try:
                strip = make_strip(sprite, members)
            except BaseException:
                strip = None
            if strip is None:
                continue
            group = {'members': members, 'image': strip,
                     'x': members[0].x, 'y': members[0].y,
                     'w': members[-1].x - members[0].x + members[-1].w,
                     'h': members[0].h,
                     'active': True}
            for b in members:
                b._brick_group = group
            result.append(group)
        return result

    def _bucket_add(self, buckets, s):
        x0 = int(s.x) // SOLID_BUCKET_W
        x1 = int(s.x + s.w) // SOLID_BUCKET_W
        for i in range(x0, x1 + 1):
            buckets.setdefault(i, []).append(s)

    def solids_near(self, rect, include_sliders=True):
        if not self.level:
            return []
        b0 = int(rect[0]) // SOLID_BUCKET_W
        b1 = int(rect[0] + rect[2]) // SOLID_BUCKET_W
        out = []
        seen = set()
        for i in range(b0 - 1, b1 + 2):
            for s in self.level['buckets'].get(i, ()):
                if id(s) not in seen:
                    seen.add(id(s))
                    out.append(s)
        if include_sliders:
            for sl in self.level['sliders']:
                if intersects(rect, sl.rect()):
                    out.append(Solid(sl.x, sl.y, sl.w, sl.h, 1, sl))
        return out

    @property
    def world_h(self):
        if self.level:
            return self.level.get('world_h', SCREEN_H)
        return SCREEN_H

    # ------------------------------------------------ 状态切换
    def new_game(self):
        self.score = 0
        self.coins = 0
        self.lives = 3
        self.state = PLAYING
        self.start_level(1)

    def open_pause_menu(self):
        self.state = PAUSED
        self.pause_menu_mode = 'main'
        self.pause_menu_index = 0
        self.selected_level = min(MAIN_LEVEL_COUNT, max(1, self.level_num))
        # Do not let the gameplay edge that opened the menu choose an item.
        self.input.jump_pressed = False
        self.input.action_pressed = False
        self.input.left_pressed = False
        self.input.right_pressed = False
        self.input.down_pressed = False

    def update_pause_menu(self):
        if self.pause_menu_mode == 'main':
            if self.input.left_pressed:
                self.pause_menu_index = (self.pause_menu_index - 1) % len(PAUSE_MENU_ITEMS)
            elif self.input.right_pressed or self.input.down_pressed:
                self.pause_menu_index = (self.pause_menu_index + 1) % len(PAUSE_MENU_ITEMS)

            if self.input.jump_pressed or self.input.action_pressed:
                if self.pause_menu_index == 0:
                    self.state = PLAYING
                elif self.pause_menu_index == 1:
                    self.start_level(self.level_num)
                else:
                    self.pause_menu_mode = 'level'
                    self.selected_level = min(MAIN_LEVEL_COUNT, max(1, self.level_num))
        else:
            if self.input.left_pressed:
                self.selected_level = ((self.selected_level - 2) % MAIN_LEVEL_COUNT) + 1
            elif self.input.right_pressed:
                self.selected_level = (self.selected_level % MAIN_LEVEL_COUNT) + 1
            elif self.input.down_pressed:
                self.selected_level = ((self.selected_level + 3) % MAIN_LEVEL_COUNT) + 1

            if self.input.jump_pressed:
                self.score = 0
                self.coins = 0
                self.lives = 3
                self.player.mode = 0
                self.start_level(self.selected_level)
            elif self.input.action_pressed:
                self.pause_menu_mode = 'main'

    def start_level(self, num, entry=None):
        self.load_level(num)
        self._select_bg_level(num)
        keep_mode = self.player.mode
        if entry is not None:
            self.player.reset(entry[0], entry[1])
            self.player.mode = keep_mode
        else:
            self.player.reset(self.level['spawn_x'], self.level['spawn_y'])
        self.transport_lock = True  # 出生/传送落点不立即触发
        self.active_groups = set()
        self.enemies = []
        self.particles = []
        self.popcoins = []
        self.powerups = []
        self.fireballs = []
        self.state = PLAYING
        self.update_camera()
        self.prepare_bg_visible()

    def check_transport(self):
        """检测玩家进入管道传送区"""
        p = self.player
        if p.dead or p.flag_slide:
            return
        pr = p.rect()
        near_transport = False
        for t in self.level.get('transports', ()):
            direction = t.get('direction', 'auto')
            active = True
            if direction == 'down':
                center_x = p.x + p.w * 0.5
                nearby = (t['x'] <= center_x <= t['x'] + t['w']
                          and abs(p.bottom - t['y']) <= 10)
                active = self.input.is_down('down')
            elif direction == 'right':
                vertical = p.y < t['y'] + t['h'] and p.bottom > t['y']
                nearby = (vertical and p.x < t['x'] + t['w']
                          and p.x + p.w >= t['x'] - 10)
                active = self.input.is_down('right')
            elif direction == 'left':
                vertical = p.y < t['y'] + t['h'] and p.bottom > t['y']
                nearby = (vertical and p.x + p.w > t['x']
                          and p.x <= t['x'] + t['w'] + 10)
                active = self.input.is_down('left')
            else:
                zr = (t['x'], t['y'] - p.h, t['w'], t['h'] + p.h * 2)
                nearby = intersects(pr, zr)
            if not nearby:
                continue
            near_transport = True
            if self.transport_lock:
                return
            if not active:
                continue
            to_level = t.get('to_level', 0)
            if to_level <= 0:
                return
            self.transport_lock = True
            if to_level == self.level_num:
                keep_mode = p.mode
                p.reset(t['to_x'], t['to_y'])
                p.mode = keep_mode
                p.set_bottom(t['to_y'])
            else:
                self.start_level(to_level, (t['to_x'], t['to_y']))
            return
        if not near_transport:
            self.transport_lock = False

    def activate_checkpoints(self):
        pr = self.player.rect()
        for c in self.level['checkpoints']:
            if c['used'] or c['type'] != 0:
                continue
            if intersects(pr, (c['x'], c['y'], c['w'], c['h'])):
                c['used'] = True
                gid = c['gid']
                if gid in self.active_groups:
                    continue
                self.active_groups.add(gid)
                # 按 gid 激活该组敌人（enemy_groupid 与 JSON 组键一致）
                for group in self.level.get('enemy_group_list', ()):
                    if group[0] == gid:
                        self.enemies.extend(group[1])
                        break

    # ------------------------------------------------ 物理更新
    def step_player(self, dt_ms):
        p = self.player
        if p.dead:
            p.death_timer += dt_ms
            p.vy = min(p.vy + 0.5, 9.0)
            p.y += p.vy
            return
        if p.flag_slide:
            self.update_flag_slide(dt_ms)
            return
        if p.walk_auto:
            p.vx = 2.4
            p.x += p.vx
            p.vy = min(p.vy + GRAVITY, MAX_FALL)
            p.y += p.vy
            self.collide_player_vertical()
            return

        left = self.input.is_down('left')
        right = self.input.is_down('right')
        down = self.input.is_down('down')
        run = self.input.is_down('b')

        # Holding down on the ground is the pipe/crouch action.
        if down and p.on_ground:
            left = False
            right = False

        accel = RUN_ACCEL if run else WALK_ACCEL
        max_speed = RUN_SPEED if run else WALK_SPEED
        if right and not left:
            if p.vx < -0.5:
                p.vx *= TURN_BRAKE
            p.vx = min(p.vx + accel, max_speed)
            p.face_right = True
        elif left and not right:
            if p.vx > 0.5:
                p.vx *= TURN_BRAKE
            p.vx = max(p.vx - accel, -max_speed)
            p.face_right = False
        else:
            p.vx *= TURN_BRAKE
            if abs(p.vx) < 0.2:
                p.vx = 0

        # 跳跃
        if self.underwater:
            # 水下: 低重力, 按 A 可连续游泳
            self.swim_cd = max(0, getattr(self, 'swim_cd', 0) - dt_ms)
            if self.input.jump_pressed and self.swim_cd == 0:
                p.vy = -4.6
                self.swim_cd = 320
                p.on_ground = False
                p.jump_held = False
        elif self.input.jump_pressed and p.on_ground:
            p.vy = JUMP_VY
            p.on_ground = False
            p.jump_held = True
        if not self.input.is_down('a'):
            p.jump_held = False

        # 水平移动与碰撞
        p.x += p.vx
        if p.x < self.level['start_x'] + p.w / 2:
            p.x = self.level['start_x'] + p.w / 2
            p.vx = 0
        r = p.rect()
        for s in self.solids_near(r):
            sr = s.rect()
            if intersects(r, sr):
                if p.vx > 0:
                    p.x = sr[0] - p.w
                elif p.vx < 0:
                    p.x = sr[0] + sr[2]
                p.vx = 0
                r = p.rect()

        # 重力与垂直碰撞
        if self.underwater:
            g = 0.3
        elif p.jump_held and p.vy < 0:
            g = JUMP_GRAVITY
        else:
            g = GRAVITY
        p.vy = min(p.vy + g, MAX_FALL if not self.underwater else 5.0)
        p.y += p.vy
        p.on_ground = False
        self.collide_player_vertical()

        # 掉出屏幕判死
        if p.y > SCREEN_H + 60:
            self.kill_player()
            return

        # 火球
        p.fire_cooldown = max(0, p.fire_cooldown - dt_ms)
        if p.fire and self.input.action_pressed and p.fire_cooldown == 0 \
                and len([f for f in self.fireballs if not f.dead]) < 2:
            fx = p.x + p.w if p.face_right else p.x - 17
            self.fireballs.append(Fireball(fx, p.y + p.h / 3, p.face_right))
            p.fire_cooldown = 350

        # 动画
        p.anim_timer += dt_ms
        if abs(p.vx) > 0.3:
            if p.anim_timer > 90:
                p.anim = (p.anim + 1) % 3
                p.anim_timer = 0
        else:
            p.anim = 0

    def collide_player_vertical(self):
        p = self.player
        r = p.rect()
        solids = self.solids_near(r)
        if not solids or p.vy == 0:
            return

        # Resolve only a surface crossed during this frame.  Resolving every
        # overlapping tile lets an adjacent brick steal a head-hit.
        previous_y = p.y - p.vy
        previous_top = previous_y
        previous_bottom = previous_y + p.h
        current_top = p.y
        current_bottom = p.y + p.h
        center_x = p.x + p.w * 0.5
        best = None
        best_score = None

        for s in solids:
            sr = s.rect()
            overlap = min(p.x + p.w, sr[0] + sr[2]) - max(p.x, sr[0])
            if overlap <= 0:
                continue

            if p.vy > 0:
                # Falling: select the first top surface crossed from above.
                crossed = previous_bottom <= sr[1] + 3 and current_bottom >= sr[1]
                if not crossed:
                    continue
                score = (sr[1], -overlap)
            else:
                # Rising: prefer the tile containing Mario's centre, then the
                # tile with the greatest horizontal overlap.
                crossed = previous_top >= sr[1] + sr[3] - 3 and current_top <= sr[1] + sr[3]
                if not crossed:
                    continue
                center_inside = sr[0] <= center_x < sr[0] + sr[2]
                distance = abs(center_x - (sr[0] + sr[2] * 0.5))
                score = (0 if center_inside else 1, -overlap, distance)

            if best is None or score < best_score:
                best = s
                best_score = score

        if best is None:
            return

        sr = best.rect()
        if p.vy > 0:
            p.y = sr[1] - p.h
            if best.kind == 4 and not self.underwater:
                p.vy = -13.0
                p.on_ground = False
            else:
                p.vy = 0
                p.on_ground = True
        else:
            p.y = sr[1] + sr[3]
            p.vy = 0.5
            self.hit_block_from_below(best)

    def collide_player_vertical_legacy(self):
        p = self.player
        r = p.rect()
        for s in self.solids_near(r):
            sr = s.rect()
            if intersects(r, sr):
                if p.vy > 0:
                    p.y = sr[1] - p.h
                    if s.kind == 4 and not self.underwater:
                        # 弹簧板
                        p.vy = -13.0
                        p.on_ground = False
                    else:
                        p.vy = 0
                        p.on_ground = True
                elif p.vy < 0:
                    p.y = sr[1] + sr[3]
                    p.vy = 0.5
                    self.hit_block_from_below(s)
                r = p.rect()

    def hit_block_from_below(self, s):
        p = self.player
        if s.kind == 2:
            b = s.obj
            if b.bump_t == 0:
                b.bump_t = 1
                if b.type == 1 and not b.used:
                    b.used = True
                    self.popcoins.append(PopCoin(b.x + 8, b.y - 26))
                    self.coins += 1
                    self.score += 100
                elif b.type == 2 and not b.used:
                    b.used = True
                    self.spawn_item(b.item, b.x, b.y - 34)
                elif b.type == 0 and p.big:
                    self.smash_brick(b)
        elif s.kind == 3:
            b = s.obj
            if b.bump_t == 0:
                b.bump_t = 1
                if not b.used:
                    b.used = True
                    if b.type == 1:
                        self.popcoins.append(PopCoin(b.x + 8, b.y - 26))
                        self.coins += 1
                        self.score += 100
                    elif b.type in (3, 4, 5, 6):
                        self.spawn_item(b.type, b.x, b.y - 34)

    def spawn_item(self, kind, x, y):
        if kind in (3, 5, 6):
            self.powerups.append(PowerUp(kind, x, y))
        elif kind == 4:
            self.powerups.append(PowerUp(4, x, y))

    def smash_brick(self, b):
        b.used = True
        for i in range(4):
            vx = -2.4 if i % 2 == 0 else 2.4
            vy = -7.0 if i < 2 else -4.0
            self.particles.append(Particle(b.x + 8, b.y + 8, vx, vy))
        self.score += 50
        # 从分桶移除
        for bucket in self.level['buckets'].values():
            for s in list(bucket):
                if s.obj is b:
                    bucket.remove(s)

    def update_sliders(self, dt_ms):
        p = self.player
        p_rect_before = p.rect()
        for sl in self.level['sliders']:
            old = sl.rect()
            sl.update()
            new = sl.rect()
            # 玩家站在平台上时随平台移动
            if p.on_ground:
                foot = (p.x, p.y + p.h - 2, p.w, 4)
                if intersects(foot, old):
                    dx = new[0] - old[0]
                    dy = new[1] - old[1]
                    p.x += dx
                    p.y += dy

    def step_enemies(self, dt_ms):
        p = self.player
        for e in self.enemies:
            if e.dead:
                continue
            # 只更新屏幕附近的敌人
            if e.x < self.camera_x - 120 or e.x > self.camera_x + SCREEN_W + 120:
                continue
            e.update(self, dt_ms)
            er = e.rect()
            if e.death_mode or p.dead:
                continue
            pr = p.rect()
            if not intersects(pr, er):
                continue
            # Piranha 任何接触都致命
            if e.type == Enemy.PIRANHA:
                if p.invulnerable <= 0:
                    self.hurt_player()
                continue
            if e.type in Enemy.NOT_STOMPABLE:
                # 尖刺/水母/火球/锤子/Bowser 等不可踩
                if p.invulnerable <= 0:
                    self.hurt_player()
                continue
            # 踩踏判定：用上一帧底部
            old_bottom = p.y + p.h - p.vy
            stomped = p.vy > 0 and old_bottom <= er[1] + 10
            if stomped:
                p.vy = STOMP_BOUNCE
                p.on_ground = False
                if e.type in (Enemy.KOOPA, Enemy.BEETLE):
                    if e.shell:
                        if e.sliding:
                            e.sliding = False
                            e.vx = 0
                            e.shell_timer = 0
                        else:
                            e.sliding = True
                            e.vx = 5.0 if p.x < e.x else -5.0
                            e.shell_timer = 0
                            self.score += 40
                    else:
                        e.shell = True
                        e.vx = 0
                        e.shell_timer = 0
                        self.score += 100
                else:
                    e.death_mode = 'flat'
                    e.death_timer = 0
                    self.score += 100
            else:
                if e.type in (Enemy.KOOPA, Enemy.BEETLE) and e.shell and not e.sliding:
                    # 踢龟壳
                    e.sliding = True
                    e.vx = 5.0 if p.x < e.x else -5.0
                    self.score += 40
                elif p.invulnerable <= 0:
                    self.hurt_player()

        # 龟壳撞其他敌人
        for e in self.enemies:
            if e.dead or e.type not in (Enemy.KOOPA, Enemy.BEETLE) or not e.sliding:
                continue
            e.bump_other_enemies(self)

        self.enemies = [e for e in self.enemies if not e.dead]

    def hurt_player(self):
        p = self.player
        if p.mode == 0:
            self.kill_player()
        else:
            p.shrink()

    def kill_player(self):
        p = self.player
        if p.dead:
            return
        p.dead = True
        p.vy = -8.0
        p.death_timer = 0
        self.state = DEAD
        self.state_t = 0

    def step_items(self, dt_ms):
        p = self.player
        for c in self.popcoins:
            c.update(dt_ms)
        self.popcoins = [c for c in self.popcoins if not c.dead]
        for pu in self.powerups:
            pu.update(self, dt_ms)
            if not pu.dead and intersects(p.rect(), pu.rect()):
                pu.dead = True
                if pu.kind == 3 and p.mode == 0:
                    p.grow(1)
                    self.score += 1000
                elif pu.kind == 4:
                    if p.mode == 0:
                        p.grow(1)
                    else:
                        p.grow(2)
                    self.score += 1000
                elif pu.kind == 5:
                    self.lives += 1
                    self.score += 1000
                elif pu.kind == 6:
                    p.invulnerable = max(p.invulnerable, 8000)
                    self.score += 1000
        self.powerups = [x for x in self.powerups if not x.dead]
        for fb in self.fireballs:
            fb.update(self, dt_ms)
            if not fb.dead and fb.boom_t == 0:
                fr = fb.rect()
                for e in self.enemies:
                    if e.dead or e.death_mode or e.type in (Enemy.PIRANHA, Enemy.BOWSER):
                        continue
                    if intersects(fr, e.rect()):
                        e.death_mode = 'flip'
                        e.vy = -6
                        e.vx = fb.vx / abs(fb.vx) if fb.vx else 1
                        fb.boom_t = 1
                        self.score += 200
                        break
        self.fireballs = [f for f in self.fireballs if not f.dead]
        for pt in self.particles:
            pt.update(dt_ms)
        self.particles = [x for x in self.particles if not x.dead]
        # 静态金币
        for c in self.level['coins']:
            if not c.taken and intersects(p.rect(), c.rect()):
                c.taken = True
                self.coins += 1
                self.score += 100
                if self.coins % 100 == 0:
                    self.lives += 1

    def update_flag_slide(self, dt_ms):
        p = self.player
        flag = self.level.get('flag_x')
        if flag is not None:
            p.x = flag - 10
        target = self.level.get('flag_bottom', 460)
        if p.bottom < target:
            p.y += 4.0
            if p.bottom > target:
                p.set_bottom(target)
            return
        # 到底后自动走向终点
        p.walk_auto = True
        p.flag_slide = False

    def check_goal(self):
        p = self.player
        if p.dead:
            return
        goal = self.level['goal_x']
        has_flag = self.level.get('flag_x') is not None
        if not has_flag:
            if p.x >= goal:
                self.finish_level()
        else:
            if not p.flag_slide and not p.walk_auto and p.x + p.w >= goal - 6:
                p.flag_slide = True
                p.vx = 0
                p.vy = 0
                self.state = LEVEL_CLEAR
                self.state_t = 0
                self.score += 1000

    def finish_level(self):
        self.state = LEVEL_CLEAR
        self.state_t = 0
        self.score += 1000

    def update_camera(self):
        target = self.player.x - SCREEN_W / 3
        max_cam = self.level['world_w'] - SCREEN_W
        self.camera_x = max(0.0, min(target, max_cam))

    def update_state(self, dt_ms):
        self.state_t += dt_ms
        if self.state == PLAYING:
            self.time_acc += dt_ms
            if self.time_acc >= 1000:
                self.time_acc -= 1000
                self.time_left -= 1
                if self.time_left <= 0:
                    self.time_left = 0
                    self.kill_player()
            _t0 = time.ticks_ms()
            self.activate_checkpoints()
            _t1 = time.ticks_ms()
            self.update_sliders(dt_ms)
            _t2 = time.ticks_ms()
            self.step_player(dt_ms)
            _t3 = time.ticks_ms()
            self.step_enemies(dt_ms)
            _t4 = time.ticks_ms()
            self.step_items(dt_ms)
            _t5 = time.ticks_ms()
            self.check_transport()
            self.check_goal()
            _t6 = time.ticks_ms()
            self.update_camera()
            self._prof = (time.ticks_diff(_t1, _t0), time.ticks_diff(_t2, _t1),
                          time.ticks_diff(_t3, _t2), time.ticks_diff(_t4, _t3),
                          time.ticks_diff(_t5, _t4), time.ticks_diff(_t6, _t5))
        elif self.state == DEAD:
            self.step_player(dt_ms)
            if self.state_t > 2500:
                self.lives -= 1
                if self.lives <= 0:
                    self.state = GAME_OVER
                    self.state_t = 0
                else:
                    self.start_level(self.level_num)
        elif self.state == LEVEL_CLEAR:
            if self.player.flag_slide:
                self.update_flag_slide(dt_ms)
            elif self.player.walk_auto:
                self.step_player(dt_ms)
            if self.state_t > 3200:
                lvl_info = self.manifest['levels'].get(str(self.level_num)) or {}
                nxt = lvl_info.get('next')
                if nxt is None:
                    nxt = self.level_num + 1
                if nxt <= 0 or nxt > 63:
                    self.state = COMPLETE
                    self.state_t = 0
                else:
                    self.start_level(nxt)
        elif self.state in (GAME_OVER, COMPLETE):
            if self.state_t > 4000:
                self.new_game()

    # ------------------------------------------------ 绘制
    @staticmethod
    def _close_image(img):
        close = getattr(img, 'close', None)
        if close is not None:
            try:
                close()
            except BaseException:
                pass

    def clear_bg_cache(self):
        if self.bg_pending is not None:
            cancel = getattr(self.load_api, 'cancel_bg_load', None)
            if cancel is not None:
                try:
                    cancel(self.bg_pending[2])
                except BaseException:
                    pass
            self.bg_pending = None
        seen = set()
        for img in self.bg_cache.values():
            if id(img) in seen:
                continue
            seen.add(id(img))
            self._close_image(img)
        self.bg_cache = {}
        self.bg_chunks = {}
        self.bg_level_lru = []
        self.bg_complete_levels = set()
        self.bg_prefetch_queue = []
        self.bg_prefetch_level = None
        self.bg_all_loaded = False
        self.bg_prefetch_failed = set()
        self.bg_prefetch_max_ms = 0
        self.bg_sync_loads = 0
        self.bg_sync_max_ms = 0
        self.bg_prefetch_started = 0
        self.bg_prefetch_finished = 0
        try:
            gc.collect()
        except BaseException:
            pass

    def _touch_bg_level(self, level):
        try:
            self.bg_level_lru.remove(level)
        except ValueError:
            pass
        self.bg_level_lru.append(level)

    def _trim_bg_levels(self):
        """Release the oldest complete level while retaining the active one."""
        cached_levels = set(key[0] for key in self.bg_cache)
        self.bg_level_lru = [level for level in self.bg_level_lru
                             if level in cached_levels]
        for level in cached_levels:
            if level not in self.bg_level_lru:
                self.bg_level_lru.append(level)
        while (len(cached_levels) > BG_KEEP_LEVELS or
               len(self.bg_cache) > BG_CACHE_MAX_CHUNKS):
            victim = None
            for level in self.bg_level_lru:
                if level != self.level_num and level in cached_levels:
                    victim = level
                    break
            if victim is None:
                break
            for key in list(self.bg_cache):
                if key[0] == victim:
                    self._close_image(self.bg_cache.pop(key))
            self.bg_complete_levels.discard(victim)
            cached_levels.discard(victim)
            try:
                self.bg_level_lru.remove(victim)
            except ValueError:
                pass
        try:
            gc.collect()
        except BaseException:
            pass

    def _select_bg_level(self, level):
        """Select the cached image dictionary for the active level."""
        self.bg_chunks = {}
        for (lev, index), img in self.bg_cache.items():
            if lev == level:
                self.bg_chunks[index] = img
        self.bg_all_loaded = level in self.bg_complete_levels

        self._touch_bg_level(level)
        self._trim_bg_levels()
        # Adjacent-level background work is intentionally disabled.  A level
        # is prepared before entering, so no decode competes with gameplay.
        self.bg_prefetch_queue = []
        if self.bg_pending is not None and self.bg_pending[0] != level:
            cancel = getattr(self.load_api, 'cancel_bg_load', None)
            if cancel is not None:
                try:
                    cancel(self.bg_pending[2])
                except BaseException:
                    pass
            self.bg_pending = None
        self.bg_prefetch_level = None

    def _queue_adjacent_bg_levels(self):
        # Background work never runs during gameplay.  The active level is
        # fully prepared by prepare_bg_visible() before state becomes PLAYING.
        self.bg_prefetch_queue = []

    def _bg_visible_span(self):
        if self.level is None:
            return None
        lvl = self.manifest['levels'].get(str(self.level_num))
        if not lvl:
            return None
        cw = self.manifest['chunk_width']
        c0 = int(self.camera_x) // cw
        c1 = int(self.camera_x + SCREEN_W - 1) // cw
        return lvl, cw, c0, c1

    def prepare_bg_visible(self):
        span = self._bg_visible_span()
        if span is None:
            return
        lvl, _cw, c0, c1 = span
        total = lvl['chunks']
        # A completed level is reused immediately.  This is the fast path for
        # death/restart and for returning to a recently visited level.
        if (self.level_num in self.bg_complete_levels and
                len(self.bg_chunks) >= total):
            self.bg_all_loaded = True
            self._touch_bg_level(self.level_num)
            return

        # Decode every chunk before entering PLAYING.  This removes the
        # gameplay-time background worker and prevents camera movement from
        # triggering a blocking read/decompression.
        self.bg_loading = True
        self.bg_loading_total = total
        done = len(self.bg_chunks)
        self.bg_loading_done = done
        self._show_bg_loading(self.level_num)
        try:
            for i in range(total):
                if i not in self.bg_chunks:
                    img = self.load_api.load_bg(self.level_num, i)
                    self.bg_chunks[i] = img
                    self.bg_cache[(self.level_num, i)] = img
                    done += 1
                self.bg_loading_done = done
                self._show_bg_loading(self.level_num)
        finally:
            self.bg_loading = False
        self.bg_complete_levels.add(self.level_num)
        self.bg_all_loaded = True
        self._touch_bg_level(self.level_num)
        self._trim_bg_levels()
        self._queue_adjacent_bg_levels()

    def _show_bg_loading(self, level):
        """Refresh a small loading screen while synchronous level data loads."""
        d = self.draw_api
        d.fill((0, 0, 0))
        total = max(1, self.bg_loading_total)
        done = min(total, self.bg_loading_done)
        d.text(300, 190, '正在加载关卡 %d' % level, (255, 255, 255))
        d.text(320, 230, '背景 %d/%d' % (done, total), (255, 220, 40))
        d.fill_rect(200, 270, 400, 16, (60, 60, 60))
        d.fill_rect(200, 270, 400 * done // total, 16, (40, 190, 80))
        present = getattr(d, 'present', None)
        if present is not None:
            present()

    def _evict_bg_cache(self, c0, c1):
        if self.bg_all_loaded:
            return
        low = c0 - BG_CACHE_BEHIND
        high = c1 + BG_PREFETCH_AHEAD
        for i in list(self.bg_chunks):
            if i < low or i > high:
                self._close_image(self.bg_chunks.pop(i))
                self.bg_cache.pop((self.level_num, i), None)

    def prefetch_bg(self):
        begin = getattr(self.load_api, 'begin_bg_load', None)
        step = getattr(self.load_api, 'step_bg_load', None)
        if begin is None or step is None:
            return False

        if self.bg_pending is not None:
            level, index, task = self.bg_pending
            t0 = time.ticks_ms()
            try:
                img = step(task, BG_STREAM_BYTES)
            except BaseException:
                cancel = getattr(self.load_api, 'cancel_bg_load', None)
                if cancel is not None:
                    try:
                        cancel(task)
                    except BaseException:
                        pass
                self.bg_prefetch_failed.add((level, index))
                self.bg_pending = None
                return False
            elapsed = time.ticks_diff(time.ticks_ms(), t0)
            if elapsed > self.bg_prefetch_max_ms:
                self.bg_prefetch_max_ms = elapsed
            if img is not None:
                self.bg_cache[(level, index)] = img
                if level == self.level_num:
                    self.bg_chunks[index] = img
                info = self.manifest.get('levels', {}).get(str(level)) or {}
                if len([k for k in self.bg_cache if k[0] == level]) >= info.get('chunks', 0):
                    self.bg_complete_levels.add(level)
                self.bg_pending = None
                self.bg_prefetch_finished += 1
            return True

        span = self._bg_visible_span()
        if span is None:
            return False
        lvl, _cw, c0, c1 = span
        self._evict_bg_cache(c0, c1)

        target_level = self.level_num
        if self.bg_all_loaded:
            while self.bg_prefetch_queue and self.bg_prefetch_queue[0] in self.bg_complete_levels:
                self.bg_prefetch_queue.pop(0)
            if self.bg_prefetch_queue:
                target_level = self.bg_prefetch_queue[0]
                info = self.manifest.get('levels', {}).get(str(target_level)) or {}
                target_chunks = info.get('chunks', 0)
                if target_chunks <= 0:
                    self.bg_complete_levels.add(target_level)
                    self.bg_prefetch_queue.pop(0)
                    return False
                if target_level in self.bg_complete_levels:
                    self.bg_prefetch_queue.pop(0)
                    return False
                candidates = range(0, target_chunks)
            else:
                return False
        else:
            candidates = list(range(c1 + 1, c1 + BG_PREFETCH_AHEAD + 1))
            if c0 > 0:
                candidates.append(c0 - 1)

        for index in candidates:
            if target_level == self.level_num:
                chunks = self.bg_chunks
                chunk_count = lvl['chunks']
            else:
                chunks = {i: img for (lev, i), img in self.bg_cache.items()
                          if lev == target_level}
                info = self.manifest.get('levels', {}).get(str(target_level)) or {}
                chunk_count = info.get('chunks', 0)
            if (index < 0 or index >= chunk_count or index in chunks
                    or (target_level, index) in self.bg_prefetch_failed):
                continue
            task = begin(target_level, index)
            if task is not None:
                self.bg_pending = (target_level, index, task)
                self.bg_prefetch_level = target_level
                self.bg_prefetch_started += 1
                return True
            self.bg_prefetch_failed.add((target_level, index))
        if target_level != self.level_num:
            self.bg_complete_levels.add(target_level)
            if self.bg_prefetch_queue and self.bg_prefetch_queue[0] == target_level:
                self.bg_prefetch_queue.pop(0)
        return False

    def draw_bg(self):
        span = self._bg_visible_span()
        if span is None:
            return
        lvl, cw, c0, c1 = span
        for i in range(c0, c1 + 1):
            if i < 0 or i >= lvl['chunks']:
                continue
            img = self.bg_chunks.get(i)
            if img is None:
                if (self.bg_pending is not None and
                        self.bg_pending[0] == self.level_num and
                        self.bg_pending[1] == i):
                    cancel = getattr(self.load_api, 'cancel_bg_load', None)
                    if cancel is not None:
                        cancel(self.bg_pending[2])
                    self.bg_pending = None
                _bg_sync_start = time.ticks_ms()
                img = self.load_api.load_bg(self.level_num, i)
                _bg_sync_ms = time.ticks_diff(time.ticks_ms(), _bg_sync_start)
                self.bg_sync_loads += 1
                if _bg_sync_ms > self.bg_sync_max_ms:
                    self.bg_sync_max_ms = _bg_sync_ms
                self.bg_chunks[i] = img
                self.bg_cache[(self.level_num, i)] = img
            x = i * cw - int(self.camera_x)
            self.draw_api.draw_image(img, x, 0)

    def sprite_for_player(self):
        p = self.player
        suffix = '' if p.face_right else '_l'
        if p.dead:
            return self.sprites['small_die']
        if p.mode == 0:
            base = 'small'
        elif p.mode == 1:
            base = 'big'
        else:
            base = 'fire'
        if p.flag_slide:
            return self.sprites[base + '_stand' + suffix]
        if not p.on_ground:
            return self.sprites[base + '_jump' + suffix]
        if abs(p.vx) > 0.3 or p.walk_auto:
            return self.sprites[base + '_walk%d%s' % (p.anim + 1, suffix)]
        return self.sprites[base + '_stand' + suffix]

    def draw(self):
        d = self.draw_api
        d.fill((0, 0, 0))
        if self.state == TITLE:
            self.draw_title()
            return
        self.draw_bg()
        cx = int(self.camera_x)
        lvl = self.level

        def vis(r):
            return r[0] + r[2] > cx - 40 and r[0] < cx + SCREEN_W + 40

        # 旗杆只画一次；JSON 中多个 type=1 是原版的杆身分段。
        pole = [f for f in lvl['flagpoles'] if f['type'] == 1]
        if pole:
            x = pole[0]['x'] - cx
            if -40 <= x <= SCREEN_W + 40:
                top = min(f['y'] for f in pole)
                bottom = max(f['y'] for f in pole) + 32
                d.fill_rect(x + 14, top, 6, bottom - top, (0, 180, 0))
        # 旗子
        if lvl.get('flag_x') is not None:
            fy = lvl.get('flag_y', 97)
            if self.player.flag_slide or self.player.walk_auto or self.state == LEVEL_CLEAR:
                fy = max(fy, lvl.get('flag_bottom', 460) - 90)
            d.draw_image(self.sprites['flag'], lvl['flag_x'] - cx, fy + 10,
                         mask=self.masks['flag'])
        # 静态平台类不绘制（背景已含视觉），只画砖块/箱子
        for group in lvl.get('brick_groups', ()):
            active = True
            for b in group['members']:
                if b.used or b.bump_t > 0:
                    active = False
                    break
            group['active'] = active
            if active and vis((group['x'], group['y'], group['w'], group['h'])):
                d.draw_image(group['image'], group['x'] - cx, group['y'])
        for b in lvl['bricks']:
            if b.used:
                continue
            group = getattr(b, '_brick_group', None)
            if group is not None and group.get('active'):
                continue
            if not vis(b.rect()):
                continue
            name = 'brick' if b.type == 0 else 'brick_used'
            if b.bump_t > 0:
                d.draw_image(self.sprites['brick'], b.x - cx, b.y - 6, mask=self.masks['brick'])
            else:
                d.draw_image(self.sprites[name], b.x - cx, b.y, mask=self.masks[name])
        for b in lvl['boxes']:
            if not vis(b.rect()):
                continue
            if b.bump_t > 0:
                img = self.sprites['box_used']
                d.draw_image(img, b.x - cx, b.y - 6, mask=self.masks['box_used'])
            elif b.used:
                d.draw_image(self.sprites['box_used'], b.x - cx, b.y, mask=self.masks['box_used'])
            else:
                t = (b.anim_t // 120) % 4
                name = ('box1', 'box2', 'box3', 'box2')[t]
                d.draw_image(self.sprites[name], b.x - cx, b.y, mask=self.masks[name])
        for c in lvl['coins']:
            if c.taken or not vis(c.rect()):
                continue
            t = (c.anim_t // 180) % 4
            d.draw_image(self.sprites['coin%d' % (t + 1)], c.x - cx, c.y,
                         mask=self.masks['coin%d' % (t + 1)])
        for sl in lvl['sliders']:
            r = sl.rect()
            if not vis(r):
                continue
            d.fill_rect(r[0] - cx, r[1], r[2], r[3], (0, 160, 60))
        for pu in self.powerups:
            if not vis(pu.rect()):
                continue
            if pu.kind == 3:
                d.draw_image(self.sprites['mushroom'], pu.x - cx, pu.y,
                             mask=self.masks['mushroom'])
            elif pu.kind == 5:
                d.draw_image(self.sprites['mushroom_1up'], pu.x - cx, pu.y,
                             mask=self.masks['mushroom_1up'])
            elif pu.kind == 6:
                t = (pu.anim_t // 100) % 4
                name = 'star%d' % (t + 1)
                d.draw_image(self.sprites[name], pu.x - cx, pu.y,
                             mask=self.masks[name])
            else:
                t = (pu.anim_t // 120) % 4
                d.draw_image(self.sprites['flower%d' % (t + 1)], pu.x - cx, pu.y,
                             mask=self.masks['flower%d' % (t + 1)])
        for c in self.popcoins:
            t = int(c.vy * 100) % 4
            d.draw_image(self.sprites['popcoin%d' % (t + 1)], c.x - cx, c.y,
                         mask=self.masks['popcoin%d' % (t + 1)])
        for e in self.enemies:
            if not vis(e.rect()):
                continue
            self.draw_enemy(e, cx)
        for fb in self.fireballs:
            if fb.boom_t > 0:
                t = min(2, fb.boom_t // 50)
                d.draw_image(self.sprites['fireboom%d' % (t + 1)], fb.x - cx, fb.y,
                             mask=self.masks['fireboom%d' % (t + 1)])
            else:
                t = (fb.anim_t // 80) % 4
                d.draw_image(self.sprites['fireball%d' % (t + 1)], fb.x - cx, fb.y,
                             mask=self.masks['fireball%d' % (t + 1)])
        for pt in self.particles:
            d.fill_rect(pt.x - cx, pt.y, 8, 8, (200, 90, 20))
        # 玩家
        if self.state != TITLE:
            spr = self.sprite_for_player()
            blink = self.player.invulnerable > 0 and (self.player.invulnerable // 100) % 2 == 0
            if not blink:
                d.draw_image(spr, self.player.x - cx, self.player.y,
                             mask=self.masks[self._sprite_name(spr)])
        self.draw_hud()
        self.draw_touch_buttons()
        if self.state == PAUSED:
            self.draw_pause_menu()
        elif self.state == DEAD and not self.player.dead:
            pass
        elif self.state == GAME_OVER:
            self.draw_center_text('游戏结束')
        elif self.state == COMPLETE:
            self.draw_center_text('恭喜全部通关')
        elif self.state == LEVEL_CLEAR:
            self.draw_center_text('顺利过关')

    def _sprite_name(self, spr):
        # 反查精灵名（绘制 mask 用）
        for n, img in self.sprites.items():
            if img is spr:
                return n
        return 'small_stand'

    def draw_enemy(self, e, cx):
        d = self.draw_api
        x = e.x - cx
        y = e.y
        right = self.player and e.vx > 0
        suffix = '' if (e.direction == 0) != bool(e.vx > 0) or True else '_l'
        # 敌人朝向：vx<0 用左向帧（_l），vx>0 用右向帧
        if e.type == Enemy.GOOMBA:
            if e.death_mode == 'flat':
                d.draw_image(self.sprites['goomba_flat'], x, y, mask=self.masks['goomba_flat'])
            else:
                t = (e.anim_t // 150) % 2
                name = 'goomba%d' % (t + 1)
                d.draw_image(self.sprites[name], x, y, mask=self.masks[name])
        elif e.type == Enemy.KOOPA:
            if e.shell or e.sliding:
                name = 'shell'
            else:
                t = (e.anim_t // 150) % 2
                name = 'koopa%d' % (t + 1)
            if e.vx < 0:
                left_name = name + '_l'
                if left_name in self.sprites:
                    name = left_name
            d.draw_image(self.sprites[name], x, y, mask=self.masks[name])
        elif e.type == Enemy.PIRANHA:
            t = (e.anim_t // 250) % 2
            name = 'piranha%d' % (t + 1)
            d.draw_image(self.sprites[name], x, y, mask=self.masks[name])
        elif e.type == Enemy.BEETLE:
            if e.shell or e.sliding:
                name = 'shell'
            else:
                t = (e.anim_t // 150) % 2
                name = 'koopa%d' % (t + 1)
            if e.vx < 0:
                left_name = name + '_l'
                if left_name in self.sprites:
                    name = left_name
            d.draw_image(self.sprites[name], x, y, mask=self.masks[name])
        elif e.type == Enemy.SPINY:
            t = (e.anim_t // 150) % 2
            name = 'spiny%d' % (t + 1)
            d.draw_image(self.sprites[name], x, y, mask=self.masks[name])
        elif e.type == Enemy.BULLET:
            d.draw_image(self.sprites['bullet'], x, y, mask=self.masks['bullet'])
        elif e.type == Enemy.CHEEP:
            t = (e.anim_t // 150) % 2
            name = 'cheep%d' % (t + 1)
            d.draw_image(self.sprites[name], x, y, mask=self.masks[name])
        elif e.type == Enemy.BLOOPER:
            t = (e.anim_t // 200) % 2
            name = 'blooper%d' % (t + 1)
            d.draw_image(self.sprites[name], x, y, mask=self.masks[name])
        elif e.type == Enemy.PODOBOO:
            d.draw_image(self.sprites['podoboo'], x, y, mask=self.masks['podoboo'])
        elif e.type == Enemy.HAMMERBRO:
            t = (e.anim_t // 180) % 2
            name = 'hammerbro%d' % (t + 1)
            d.draw_image(self.sprites[name], x, y, mask=self.masks[name])
        elif e.type == Enemy.HAMMER:
            t = (e.anim_t // 100) % 2
            name = 'hammer' if t == 0 else 'spiny_egg'
            d.draw_image(self.sprites[name], x, y, mask=self.masks[name])
        elif e.type == Enemy.BOWSER:
            t = (e.anim_t // 220) % 2
            name = 'bowser%d' % (t + 1)
            d.draw_image(self.sprites[name], x, y, mask=self.masks[name])
        elif e.type == Enemy.BFIRE:
            d.draw_image(self.sprites['bfire'], x, y, mask=self.masks['bfire'])

    def draw_hud(self):
        d = self.draw_api
        d.fill_rect(0, 0, SCREEN_W, HUD_H, (0, 0, 0))
        d.text(12, 8, '马里奥', (255, 255, 255))
        d.text(12, 24, '%06d' % self.score, (255, 255, 255))
        d.text(200, 8, '金币 x%02d' % self.coins, (255, 255, 255))
        info = self.manifest['levels'].get(str(self.level_num)) or {}
        lbl = info.get('label') or ('1-%d' % self.level_num)
        if self.level_num > MAIN_LEVEL_COUNT and lbl[-1:] in 'BCDE':
            lbl = lbl[:-1]
        d.text(380, 8, '世界 %s' % lbl, (255, 255, 255))
        d.text(560, 8, '时间 %d' % self.time_left, (255, 255, 255))
        d.text(700, 8, '生命 x%d' % self.lives, (255, 255, 255))

        # Cache status is compact and allocation-light because this runs every frame.
        current_total = info.get('chunks', 0)
        current_done = len(self.bg_chunks)
        if self.bg_loading:
            status = '加载 %d/%d' % (self.bg_loading_done,
                                     max(1, self.bg_loading_total))
            status_color = (255, 220, 40)
        elif current_total and current_done < current_total:
            status = '缓存 %d/%d' % (current_done, current_total)
            status_color = (255, 220, 40)
        else:
            target_level = None
            if (self.bg_pending is not None and
                    self.bg_pending[0] != self.level_num):
                target_level = self.bg_pending[0]
            elif self.bg_prefetch_queue:
                target_level = self.bg_prefetch_queue[0]
            if target_level is not None:
                target_info = self.manifest['levels'].get(str(target_level)) or {}
                target_total = target_info.get('chunks', 0)
                target_done = 0
                for key in self.bg_cache:
                    if key[0] == target_level:
                        target_done += 1
                status = '后台%d %d/%d' % (target_level, target_done,
                                           max(1, target_total))
                status_color = (100, 220, 255)
            elif current_total:
                status = '缓存完成'
                status_color = (100, 220, 120)
            else:
                status = ''
                status_color = (255, 255, 255)
        if status:
            d.text(360, 24, status, status_color)

    def draw_touch_buttons(self):
        d = self.draw_api
        for name, cx0, cy0, r in TOUCH_BUTTONS:
            active = self.input.is_down(name)
            color = (255, 90, 60) if active else (60, 60, 60)
            d.draw_circle(cx0, cy0, r, color, fill=False)
            label = {'left': '<', 'right': '>', 'down': 'v',
                     'b': 'B', 'a': 'A'}[name]
            d.text(cx0 - 6, cy0 - 10, label, (255, 255, 255))
        d.text(PAUSE_AREA[0] + 10, PAUSE_AREA[1], 'II', (255, 255, 255))

    def draw_pause_menu(self):
        d = self.draw_api
        d.fill_rect(220, 90, 360, 270, (0, 0, 0))
        if self.pause_menu_mode == 'level':
            world = (self.selected_level - 1) // 4 + 1
            stage = (self.selected_level - 1) % 4 + 1
            d.text(368, 125, '选择关卡', (255, 255, 255))
            d.text(340, 215, '<  世界 %d-%d  >' % (world, stage),
                   (255, 220, 40))
            return

        d.text(368, 115, '游戏暂停', (255, 255, 255))
        for index, label in enumerate(PAUSE_MENU_ITEMS):
            selected = index == self.pause_menu_index
            prefix = '> ' if selected else '  '
            color = (255, 220, 40) if selected else (255, 255, 255)
            d.text(300, 175 + index * 48, prefix + label, color)

    def draw_title(self):
        d = self.draw_api
        logo = self.sprites.get('menu_logo')
        if logo is not None:
            d.draw_image(logo, (SCREEN_W - logo.width()) // 2, 60)
        self.menu_blink += 1
        if (self.menu_blink // 30) % 2 == 0:
            d.text(352, 350, '触摸屏幕开始', (255, 255, 255))
            d.text(344, 382, '按 A 或 B 开始', (255, 255, 255))

    def draw_center_text(self, s):
        self.draw_api.text(SCREEN_W // 2 - len(s) * 9, 200, s, (255, 255, 255))

    # ------------------------------------------------ 主循环
    def run(self):
        last = time.ticks_ms()
        self.fps_start = last
        acc = 0
        t_draw = 0
        t_upd = 0
        t_show = 0
        t_input = 0
        while True:
            os.exitpoint()
            frame_start = time.ticks_ms()
            dt = time.ticks_diff(frame_start, last)
            last = frame_start
            acc += min(dt, 100)
            steps = 0
            if self.frame_count % TOUCH_POLL_FRAMES == 0:
                _i0 = time.ticks_ms()
                self.input.poll()
                t_input += time.ticks_diff(time.ticks_ms(), _i0)
            if self.input.pause_pressed:
                if self.state == PLAYING:
                    self.open_pause_menu()
                elif self.state == PAUSED:
                    self.state = PLAYING
                self.input.pause_pressed = False
            _u0 = time.ticks_ms()
            while acc >= SIM_MS and steps < MAX_CATCHUP:
                if self.state == TITLE:
                    if (self.input.touch_pressed or self.input.jump_pressed
                            or self.input.action_pressed):
                        self.new_game()
                elif self.state == PAUSED:
                    self.update_pause_menu()
                else:
                    for b in self.level['bricks']:
                        if b.bump_t > 0:
                            b.bump_t += SIM_MS
                            if b.bump_t > 160:
                                b.bump_t = 0
                    for b in self.level['boxes']:
                        b.anim_t += SIM_MS
                        if b.bump_t > 0:
                            b.bump_t += SIM_MS
                            if b.bump_t > 160:
                                b.bump_t = 0
                    self.update_state(SIM_MS)
                self.input.consume_actions()
                acc -= SIM_MS
                steps += 1
            if steps == MAX_CATCHUP and acc >= SIM_MS:
                # 慢帧后丢弃过期积压，避免每帧永久执行五次触摸和物理更新。
                acc %= SIM_MS
            t_upd += time.ticks_diff(time.ticks_ms(), _u0)
            if self.player.invulnerable > 0:
                self.player.invulnerable -= dt
            _d0 = time.ticks_ms()
            self.draw()
            _s0 = time.ticks_ms()
            self.draw_api.present()
            t_show += time.ticks_diff(time.ticks_ms(), _s0)
            t_draw += time.ticks_diff(_s0, _d0)
            self.prefetch_bg()
            # 性能统计
            self.frame_count += 1
            fm = time.ticks_diff(time.ticks_ms(), frame_start)
            if fm > self.max_frame_ms:
                self.max_frame_ms = fm
            if PERF_LOG_ENABLED and self.frame_count % 150 == 0:
                now = time.ticks_ms()
                self.fps = 1000.0 * 150 / max(1, time.ticks_diff(now, self.fps_start))
                self.fps_start = now
                n = max(1, self.frame_count)
                pr = getattr(self, '_prof', (0, 0, 0, 0, 0, 0))
                write_marker('mario_perf.txt',
                            'fps=%.1f max_frame_ms=%d input=%d upd=%d draw=%d show=%d bgmax=%d sync=%d/%d pf=%d/%d pend=%d cache=%d cam=%d px=%d state=%d cp=%d sld=%d pl=%d en=%d it=%d\n'
                            % (self.fps, self.max_frame_ms, t_input // n, t_upd // n,
                                t_draw // n, t_show // n,
                                self.bg_prefetch_max_ms,
                                self.bg_sync_loads, self.bg_sync_max_ms,
                                self.bg_prefetch_started, self.bg_prefetch_finished,
                                1 if self.bg_pending is not None else 0, len(self.bg_chunks),
                                int(self.camera_x), int(self.player.x), self.state,
                                pr[0], pr[1], pr[2], pr[3], pr[4]))
            if self.frame_count == 30:
                write_marker('mario_boot_ok.txt',
                             'level=%d state=%s\n' %
                             (self.level_num,
                              'title' if self.state == TITLE else 'playing'))
            elapsed = time.ticks_diff(time.ticks_ms(), frame_start)
            if elapsed < FRAME_MS:
                time.sleep_ms(FRAME_MS - elapsed)

# ==== SUPER_MARIO_BODY_END ====


def main(auto_start=True):
    write_marker('mario_main_started.txt', 'started\n')
    write_marker('mario_boot_ok.txt', '')
    write_marker('mario_error.txt', '')
    write_marker('mario_perf.txt', '')
    try:
        os.exitpoint(os.EXITPOINT_ENABLE)
        Display.init(Display.ST7701, width=SCREEN_W, height=SCREEN_H, to_ide=False)
        MediaManager.init()
        touch_mode = 'direct i2c3 burst'
        try:
            tp = FastFT5316Touch(3)
        except BaseException as touch_error:
            touch_mode = 'native fallback: %s' % touch_error
            tp = TOUCH(0)

        class DisplayAPI(object):
            def draw_image(self, img, x, y, mask=None):
                if isinstance(mask, SpriteMask):
                    if mask.opaque:
                        canvas.draw_image(img, int(x), int(y))
                        return
                    for x0, width, y0, height in mask.rects:
                        canvas.draw_image(img, int(x) + x0, int(y) + y0,
                                          roi=(x0, y0, width, height))
                else:
                    canvas.draw_image(img, int(x), int(y))

            def fill(self, color):
                canvas.draw_rectangle(0, 0, SCREEN_W, SCREEN_H, color=color, fill=True)

            def fill_rect(self, x, y, w, h, color):
                canvas.draw_rectangle(int(x), int(y), int(w), int(h), color=color, fill=True)

            def draw_circle(self, x, y, r, color, fill=False):
                canvas.draw_circle(int(x), int(y), int(r), color=color, fill=fill)

            def text(self, x, y, s, color):
                canvas.draw_string_advanced(int(x), int(y), 16, s, color=color)

            def present(self):
                Display.show_image(canvas)

        class LoadAPI(object):
            def __init__(self):
                try:
                    self.deflate = __import__('deflate')
                except BaseException:
                    self.deflate = None

            @staticmethod
            def load_sprite(name):
                p = path_join(RESOURCE_DIR, 'sprites/' + name + '.png')
                img = image.Image(p).to_rgb565()
                mp = path_join(RESOURCE_DIR, 'masks/' + name + '.png')
                mask = SpriteMask(image.Image(mp).to_rgb565().to_bitmap())
                return img, mask

            @staticmethod
            def make_brick_strip(brick_img, members):
                # Brick tiles are fully opaque, so a strip can be assembled
                # without masks and drawn with one image operation.
                base_x = members[0].x
                width = int(members[-1].x - base_x + brick_img.width())
                strip = image.Image(width, brick_img.height(), image.RGB565)
                for member in members:
                    strip.draw_image(brick_img, int(member.x - base_x), 0)
                return strip

            def begin_bg_load(self, level, idx):
                if self.deflate is None:
                    return None
                p = path_join(RESOURCE_DIR, 'bg/level%d_%02d.rgb565z' % (level, idx))
                if not path_exists(p):
                    return None
                f = None
                img = None
                try:
                    f = open(p, 'rb')
                    header = f.read(4)
                    if len(header) != 4:
                        raise OSError('invalid fast background header')
                    width, height = struct.unpack('<HH', header)
                    img = image.Image(width, height, image.RGB565)
                    stream = self.deflate.DeflateIO(f, self.deflate.ZLIB)
                    return [f, stream, img, img.bytearray(), 0, width * height * 2]
                except BaseException:
                    if img is not None:
                        try:
                            img.close()
                        except BaseException:
                            pass
                    if f is not None:
                        try:
                            f.close()
                        except BaseException:
                            pass
                    return None

            @staticmethod
            def step_bg_load(task, max_bytes):
                remaining = task[5] - task[4]
                if remaining <= 0:
                    return task[2]
                part = task[1].read(min(max_bytes, remaining))
                if not part:
                    raise OSError('truncated fast background')
                offset = task[4]
                task[3][offset:offset + len(part)] = part
                task[4] = offset + len(part)
                if task[4] < task[5]:
                    return None
                try:
                    task[1].close()
                except BaseException:
                    pass
                try:
                    task[0].close()
                except BaseException:
                    pass
                img = task[2]
                task[0] = None
                task[1] = None
                task[2] = None
                task[3] = None
                return img

            @staticmethod
            def cancel_bg_load(task):
                for pos in (1, 0):
                    obj = task[pos]
                    if obj is not None:
                        try:
                            obj.close()
                        except BaseException:
                            pass
                        task[pos] = None
                if task[2] is not None:
                    try:
                        task[2].close()
                    except BaseException:
                        pass
                    task[2] = None
                task[3] = None

            def load_bg(self, level, idx):
                task = self.begin_bg_load(level, idx)
                if task is not None:
                    try:
                        img = None
                        while img is None:
                            img = self.step_bg_load(task, BG_LOAD_BYTES)
                        return img
                    except BaseException:
                        self.cancel_bg_load(task)
                p = path_join(RESOURCE_DIR, 'bg/level%d_%02d.png' % (level, idx))
                try:
                    return image.Image(p, cvt_565=True)
                except TypeError:
                    return image.Image(p).to_rgb565()

        canvas = image.Image(SCREEN_W, SCREEN_H, image.RGB565)
        dapi = DisplayAPI()
        lapi = LoadAPI()
        usb_pad = USBGamepadInput()
        game = MarioGame(dapi, lapi, tp)
        game.input = TouchInput(tp, usb_pad)
        # LOADING 画面
        canvas.clear()
        canvas.draw_string_advanced(304, 220, 32, '正在加载游戏', color=(255, 255, 255))
        Display.show_image(canvas)
        game.preload()
        write_marker('mario_touch_mode.txt', touch_mode + '\n')
        write_marker('mario_gamepad.txt',
                     ('connected %s: %s\n' %
                      (usb_pad.device_kind or 'evdev', ','.join(usb_pad.paths)))
                     if usb_pad.connected else 'not connected\n')
        if auto_start:
            game.new_game()
        else:
            game.state = TITLE
        game.run()
    except KeyboardInterrupt:
        pass
    except BaseException as e:
        if str(e) == 'IDE interrupt':
            return
        write_marker('mario_error.txt', '%s: %s\n' % (type(e).__name__, e))
        try:
            canvas.draw_string_advanced(20, 200, 16, '错误：%s' % e, color=(255, 0, 0))
            Display.show_image(canvas)
        except BaseException:
            pass
        raise
    finally:
        try:
            tp.deinit()
        except BaseException:
            pass
        try:
            usb_pad.deinit()
        except BaseException:
            pass
        try:
            Display.deinit()
        except BaseException:
            pass
        try:
            MediaManager.deinit()
        except BaseException:
            pass
