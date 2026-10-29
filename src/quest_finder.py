import asyncio
import re

from loguru import logger
from wizwalker import Client, Keycode

from src.paths import *
from src.utils import get_window_from_path, is_visible_by_path

FINDER_PAGE_RE = re.compile(r"quest\s*finder|find an available quest", re.I)
_ICON_NAME_RE = re.compile(r"finder|find", re.I)
_NEXT_NAME_RE = re.compile(r"next|forward|right|arrow", re.I)
MAX_PAGE_TURNS = 15
# Provisional: which tag marks hidden quests is unconfirmed; dump_quest_data logs the real tags.
HIDDEN_TAG_RE = re.compile(r"hidden", re.I)


class QuestFinderUIError(Exception):
    pass


def world_key(zone_name: str) -> str:
    # "Grizzleheim/GH_RedClaw/Interiors/X" -> "Grizzleheim"
    return zone_name.split('/')[0] if zone_name else ''


async def _text(window) -> str:
    try:
        return re.sub(r'<[^>]*>', '', await window.maybe_text()).strip()
    except Exception:
        return ''


async def visible_windows(client: Client, root_path: list[str] = None, max_depth: int = 14):
    # Returns [(path, window)] for every visible window under root_path; hidden parents hide their children.
    root_path = root_path or quest_book_root_path
    root = await get_window_from_path(client.root_window, root_path)
    if root is False:
        return []

    found = []

    async def walk(window, parent_path, depth):
        try:
            if not await window.is_visible():
                return
            path = parent_path + [await window.name()]
            found.append((path, window))
            if depth < max_depth:
                for child in await window.children():
                    await walk(child, path, depth + 1)
        except Exception:
            return

    await walk(root, root_path[:-1], 0)
    return found


async def dump_quest_ui(client: Client):
    # Open the quest book on the Quest Finder page first; copy the icon / next-page paths into paths.py.
    windows = await visible_windows(client)
    lines = []
    for path, window in windows:
        try:
            type_name = await window.maybe_read_type_name()
        except Exception:
            type_name = '?'
        try:
            rect = await window.window_rectangle()
        except Exception:
            rect = None
        text = (await _text(window))[:80]
        lines.append(f"{path!r} <{type_name}> text={text!r} rect={rect}")
    logger.info(f"[quest_ui] {len(windows)} visible windows under {quest_book_root_path}\n" + "\n".join(lines))


async def is_finder_page_visible(client: Client) -> bool:
    for _, window in await visible_windows(client):
        if FINDER_PAGE_RE.search(await _text(window)):
            return True
    return False


async def _is_book_open(client: Client) -> bool:
    if await is_visible_by_path(client, quest_buttons_parent_path):
        return True
    return await is_finder_page_visible(client)


async def _toggle_book(client: Client, want_open: bool):
    for _ in range(4):
        if await _is_book_open(client) == want_open:
            return
        await client.send_key(Keycode.Q)
        await asyncio.sleep(.6)
    if await _is_book_open(client) != want_open:
        raise QuestFinderUIError(f"Could not {'open' if want_open else 'close'} the quest book.")


async def _click(client: Client, path: list[str] | None, window=None):
    if window is None:
        window = await get_window_from_path(client.root_window, path)
        if window is False:
            raise QuestFinderUIError(f"Window not found: {path}")
    async with client.mouse_handler:
        await client.mouse_handler.click_window(window)


async def _find_icon(client: Client):
    if quest_finder_icon_path is not None:
        return quest_finder_icon_path, None

    for path, window in await visible_windows(client):
        if _ICON_NAME_RE.search(path[-1]) and not FINDER_PAGE_RE.search(await _text(window)):
            logger.info(f"[quest_finder] guessed icon window {path}")
            return path, window
    return None, None


async def _find_next_page(client: Client):
    if quest_book_next_page_path is not None:
        return quest_book_next_page_path, None

    guesses = [(p, w) for p, w in await visible_windows(client) if _NEXT_NAME_RE.search(p[-1])]
    if guesses:
        path, window = guesses[-1]
        logger.info(f"[quest_finder] guessed next-page window {path}")
        return path, window
    return None, None


async def _tags(tag_list) -> list[str]:
    if tag_list is None:
        return []
    try:
        return await tag_list.client_tags()
    except Exception:
        return []


async def hidden_reason(quest) -> str:
    # Returns why a quest counts as hidden, or '' if it does not.
    for tag in await _tags(await quest.client_tags()):
        if HIDDEN_TAG_RE.search(tag):
            return f"quest tag {tag!r}"
    for goal in (await quest.goal_data()).values():
        for tag in await _tags(await goal.client_tag_list()):
            if HIDDEN_TAG_RE.search(tag):
                return f"goal tag {tag!r}"
    return ''


async def dump_quest_data(client: Client):
    # One line per accepted quest with every flag that might separate hidden from visible quests.
    lines = []
    for quest_id, quest in (await (await client.quest_manager()).quest_data()).items():
        try:
            key = await quest.name_lang_key()
            try:
                name = key if key == 'Quest Finder' else await client.cache_handler.get_langcode_name(key)
            except Exception:
                name = key
            goals = []
            for goal_id, goal in (await quest.goal_data()).items():
                try:
                    goal_type = (await goal.goal_type()).name
                except Exception:
                    goal_type = '?'
                goals.append(dict(id=goal_id, done=await goal.goal_status(), type=goal_type, no_helper=await goal.no_quest_helper(),
                                  pet=await goal.pet_only_quest(), zone=await goal.goal_destination_zone(), tags=await _tags(await goal.client_tag_list())))
            lines.append(
                f"{quest_id} {name!r} mainline={await quest.mainline()} type={await quest.quest_type()} level={await quest.quest_level()} "
                f"activity={(await quest.activity_type()).name} permit_helper={await quest.permit_quest_helper()} ready={await quest.ready_to_turn_in()} "
                f"pet={await quest.pet_only_quest()} tags={await _tags(await quest.client_tags())} goals={goals}")
        except Exception as e:
            lines.append(f"{quest_id} <read error: {e!r}>")
    logger.info(f"[quest_data] {len(lines)} accepted quests\n" + "\n".join(lines))


async def tracked_snapshot(client: Client):
    ids = (await client.quest_id(), await client.goal_id())
    quest_ids = frozenset((await (await client.quest_manager()).quest_data()).keys())
    pos = await client.quest_position.position()
    return ids, quest_ids, (round(pos.x), round(pos.y), round(pos.z))


async def press_quest_finder(client: Client):
    # Opens the quest book, turns to the last page, clicks the Quest Finder icon, closes the book.
    await _toggle_book(client, True)
    try:
        for _ in range(MAX_PAGE_TURNS):
            if await is_finder_page_visible(client):
                break
            path, window = await _find_next_page(client)
            if path is None:
                raise QuestFinderUIError("No next-page button found; run Dump Quest UI and set quest_book_next_page_path in paths.py.")
            await _click(client, path, window)
            await asyncio.sleep(.5)
        else:
            raise QuestFinderUIError("Never reached the Quest Finder page.")

        path, window = await _find_icon(client)
        if path is None:
            raise QuestFinderUIError("Quest Finder icon not found; run Dump Quest UI and set quest_finder_icon_path in paths.py.")
        await _click(client, path, window)
        await asyncio.sleep(1.5)
    finally:
        try:
            await _toggle_book(client, False)
        except QuestFinderUIError as e:
            logger.warning(f"[quest_finder] {e}")
