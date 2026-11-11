"""Monotonic timing rules for five optional thermal QA clips."""
OFFSETS = (120, 300, 480, 660, 840)
DURATION = 5
STOP = 900

class VideoSchedule:
    def __init__(self, started):
        self.started = started
        self.started_clips = set()
        self.missed = set()

    def update(self, now):
        elapsed = now - self.started
        if elapsed < 0:
            raise ValueError('monotonic clock moved backwards')
        for offset in OFFSETS:
            if offset not in self.started_clips and offset not in self.missed:
                if offset <= elapsed < offset + DURATION:
                    self.started_clips.add(offset)
                    return ('start', offset, self.started + offset + DURATION)
                if elapsed >= offset + DURATION:
                    self.missed.add(offset)
        return ('complete', None, None) if elapsed >= STOP else ('waiting', None, None)
