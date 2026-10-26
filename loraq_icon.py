"""Shared LoRaQ artwork and running-window identity."""
import logging
import sys
from pathlib import Path

import pygame


STARTUP_BG = (9, 27, 88)


def load_startup_logo() -> pygame.Surface:
    image = pygame.image.load(str(Path(__file__).with_name("assets") / "loraq.png"))
    width, height = image.get_size()
    # Remove the square icon's padding and rounded corners, retaining the original lettering.
    lettering = image.subsurface((width * 3 // 32, height * 3 // 8,
                                  width * 13 // 16, height // 4)).copy()
    lettering.set_colorkey(STARTUP_BG)
    logo = pygame.Surface(lettering.get_size(), pygame.SRCALPHA)
    logo.blit(lettering, (0, 0))
    return logo


def create_app_icon(size: int = 1024) -> pygame.Surface:
    name = "lq.png" if size < 64 else "loraq.png"
    image = pygame.image.load(str(Path(__file__).with_name("assets") / name))
    return pygame.transform.smoothscale(image, (size, size))


def set_app_icon(app_id: str) -> None:
    if sys.platform == "win32":
        import ctypes

        shell32 = ctypes.WinDLL("shell32")
        set_identity = shell32.SetCurrentProcessExplicitAppUserModelID
        set_identity.argtypes = [ctypes.c_wchar_p]
        set_identity.restype = ctypes.c_long
        result = set_identity(app_id)
        if result < 0:
            logging.getLogger("loraq.icon").warning(
                "Could not set Windows taskbar identity %s (HRESULT 0x%08X)",
                app_id, result & 0xFFFFFFFF,
            )
    pygame.display.set_icon(create_app_icon(1024 if sys.platform == "darwin" else 32))
