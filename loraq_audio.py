"""Local alert sounds for receivers and remote cues heard by transmitters."""
from array import array
import logging
import math

import pygame

from loraq_protocol import EFFECTS

SAMPLE_RATE = 44100
log = logging.getLogger("loraq.audio")


def sound_samples(name: str) -> array:
    patterns = {
        "buzzer": ((220, 0.6, True),),
        "beep": ((1000, 0.2, False),),
        "double-beep": ((1000, 0.15, False), (0, 0.1, False), (1000, 0.15, False)),
    }
    samples = array("h")
    ramp_samples = int(SAMPLE_RATE * 0.005)
    for frequency, duration, square in patterns[name]:
        count = int(SAMPLE_RATE * duration)
        for index in range(count):
            phase = math.sin(2 * math.pi * frequency * index / SAMPLE_RATE)
            wave = (1 if phase >= 0 else -1) if square else phase
            envelope = min(1.0, index / ramp_samples, (count - 1 - index) / ramp_samples)
            samples.append(int(32767 * wave * envelope) if frequency else 0)
    return samples


class CueAudio:
    def __init__(self, enabled: bool = False):
        self.channel = None
        self.sounds = {}
        self._owns_mixer = False
        if not enabled:
            log.warning("Cue audio disabled by configuration; set audio_enabled = true "
                        "in this application's [receiver] or [transmitter] section and restart")
            return
        try:
            self._owns_mixer = pygame.mixer.get_init() is None
            pygame.mixer.init(frequency=SAMPLE_RATE, size=-16, channels=1, buffer=2048,
                              allowedchanges=0)
            if pygame.mixer.get_init() != (SAMPLE_RATE, -16, 1):
                raise pygame.error("Audio output does not support 44100 Hz signed 16-bit mono")
            self.sounds = {name: pygame.mixer.Sound(buffer=sound_samples(name).tobytes())
                           for name in ("buzzer", "beep", "double-beep")}
            self.channel = pygame.mixer.Channel(0)
            self.channel.set_volume(1.0)
            log.info("Cue audio enabled at maximum application volume on the system audio output")
        except pygame.error as exc:
            self.close()
            log.warning("Cue audio unavailable (%s); visual cues remain enabled", exc)

    def play_records(self, records) -> None:
        if self.channel is None:
            return
        name = None
        for record in records:
            if record.op in ("D", "F"):
                sound = EFFECTS[record.effect_id][1]
                if sound is not None:
                    name = sound
        if name is not None:
            try:
                self.channel.play(self.sounds[name])
            except pygame.error as exc:
                self.close()
                log.warning("Cue sound failed (%s); audio disabled, visual cues remain enabled", exc)

    def close(self) -> None:
        if self.channel is not None:
            try:
                self.channel.stop()
            except pygame.error:
                pass
            self.channel = None
        self.sounds.clear()
        if self._owns_mixer:
            pygame.mixer.quit()
            self._owns_mixer = False