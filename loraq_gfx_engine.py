import time

import pygame

from loraq_config import SETTINGS
from loraq_icon import STARTUP_BG, load_startup_logo
from loraq_protocol import APP_VERSION, EFFECTS, format_timer, rssi_dbm

# ======================================================================
# CONSTANTS & MAPS
# ======================================================================
VERSION = APP_VERSION

COLOUR_MAP = {
    'W': (255, 255, 255), # White
    'R': (255, 30, 30),   # Red
    'G': (30, 255, 30),   # Green
    'Y': (255, 230, 0),   # Yellow
    'B': (0, 160, 255),   # Blue
    'O': (255, 130, 0)    # Orange
}

BG_COLOUR = (0, 0, 0)       # Black (used during strobe alerts)
AREA1_BG  = STARTUP_BG      # Shared navy background for branding and Area 1
PANEL_BG  = (38, 51, 74)    # Slate blue background for Area 2
STATS_BG = (24, 31, 34)
STATS_LINE = (91, 108, 108)
STATS_MUTED = (179, 195, 196)
STATS_HEADING = (115, 210, 220)
REMOTE_CUE_COLOUR = (0, 200, 255)
TX_HIGHLIGHT_COLOUR = (255, 230, 0)
TX_FAILURE_COLOUR = (255, 50, 50)
TX_HIGHLIGHT_DURATION = 0.5
WEB_PENDING_COLOUR = (255, 165, 0)
OSC_STATUS_COLOURS = {
    'STANDBY': (255, 190, 0),
    'FORWARDED': (50, 255, 50),
    'IGNORED': (255, 90, 90),
    'ERROR': (255, 50, 50),
}

SLIDE_DURATION = 0.30       # Animation duration in seconds
FLASH_DURATION = 2.0
FLASH_RATE_HZ = 3.0         # At or below the 3 flashes/s photosensitive-safety guideline.
LINE_SPACING = 8
SECTION_SPACING = 20
TIMER_BOX_OFFSET = 4
TIMER_BOX_PAD_Y = 5
AREA2_CLEARANCE = 4

STATION_STATE_COLOURS = {
    'OK': (50, 255, 50),
    'ONLINE': (50, 255, 50),
    'MISSED': (255, 190, 0),
    'NO_RESPONSE': (255, 50, 50),
    'UNKNOWN': (200, 200, 200),
}

PREVIEW_STATS_HEIGHT = 150


class LoRaQEngine:
    """Two-zone cue renderer. Not thread-safe: call every method from the pygame thread."""

    def __init__(self, surface, show_telemetry=False, station_id='RX', ack_slot=0, show_tx_info=True,
                 show_web_info=True):
        self.show_telemetry = show_telemetry
        self.show_tx_info = show_tx_info
        self.show_web_info = show_web_info
        self.received_source = ()
        self._source_visible = False
        self.station_id = station_id
        self.ack_slot = ack_slot
        self._startup_active = True
        self._link_status = None
        self._startup_artwork = load_startup_logo()

        self.state = {
            'area1_text': self._startup_text('Initialising serial device...'),
            'area1_colour': COLOUR_MAP['W'],
            'area1_wrapped_lines': [],
            'area1_active_font': None,
            'area1_has_timer': False,
            'area1_start_time': None,
            'area1_duration': 0,

            'area2_text': '',
            'area2_display_text': '',
            'area2_colour': COLOUR_MAP['W'],
            'area2_has_timer': False,
            'area2_start_time': None,
            'area2_duration': 0,

            'flash_start_time': 0.0,
            'flash_end_time': 0.0,
            'flash_colour': COLOUR_MAP['W']
        }

        # Transmitter-only telemetry: list of (station_id, state, label)
        self.stations = []
        self.sent_seq = None
        self.tx_highlight_until = 0.0
        self.tx_failed = False
        self.web_highlight_until = 0.0
        self.web_pending = 0
        self.web_failed = False
        self.web_status_revision = 0
        self.remote_cue = None
        self.osc_state = 'IDLE'
        self.osc_text = ''
        self.duty_cycle_pct = 0.0
        self.duty_cycle_sec = 0.0
        self.last_tx_dwell_ms = 0.0
        self.has_duty_cycle = False

        self.anim_state = 'HIDDEN'
        self.anim_start_time = 0.0

        self.resize(surface)

    def resize(self, surface):
        """Rebuilds fonts and geometry for a new surface size, keeping all display state."""
        self.display_surface = surface
        if self.show_telemetry:
            window_width, window_height = surface.get_size()
            reserved_height = min(PREVIEW_STATS_HEIGHT, max(80, window_height // 3))
            available_height = max(1, window_height - reserved_height)
            stage_width = min(window_width, max(1, round(available_height * 16 / 9)))
            stage_height = min(available_height, max(1, round(stage_width * 9 / 16)))
            self.stage_rect = pygame.Rect((window_width - stage_width) // 2, 0, stage_width, stage_height)
            self.stats_rect = pygame.Rect(0, stage_height, window_width, window_height - stage_height)
            self.screen = pygame.Surface((stage_width, stage_height))
        else:
            self.screen = surface
        self.width, self.height = self.screen.get_size()
        self.max_text_width = int(self.width * 0.92)
        info_size = 20
        self.font_info = pygame.font.Font(None, info_size)
        while info_size > 8 and self.font_info.size('TX: ' + 'W' * 16 + '    RSSI: unavailable')[0] > self.width - 20:
            info_size -= 1
            self.font_info = pygame.font.Font(None, info_size)
        self.source_bar_height = self.font_info.get_linesize() + 12
        self._source_surface = None
        self._source_visible = False
        self.area1_top = max(int(self.height * 0.10),
                             self.source_bar_height + 6 if self.show_tx_info else 0)

        self.area1_min_size = max(8, int(self.height * 0.05))
        self.area1_max_size = max(self.area1_min_size, int(self.height * 0.25))
        self._area1_fonts = {}
        self._area1_line_heights = {}
        self._timer_widths = {}
        self.font_sub = pygame.font.Font(None, max(8, int(self.height * 0.05)))
        self.font_area2 = pygame.font.Font(None, max(8, int(self.height * 0.11)))

        center_y_area2 = int(self.height * 0.82)
        subtext_h = self.font_sub.size("CURRENT CUE")[1]
        area2_total_h = subtext_h + self.font_area2.size("A")[1] + 16

        self.area2_top_bound = center_y_area2 - (area2_total_h // 2) - 10
        self.panel_height = self.height - self.area2_top_bound

        self.area2_timer_width = self._timer_text_width(self.font_area2)
        timer_box_w = self.area2_timer_width + 20
        timer_zone_w = timer_box_w + 35

        self.area2_text_left = 15
        self.area2_text_right = self.width - timer_zone_w
        self.area2_text_center_x = (self.area2_text_left + self.area2_text_right) // 2
        self.area2_max_text_w = self.area2_text_right - self.area2_text_left

        if self.show_telemetry:
            panel_height = self.stats_rect.height
            self.font_status = pygame.font.Font(None, max(17, min(23, int(panel_height * 0.16))))
            self.font_status_heading = pygame.font.Font(None, max(19, min(26, int(panel_height * 0.18))))
            self.font_duty_value = pygame.font.Font(None, max(23, min(34, int(panel_height * 0.24))))

        if self.anim_state == 'SLIDING_UP':
            self.anim_state = 'VISIBLE'
        elif self.anim_state == 'SLIDING_DOWN':
            self.anim_state = 'HIDDEN'
        self.current_panel_y = self.area2_top_bound if self.anim_state == 'VISIBLE' else self.height
        self.anim_start_y = self.current_panel_y

        self._area1_key = None
        self._startup_key = None
        self._startup_surfaces = []
        self._area1_surfaces = []
        self._area2_key = None
        self._area2_surface = None
        self._header_key = None
        self._header_surface = None
        self._timer1_key = None
        self._timer1_surface = None
        self._timer2_key = None
        self._timer2_surface = None
        self._strobe_on = False
        self._tx_highlighted = False
        self._web_highlighted = False

        self.state['area2_display_text'] = self.truncate_text(self.state['area2_text'], self.font_area2, self.area2_max_text_w)
        self.recalculate_area1_layout()

    # ------------------------------------------------------------------
    # Telemetry Updates (transmitter preview only)
    # ------------------------------------------------------------------
    def update_duty_cycle(self, pct, sec, dwell_ms):
        self.duty_cycle_pct = pct
        self.duty_cycle_sec = sec
        self.last_tx_dwell_ms = dwell_ms
        self.has_duty_cycle = True

    def set_stations(self, stations):
        self.stations = list(stations)

    def set_sent_seq(self, seq, successful=True):
        self.sent_seq = seq
        self.tx_failed = not successful
        self.tx_highlight_until = time.monotonic() + TX_HIGHLIGHT_DURATION if successful else 0.0
        self.remote_cue = None

    def set_tx_failed(self, seq=None):
        if seq is not None:
            self.sent_seq = seq
        self.tx_failed = True
        self.tx_highlight_until = 0.0

    def set_web_status(self, revision, connected, pending, delivered=False, error=None):
        if revision <= self.web_status_revision:
            return
        self.web_status_revision = revision
        self.web_pending = pending
        self.web_failed = error is not None or (connected is False and pending > 0)
        if delivered:
            self.web_highlight_until = time.monotonic() + TX_HIGHLIGHT_DURATION

    def set_remote_cue(self, station, session, seq):
        """Marks the on-screen cue as received from another transmitter, until we send our own."""
        self.remote_cue = f"REMOTE CUE {station} #{seq} @ {time.strftime('%H:%M:%S')}"

    def set_osc_status(self, state, text):
        self.osc_state = state
        self.osc_text = text

    def set_received_source(self, station, rssi):
        signal = rssi_dbm(rssi)
        self.received_source = (f'TX: {station}',
                                'RSSI: unavailable' if signal is None else f'RSSI: {signal:.1f} dBm')
        self._source_surface = None

    def clear_received_source(self):
        self.received_source = ()
        self._source_surface = None

    def _source_active(self):
        return self.show_tx_info and bool(self.received_source)

    def set_link_status(self, connected, error=None):
        status = (connected, error)
        if not self._startup_active or status == self._link_status:
            return False
        self._link_status = status
        message = None if connected else 'LoRa device unavailable'
        self.state['area1_text'] = self._startup_text(message)
        self.state['area1_colour'] = COLOUR_MAP['W' if connected else 'R']
        self.recalculate_area1_layout()
        return True

    def _startup_text(self, status=None):
        lines = [
            f'Station ID: {self.station_id} [{self.ack_slot}]',
            f'Version {VERSION}',
        ]
        if status:
            lines.append(status)
        return '\n'.join(lines)

    # ------------------------------------------------------------------
    # Helper Math & Layout Functions
    # ------------------------------------------------------------------
    def wrap_text(self, text, font, max_width):
        raw_lines = text.split('\n')
        lines = []
        for raw_line in raw_lines:
            words = raw_line.split(' ')
            current_line = []
            for word in words:
                test_line = ' '.join(current_line + [word]) if current_line else word
                if font.size(test_line)[0] <= max_width:
                    current_line.append(word)
                    continue
                if current_line:
                    lines.append(' '.join(current_line))
                    current_line = []
                if font.size(word)[0] <= max_width:
                    current_line = [word]
                    continue
                chunk = ""
                for ch in word:
                    if chunk and font.size(chunk + ch)[0] > max_width:
                        lines.append(chunk)
                        chunk = ""
                    chunk += ch
                current_line = [chunk] if chunk else []
            if current_line:
                lines.append(' '.join(current_line))
        return lines if lines else [text]

    def truncate_text(self, text, font, max_width):
        if font.size(text)[0] <= max_width:
            return text
        display_str = text
        while display_str and font.size(display_str + "...")[0] > max_width:
            display_str = display_str[:-1]
        return display_str + "..."

    def recalculate_area1_layout(self):
        text = self.state['area1_text']
        has_timer = self.state['area1_has_timer']
        
        start_y = self.area1_top
        available_h = self.area2_top_bound - start_y - AREA2_CLEARANCE

        chosen_font = None
        words = text.split()

        def fits(size, allow_word_breaks):
            font = self._area1_font(size)
            if not allow_word_breaks and any(font.size(w)[0] > self.max_text_width for w in words):
                return None
            lines = self.wrap_text(text, font, self.max_text_width)
            # Rendered surfaces are taller than font.size() reports, and drawing advances by surface height.
            line_h = self._area1_line_heights[size]
            total_h = len(lines) * line_h + (len(lines) - 1) * LINE_SPACING
            if has_timer:
                total_h += SECTION_SPACING + line_h + TIMER_BOX_OFFSET + TIMER_BOX_PAD_Y
            return lines if total_h <= available_h else None

        # Prefer a font size that keeps whole words intact; only then allow mid-word breaks.
        for allow_word_breaks in (False, True):
            lo, hi = self.area1_min_size, self.area1_max_size
            while lo <= hi:
                mid = (lo + hi) // 2
                lines = fits(mid, allow_word_breaks)
                if lines is not None:
                    chosen_font, chosen_lines = self._area1_font(mid), lines
                    lo = mid + 1
                else:
                    hi = mid - 1
            if chosen_font is not None:
                break
        else:
            chosen_font = self._area1_font(self.area1_min_size)
            chosen_lines = self.wrap_text(text, chosen_font, self.max_text_width)

        self.state['area1_active_font'] = chosen_font
        self.state['area1_wrapped_lines'] = chosen_lines

    def _area1_font(self, size):
        font = self._area1_fonts.get(size)
        if font is None:
            font = self._area1_fonts[size] = pygame.font.Font(None, size)
            self._area1_line_heights[size] = font.render("0", True, (255, 255, 255)).get_height()
        return font

    # ------------------------------------------------------------------
    # Cue Record Application (records are pre-validated by loraq_protocol)
    # ------------------------------------------------------------------
    def apply_records(self, records):
        """Applies every record of one frame together so both zones change on the same frame."""
        now = time.monotonic()
        for rec in records:
            if rec.op == 'F':
                self.trigger_effect(rec.effect_id, self.state['area1_colour'], now)
                continue
            if rec.op == 'C':
                self._clear(rec.area, now)
                continue
            if rec.area == '2':
                self._set_area2(rec, now)
            else:
                self._set_area1(rec)
            self.trigger_effect(rec.effect_id, COLOUR_MAP[rec.colour], now)

    def trigger_effect(self, effect_id, colour, now=None):
        if EFFECTS[effect_id][0]:
            now = time.monotonic() if now is None else now
            self.state['flash_start_time'] = now
            self.state['flash_end_time'] = now + FLASH_DURATION
            self.state['flash_colour'] = colour

    def _set_timer(self, area, seconds):
        self.state[f'area{area}_has_timer'] = seconds is not None
        self.state[f'area{area}_duration'] = seconds or 0
        self.state[f'area{area}_start_time'] = None

    def _set_area1(self, rec):
        self._startup_active = False
        self.state['area1_text'] = rec.text
        self.state['area1_colour'] = COLOUR_MAP[rec.colour]
        self._set_timer(1, rec.timer)
        self.recalculate_area1_layout()

    def _set_area2(self, rec, now):
        prev_text = self.state['area2_text']
        self.state['area2_text'] = rec.text
        self.state['area2_colour'] = COLOUR_MAP[rec.colour]
        self._set_timer(2, rec.timer)
        self.state['area2_display_text'] = self.truncate_text(rec.text, self.font_area2, self.area2_max_text_w)

        if rec.text.strip() and not prev_text.strip():
            if self.anim_state in ('HIDDEN', 'SLIDING_DOWN'):
                self._start_slide('SLIDING_UP', now)
        elif not rec.text.strip() and prev_text.strip():
            if self.anim_state in ('VISIBLE', 'SLIDING_UP'):
                self._start_slide('SLIDING_DOWN', now)

    def _clear(self, area, now):
        if area in ('1', '0'):
            self._startup_active = False
            self.state['area1_text'] = ''
            self._set_timer(1, None)
            self.state['flash_end_time'] = 0.0
            self.recalculate_area1_layout()

        if area in ('2', '0'):
            if self.state['area2_text'].strip() or self.anim_state in ('VISIBLE', 'SLIDING_UP'):
                self._start_slide('SLIDING_DOWN', now)
            self.state['area2_text'] = ''
            self.state['area2_display_text'] = ''
            self._set_timer(2, None)

    def _start_slide(self, target, now):
        self.anim_state = target
        self.anim_start_time = now
        self.anim_start_y = self.current_panel_y

    # ------------------------------------------------------------------
    # Render Loop Method
    # ------------------------------------------------------------------
    def _strobe_active(self, now):
        return (now < self.state['flash_end_time']
                and int((now - self.state['flash_start_time']) * FLASH_RATE_HZ * 2) % 2 == 0)

    def has_active_effects(self, now):
        if self._source_active() != self._source_visible:
            return True
        if self.anim_state in ('SLIDING_UP', 'SLIDING_DOWN') or self._strobe_active(now) != self._strobe_on:
            return True
        highlighted = not self.tx_failed and now < self.tx_highlight_until
        if self.show_telemetry and highlighted != self._tx_highlighted:
            return True
        web_highlighted = now < self.web_highlight_until
        if self.show_telemetry and web_highlighted != self._web_highlighted:
            return True
        for area in (1, 2):
            if self.state[f'area{area}_has_timer']:
                if area == 2 and self.anim_state == 'HIDDEN':
                    continue
                start = self.state[f'area{area}_start_time']
                last_key = self._timer1_key if area == 1 else self._timer2_key
                if start is None or last_key is None:
                    return True
                remaining = max(0, self.state[f'area{area}_duration'] - int(now - start))
                if last_key[0] != remaining:
                    return True
        return False

    def render(self):
        now = time.monotonic()
        st = self.state

        # Timers start on the first rendered frame so the countdown is frame-locked.
        if st['area1_has_timer'] and st['area1_start_time'] is None:
            st['area1_start_time'] = now
        if st['area2_has_timer'] and st['area2_start_time'] is None:
            st['area2_start_time'] = now

        self._advance_animation(now)

        strobe_on = self._strobe_active(now)
        self._strobe_on = strobe_on
        if strobe_on:
            draw_bg = st['flash_colour']
            draw_fg_area1 = draw_fg_area2 = draw_panel_bg = BG_COLOUR
        else:
            draw_bg = STARTUP_BG if self._startup_active else AREA1_BG
            draw_fg_area1 = st['area1_colour']
            draw_fg_area2 = st['area2_colour']
            draw_panel_bg = PANEL_BG

        self.screen.fill(draw_bg)
        if self._startup_active:
            self._draw_startup(draw_fg_area1)
        else:
            self._draw_area1(now, draw_fg_area1)
        if self.anim_state != 'HIDDEN':
            self._draw_area2(now, draw_fg_area2, draw_panel_bg)
        self._source_visible = self._source_active()
        if self._source_visible:
            self._draw_received_source()
        if self.show_telemetry:
            self.display_surface.fill(STATS_BG)
            self.display_surface.blit(self.screen, self.stage_rect.topleft)
            self._tx_highlighted = not self.tx_failed and now < self.tx_highlight_until
            self._web_highlighted = now < self.web_highlight_until
            self._draw_status_bar()

    def _draw_received_source(self):
        if self._source_surface is None:
            text = self.font_info.render('    '.join(self.received_source), True, COLOUR_MAP['W'])
            self._source_surface = pygame.Surface((self.width, self.source_bar_height))
            self._source_surface.fill(BG_COLOUR)
            self._source_surface.blit(text, text.get_rect(center=self._source_surface.get_rect().center))
        self.screen.blit(self._source_surface, (0, 0))

    def _advance_animation(self, now):
        if self.anim_state == 'SLIDING_UP':
            elapsed = now - self.anim_start_time
            if elapsed >= SLIDE_DURATION:
                self.anim_state = 'VISIBLE'
                self.current_panel_y = self.area2_top_bound
            else:
                t = elapsed / SLIDE_DURATION
                ease = t * (2.0 - t)
                self.current_panel_y = int(self.anim_start_y + (self.area2_top_bound - self.anim_start_y) * ease)

        elif self.anim_state == 'SLIDING_DOWN':
            elapsed = now - self.anim_start_time
            if elapsed >= SLIDE_DURATION:
                self.anim_state = 'HIDDEN'
                self.current_panel_y = self.height
            else:
                t = elapsed / SLIDE_DURATION
                ease = t * t
                self.current_panel_y = int(self.anim_start_y + (self.height - self.anim_start_y) * ease)

        elif self.anim_state == 'VISIBLE':
            self.current_panel_y = self.area2_top_bound
        else:
            self.current_panel_y = self.height

    def _draw_startup(self, fg):
        bottom = (int(self.height * 0.94) if self.anim_state == 'HIDDEN'
                  else self.area2_top_bound - AREA2_CLEARANCE)
        key = (self.state['area1_text'], fg, bottom, self._strobe_on)
        if key != self._startup_key:
            self._startup_key = key
            available_h = bottom - self.area1_top
            gap = max(1, int(self.height * 0.02))
            logo_scale = min(self.max_text_width / self._startup_artwork.get_width(),
                             available_h * 0.60 / self._startup_artwork.get_height())
            logo = pygame.transform.smoothscale(
                self._startup_artwork,
                (max(1, round(self._startup_artwork.get_width() * logo_scale)),
                 max(1, round(self._startup_artwork.get_height() * logo_scale))))
            if self._strobe_on:
                logo.fill((*BG_COLOUR, 255), special_flags=pygame.BLEND_RGBA_MULT)
            logo_height = logo.get_bounding_rect().bottom
            subtitle_gap = max(1, gap // 4)
            status_gap = gap * 6
            lines = ['Long Range Cue Display', *self.state['area1_text'].splitlines()]
            # Reserve the status row even when connected, including its font-fitting constraints.
            layout_lines = [*lines[:3], 'Initialising serial device...', 'LoRa device unavailable']
            line_count = 4
            text_height = available_h - logo_height - subtitle_gap - status_gap
            size = max(8, int(self.height * 0.055))
            while True:
                font = pygame.font.Font(None, size)
                if size <= 1 or (all(font.size(line)[0] <= self.max_text_width for line in layout_lines)
                                 and font.get_linesize() * line_count <= text_height):
                    break
                size -= 1
            subtitle_size = size * 3
            subtitle_height = text_height - font.get_linesize() * (line_count - 1)
            while True:
                subtitle_font = pygame.font.Font(None, subtitle_size)
                if subtitle_size <= 1 or (
                        subtitle_font.size(lines[0])[0] <= self.max_text_width
                        and subtitle_font.get_linesize() <= subtitle_height):
                    break
                subtitle_size -= 1
            fonts = [subtitle_font, *([font] * (len(lines) - 1))]
            text_surfaces = [
                line_font.render(line, True, fg if self._strobe_on or index >= 3 else COLOUR_MAP['W'])
                for index, (line, line_font) in enumerate(zip(lines, fonts))
            ]
            total_h = (logo_height + subtitle_gap + status_gap + subtitle_font.get_linesize()
                       + font.get_linesize() * (line_count - 1))
            y = self.area1_top + max(0, (available_h - total_h) // 2)
            self._startup_surfaces = [(logo, logo.get_rect(centerx=self.width // 2, top=y))]
            y += logo_height + subtitle_gap
            for index, surface in enumerate(text_surfaces):
                rect = surface.get_rect(centerx=self.width // 2, top=y)
                self._startup_surfaces.append((surface, rect))
                y += fonts[index].get_linesize() + (status_gap if index == 0 else 0)
        for surface, rect in self._startup_surfaces:
            self.screen.blit(surface, rect)

    def _draw_area1(self, now, fg):
        st = self.state
        font = st['area1_active_font']
        lines = st['area1_wrapped_lines']

        key = (tuple(lines), font, fg)
        if key != self._area1_key:
            self._area1_key = key
            self._area1_surfaces = [font.render(line, True, fg) for line in lines]

        timer_surface = None
        if st['area1_has_timer']:
            remaining = max(0, st['area1_duration'] - int(now - st['area1_start_time']))
            timer_key = (remaining, font, fg)
            if timer_key != self._timer1_key:
                self._timer1_key = timer_key
                self._timer1_surface = font.render(format_timer(remaining), True, fg)
                self._timer1_width = self._timer_text_width(font)
            timer_surface = self._timer1_surface

        current_y = self.area1_top
        for surface in self._area1_surfaces:
            rect = surface.get_rect(centerx=self.width // 2, top=current_y)
            self.screen.blit(surface, rect)
            current_y += surface.get_height() + LINE_SPACING

        if timer_surface:
            current_y += (SECTION_SPACING - LINE_SPACING)
            rect = pygame.Rect(0, current_y + TIMER_BOX_OFFSET, self._timer1_width, timer_surface.get_height())
            rect.centerx = self.width // 2
            pygame.draw.rect(self.screen, fg, rect.inflate(24, 2 * TIMER_BOX_PAD_Y), width=2, border_radius=6)
            text_rect = timer_surface.get_rect(center=rect.center)
            self.screen.blit(timer_surface, text_rect.move(0, self._digit_centre_offset(font, rect.height)))

    def _draw_area2(self, now, fg, panel_bg):
        st = self.state
        panel_rect = pygame.Rect(0, self.current_panel_y, self.width, self.height - self.current_panel_y)
        pygame.draw.rect(self.screen, panel_bg, panel_rect)
        pygame.draw.line(self.screen, fg, (0, self.current_panel_y), (self.width, self.current_panel_y), 2)

        if fg != self._header_key:
            self._header_key = fg
            self._header_surface = self.font_sub.render("CURRENT CUE", True, fg)

        header_rect = self._header_surface.get_rect(centerx=self.width // 2, top=self.current_panel_y + 8)
        self.screen.blit(self._header_surface, header_rect)

        header_h = self._header_surface.get_height()
        content_center_y = self.current_panel_y + 8 + header_h + ((self.panel_height - (8 + header_h)) // 2)

        display_text = st['area2_display_text']
        if display_text:
            key = (display_text, fg)
            if key != self._area2_key:
                self._area2_key = key
                self._area2_surface = self.font_area2.render(display_text, True, fg)
            text_rect = self._area2_surface.get_rect(centerx=self.area2_text_center_x, centery=content_center_y)
            self.screen.blit(self._area2_surface, text_rect)

        if st['area2_has_timer']:
            remaining = max(0, st['area2_duration'] - int(now - st['area2_start_time']))
            timer_key = (remaining, fg)
            if timer_key != self._timer2_key:
                self._timer2_key = timer_key
                self._timer2_surface = self.font_area2.render(format_timer(remaining), True, fg)
            timer_rect = pygame.Rect(0, 0, self.area2_timer_width, self._timer2_surface.get_height())
            timer_rect.right = self.width - 20
            timer_rect.centery = content_center_y
            pygame.draw.rect(self.screen, fg, timer_rect.inflate(20, 10), width=2, border_radius=6)
            offset = self._digit_centre_offset(self.font_area2, timer_rect.height)
            text_rect = self._timer2_surface.get_rect(center=timer_rect.center)
            self.screen.blit(self._timer2_surface, text_rect.move(0, offset))

    def _timer_text_width(self, font):
        if font not in self._timer_widths:
            self._timer_widths[font] = max(
                font.render(f"{digit}{digit}:{digit}{digit}", True, COLOUR_MAP['W']).get_width()
                for digit in range(10))
        return self._timer_widths[font]

    @staticmethod
    def _digit_centre_offset(font, surface_h):
        # Rendered surfaces include descender space, so digit ink sits above the surface centre.
        _, _, miny, maxy, _ = font.metrics("0")[0]
        ink_centre = font.get_ascent() - (maxy + miny) / 2
        return round(surface_h / 2 - ink_centre)

    def _draw_status_bar(self):
        panel = self.stats_rect
        target = self.display_surface
        padding = max(10, min(18, panel.width // 32))
        split_x = int(panel.width * 0.64)
        top = panel.top + 9
        pygame.draw.line(target, STATS_LINE, (0, panel.top), (panel.width, panel.top), 2)
        pygame.draw.line(target, STATS_LINE, (split_x, panel.top + 12), (split_x, panel.bottom - 12))

        duty_padding = max(6, padding // 3)
        duty_x = split_x + duty_padding
        duty_width = panel.width - duty_x - duty_padding
        left_header = self.font_status_heading.render("Transmission stats", True, STATS_HEADING)
        right_header = self.font_status_heading.render("LoRa stats", True, STATS_HEADING)
        target.blit(left_header, left_header.get_rect(centerx=split_x // 2, top=top))
        target.blit(right_header, right_header.get_rect(centerx=(split_x + panel.width) // 2, top=top))
        divider_y = top + max(left_header.get_height(), right_header.get_height()) + 5
        pygame.draw.line(target, STATS_LINE, (padding, divider_y), (split_x - 6, divider_y))
        pygame.draw.line(target, STATS_LINE, (split_x + 6, divider_y),
                         (panel.width - padding, divider_y))
        content_top = divider_y + 6

        lora_label = f"LORA #{self.sent_seq}" if self.sent_seq is not None else "LORA --"
        lora_surface = self.font_status_heading.render(
            lora_label, True, STATS_BG if self._tx_highlighted or self.tx_failed else (255, 255, 255))
        tx_top = content_top
        if self._tx_highlighted or self.tx_failed:
            highlight_colour = TX_FAILURE_COLOUR if self.tx_failed else TX_HIGHLIGHT_COLOUR
            pygame.draw.rect(target, highlight_colour,
                             lora_surface.get_rect(topleft=(padding, tx_top)).inflate(10, 4), border_radius=4)
        target.blit(lora_surface, (padding, tx_top))
        remote_x = padding + lora_surface.get_width() + padding
        if self.show_web_info:
            web_colour = STATS_BG if self._web_highlighted or self.web_failed else (255, 255, 255)
            web_surface = self.font_status_heading.render("WEB", True, web_colour)
            if self.web_failed or self.web_pending or self._web_highlighted:
                highlight_colour = (TX_FAILURE_COLOUR if self.web_failed else
                                   WEB_PENDING_COLOUR if self.web_pending and not self._web_highlighted else
                                   TX_HIGHLIGHT_COLOUR)
                pygame.draw.rect(target, highlight_colour,
                                 web_surface.get_rect(topleft=(remote_x, tx_top)).inflate(10, 4), border_radius=4)
            target.blit(web_surface, (remote_x, tx_top))
            remote_x += web_surface.get_width() + padding
        if self.remote_cue:
            remote = self.truncate_text(self.remote_cue, self.font_status, split_x - padding - remote_x)
            remote_surface = self.font_status.render(remote, True, REMOTE_CUE_COLOUR)
            target.blit(remote_surface, (remote_x, tx_top + (lora_surface.get_height() - remote_surface.get_height()) // 2))

        station_top = tx_top + lora_surface.get_height() + 12
        row_height = self.font_status.get_linesize() + 2
        if self.osc_text:
            osc = self.truncate_text(self.osc_text, self.font_status, split_x - 2 * padding)
            colour = OSC_STATUS_COLOURS.get(self.osc_state, STATS_MUTED)
            target.blit(self.font_status.render(osc, True, colour), (padding, station_top - 6))
            station_top += row_height
        available_rows = max(0, (panel.bottom - padding - station_top) // row_height)
        shown = min(len(self.stations), available_rows)
        if shown < len(self.stations) and shown:
            shown -= 1
        for row, (station_id, slot, state, label) in enumerate(self.stations[:shown]):
            colour = STATION_STATE_COLOURS.get(state, STATION_STATE_COLOURS['UNKNOWN'])
            slot_text = f" ({slot})" if slot is not None else ""
            text = f"{station_id}{slot_text}  {label}"
            text = self.truncate_text(text, self.font_status, split_x - 2 * padding)
            target.blit(self.font_status.render(text, True, colour),
                        (padding, station_top + row * row_height))
        if shown < len(self.stations) and available_rows:
            hidden = len(self.stations) - shown
            more = self.font_status.render(f"+{hidden} more", True, STATS_MUTED)
            target.blit(more, (padding, station_top + shown * row_height))

        limit = SETTINGS.transmitter.duty_cycle_limit_pct
        if self.duty_cycle_pct >= limit:
            duty_colour = (255, 90, 90)
        elif self.duty_cycle_pct >= 0.8 * limit:
            duty_colour = (255, 230, 0)
        else:
            duty_colour = (183, 229, 205)
        value = f"{self.duty_cycle_pct:.2f}%" if self.has_duty_cycle else "--"
        value_surface = self.font_duty_value.render(value, True, duty_colour)
        duty_y = content_top
        duty_label = self.truncate_text("Duty Cycle", self.font_status,
                        max(0, duty_width - value_surface.get_width() - 5))
        duty_label_surface = self.font_status.render(duty_label, True, STATS_MUTED)
        target.blit(duty_label_surface,
                (duty_x, duty_y + (value_surface.get_height() - duty_label_surface.get_height()) // 2))
        target.blit(value_surface, (panel.right - duty_padding - value_surface.get_width(), duty_y))
        allowance = f"{self.duty_cycle_sec:.1f}s / {36 * limit:.1f}s" if self.has_duty_cycle else "No transmissions"
        allowance = self.truncate_text(allowance, self.font_status, duty_width)
        allowance_surface = self.font_status.render(allowance, True, STATS_MUTED)
        allowance_y = duty_y + value_surface.get_height() + 3
        target.blit(allowance_surface, (duty_x, allowance_y))
        dwell_value = f"{self.last_tx_dwell_ms:.1f} ms" if self.has_duty_cycle else "--"
        dwell_limit = SETTINGS.transmitter.max_dwell_time
        dwell_colour = ((255, 90, 90) if dwell_limit > 0 and self.last_tx_dwell_ms > dwell_limit
                else STATS_MUTED)
        dwell_surface = self.font_status.render(dwell_value, True, dwell_colour)
        dwell_y = allowance_y + allowance_surface.get_height() + 3
        dwell_label = self.truncate_text("Last TX dwell time:", self.font_status,
                         max(0, duty_width - dwell_surface.get_width() - 5))
        target.blit(self.font_status.render(dwell_label, True, STATS_MUTED), (duty_x, dwell_y))
        target.blit(dwell_surface, (panel.right - duty_padding - dwell_surface.get_width(), dwell_y))