import asyncio
import ctypes
import ctypes.wintypes
import math
import re
import time

from loguru import logger
from wizwalker.constants import user32

from src.world_to_screen import get_camera_state, project_point

# Measured in-game: the rug's clickable disc is centred about here relative to the rug entity position (world units)
# and reaches ~137 units out. A soil placed by clicking the rug snaps to the rug centre, so the click only has to land on
# rug that no existing soil covers. The gardening camera looks at the rug from lower than the housing camera, which
# shifts the rug's edge, so several radii are tried instead of one.
RIM_CENTER = (-15.0, -10.0)
RIM_RADII_SELECT = (125.0, 110.0, 95.0, 135.0)
RIM_RADII_PLACE = (100.0, 85.0, 115.0, 70.0, 125.0)
RIM_DIRECTIONS = 24
_rim_select = {'last': 0.0, 'attempt': 0}


class SoilPlacementError(Exception):
    """No click placed a soil; the layout would end up with a hole, so the bot has to stop."""


async def _soils(client) -> dict:
    """Every 'Prepared Soil' entity currently in the zone: {entity address: (x, y, z)}."""
    found = {}
    for entity in await client.get_base_entity_list():
        try:
            entity_name = await entity.object_name()
            if entity_name and 'prepared soil' in entity_name.lower():
                loc = await entity.location()
                found[await entity.read_base_address()] = (loc.x, loc.y, loc.z)
        except Exception:
            continue
    return found


_watch = {'known': None}


async def _watch_soils(client, rug):
    """Log every soil that appears or disappears since the last call, with its position relative to the rug and to
    the closest other soil (the distance to that neighbour is how close the game lets two plots sit)."""
    soils = await _soils(client)
    known = _watch['known']
    _watch['known'] = dict(soils)
    if known is None:
        logger.debug(f"soil watch: started, {len(soils)} soils in the zone")
        return
    for addr, pos in soils.items():
        if addr in known:
            continue
        others = [p for a, p in soils.items() if a != addr]
        if others:
            near = min(others, key=lambda p: math.hypot(p[0] - pos[0], p[1] - pos[1]))
            gap = (f"nearest_soil_offset=({near[0] - pos[0]:.0f},{near[1] - pos[1]:.0f}) "
                   f"dist={math.hypot(near[0] - pos[0], near[1] - pos[1]):.0f}")
        else:
            gap = 'no other soil'
        logger.debug(f"soil watch: NEW soil at ({pos[0]:.0f},{pos[1]:.0f},{pos[2]:.0f}) "
                     f"rug_offset=({pos[0] - rug.x:.0f},{pos[1] - rug.y:.0f}) {gap} total={len(soils)}")
    for addr, pos in known.items():
        if addr not in soils:
            logger.debug(f"soil watch: soil removed from ({pos[0]:.0f},{pos[1]:.0f}) total={len(soils)}")


async def _click_world(client, cam, x, y, z) -> bool:
    point = project_point(cam, x, y, z)
    if point is None or not (0 <= point[0] < cam['client_w'] and 0 <= point[1] < cam['client_h']):
        logger.error(f"soil: world ({x:.0f},{y:.0f},{z:.0f}) is off screen or behind the camera")
        return False
    async with client.mouse_handler:
        await client.mouse_handler.click(point[0], point[1])
    return True


async def _wait_new_soil(client, before: dict):
    """Poll up to ~1.5s for a soil entity that was not there before; returns its (x, y, z) or None."""
    for _ in range(6):
        await asyncio.sleep(0.2)
        now = await _soils(client)
        new = [pos for addr, pos in now.items() if addr not in before]
        if new:
            return new[0]
    return None


async def _rim_point(client, cam, rug, radius: float):
    """The point on a ring around the rug that is on screen and farthest from every existing soil at the rug's height.

    Returns (clearance, x, y) or None. clearance is the distance to the closest soil centre.
    """
    soils = await _soils(client)
    near = [p for p in soils.values() if math.hypot(p[0] - rug.x, p[1] - rug.y) < 450 and abs(p[2] - rug.z) < 60]
    best = None
    for i in range(RIM_DIRECTIONS):
        angle = 2 * math.pi * i / RIM_DIRECTIONS
        x = rug.x + RIM_CENTER[0] + radius * math.cos(angle)
        y = rug.y + RIM_CENTER[1] + radius * math.sin(angle)
        point = project_point(cam, x, y, rug.z)
        if point is None or not (0 <= point[0] < cam['client_w'] and 0 <= point[1] < cam['client_h']):
            continue
        clearance = min((math.hypot(x - p[0], y - p[1]) for p in near), default=1e9)
        if best is None or clearance > best[0]:
            best = (clearance, x, y)
    return best


async def _place_soil(client, cam, rug, fatal: bool) -> bool:
    """Place soil on the rug while the soil is selected in gardening mode.

    Clicks a rug rim point far from existing soil and waits for a new 'Prepared Soil' entity to confirm, trying other
    radii if the game ignores the click. When nothing works it raises SoilPlacementError if fatal, else logs.
    """
    before = await _soils(client)
    for radius in RIM_RADII_PLACE:
        best = await _rim_point(client, cam, rug, radius)
        if best is None or not await _click_world(client, cam, best[1], best[2], rug.z):
            continue
        new = await _wait_new_soil(client, before)
        offset = f"({best[1] - rug.x:.0f},{best[2] - rug.y:.0f})"
        if new:
            logger.debug(f"soil: placed from rim point {offset} radius {radius:.0f} clearance {best[0]:.0f}; "
                         f"landed {new[0] - rug.x:.0f},{new[1] - rug.y:.0f} from the rug")
            return True
        logger.debug(f"soil: rim point {offset} radius {radius:.0f} clearance {best[0]:.0f} was ignored")
    if fatal:
        raise SoilPlacementError(f"no rim point placed a soil at rug ({rug.x:.0f},{rug.y:.0f})")
    logger.warning('soil: nothing placed; assuming soil is already on the rug and carrying on')
    return False


def _unproject_to_plane(cam, sx, sy, z):
    """Inverse of project_point: the world x,y where the pixel's view ray crosses the horizontal plane at height z."""
    norm_x = sx / cam['client_w']
    norm_y = 1.0 - sy / cam['client_h']
    u = (norm_x - cam['sp_left']) / (cam['sp_right'] - cam['sp_left'])
    v = 1.0 - (norm_y - cam['sp_top']) / (cam['sp_bottom'] - cam['sp_top'])
    px = cam['vp_left'] + u * (cam['vp_right'] - cam['vp_left'])
    py = cam['vp_bottom'] + v * (cam['vp_top'] - cam['vp_bottom'])
    dx = cam['fwd_x'] + px * cam['right_x'] + py * cam['up_x']
    dy = cam['fwd_y'] + px * cam['right_y'] + py * cam['up_y']
    dz = cam['fwd_z'] + px * cam['right_z'] + py * cam['up_z']
    if dz == 0:
        return None
    t = (z - cam['cam_z']) / dz
    return cam['cam_x'] + t * dx, cam['cam_y'] + t * dy


def _norm(text: str) -> str:
    return re.sub(r'[^a-z0-9]', '', text.lower())


async def click_entity(client, name: str) -> bool:
    """Click the on-screen position of the closest entity whose name matches `name`.

    Matching ignores case, spaces and underscores, so 'Golden Blossom Rug' also matches
    an object template named like 'Rug_GoldenBlossom'. Logs the chosen entity and pixel,
    or the nearest entity names when nothing matches, so the right name can be found.
    """
    # 'Name@dx,dy' clicks a pixel offset from the projected entity center; 'Name@w:dx,dy' offsets the world position
    # instead (e.g. to click a rug's rim that is not covered by an object sitting on its center).
    offset = (0, 0)
    world_offset = (0.0, 0.0)
    soil_mode = None
    # A leading '?' only measures: logs where the mouse cursor is relative to the entity and does not click.
    measure = name.startswith('?')
    name = name.lstrip('?')
    if '@' in name:
        name, _, off = name.partition('@')
        if off in ('soil', 'soilfirst', 'soilsame', 'rim'):
            soil_mode = off
        elif off.startswith('w:'):
            world_offset = tuple(float(v) for v in off[2:].split(','))
        else:
            offset = tuple(int(v) for v in off.split(','))
    words = [_norm(w) for w in re.split(r'[\s_]+', name) if w]
    want = _norm(name)
    self_pos = await client.body.position()

    candidates = []
    seen = []
    for entity in await client.get_base_entity_list():
        # Memory reads of unrelated entities can fail mid-zone; skip them rather than abort the click.
        try:
            entity_name = await entity.object_name()
            if not entity_name:
                continue
            location = await entity.location()
        except Exception:
            continue
        dist = self_pos.distance(location)
        seen.append((dist, entity_name))
        norm = _norm(entity_name)
        if want in norm or all(w in norm for w in words):
            candidates.append((dist, entity_name, location))

    if not candidates:
        nearest = ', '.join(f"{n} ({d:.0f})" for d, n in sorted(seen)[:15])
        logger.error(f"clickentity '{name}': no entity matched. Nearest entities: {nearest}")
        return False

    dist, entity_name, location = min(candidates, key=lambda c: c[0])
    cam = await get_camera_state(client)
    if soil_mode:
        if not cam:
            logger.error('soil: camera state is unavailable')
            return False
        if soil_mode == 'rim':
            # Successive rim clicks within a few seconds (the bot's retries) walk through the radii; a fresh
            # selection starts over at the first one.
            now = time.monotonic()
            if now - _rim_select['last'] > 5:
                _rim_select['attempt'] = 0
            radius = RIM_RADII_SELECT[_rim_select['attempt'] % len(RIM_RADII_SELECT)]
            _rim_select.update(last=now, attempt=_rim_select['attempt'] + 1)
            best = await _rim_point(client, cam, location, radius)
            if best is None:
                logger.error('rim: no rim point is on screen')
                return False
            logger.debug(f"rim click at ({best[1] - location.x:.0f},{best[2] - location.y:.0f}) from the rug, "
                         f"radius {radius:.0f} clearance {best[0]:.0f}")
            return await _click_world(client, cam, best[1], best[2], location.z)
        # 'soilfirst' only warns when nothing could be placed (soil may already be there); 'soil' stops the bot.
        return await _place_soil(client, cam, location, fatal=soil_mode != 'soilfirst')
    point = project_point(cam, location.x + world_offset[0], location.y + world_offset[1], location.z) if cam else None
    if point is not None:
        point = (point[0] + offset[0], point[1] + offset[1])
    if point is None:
        logger.error(f"clickentity '{name}': '{entity_name}' is behind the camera or the camera state is unavailable")
        return False
    if not (0 <= point[0] < cam['client_w'] and 0 <= point[1] < cam['client_h']):
        logger.error(f"clickentity '{name}': '{entity_name}' projects off screen at {point} "
                     f"(client {cam['client_w']}x{cam['client_h']})")
        return False

    logger.debug(f"clickentity '{name}' -> '{entity_name}' world=({location.x:.0f},{location.y:.0f},{location.z:.0f}) "
                 f"dist={dist:.0f} pixel={point} client={cam['client_w']}x{cam['client_h']} "
                 f"cam=({cam['cam_x']:.0f},{cam['cam_y']:.0f},{cam['cam_z']:.0f}) "
                 f"fwd=({cam['fwd_x']:.2f},{cam['fwd_y']:.2f},{cam['fwd_z']:.2f}) "
                 f"player=({self_pos.x:.0f},{self_pos.y:.0f},{self_pos.z:.0f})")
    if measure:
        cursor = ctypes.wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(cursor))
        user32.ScreenToClient(client.window_handle, ctypes.byref(cursor))
        ground = _unproject_to_plane(cam, cursor.x, cursor.y, location.z)
        world = f"({ground[0] - location.x:.0f},{ground[1] - location.y:.0f})" if ground else "unknown"
        logger.debug(f"mouse measure: cursor=({cursor.x},{cursor.y}) entity_pixel={point} "
                     f"pixel_offset=({cursor.x - point[0]},{cursor.y - point[1]}) world_offset_xy={world}")
        await _watch_soils(client, location)
        return True
    async with client.mouse_handler:
        await client.mouse_handler.click(point[0], point[1])
    return True
