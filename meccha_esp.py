#!/usr/bin/env python3
import sys, struct, math, ctypes, time, subprocess, os
from dataclasses import dataclass, field

import pymem
from PyQt5.QtWidgets import (QApplication, QWidget, QCheckBox, QLabel,
    QVBoxLayout, QHBoxLayout, QPushButton, QFrame, QColorDialog,
    QSpinBox, QTabWidget)
from PyQt5.QtCore import Qt, QTimer, QPointF, QPolygonF
from PyQt5.QtGui import QPainter, QPen, QColor, QFont, QBrush

STEAM_APP_ID = "4704690"

BOOT_OFFSETS = {
    "UObjectBase::ClassPrivate": 0x10,
    "UObjectBase::NamePrivate": 0x18,
    "UObjectBase::OuterPrivate": 0x20,
    "UStruct::SuperStruct": 0x40,
    "UStruct::ChildProperties": 0x50,
    "FField::Next": 0x18,
    "FField::NamePrivate": 0x20,
    "FProperty::Offset_Internal": 0x44,
    "UField::Next": 0x28,
    "UStruct::Children": 0x48,
    "FCameraCacheEntry::POV": 0x10,
    "FMinimalViewInfo::Location": 0x0,
    "FMinimalViewInfo::Rotation": 0x18,
    "FMinimalViewInfo::FOV": 0x30,
    "USceneComponent::ComponentToWorld": 0x1E0,
    "FTransform::Translation": 0x20,
    "USkeletalMeshComponent::ComponentSpaceTransforms": 0x9B8,
    "USkeletalMeshComponent::BoneSpaceTransforms": 0x9A8,
    "FTransform::Rotation": 0x00,
    "FTransform::Scale3D": 0x40,
}

BONE_TRANSFORM_STRIDE = 0x60

SKELETON_MAP = [
    (0, 1), (1, 2), (2, 3), (3, 4), (4, 5), (5, 6),
    (7, 8), (8, 9), (9, 10),
    (11, 12), (12, 13), (13, 14),
    (1, 15), (15, 16), (16, 17),
    (1, 18), (18, 19), (19, 20),
]

def rp(pm, addr):
    try: return struct.unpack("<Q", pm.read_bytes(addr, 8))[0]
    except: return 0

def ru32(pm, addr):
    try: return struct.unpack("<I", pm.read_bytes(addr, 4))[0]
    except: return 0

def rfloat(pm, addr):
    try: return struct.unpack("<f", pm.read_bytes(addr, 4))[0]
    except: return 0.0

def rvec3(pm, addr):
    try: return struct.unpack("<ddd", pm.read_bytes(addr, 24))
    except: return (0.0, 0.0, 0.0)

def rquat(pm, addr):
    try: return struct.unpack("<dddd", pm.read_bytes(addr, 32))
    except: return (0.0, 0.0, 0.0, 1.0)

def read_tarray(pm, addr):
    try:
        data = rp(pm, addr)
        count = ru32(pm, addr + 8)
        return data, count
    except: return 0, 0

def set_anti_capture(hwnd):
    try:
        ctypes.windll.user32.SetWindowDisplayAffinity(hwnd, 0x11)
    except: pass

def dist(a, b):
    return math.sqrt((a[0]-b[0])**2 + (a[1]-b[1])**2 + (a[2]-b[2])**2)

def qrotate(q, v):
    x, y, z, w = q
    cx = y * v[2] - z * v[1]
    cy = z * v[0] - x * v[2]
    cz = x * v[1] - y * v[0]
    dxx = y * cz - z * cy
    dyy = z * cx - x * cz
    dzz = x * cy - y * cx
    return (v[0] + 2 * (w * cx + dxx),
            v[1] + 2 * (w * cy + dyy),
            v[2] + 2 * (w * cz + dzz))

def cross_oab(o, a, b):
    return (a[0]-o[0])*(b[1]-o[1]) - (a[1]-o[1])*(b[0]-o[0])

def convex_hull(pts):
    if len(pts) <= 1:
        return pts
    pts = sorted(set(pts))
    lower = []
    for p in pts:
        while len(lower) >= 2 and cross_oab(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper = []
    for p in reversed(pts):
        while len(upper) >= 2 and cross_oab(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]

def launch_steam_and_game():
    """Launch Steam if not running, then launch the game."""
    steam_exe = "C:\\Program Files (x86)\\Steam\\Steam.exe"
    if not os.path.isfile(steam_exe):
        alt = os.path.expandvars("%ProgramW6432%\\Steam\\Steam.exe")
        if os.path.isfile(alt):
            steam_exe = alt
        else:
            alt2 = os.path.expandvars("%LOCALAPPDATA%\\Steam\\Steam.exe")
            if os.path.isfile(alt2):
                steam_exe = alt2

    steam_running = False
    try:
        import psutil
        for p in psutil.process_iter(['name']):
            if p.info['name'] and 'steam' in p.info['name'].lower():
                steam_running = True
                break
    except:
        pass

    if not steam_running and os.path.isfile(steam_exe):
        subprocess.Popen([steam_exe, "-silent"], shell=False)

    subprocess.Popen(["steam://rungameid/" + STEAM_APP_ID], shell=True)
    print(f"[+] Launched MECCA CHAMELEON via Steam (AppID: {STEAM_APP_ID})")
    print("[+] Waiting for game process...")

class PatternScanner:
    CHUNK = 0x200000
    def __init__(self, pm, mod_name):
        self.pm = pm
        mod = pymem.process.module_from_name(pm.process_handle, mod_name)
        if not mod: raise RuntimeError(f"Module {mod_name} not found")
        self.base, self.size = mod.lpBaseOfDll, mod.SizeOfImage

    def scan(self, pattern, mask):
        pat = bytes(pattern)
        for start in range(0, self.size, self.CHUNK):
            end = min(start + self.CHUNK + len(pat), self.size)
            try: data = self.pm.read_bytes(self.base + start, end - start)
            except: continue
            for i in range(len(data) - len(pat)):
                ok = True
                for j in range(len(pat)):
                    if mask[j] and data[i+j] != pat[j]: ok = False; break
                if ok: return self.base + start + i
        return 0

class FNameResolver:
    TABLE_OFFSETS = (0x8, 0x10, 0x18, 0x20, 0x28, 0x30, 0x38, 0x40, 0x48, 0x50, 0x58, 0x60, 0x68, 0x70)
    def __init__(self, pm, pool):
        self.pm = pm; self.pool = pool
        self.tbl_off = 0x10; self.style = "ue5"
        self._detect()
    def _read(self, eid, to, style):
        bi, wi = eid >> 16, (eid & 0xFFFF) << 1
        ba = rp(self.pm, self.pool + to + bi * 8)
        if not ba: return None
        hdr = struct.unpack("<H", self.pm.read_bytes(ba + wi, 2))[0]
        if style == "ue4": isw, ln = hdr & 1, hdr >> 1
        elif style == "custom": isw, ln = hdr & 1, (hdr >> 6) & 0x3FF
        else: ln, isw = hdr & 0x3FF, (hdr >> 10) & 1
        if ln == 0 or ln > 512: return None
        raw = self.pm.read_bytes(ba + wi + 2, ln * 2 if isw else ln)
        return raw.decode("utf-16-le" if isw else "latin-1")
    def _detect(self):
        for off in self.TABLE_OFFSETS:
            for st in ("custom", "ue5", "ue4"):
                try:
                    if self._read(0, off, st) == "None": self.tbl_off, self.style = off, st; return
                except: pass
    def resolve(self, eid):
        try:
            n = self._read(eid, self.tbl_off, self.style)
            if n is not None: return n
        except: pass
        for off in self.TABLE_OFFSETS:
            for st in ("custom", "ue5", "ue4"):
                if off == self.tbl_off and st == self.style: continue
                try:
                    n = self._read(eid, off, st)
                    if n is not None: self.tbl_off, self.style = off, st; return n
                except: pass
        return None

class UObjectArray:
    def __init__(self, pm, guobj, fname_pool):
        self.pm = pm; self.guobj = guobj; self.fnames = FNameResolver(pm, fname_pool)
        self._meta = None; self._cache = {}
    def obj_name(self, o): return self.fnames.resolve(ru32(self.pm, o + BOOT_OFFSETS["UObjectBase::NamePrivate"]))
    def obj_class(self, o): return rp(self.pm, o + BOOT_OFFSETS["UObjectBase::ClassPrivate"])
    def iter_objs(self):
        ptr = rp(self.pm, self.guobj + 0x10)
        if not ptr: return
        for ci in range(64):
            ch = rp(self.pm, ptr + ci * 8)
            if not ch: break
            for wi in range(0x10000):
                o = rp(self.pm, ch + wi * 0x18)
                if o: yield o
    def _meta_class(self):
        if self._meta is None or not self._meta:
            for o in self.iter_objs():
                if self.obj_name(o) == "Class": self._meta = o; break
        return self._meta
    def find_class(self, name):
        c = self._cache.get(name)
        if c:
            if self.obj_name(c) == name: return c
            del self._cache[name]
        mc = self._meta_class()
        if not mc: return 0
        for o in self.iter_objs():
            if self.obj_class(o) == mc and self.obj_name(o) == name:
                self._cache[name] = o; return o
        return 0
    def find_first(self, cls_name, skip_default=True):
        cls = self.find_class(cls_name)
        if not cls: return 0
        for o in self.iter_objs():
            if self.obj_class(o) == cls:
                n = self.obj_name(o)
                if skip_default and n and n.startswith("Default__"): continue
                return o
        return 0

class OffsetResolver:
    def __init__(self, pm, objs):
        self.pm = pm; self.objs = objs; self.cache = dict(BOOT_OFFSETS)
    def field_name(self, f):
        return self.objs.fnames.resolve(ru32(self.pm, f + self.cache["FField::NamePrivate"]))
    def resolve_on(self, cls, pn):
        prop = rp(self.pm, cls + self.cache["UStruct::ChildProperties"]); d = 0
        while prop and d < 512:
            if self.field_name(prop) == pn: return ru32(self.pm, prop + self.cache["FProperty::Offset_Internal"])
            prop = rp(self.pm, prop + self.cache["FField::Next"]); d += 1
        return None
    def resolve(self, cls_name, prop_name):
        k = f"{cls_name}::{prop_name}"
        if k in self.cache: return self.cache[k]
        cls = self.objs.find_class(cls_name)
        if not cls: return None
        off = self.resolve_on(cls, prop_name)
        seen = {cls}
        while off is None:
            sc = rp(self.pm, cls + self.cache["UStruct::SuperStruct"])
            if not sc or sc in seen: break
            seen.add(sc); off = self.resolve_on(sc, prop_name)
        if off is not None: self.cache[k] = off
        return off

@dataclass
class Config:
    enabled: bool = True
    box_esp: bool = True
    corner_esp: bool = False
    outline_esp: bool = False
    skeleton_esp: bool = True
    show_names: bool = True
    show_distance: bool = True
    health_bar: bool = True
    shield_bar: bool = True
    snap_lines: bool = False
    show_local: bool = False
    dot_radius: int = 6
    box_height: float = 80.0
    box_width: float = 30.0
    y_offset: int = 0
    enemy_color: list = field(default_factory=lambda: [255, 50, 50])
    local_color: list = field(default_factory=lambda: [50, 255, 50])
    skeleton_color: list = field(default_factory=lambda: [255, 255, 0])
    show_playerlist: bool = True

class GameReader:
    PROC = "PenguinHotel-Win64-Shipping.exe"
    MOD = "PenguinHotel-Win64-Shipping.exe"
    GUOBJ_SIG = bytes([0x48,0x8D,0x05,0,0,0,0,0x48,0x89,0x01,0x45,0x8B,0xD1])
    GUOBJ_MSK = bytes([1,1,1,0,0,0,0,1,1,1,1,1,1])
    FNAME_DELTA = 0xE3B40
    FNAME_PATS = [
        (bytes([0x48,0x8D,0x0D,0,0,0,0,0xE8,0,0,0,0,0x4C,0x8B,0xC0]), bytes([1,1,1,0,0,0,0,1,0,0,0,0,1,1,1])),
        (bytes([0x48,0x8D,0x35,0,0,0,0]), bytes([1,1,1,0,0,0,0])),
    ]
    OFFSET_MAP = {
        "World::GameState": ("World", "GameState"),
        "World::OwningGameInstance": ("World", "OwningGameInstance"),
        "GameInstance::LocalPlayers": ("GameInstance", "LocalPlayers"),
        "Player::PlayerController": ("Player", "PlayerController"),
        "Engine::GameViewport": ("Engine", "GameViewport"),
        "GameViewportClient::World": ("GameViewportClient", "World"),
        "GameStateBase::PlayerArray": ("GameStateBase", "PlayerArray"),
        "PlayerState::PawnPrivate": ("PlayerState", "PawnPrivate"),
        "Controller::PlayerState": ("Controller", "PlayerState"),
        "PlayerController::AcknowledgedPawn": ("PlayerController", "AcknowledgedPawn"),
        "PlayerController::PlayerCameraManager": ("PlayerController", "PlayerCameraManager"),
        "PlayerCameraManager::CameraCachePrivate": ("PlayerCameraManager", "CameraCachePrivate"),
        "Actor::RootComponent": ("Actor", "RootComponent"),
        "SceneComponent::RelativeLocation": ("SceneComponent", "RelativeLocation"),
        "Character::Mesh": ("Character", "Mesh"),
    }
    HEALTH_PROPS = ("Health", "CurrentHealth", "HP", "HitPoints", "health")
    SHIELD_PROPS = ("Shield", "Armor", "ShieldHealth", "shield")

    def __init__(self):
        self.pm = pymem.Pymem(self.PROC)
        self.guobj = self._scan_guobj()
        if not self.guobj: raise RuntimeError("GUObjectArray not found")
        self.fname_pool = self._scan_fname()
        if not self.fname_pool: raise RuntimeError("FNamePool not found")
        self.objs = UObjectArray(self.pm, self.guobj, self.fname_pool)
        self.resolver = OffsetResolver(self.pm, self.objs)
        self.offsets = {}
        for k, (cls, prop) in self.OFFSET_MAP.items():
            v = self.resolver.resolve(cls, prop)
            if v is not None: self.offsets[k] = v
        for k in BOOT_OFFSETS: self.offsets[k] = BOOT_OFFSETS[k]
        self.gengine = self.objs.find_first("GameEngine")
        if not self.gengine: raise RuntimeError("GEngine not found")
        self._hp_offs = None
        self.mesh_off = self.offsets.get("Character::Mesh", 0x418)
        self.comp_to_world_off = 0x1E0
        self.cst_off = 0x9B8

    def _scan_guobj(self):
        s = PatternScanner(self.pm, self.MOD)
        a = s.scan(self.GUOBJ_SIG, self.GUOBJ_MSK)
        if not a: return 0
        rel = struct.unpack("<i", self.pm.read_bytes(a+3, 4))[0]
        return a + 7 + rel

    def _scan_fname(self):
        dc = self.guobj - self.FNAME_DELTA
        if self._verify_fname(dc): return dc
        s = PatternScanner(self.pm, self.MOD)
        for sig, msk in self.FNAME_PATS:
            a = s.scan(sig, msk)
            if not a: continue
            rel = struct.unpack("<i", self.pm.read_bytes(a+3, 4))[0]
            c = a + 7 + rel
            if self._verify_fname(c): return c
        return dc

    def _verify_fname(self, addr):
        r = FNameResolver(self.pm, addr)
        return r.resolve(0) == "None"

    def _get_world(self):
        vp = rp(self.pm, self.gengine + self.offsets.get("Engine::GameViewport", 0))
        if not vp: return 0
        return rp(self.pm, vp + self.offsets.get("GameViewportClient::World", 0))

    def _get_local_pc(self, world):
        if not world: return 0
        gi = rp(self.pm, world + self.offsets.get("World::OwningGameInstance", 0))
        if not gi: return 0
        lp, cnt = read_tarray(self.pm, gi + self.offsets.get("GameInstance::LocalPlayers", 0))
        if not lp or cnt == 0: return 0
        lpl = rp(self.pm, lp)
        if not lpl: return 0
        return rp(self.pm, lpl + self.offsets.get("Player::PlayerController", 0))

    def get_camera(self):
        w = self._get_world()
        if not w: return None
        pc = self._get_local_pc(w)
        if not pc: return None
        cam = rp(self.pm, pc + self.offsets.get("PlayerController::PlayerCameraManager", 0))
        if not cam: return None
        cc = cam + self.offsets.get("PlayerCameraManager::CameraCachePrivate", 0)
        pov = cc + BOOT_OFFSETS["FCameraCacheEntry::POV"]
        try:
            loc = rvec3(self.pm, pov + BOOT_OFFSETS["FMinimalViewInfo::Location"])
            rot = rvec3(self.pm, pov + BOOT_OFFSETS["FMinimalViewInfo::Rotation"])
            fov = rfloat(self.pm, pov + BOOT_OFFSETS["FMinimalViewInfo::FOV"])
            return {"loc": loc, "rot": rot, "fov": fov}
        except: return None

    def get_actor_pos(self, actor):
        if not actor: return None
        root = rp(self.pm, actor + self.offsets.get("Actor::RootComponent", 0))
        if not root: return None
        return rvec3(self.pm, root + self.offsets.get("SceneComponent::RelativeLocation", 0))

    def get_mesh(self, actor):
        if not actor: return 0
        return rp(self.pm, actor + self.mesh_off)

    def get_comp_to_world(self, mesh):
        if not mesh: return None
        addr = mesh + self.comp_to_world_off
        rot = rquat(self.pm, addr + 0x00)
        trans = rvec3(self.pm, addr + 0x20)
        scale = rvec3(self.pm, addr + 0x40)
        return {"trans": trans, "rot": rot, "scale": scale}

    def read_bone_transforms(self, actor):
        mesh = self.get_mesh(actor)
        if not mesh: return None
        ctw = self.get_comp_to_world(mesh)
        if not ctw: return None
        cst, count = read_tarray(self.pm, mesh + self.cst_off)
        if not cst or count < 5 or count > 200: return None
        ctw_r, ctw_t, ctw_s = ctw["rot"], ctw["trans"], ctw["scale"]
        bones = []
        for i in range(count):
            addr = cst + i * BONE_TRANSFORM_STRIDE
            try:
                pos = rvec3(self.pm, addr + 0x20)
            except: pos = (0, 0, 0)
            sx, sy, sz = pos[0]*ctw_s[0], pos[1]*ctw_s[1], pos[2]*ctw_s[2]
            rx, ry, rz = qrotate(ctw_r, (sx, sy, sz))
            bones.append((rx + ctw_t[0], ry + ctw_t[1], rz + ctw_t[2]))
        return bones

    def get_health(self, actor):
        if self._hp_offs is not None:
            _, h_off, _, s_off = self._hp_offs
            health = rfloat(self.pm, actor + h_off) if h_off >= 0 and actor else None
            shield = rfloat(self.pm, actor + s_off) if s_off >= 0 and actor else None
            if health is not None: return max(0, health), max(0, shield or 0)
            return None, None
        cls = self.objs.obj_class(actor)
        if not cls: return None, None
        h_off = self.resolver.resolve_on(cls, "Health")
        if h_off is None: h_off = -1
        s_off = -1
        for p in self.SHIELD_PROPS:
            off = self.resolver.resolve_on(cls, p)
            if off is not None: s_off = off; break
        self._hp_offs = ("", h_off, "", s_off)
        if h_off >= 0:
            health = rfloat(self.pm, actor + h_off)
            shield = rfloat(self.pm, actor + s_off) if s_off >= 0 else 0
            if health is not None: return max(0, health), max(0, shield or 0)
        return None, None

    def get_player_name(self, ps):
        if not ps: return None
        off = self.offsets.get("PlayerState::PlayerNamePrivate")
        if off is None:
            cls = self.objs.find_class("PlayerState")
            if cls:
                for prop in ("PlayerNamePrivate", "PlayerName"):
                    o = self.resolver.resolve_on(cls, prop)
                    if o is not None:
                        self.offsets["PlayerState::PlayerNamePrivate"] = o
                        off = o
                        break
        if off is None:
            off = 0x320
        data, count = read_tarray(self.pm, ps + off)
        if not data or count <= 0 or count > 32: return None
        try:
            raw = self.pm.read_bytes(data, count * 2)
            return raw.decode("utf-16-le", errors="replace").strip("\x00")
        except: return None

    def iter_players(self):
        w = self._get_world()
        if not w: return
        gs = rp(self.pm, w + self.offsets.get("World::GameState", 0))
        if not gs: return
        pa, cnt = read_tarray(self.pm, gs + self.offsets.get("GameStateBase::PlayerArray", 0))
        if not pa or cnt == 0: return
        lpc = self._get_local_pc(w)
        lpawn = rp(self.pm, lpc + self.offsets.get("PlayerController::AcknowledgedPawn", 0)) if lpc else 0
        seen = set()
        for i in range(cnt):
            ps = rp(self.pm, pa + i * 8)
            if not ps or ps in seen: continue
            seen.add(ps)
            pawn = rp(self.pm, ps + self.offsets.get("PlayerState::PawnPrivate", 0))
            if not pawn or pawn in seen: continue
            seen.add(pawn)
            pos = self.get_actor_pos(pawn)
            if not pos: continue
            is_local = pawn == lpawn
            name = self.get_player_name(ps) or f"Player {i}"
            yld = {"is_local": is_local, "pos": pos, "actor": pawn, "ps": ps, "idx": i, "name": name}
            if not is_local:
                bones = self.read_bone_transforms(pawn)
                yld["bones"] = bones
            else:
                yld["bones"] = None
            yield yld

def rot_to_axes(rot):
    p, y, r = [math.radians(x) for x in rot]
    sp, cp = math.sin(p), math.cos(p)
    sy, cy = math.sin(y), math.cos(y)
    sr, cr = math.sin(r), math.cos(r)
    fwd = (cp*cy, cp*sy, sp)
    rgt = (sr*sp*cy - cr*sy, sr*sp*sy + cr*cy, -sr*cp)
    up = (-(cr*sp*cy + sr*sy), cy*sr - cr*sp*sy, cr*cp)
    return fwd, rgt, up

def w2s(pos, cam, sw, sh):
    loc, rot, fov = cam["loc"], cam["rot"], cam["fov"]
    fwd, rgt, up = rot_to_axes(rot)
    dx, dy, dz = pos[0]-loc[0], pos[1]-loc[1], pos[2]-loc[2]
    vx = dx*fwd[0] + dy*fwd[1] + dz*fwd[2]
    if vx <= 0.1: return None
    vy = dx*rgt[0] + dy*rgt[1] + dz*rgt[2]
    vz = dx*up[0] + dy*up[1] + dz*up[2]
    aspect = sw/sh
    thf = math.tan(math.radians(fov)/2.0)
    nx = vy/(vx*thf); ny = vz/(vx*thf/aspect)
    return (1.0+nx)*sw/2.0, (1.0-ny)*sh/2.0

class Overlay(QWidget):
    def __init__(self, reader: GameReader, cfg: Config):
        super().__init__()
        self.reader = reader; self.cfg = cfg
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
                            | Qt.Tool | Qt.WindowTransparentForInput)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._key_state = {}
        self._menu = None
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(16)
        self._find_game()
        self._resize()

    def set_menu(self, menu): self._menu = menu

    def _find_game(self):
        try:
            import win32gui
            self._hwnd = win32gui.FindWindow(None, "MECCA CHAMELEON")
            if not self._hwnd:
                self._hwnd = win32gui.FindWindow(None, "Chameleon")
        except: self._hwnd = 0

    def _resize(self):
        try:
            import win32gui
            if self._hwnd:
                r = win32gui.GetClientRect(self._hwnd)
                tl = win32gui.ClientToScreen(self._hwnd, (r[0], r[1]))
                br = win32gui.ClientToScreen(self._hwnd, (r[2], r[3]))
                self.setGeometry(tl[0], tl[1], br[0]-tl[0], br[1]-tl[1])
            else: self.setGeometry(0, 0, 1920, 1080)
        except: self.setGeometry(0, 0, 1920, 1080)

    def _tick(self):
        self._resize()
        self._poll_keys()
        self.update()

    def _poll_keys(self):
        VK_INSERT, VK_F1 = 0x2D, 0x70
        for vk, name in [(VK_INSERT, "ins"), (VK_F1, "f1")]:
            down = ctypes.windll.user32.GetAsyncKeyState(vk) & 0x8000
            if down and not self._key_state.get(name):
                if self._menu:
                    self._menu.setVisible(not self._menu.isVisible())
            self._key_state[name] = bool(down)
        VK_END = 0x23
        if ctypes.windll.user32.GetAsyncKeyState(VK_END) & 0x8000:
            if not self._key_state.get("end"): QApplication.quit()
        self._key_state["end"] = bool(ctypes.windll.user32.GetAsyncKeyState(VK_END) & 0x8000)

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        f = QFont("Consolas", 10); p.setFont(f)
        sw, sh = self.width(), self.height()
        if not self.cfg.enabled:
            p.setPen(QPen(QColor(255,255,255)))
            p.drawText(10, 20, "ESP OFF [Ins/F1]"); return
        cam = self.reader.get_camera()
        if not cam:
            p.setPen(QPen(QColor(255,100,100)))
            p.drawText(10, 20, "No Camera (in game?)"); return
        players = list(self.reader.iter_players())
        health_cache = {}
        for pl in players:
            actor = pl["actor"]
            if actor not in health_cache:
                health_cache[actor] = self.reader.get_health(actor)
        alive = 0
        for pl in players:
            hp, shp = health_cache.get(pl["actor"], (None, None))
            if pl["is_local"]:
                if self.cfg.show_local:
                    self._draw_player(p, pl, cam, sw, sh, hp, shp)
            else:
                if hp is not None and hp <= 0: continue
                alive += 1
                self._draw_player(p, pl, cam, sw, sh, hp, shp)
        if self.cfg.show_playerlist:
            y = 40
            p.setPen(QPen(QColor(200, 200, 200, 180)))
            for pl in players:
                if pl["is_local"]: continue
                hp, _ = health_cache.get(pl["actor"], (None, None))
                if hp is not None and hp <= 0: continue
                hpc = f"HP:{int(hp)}" if hp is not None else ""
                txt = f"{pl['name']} [{int(dist(pl['pos'], cam['loc'])/100)}m] {hpc}"
                p.drawText(10, y, txt)
                y += 16
                if y > sh - 20: break
        p.setPen(QPen(QColor(200,200,200)))
        p.drawText(10, 20, f"Players: {alive}")

    def _draw_player(self, p, pl, cam, sw, sh, hp=None, shp=None):
        is_local = pl["is_local"]
        pos = pl["pos"]
        bones = pl.get("bones")
        color = self.cfg.local_color if is_local else self.cfg.enemy_color
        d = dist(pos, cam["loc"])
        scale = max(0.4, min(2.0, 100.0 / d)) if d > 0 else 1.0
        base = w2s(pos, cam, sw, sh)
        if not base: return
        sx, sy = base[0], base[1] + self.cfg.y_offset

        bone_pts = []
        if bones and len(bones) > 5:
            for bp in bones:
                s = w2s(bp, cam, sw, sh)
                if s: bone_pts.append(s)

        box = None
        if bone_pts:
            xs = [pt[0] for pt in bone_pts]
            ys = [pt[1] for pt in bone_pts]
            bx, by = min(xs), min(ys)
            bw, bh = max(xs) - bx, max(ys) - by
            if bw > 0 and bh > 0:
                box = (bx, by, bw, bh)

        if self.cfg.outline_esp and not is_local and len(bone_pts) >= 3:
            hull = convex_hull(bone_pts)
            if len(hull) >= 3:
                poly = QPolygonF([QPointF(x, y) for x, y in hull])
                p.setBrush(QColor(*color, 40))
                p.setPen(QPen(QColor(*color), 1.5))
                p.drawPolygon(poly)

        if self.cfg.box_esp and not is_local and box:
            bx, by, bw, bh = box
            if self.cfg.corner_esp:
                self._draw_corners(p, bx, by, bw, bh, color)
            else:
                p.setPen(QPen(QColor(*color), 1.5))
                p.setBrush(Qt.NoBrush)
                p.drawRect(int(bx), int(by), int(bw), int(bh))

        if (self.cfg.health_bar or self.cfg.shield_bar) and not is_local and hp is not None:
            if box:
                bx, by, bw, bh = box
                hx = bx - 6
                hy = by
                hw = 4
                hh = bh * 0.8
            else:
                hx = sx - self.cfg.box_width * scale / 2.0 - 6
                hy = sy - self.cfg.box_height * scale
                hw = 4
                hh = self.cfg.box_height * scale * 0.8
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(30, 30, 30, 180))
            p.drawRect(int(hx), int(hy), int(hw), int(hh))
            h_pct = max(0, min(1.0, hp / 100.0))
            h_fill = int(hh * h_pct)
            hr = int(255 * (1 - h_pct))
            hg = int(255 * h_pct)
            p.setBrush(QColor(hr, hg, 0, 220))
            p.drawRect(int(hx), int(hy + hh - h_fill), int(hw), h_fill)
            if self.cfg.shield_bar and shp is not None and shp > 0:
                s_pct = max(0, min(1.0, shp / 100.0))
                s_fill = int(hh * s_pct)
                p.setBrush(QColor(0, 120, 255, 200))
                p.drawRect(int(hx + hw + 2), int(hy + hh - s_fill), int(hw), s_fill)

        if self.cfg.skeleton_esp and bones and len(bones) > 20:
            skel_color = self.cfg.skeleton_color
            pts = {}
            for i, bp in enumerate(bones):
                s = w2s(bp, cam, sw, sh)
                if s: pts[i] = s
            for a, b in SKELETON_MAP:
                if a in pts and b in pts:
                    p.setPen(QPen(QColor(*skel_color), 1.5))
                    p.drawLine(int(pts[a][0]), int(pts[a][1]), int(pts[b][0]), int(pts[b][1]))

        if self.cfg.show_names or self.cfg.show_distance:
            parts = []
            if self.cfg.show_names:
                parts.append("YOU" if is_local else pl.get("name", f"Player {pl['idx']}"))
            if self.cfg.show_distance:
                parts.append(f"{int(d/100)}m")
            if parts:
                txt = " | ".join(parts)
                p.setPen(QPen(QColor(*color)))
                p.drawText(int(sx + 8), int(sy), txt)

        if self.cfg.snap_lines and not is_local:
            p.setPen(QPen(QColor(*color), 1, Qt.DashLine))
            p.drawLine(int(sw/2), int(sh), int(sx), int(sy))

        r = int(self.cfg.dot_radius * scale)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(*color))
        p.drawEllipse(int(sx - r/2), int(sy - r/2), r, r)

    def _draw_corners(self, p, x, y, w, h, color, ln=0.25):
        c = max(4, int(min(w, h) * ln))
        pen = QPen(QColor(*color), 2); p.setPen(pen)
        p.drawLine(int(x), int(y), int(x+c), int(y))
        p.drawLine(int(x), int(y), int(x), int(y+c))
        p.drawLine(int(x+w-c), int(y), int(x+w), int(y))
        p.drawLine(int(x+w), int(y), int(x+w), int(y+c))
        p.drawLine(int(x), int(y+h-c), int(x), int(y+h))
        p.drawLine(int(x), int(y+h), int(x+c), int(y+h))
        p.drawLine(int(x+w-c), int(y+h), int(x+w), int(y+h))
        p.drawLine(int(x+w), int(y+h-c), int(x+w), int(y+h))

class MenuWindow(QWidget):
    def __init__(self, cfg: Config):
        super().__init__()
        self.cfg = cfg
        self.setWindowTitle("Meccha ESP")
        self.setWindowFlags(Qt.WindowStaysOnTopHint | Qt.FramelessWindowHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self._drag = None
        self._build()
        self.setFixedSize(300, 480)

    def _build(self):
        self.setStyleSheet("""
            QWidget { background: rgba(16,16,24,235); }
            QTabWidget::pane { background: rgba(16,16,24,235); border: 1px solid #3a3a5a; border-radius: 4px; }
            QTabBar::tab { background: #1a1a28; color: #888; padding: 6px 12px; border: 1px solid #3a3a5a; border-bottom: none; border-radius: 4px 4px 0 0; font-size: 10px; }
            QTabBar::tab:selected { background: #2a2a40; color: #5af; }
            QLabel { color: #ccc; font-size: 11px; }
            QCheckBox { color: #ccc; font-size: 11px; spacing: 6px; }
            QCheckBox::indicator { width: 14px; height: 14px; border: 1px solid #555; border-radius: 3px; background: #222; }
            QCheckBox::indicator:checked { background: #4a8af4; border-color: #6aaaff; }
            QPushButton { background: #2a2a40; color: #ccc; border: 1px solid #444; padding: 4px 8px; border-radius: 4px; font-size: 11px; }
            QPushButton:hover { background: #3a3a55; }
            QSpinBox { background: #1a1a28; color: #ccc; border: 1px solid #444; padding: 2px; border-radius: 3px; font-size: 11px; }
        """)

        tabs = QTabWidget(self)
        tabs.addTab(self._build_visuals(), "Visuals")
        tabs.addTab(self._build_health(), "Health")
        tabs.addTab(self._build_colors(), "Colors")
        tabs.addTab(self._build_info(), "Info")

        lo = QVBoxLayout(self)
        lo.setContentsMargins(4, 4, 4, 4)
        lo.addWidget(tabs)
        self.setLayout(lo)

    def _build_visuals(self):
        w = QWidget()
        lo = QVBoxLayout(w)
        lo.setContentsMargins(8, 8, 8, 8)
        lo.setSpacing(4)

        t = QLabel("MECCHA ESP v1.2")
        t.setStyleSheet("font-size: 14px; font-weight: bold; color: #5af;")
        lo.addWidget(t)

        self._chk("ESP Enabled", "enabled", lo)
        lo.addWidget(QLabel("\u2500" * 30))
        lo.addWidget(QLabel("Visuals"))
        self._chk("Box ESP", "box_esp", lo)
        self._chk("Corner Box", "corner_esp", lo)
        self._chk("Outline ESP", "outline_esp", lo)
        self._chk("Skeleton ESP", "skeleton_esp", lo)
        self._chk("Nametags", "show_names", lo)
        self._chk("Distance", "show_distance", lo)
        self._chk("Snap Lines", "snap_lines", lo)
        self._chk("Show Local", "show_local", lo)
        lo.addWidget(QLabel("\u2500" * 30))
        lo.addWidget(QLabel("Overlay"))
        self._chk("Player List", "show_playerlist", lo)

        lo.addStretch()
        ft = QLabel("Ins/F1: Menu | END: Exit")
        ft.setStyleSheet("color: #666; font-size: 9px;")
        lo.addWidget(ft)
        return w

    def _build_health(self):
        w = QWidget()
        lo = QVBoxLayout(w)
        lo.setContentsMargins(8, 8, 8, 8)
        lo.setSpacing(4)
        lo.addWidget(QLabel("Health"))
        self._chk("Health Bar", "health_bar", lo)
        self._chk("Shield Bar", "shield_bar", lo)
        lo.addWidget(QLabel("\u2500" * 30))
        lo.addWidget(QLabel("Scale"))
        r = QHBoxLayout()
        r.addWidget(QLabel("Box H:")); self._spn("box_height", 40, 200, 5, r)
        r.addWidget(QLabel("W:")); self._spn("box_width", 10, 80, 2, r)
        lo.addLayout(r)
        r2 = QHBoxLayout()
        r2.addWidget(QLabel("Y Offset:")); self._spn("y_offset", -50, 50, 1, r2)
        r2.addWidget(QLabel("Dot R:")); self._spn("dot_radius", 2, 20, 1, r2)
        lo.addLayout(r2)
        lo.addStretch()
        return w

    def _build_colors(self):
        w = QWidget()
        lo = QVBoxLayout(w)
        lo.setContentsMargins(8, 8, 8, 8)
        lo.setSpacing(6)
        lo.addWidget(QLabel("Colors"))
        b1 = QPushButton("Enemy Color")
        b1.clicked.connect(self._pick_enemy)
        b2 = QPushButton("Local Color")
        b2.clicked.connect(self._pick_local)
        b3 = QPushButton("Skeleton Color")
        b3.clicked.connect(self._pick_skeleton)
        lo.addWidget(b1); lo.addWidget(b2); lo.addWidget(b3)
        lo.addStretch()
        return w

    def _build_info(self):
        w = QWidget()
        lo = QVBoxLayout(w)
        lo.setContentsMargins(8, 8, 8, 8)
        lo.setSpacing(6)
        lbl = QLabel("MECCA CHAMELEON ESP")
        lbl.setStyleSheet("font-size: 13px; font-weight: bold; color: #5af;")
        lo.addWidget(lbl)
        lo.addWidget(QLabel("v1.2"))
        lo.addWidget(QLabel("\u2500" * 30))
        lo.addWidget(QLabel("Controls:"))
        lo.addWidget(QLabel("  Ins/F1  - Toggle Menu"))
        lo.addWidget(QLabel("  END     - Exit"))
        lo.addWidget(QLabel("\u2500" * 30))
        lo.addWidget(QLabel("Features:"))
        lo.addWidget(QLabel("  Box / Corner ESP"))
        lo.addWidget(QLabel("  Outline ESP"))
        lo.addWidget(QLabel("  Skeleton ESP"))
        lo.addWidget(QLabel("  Health & Shield bars"))
        lo.addWidget(QLabel("  Player nametags"))
        lo.addWidget(QLabel("  Distance display"))
        lo.addWidget(QLabel("  Snap lines"))
        lo.addWidget(QLabel("  Player list overlay"))
        lo.addWidget(QLabel("\u2500" * 30))
        lo.addStretch()
        ft = QLabel("Steam AppID: 4704690")
        ft.setStyleSheet("color: #555; font-size: 9px;")
        lo.addWidget(ft)
        return w

    def _chk(self, text, attr, lo):
        cb = QCheckBox(text)
        cb.setChecked(getattr(self.cfg, attr))
        cb.stateChanged.connect(lambda s, a=attr: setattr(self.cfg, a, bool(s)))
        lo.addWidget(cb)

    def _spn(self, attr, mn, mx, step, lo):
        s = QSpinBox()
        s.setRange(mn, mx); s.setSingleStep(step)
        s.setValue(int(getattr(self.cfg, attr)))
        s.valueChanged.connect(lambda v, a=attr: setattr(self.cfg, a, float(v) if isinstance(getattr(self.cfg, a), float) else v))
        lo.addWidget(s)

    def _pick_enemy(self):
        c = QColorDialog.getColor(QColor(*self.cfg.enemy_color), self)
        if c.isValid(): self.cfg.enemy_color = [c.red(), c.green(), c.blue()]

    def _pick_local(self):
        c = QColorDialog.getColor(QColor(*self.cfg.local_color), self)
        if c.isValid(): self.cfg.local_color = [c.red(), c.green(), c.blue()]

    def _pick_skeleton(self):
        c = QColorDialog.getColor(QColor(*self.cfg.skeleton_color), self)
        if c.isValid(): self.cfg.skeleton_color = [c.red(), c.green(), c.blue()]

    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton: self._drag = ev.globalPos() - self.frameGeometry().topLeft(); ev.accept()
    def mouseMoveEvent(self, ev):
        if self._drag and ev.buttons() == Qt.LeftButton: self.move(ev.globalPos() - self._drag); ev.accept()
    def mouseReleaseEvent(self, ev): self._drag = None

def main():
    app = QApplication(sys.argv)
    cfg = Config()

    if not ctypes.windll.shell32.IsUserAnAdmin():
        from PyQt5.QtWidgets import QMessageBox
        QMessageBox.critical(None, "Meccha ESP - Admin Required",
            "This tool requires Administrator privileges.\n\n"
            "The EXE should auto-prompt for elevation. If not,\n"
            "right-click and select Run as Administrator.")
        sys.exit(1)

    from PyQt5.QtWidgets import QMessageBox

    launch_steam_and_game()

    reader = None
    retries = 0
    max_retries = 100
    while reader is None and retries < max_retries:
        try:
            reader = GameReader()
        except pymem.exception.ProcessNotFound:
            retries += 1
            if retries == 1:
                print(f"[!] Waiting for {GameReader.PROC}... (retrying every 1s)")
            elif retries == 10:
                try:
                    import psutil
                    game_procs = [p for p in psutil.process_iter(['name']) if 'Penguin' in p.info['name'] or 'Hotel' in p.info['name']]
                    if game_procs:
                        for p in game_procs:
                            print(f"    Found: {p.info['name']} (PID: {p.pid})")
                    else:
                        print(f"    No process with 'Penguin' or 'Hotel' found.")
                        print(f"    Make sure MECCA CHAMELEON is running!")
                except: pass
            time.sleep(1)
        except pymem.exception.CouldNotOpenProcess as e:
            QMessageBox.critical(None, "Meccha ESP - Permission Error",
                f"Could not open game process.\n\n"
                f"Make sure you are running as Administrator.\n\n"
                f"Error: {e}")
            sys.exit(1)
        except Exception as e:
            QMessageBox.critical(None, "Meccha ESP Error",
                f"Failed to initialize:\n{e}\n\n"
                f"Make sure {GameReader.PROC} is running\n"
                f"and you are running as Administrator.")
            sys.exit(1)

    if reader is None:
        QMessageBox.critical(None, "Meccha ESP Error",
            f"Could not find {GameReader.PROC} after 100 seconds.\n\n"
            "Make sure the game is running and check the process name\n"
            "in Task Manager (Details tab).")
        sys.exit(1)

    overlay = Overlay(reader, cfg)
    menu = MenuWindow(cfg)
    overlay.set_menu(menu)

    overlay.show()
    menu.show()

    set_anti_capture(int(overlay.winId()))
    set_anti_capture(int(menu.winId()))

    sys.exit(app.exec())

if __name__ == "__main__":
    main()
